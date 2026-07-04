"""Tests for live consensus-timeout widening in the phase poll loop (#3490).

When the consensus wall is about to fire, the poll loop re-resolves the
budget from a freshly-loaded pipeline config. An operator who widened
``consensus_timeout_minutes*`` via ``PATCH /config`` on the running
pipeline therefore extends the window in place; the slice keeps
polling instead of hard-failing with unresolved NACKs and cascading the
blast to downstream slices (the #3490 incident).
"""

from datetime import UTC, datetime
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

_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _make_pipeline(timeout_minutes: int) -> Pipeline:
    return Pipeline(
        id="issue-3490",
        issue_number=3490,
        repo="owner/repo",
        branch="egg/issue-3490",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(
            concurrent_execution=True,
            consensus_timeout_minutes=timeout_minutes,
        ),
    )


def _make_execution(role: AgentRole, container_id: str) -> AgentExecution:
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.RUNNING,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


class TestConsensusTimeoutLiveWidening:
    @patch("routes.pipelines._handle_brc_consensus_timeout")
    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_widened_config_extends_window_before_wall_fires(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
        mock_timeout_handler,
    ):
        """A live-widened budget keeps the loop polling past the original
        wall; consensus reached inside the widened window succeeds.

        Simulates the #3490 incident with the fix applied: the original
        30-minute budget elapses while the coder is still iterating, the
        operator has PATCHed the timeout to 200 minutes, and consensus
        completes on the next poll; the slice must exit 0 instead of
        hard-failing at the original wall.
        """
        call_count = [0]

        def _monotonic():
            # Each call advances 2000s, so the second loop iteration is
            # already past the original 30-minute (1800s) wall but far
            # inside the widened 200-minute (12000s) one.
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-3490-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_pipeline(timeout_minutes=30)

        # The freshly-loaded state carries the operator's live widening.
        widened_state = MagicMock()
        widened_state.config = PipelineConfig(
            concurrent_execution=True,
            consensus_timeout_minutes=200,
        )
        widened_state.status = PipelineStatus.RUNNING
        widened_state.get_phase_execution.return_value = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = widened_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Not complete when the original wall is crossed; complete on the
        # next poll inside the widened window.
        consensus_call_count = [0]

        def _check_consensus():
            consensus_call_count[0] += 1
            if consensus_call_count[0] == 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-3490",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0, (
            f"Expected exit 0 (widened window let consensus complete), got "
            f"{exit_code}. Consensus checked {consensus_call_count[0]} times. "
            f"Logs: {logs}"
        )
        # The timeout path was never taken: no consensus-timeout alert or
        # container-exit fallback was triggered.
        mock_timeout_handler.assert_not_called()
