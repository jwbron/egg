"""Tests for the post-consensus-timeout per-iteration rebaseline (#2245).

After ``consensus_timeout_minutes`` elapses, the post-timeout poll loop
in ``_run_concurrent_phase`` waits for remaining containers.  Pre-#2245
the wait was a single fixed 3600s budget; post-#2245 the budget is
per-iteration and rebaselines whenever a producer issues a fresh
CONSENSUS_PROPOSE (initial or NACK→re-propose).  An absolute cap
(``post_consensus_max_total_seconds``) bounds the total wait so an
unbounded propose churn can't stall the pipeline.

These tests verify:

1. The new ``PipelineConfig`` knobs default to 3600 / 14400 and are
   readable via ``getattr`` for backwards compatibility.
2. With no tracker proposals during the wait, the iteration budget
   elapses and the phase force-kills (the pre-#2245 behaviour).
3. A fresh CONSENSUS_PROPOSE during the wait extends the iteration
   budget so a productive multi-iteration BRC consensus cycle is no
   longer cut off mid-iteration.
4. The absolute cap still bounds the total wait even when proposals
   keep arriving.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _run_concurrent_phase


def _make_concurrent_pipeline(
    pipeline_id: str = "issue-2245",
    *,
    iteration_budget: int | None = None,
    max_total: int | None = None,
) -> Pipeline:
    config = PipelineConfig()
    overrides: dict[str, object] = {
        "concurrent_execution": True,
        "max_concurrent_agents": 5,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }
    if iteration_budget is not None:
        overrides["post_consensus_iteration_budget_seconds"] = iteration_budget
    if max_total is not None:
        overrides["post_consensus_max_total_seconds"] = max_total

    for key, val in overrides.items():
        try:
            setattr(config, key, val)
        except (AttributeError, ValueError):
            config.__dict__[key] = val

    return Pipeline(
        id=pipeline_id,
        issue_number=2245,
        repo="owner/repo",
        branch="egg/issue-2245",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str):
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.RUNNING,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


def _make_phase_execution():
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _common_mocks(executions, container_status=ContainerStatus.RUNNING):
    """Standard mock setup shared by the rebaseline tests."""
    pipeline_id = "issue-2245"

    container_infos = {
        e.container_id: ContainerInfo(
            container_id=e.container_id,
            container_name=f"{pipeline_id}-{e.role.value}",
            status=container_status,
            exit_code=None,
        )
        for e in executions
    }

    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = _make_phase_execution()
    mock_pipeline_state.status = PipelineStatus.RUNNING
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()
    mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
    mock_docker.stop_container.return_value = ContainerInfo(
        container_id="stopped",
        container_name="stopped",
        status=ContainerStatus.EXITED,
        exit_code=137,
    )

    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

    return mock_store, mock_spawner, mock_docker, container_infos


class TestPipelineConfigKnobs:
    """The new knobs default correctly and accept overrides."""

    def test_iteration_budget_default(self):
        config = PipelineConfig()
        assert config.post_consensus_iteration_budget_seconds == 3600

    def test_max_total_default(self):
        config = PipelineConfig()
        assert config.post_consensus_max_total_seconds == 14400

    def test_iteration_budget_override(self):
        config = PipelineConfig(post_consensus_iteration_budget_seconds=7200)
        assert config.post_consensus_iteration_budget_seconds == 7200

    def test_max_total_override(self):
        config = PipelineConfig(post_consensus_max_total_seconds=21600)
        assert config.post_consensus_max_total_seconds == 21600

    def test_iteration_budget_validates_min(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PipelineConfig(post_consensus_iteration_budget_seconds=30)

    def test_max_total_validates_min(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PipelineConfig(post_consensus_max_total_seconds=30)


class TestPostTimeoutRebaseline:
    """Per-iteration budget resets on producer progress (#2245)."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_no_progress_during_wait_force_kills_at_iteration_budget(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """Without producer proposals, the iteration budget bounds the wait.

        Mirrors pre-#2245 behaviour: no progress signal, no rebaseline,
        and the per-iteration budget acts as the wait bound.
        """
        # Each call advances 1000s.  After consensus_timeout (1800s)
        # fires at call 2, the post-timeout loop sees 2000s of
        # iteration_elapsed by call 4 and exits cleanly.
        _calls = [0]

        def _monotonic():
            _calls[0] += 1
            if _calls[0] == 1:
                return 0.0
            return float(1801.0 + _calls[0] * 1000.0)

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        # Tighten the iteration budget so the loop exits after one
        # post-timeout iteration even with 1000s/call advancement.
        pipeline = _make_concurrent_pipeline(iteration_budget=1500)
        mock_store, mock_spawner, mock_docker, _ = _common_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=None):
            exit_code, _logs = _run_concurrent_phase(
                pipeline_id="issue-2245",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # No tracker → no rebaseline → iteration budget exhausted →
        # force-kill → exit 1.
        assert exit_code == 1
        # Force-kill was invoked.
        mock_docker.stop_container.assert_called_with("coder-1", timeout=30)

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_fresh_proposal_extends_iteration_budget(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """A fresh CONSENSUS_PROPOSE rebaselines the iteration budget.

        Setup: iteration budget = 1500s, monotonic advances 1000s/call.

        Without a rebaseline, iteration_elapsed crosses 1500 by the
        second post-timeout iteration → force-kill → exit 1.

        With a fresh proposal arriving on the first iteration, the
        clock resets — the loop survives the second iteration.  We
        then converge on iteration 3 (consensus reached) so the phase
        succeeds.  Exit 0 + the rebaseline log line proves the budget
        actually extended past the original cutoff.
        """
        _calls = [0]

        def _monotonic():
            _calls[0] += 1
            if _calls[0] == 1:
                return 0.0
            return float(1801.0 + _calls[0] * 1000.0)

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline = _make_concurrent_pipeline(iteration_budget=1500)
        mock_store, mock_spawner, mock_docker, _ = _common_mocks(executions)

        # Consensus: incomplete on first check (triggers timeout
        # path), incomplete during the post-timeout wait until the
        # third check, where it converges.
        consensus_calls = [0]

        def _check_consensus():
            consensus_calls[0] += 1
            if consensus_calls[0] >= 3:
                return {
                    "is_complete": True,
                    "has_objections": False,
                    "blocking_agents": [],
                }
            return {
                "is_complete": False,
                "has_objections": False,
                "blocking_agents": ["coder"],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        # Tracker yields a *fresh* proposal timestamp on each call so
        # the rebaseline branch fires on every iteration.
        ts_calls = [0]
        base_ts = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)

        def _latest_ts():
            ts_calls[0] += 1
            return base_ts + timedelta(seconds=ts_calls[0] * 60)

        mock_tracker = MagicMock()
        mock_tracker.get_latest_proposal_timestamp.side_effect = _latest_ts

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker):
            exit_code, _logs = _run_concurrent_phase(
                pipeline_id="issue-2245",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # Rebaseline kept the loop alive until consensus converged.
        # Exit 0 is the proof: without rebaseline, iteration_elapsed
        # would have crossed 1500s by call 4 (iteration 2) and the
        # loop would have force-killed (exit 1) before consensus_calls
        # reached 3.  The rebaseline branch must have fired to keep
        # the loop alive that long.
        assert exit_code == 0
        # Proposal lookup ran at least once per iteration (snapshot +
        # per-iteration check).
        assert ts_calls[0] >= 2

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_absolute_cap_bounds_unbounded_proposal_churn(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """The absolute cap bounds the wait even with non-stop proposals.

        Even if a producer keeps issuing fresh CONSENSUS_PROPOSE
        messages — rebaselining the per-iteration clock every loop —
        the absolute ``post_consensus_max_total_seconds`` cap forces
        the loop to terminate.  Without this cap a churning producer
        could stall the pipeline indefinitely.
        """
        _calls = [0]

        def _monotonic():
            _calls[0] += 1
            if _calls[0] == 1:
                return 0.0
            return float(1801.0 + _calls[0] * 1000.0)

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        # Iteration budget = 5000s (won't fire), max_total = 2500s
        # (will fire on the 3rd post-timeout monotonic read).
        pipeline = _make_concurrent_pipeline(iteration_budget=5000, max_total=2500)
        mock_store, mock_spawner, mock_docker, _ = _common_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        # Always a fresh proposal — so iteration clock keeps
        # rebaselining and only the absolute cap can stop the loop.
        ts_calls = [0]
        base_ts = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)

        def _latest_ts():
            ts_calls[0] += 1
            return base_ts + timedelta(seconds=ts_calls[0] * 60)

        mock_tracker = MagicMock()
        mock_tracker.get_latest_proposal_timestamp.side_effect = _latest_ts

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker):
            exit_code, _logs = _run_concurrent_phase(
                pipeline_id="issue-2245",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # Absolute cap fires → force-kill → exit 1.
        assert exit_code == 1
        mock_docker.stop_container.assert_called_with("coder-1", timeout=30)
