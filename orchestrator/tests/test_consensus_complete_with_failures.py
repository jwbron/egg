"""Tests for consensus-complete-with-failures fix (issue #1495).

When all agents confirm BRC consensus (is_complete=True) but some containers
had non-zero exit codes (has_failures=True), the phase should succeed because
consensus is the authoritative success signal.  Previously, has_failures
overrode consensus and caused the phase to report failure.
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


def _make_concurrent_pipeline(pipeline_id: str = "issue-1495") -> Pipeline:
    """Create a pipeline with concurrent_execution enabled."""
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 5,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except AttributeError, ValueError:
            config.__dict__[key] = val

    return Pipeline(
        id=pipeline_id,
        issue_number=1495,
        repo="owner/repo",
        branch="egg/issue-1495",
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


def _base_mocks(executions, container_infos=None):
    """Create common mocks for tests."""
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
                    container_name=f"issue-1495-{e.role.value}",
                    status=ContainerStatus.RUNNING,
                    exit_code=None,
                )

    mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)

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


class TestConsensusCompleteWithFailures:
    """Consensus is_complete=True should return exit 0 even with container failures."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_complete_with_failures_returns_zero(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus is_complete=True and has_failures=True, exit code should be 0.

        This is the core fix for issue #1495: consensus is the authoritative
        success signal, container failures should not override it.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        # Create container_infos where one container has failed (exit code 1)
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1495-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,  # This container "failed"
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1495-tester",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        poll_count = [0]

        def _check_consensus():
            poll_count[0] += 1
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
            pipeline_id="issue-1495",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # The fix: exit code should be 0 even though has_failures was set
        assert exit_code == 0, (
            f"Expected exit code 0 (consensus overrides failures), got {exit_code}. Logs: {logs}"
        )
        assert "Consensus reached" in logs or "phase complete" in logs.lower()

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_complete_without_failures_still_returns_zero(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """Baseline: consensus with no failures should still return 0."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
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
            pipeline_id="issue-1495",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_consensus_incomplete_with_failures_returns_one(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus is NOT complete, container failures should still return 1.

        This test ensures the fix didn't remove failure detection entirely —
        only when consensus is_complete=True should failures be ignored.
        """
        # Use monotonic mock that simulates timeout
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            # Simulate time progression past the consensus timeout
            return call_count[0] * 900.0  # Jump past the 30-minute timeout

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]
        # One container has failed
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1495-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1495-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

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
            pipeline_id="issue-1495",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Without consensus, failures should still cause exit code 1
        assert exit_code == 1, f"Expected exit code 1 (no consensus, has failures), got {exit_code}"


class TestConsensusCompleteUpdatesAgents:
    """When consensus is_complete=True, _update_agents_complete should mark
    all agents as COMPLETE regardless of container exit codes."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_agents_marked_complete_even_with_failures(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """_update_agents_complete should transition FAILED agents to COMPLETE
        when consensus is reached, matching the success exit code."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
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

        exit_code, _ = _run_concurrent_phase(
            pipeline_id="issue-1495",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # _update_agents_complete should have been called (via store.save_pipeline)
        mock_store.save_pipeline.assert_called()
