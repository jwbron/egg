"""Tests for issue #2205: per-role exit info preserved on PhaseExecution.

`_record_container_exit` (the per-container exit hook in
`_run_concurrent_phase`'s polling loop) must append an `AgentExitInfo` to
`phase_execution.agent_exits` so failure triage survives container cleanup.
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


def _make_concurrent_pipeline() -> Pipeline:
    config = PipelineConfig()
    config.concurrent_execution = True
    config.max_concurrent_agents = 4
    config.consensus_timeout_minutes = 30
    return Pipeline(
        id="issue-2205",
        issue_number=2205,
        repo="owner/repo",
        branch="egg/issue-2205",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str) -> AgentExecution:
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.RUNNING,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _setup(container_infos: dict[str, ContainerInfo], executions: list[AgentExecution]):
    """Build the shared scaffolding and expose the PhaseExecution for assertions."""
    pipeline = _make_concurrent_pipeline()
    phase_exec = PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )

    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()
    mock_docker.get_container_info.side_effect = lambda cid: container_infos[cid]

    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

    return pipeline, mock_store, mock_spawner, mock_docker, phase_exec


class TestAgentExitsRecorded:
    """`_record_container_exit` populates `PhaseExecution.agent_exits`."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_nonzero_exit_records_role_code_and_log_tail(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """A non-zero container exit appends an AgentExitInfo with the log tail."""
        mock_monotonic.return_value = 0.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-2205-coder",
                status=ContainerStatus.FAILED,
                exit_code=1,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = _setup(
            container_infos, executions
        )
        mock_docker.get_container_logs.return_value = "line one\nline two\nline three"

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
            pipeline_id="issue-2205",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert len(phase_exec.agent_exits) == 1
        info = phase_exec.agent_exits[0]
        assert info.role == AgentRole.CODER
        assert info.exit_code == 1
        assert info.last_lines == ["line one", "line two", "line three"]
        assert info.container_id == "coder-1"
        assert info.terminated_at is not None

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_records_empty_log_tail(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """exit_code=0 still produces an AgentExitInfo (last_lines empty by design)."""
        mock_monotonic.return_value = 0.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-2205-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = _setup(
            container_infos, executions
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
            pipeline_id="issue-2205",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert len(phase_exec.agent_exits) == 1
        info = phase_exec.agent_exits[0]
        assert info.role == AgentRole.CODER
        assert info.exit_code == 0
        # Clean exits don't fetch logs (no IO on healthy containers).
        assert info.last_lines == []

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_per_role_exit_records_in_chronological_order(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """Multiple containers each get their own AgentExitInfo, in observation order."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-2205-coder",
                status=ContainerStatus.FAILED,
                exit_code=1,
                exited_at=datetime.now(UTC),
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-2205-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = _setup(
            container_infos, executions
        )
        mock_docker.get_container_logs.return_value = "boom"

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

        _run_concurrent_phase(
            pipeline_id="issue-2205",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert len(phase_exec.agent_exits) == 2
        roles = {ae.role for ae in phase_exec.agent_exits}
        assert roles == {AgentRole.CODER, AgentRole.TESTER}
        coder = next(ae for ae in phase_exec.agent_exits if ae.role == AgentRole.CODER)
        tester = next(ae for ae in phase_exec.agent_exits if ae.role == AgentRole.TESTER)
        assert coder.exit_code == 1
        assert tester.exit_code == 0
        assert coder.last_lines == ["boom"]
        assert tester.last_lines == []
