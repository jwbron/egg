"""Tests for consensus-driven phase advancement in _run_concurrent_phase.

Covers the polling loop that checks consensus, handles objections and timeouts,
and falls back to container-exit-based completion.
"""

from datetime import datetime
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
        started_at=datetime.utcnow(),
    )


def _make_phase_execution():
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


def _base_mocks(executions, container_infos=None):
    """Create common mocks for the consensus polling tests.

    Args:
        executions: AgentExecution list returned by spawn_all.
        container_infos: Dict of container_id -> ContainerInfo for get_container_info.
            Defaults to RUNNING status for all containers.
    """
    pipeline = _make_concurrent_pipeline()
    phase_exec = _make_phase_execution()

    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

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
        # Start past the timeout (30 min = 1800s)
        mock_monotonic.side_effect = [0.0, 1801.0]

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        # Container exits cleanly during the fallback wait
        mock_docker.wait_for_container.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.utcnow(),
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

        assert exit_code == 0
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

        mock_monotonic.side_effect = [0.0, 1801.0]

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)
        mock_docker.wait_for_container.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.utcnow(),
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
    def test_objection_creates_hitl_decision_once(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """When has_objections=True, a HITL decision is created only once."""
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
            exited_at=datetime.utcnow(),
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

        mock_add_decision = MagicMock(return_value=MagicMock(id="dec-1"))
        with patch.object(type(pipeline), "add_decision", mock_add_decision):
            _run_concurrent_phase(
                pipeline_id="issue-999",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # add_decision called exactly once despite multiple polls with objections
        mock_add_decision.assert_called_once()
        call_args = mock_add_decision.call_args
        question = call_args[1].get("question", call_args[0][0] if call_args[0] else "")
        assert "objecting" in question.lower()


class TestContainerExitFallback:
    """All containers exit before consensus — fallback to exit codes."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_all_containers_exit_success(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """When all containers exit code 0 without consensus, returns (0, ...)."""
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
                exited_at=datetime.utcnow(),
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-999-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
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

        assert exit_code == 0
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
                exited_at=datetime.utcnow(),
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
            exited_at=datetime.utcnow(),
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
                exited_at=datetime.utcnow(),
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-999-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
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
            exited_at=datetime.utcnow(),
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

        # Should succeed via container-exit fallback despite first consensus error
        assert exit_code == 0
