"""Tests for consensus-driven phase advancement in _run_concurrent_phase.

Covers the polling loop that checks consensus, handles objections and timeouts,
and falls back to container-exit-based completion.
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


def _make_concurrent_pipeline(pipeline_id: str = "issue-999") -> Pipeline:
    """Create a pipeline with concurrent_execution enabled."""
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 4,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except (AttributeError, ValueError):
            config.__dict__[key] = val

    return Pipeline(
        id=pipeline_id,
        issue_number=999,
        repo="owner/repo",
        branch="egg/issue-999",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str, status=AgentExecutionStatus.RUNNING):
    return AgentExecution(
        role=role,
        status=status,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


def _make_phase_execution():
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


def _make_real_store(pipeline):
    """MagicMock store that round-trips a real Pipeline through load/save.

    Serializes on save and deserializes on load via Pydantic's
    ``model_dump_json`` / ``model_validate_json`` so a missing
    ``save_pipeline`` call after an in-place mutation surfaces as a
    failed assertion: the in-memory mutation is invisible to the next
    ``load_pipeline`` because the held state is the JSON snapshot from
    the last save.  This is the round-trip fidelity the issue #2208
    re-review asked for — verifying the *persistence* path, not just
    that ``add_decision`` was called.

    Preserves the MagicMock surface (``load_pipeline.call_count``,
    ``save_pipeline.call_args_list``) for tests that inspect call
    history.
    """
    holder = {"json": pipeline.model_dump_json()}
    store = MagicMock()
    store.load_pipeline.side_effect = lambda _pid: Pipeline.model_validate_json(holder["json"])

    def _save(p):
        holder["json"] = p.model_dump_json()

    store.save_pipeline.side_effect = _save
    return store


def _base_mocks(executions, container_infos=None):
    """Create common mocks for the consensus polling tests.

    Args:
        executions: AgentExecution list returned by spawn_all.
        container_infos: Dict of container_id -> ContainerInfo for get_container_info.
            Defaults to RUNNING status for all containers.
    """
    pipeline = _make_concurrent_pipeline()
    # Disk-side pipeline mirrors the in-memory one but is a distinct
    # object; tests assert HITL decisions land on this side, mirroring
    # what /sdlc reads from disk.
    disk_pipeline = _make_concurrent_pipeline()
    mock_store = _make_real_store(disk_pipeline)

    mock_docker = MagicMock()

    if container_infos is None:
        container_infos = {}
        for e in executions:
            if e.container_id:
                container_infos[e.container_id] = ContainerInfo(
                    container_id=e.container_id,
                    container_name=f"issue-999-{e.role.value}",
                    status=ContainerStatus.RUNNING,
                    exit_code=None,
                )

    mock_docker.get_container_info.side_effect = lambda cid: container_infos[cid]

    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

    return pipeline, mock_store, mock_spawner, mock_docker


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


class TestConsensusReached:
    """Consensus is reached before timeout or container exit."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_reached_returns_zero(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When check_consensus returns is_complete=True, returns (0, ...) immediately."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        assert "Consensus reached" in logs
        # Containers should be stopped on consensus
        assert mock_docker.stop_container.call_count == 2
        # No sleep needed — consensus on first poll
        mock_sleep.assert_not_called()

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_reached_after_n_polls(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """Consensus reached after several polls — sleep is called between polls."""
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        def _check_consensus():
            poll_count[0] += 1
            if poll_count[0] >= 3:
                return {"is_complete": True, "has_objections": False, "blocking_agents": []}
            return {"is_complete": False, "has_objections": False, "blocking_agents": ["coder"]}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # sleep called twice (polls 1 and 2; poll 3 returns consensus)
        assert mock_sleep.call_count == 2

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_emits_event(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """CONSENSUS_REACHED event is emitted when consensus completes."""
        from events import EventType

        mock_monotonic.return_value = 42.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # elapsed_seconds is 0.0 because mock_monotonic always returns 42.0,
        # so start_time and the loop's time.monotonic() are identical.
        mock_emit.assert_any_call(
            EventType.CONSENSUS_REACHED,
            "issue-999",
            data={"elapsed_seconds": 0.0},
        )


class TestConsensusTimeout:
    """Consensus not reached within timeout window."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_creates_hitl_decision(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus times out, a HITL decision is created."""
        # Use a callable side_effect that (a) starts before the timeout,
        # (b) jumps past the 30-min consensus timeout, and (c) keeps
        # advancing so the post-timeout polling budget (#1921) also
        # exhausts within a bounded number of iterations.
        _calls = [0]

        def _monotonic():
            _calls[0] += 1
            if _calls[0] == 1:
                return 0.0
            # Each subsequent call jumps 2000s so both the 1800s
            # consensus timeout and the 3600s post-timeout budget
            # elapse quickly.
            return float(1801.0 + _calls[0] * 2000.0)

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

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

        mock_add_decision = MagicMock(return_value=MagicMock(id="dec-1"))
        with patch.object(type(pipeline), "add_decision", mock_add_decision):
            exit_code, logs = _run_concurrent_phase(
                pipeline_id="issue-999",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # Timeout with no convergence → force-kill path → exit 1 (#1921).
        # Prior to #1921 this returned 0 because wait_for_container was
        # mocked to return a clean exit; post-#1921 the polling loop
        # force-kills still-running containers when the 3600s budget
        # elapses, which is the realistic outcome.
        assert exit_code == 1
        # HITL decision should have been created on the pipeline
        mock_add_decision.assert_called_once()
        call_args = mock_add_decision.call_args
        question = call_args[1].get("question", call_args[0][0] if call_args[0] else "")
        assert "30 minutes" in question

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_emits_event(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """CONSENSUS_TIMEOUT event is emitted on timeout."""
        from events import EventType

        # See test_timeout_creates_hitl_decision for why monotonic must
        # keep advancing past the 3600s post-timeout budget (#1921).
        _calls = [0]

        def _monotonic():
            _calls[0] += 1
            if _calls[0] == 1:
                return 0.0
            return float(1801.0 + _calls[0] * 2000.0)

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

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

        _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        mock_emit.assert_any_call(
            EventType.CONSENSUS_TIMEOUT,
            "issue-999",
            data={"timeout_minutes": 30.0, "blocking_agents": ["coder"]},
        )


class TestObjectionHandling:
    """Objections trigger HITL decisions."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_objection_dedup_distinct_from_incomplete_consensus_hitl(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """The objection HITL is created exactly once despite repeated polls
        with ``has_objections=True``; it's a distinct decision from the
        incomplete-consensus HITL that fires once at exit (issue #2203)."""
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]

        # Container exits on third poll
        running_info = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.RUNNING,
            exit_code=None,
        )
        exited_info = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )

        def _get_info(cid):
            if poll_count[0] >= 3:
                return exited_info
            return running_info

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)
        mock_docker.get_container_info.side_effect = _get_info

        def _check_consensus():
            poll_count[0] += 1
            return {
                "is_complete": False,
                "has_objections": True,
                "blocking_agents": ["coder"],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Read the persisted (round-tripped) decisions to verify what
        # ``/sdlc`` would actually see.  Issue #2208 review pointed out
        # that asserting only on ``add_decision`` call shape masks
        # missing-save bugs.
        disk_decisions = mock_store.load_pipeline("issue-999").decisions
        objection_decisions = [d for d in disk_decisions if "objecting" in d.question.lower()]
        # Deduplication: only one objection HITL despite repeated polls.
        assert len(objection_decisions) == 1
        # Plus exactly one incomplete-consensus HITL fired at exit
        # (clean-exit-without-consensus path, issue #2203).
        incomplete_decisions = [
            d for d in disk_decisions if "consensus incomplete" in d.question.lower()
        ]
        assert len(incomplete_decisions) == 1
        # The two decisions carry different option lists — they convey
        # different operator actions and are intentionally distinct.
        assert objection_decisions[0].options != incomplete_decisions[0].options


class TestContainerExitFallback:
    """All containers exit before consensus — fallback to exit codes."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_all_containers_exit_without_consensus_returns_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """When all containers exit code 0 without consensus, returns (1, ...)."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-999-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-999-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(
            executions, container_infos=container_infos
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": ["coder", "tester"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Issue #1581: clean exit without consensus must return failure
        assert exit_code == 1
        assert "coder" in logs
        assert "tester" in logs

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_container_exit_failure_returns_nonzero(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """When a container exits non-zero, returns (1, ...) via fallback."""
        mock_monotonic.return_value = 0.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-999-coder",
                status=ContainerStatus.FAILED,
                exit_code=1,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(
            executions, container_infos=container_infos
        )

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

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1
        # handle_agent_failure should have been called
        mock_executor_instance.handle_agent_failure.assert_called_once_with(
            role="coder",
            error="Container exited with code 1",
        )


class TestMixedScenarios:
    """Container exits and consensus interact correctly."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_some_containers_exit_then_consensus(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """One container exits early, then consensus reached — still returns success."""
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        exited_coder = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )
        running_tester = ContainerInfo(
            container_id="tester-1",
            container_name="issue-999-tester",
            status=ContainerStatus.RUNNING,
            exit_code=None,
        )

        def _get_info(cid):
            if cid == "coder-1":
                return exited_coder
            return running_tester

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)
        mock_docker.get_container_info.side_effect = _get_info

        def _check_consensus():
            poll_count[0] += 1
            if poll_count[0] >= 2:
                return {"is_complete": True, "has_objections": False, "blocking_agents": []}
            return {"is_complete": False, "has_objections": False, "blocking_agents": ["tester"]}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # Tester container should be stopped on consensus
        stopped_ids = {c.args[0] for c in mock_docker.stop_container.call_args_list}
        assert "tester-1" in stopped_ids

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_agent_failure_calls_handle_agent_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """When a container crashes, handle_agent_failure() is called."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-999-coder",
                status=ContainerStatus.FAILED,
                exit_code=137,
                exited_at=datetime.now(UTC),
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-999-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(
            executions, container_infos=container_infos
        )

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

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1
        mock_executor_instance.handle_agent_failure.assert_called_once_with(
            role="coder",
            error="Container exited with code 137",
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_with_prior_failure_returns_zero(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When a container fails but remaining agents reach consensus, returns (0, ...).

        Issue #1495: consensus is the authoritative success signal — container
        failures should not override it when consensus is_complete=True.
        """
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        # Coder exits 137 (OOM kill) immediately; tester stays running
        failed_coder = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.FAILED,
            exit_code=137,
            exited_at=datetime.now(UTC),
        )
        running_tester = ContainerInfo(
            container_id="tester-1",
            container_name="issue-999-tester",
            status=ContainerStatus.RUNNING,
            exit_code=None,
        )

        def _get_info(cid):
            if cid == "coder-1":
                return failed_coder
            return running_tester

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)
        mock_docker.get_container_info.side_effect = _get_info

        def _check_consensus():
            poll_count[0] += 1
            # After handle_agent_failure removes coder, tester alone reaches consensus
            if poll_count[0] >= 2:
                return {"is_complete": True, "has_objections": False, "blocking_agents": []}
            return {"is_complete": False, "has_objections": False, "blocking_agents": ["tester"]}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Issue #1495 fix: consensus is the authoritative success signal.
        # Even though a container failed (OOM), consensus was reached so
        # the phase should succeed (exit 0).
        assert exit_code == 0
        # handle_agent_failure should have been called for the crashed coder
        mock_executor_instance.handle_agent_failure.assert_called_once_with(
            role="coder",
            error="Container exited with code 137",
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_check_error_continues_polling(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """If check_consensus raises, the loop continues polling."""
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [_make_execution(AgentRole.CODER, "coder-1")]

        running_info = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.RUNNING,
            exit_code=None,
        )
        exited_info = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )

        def _get_info(cid):
            if poll_count[0] >= 3:
                return exited_info
            return running_info

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)
        mock_docker.get_container_info.side_effect = _get_info

        def _check_consensus():
            poll_count[0] += 1
            if poll_count[0] == 1:
                raise RuntimeError("evaluator error")
            return {"is_complete": False, "has_objections": False, "blocking_agents": ["coder"]}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Issue #1581: clean exit without consensus must return failure
        # (consensus error on first check, then is_complete=False on recheck)
        assert exit_code == 1


class TestFailedRecovery:
    """Tests for the external FAILED recovery guard (issue #1273, step 3c).

    Covers the scenario where the container_monitor reconciliation thread
    marks the pipeline FAILED while _run_concurrent_phase is actively
    monitoring.
    """

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_recovers_failed_pipeline_when_consensus_complete(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """Pipeline externally marked FAILED is recovered when consensus is complete.

        Simulates the reconciliation thread marking the pipeline FAILED between
        poll iterations.  When consensus completes on a subsequent iteration,
        the monitoring loop should recover the pipeline status to RUNNING and
        return success.
        """
        poll_count = [0]

        def _monotonic():
            return poll_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        # Simulate the reconciliation thread marking the pipeline FAILED.
        # The store round-trips through JSON, so mutate then save back to
        # update the held snapshot.
        disk_pipeline = mock_store.load_pipeline("issue-999")
        disk_pipeline.status = PipelineStatus.FAILED
        disk_pipeline.error = "Container exited unexpectedly"
        mock_store.save_pipeline(disk_pipeline)

        def _check_consensus():
            poll_count[0] += 1
            if poll_count[0] >= 2:
                return {"is_complete": True, "has_objections": False, "blocking_agents": []}
            return {"is_complete": False, "has_objections": False, "blocking_agents": ["coder"]}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Function should return success despite pipeline being FAILED in store.
        assert exit_code == 0
        assert "Consensus reached" in logs

        # The recovery guard runs inside the consensus-complete block on
        # iteration 2 (when consensus completes).  It should detect
        # status=FAILED, acquire the lock, and persist status=RUNNING.
        assert mock_store.load_pipeline.call_count >= 2
        # Lock is acquired 3 times: spawn recording, recovery guard,
        # and _update_agents_complete.  The extra lock (vs 2 in normal case)
        # proves recovery ran.
        assert mock_lock.call_count == 3
        # Verify save_pipeline was called to persist recovery.  The first
        # save is the test's own setup (writing FAILED to the holder); the
        # recovery save is the next one — find it by status.
        save_calls = mock_store.save_pipeline.call_args_list
        recovered = next(
            (call[0][0] for call in save_calls if call[0][0].status == PipelineStatus.RUNNING),
            None,
        )
        assert recovered is not None, "recovery save not found"
        assert recovered.error is None

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_recovery_skipped_when_pipeline_running(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """No recovery needed when pipeline status is RUNNING (normal case)."""
        mock_monotonic.return_value = 10.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        # Pipeline is RUNNING (normal state, from _make_concurrent_pipeline)
        # — recovery should not trigger.
        assert mock_store.load_pipeline("issue-999").status == PipelineStatus.RUNNING

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # Recovery should NOT have been triggered — the pipeline was already
        # RUNNING, so the recovery guard's inner if-check (status==FAILED)
        # is False.  The lock is acquired twice: once for spawn recording
        # and once for _update_agents_complete.  If recovery also ran, it
        # would be 3.
        assert mock_lock.call_count == 2


class TestUpdateAgentsCompleteContainerCleanup:
    """Verify _update_agents_complete also marks containers EXITED (issue #1294)."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_marks_containers_exited(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus completes, RUNNING containers for completed agents are marked EXITED.

        Regression test for issue #1294: stale RUNNING container entries caused the
        container monitor to mark the pipeline FAILED during phase transition.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        # Set up a real pipeline object so we can inspect state mutations
        real_pipeline = _make_concurrent_pipeline()
        phase_exec = real_pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.now(UTC)

        # Add RUNNING agents with container IDs
        for e in executions:
            phase_exec.agents.append(
                AgentExecution(
                    role=e.role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=e.container_id,
                    started_at=datetime.now(UTC),
                )
            )
            phase_exec.containers.append(
                ContainerInfo(
                    container_id=e.container_id,
                    container_name=f"issue-999-{e.role.value}",
                    status=ContainerStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )

        # Override the in-memory store's held pipeline so subsequent
        # load_pipeline() calls observe the test's pre-populated state
        # (containers + agents).  Saves continue to update the holder.
        mock_store.load_pipeline.side_effect = lambda _pid: real_pipeline

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0

        # Agents should be marked COMPLETE
        for agent in phase_exec.agents:
            assert agent.status == AgentExecutionStatus.COMPLETE
            assert agent.completed_at is not None

        # Containers should be marked EXITED with exit_code=0 (issue #1294)
        for ci in phase_exec.containers:
            assert ci.status == ContainerStatus.EXITED, (
                f"Container {ci.container_id} should be EXITED, got {ci.status}"
            )
            assert ci.exit_code == 0
            assert ci.exited_at is not None
