"""Tests for _run_concurrent_phase wait/state-tracking and partial-failure cleanup.

Covers the container wait lifecycle, pipeline state recording/updating, and
the behavior when a subset of agents fail to spawn.
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
    """Create an AgentExecution with the given role and container."""
    return AgentExecution(
        role=role,
        status=status,
        container_id=container_id,
        started_at=datetime.utcnow(),
    )


def _make_failed_execution(role: AgentRole):
    """Create a failed AgentExecution (no container)."""
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.FAILED,
        error="Spawn failed",
    )


def _make_phase_execution():
    """Create a PhaseExecution for implement phase."""
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


# Import the function under test.  The routes module uses relative imports
# internally; the test conftest ensures orchestrator/ is on sys.path.
from routes.pipelines import _run_concurrent_phase  # noqa: E402


class TestRunConcurrentPhaseWait:
    """Tests for the container wait and state-tracking logic in _run_concurrent_phase."""

    def _make_mocks(self, executions, wait_results=None):
        """Create common mocks for _run_concurrent_phase.

        Args:
            executions: List of AgentExecution returned by spawn_all.
            wait_results: Dict mapping container_id to ContainerInfo returned
                by wait_for_container.  Defaults to exit_code=0 for all.
        """
        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        # Store mock
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_store.load_pipeline.return_value = mock_pipeline_state

        # Docker client mock
        mock_docker = MagicMock()
        if wait_results is None:
            wait_results = {}
            for e in executions:
                if e.container_id:
                    wait_results[e.container_id] = ContainerInfo(
                        container_id=e.container_id,
                        container_name=f"issue-999-{e.role.value}",
                        status=ContainerStatus.EXITED,
                        exit_code=0,
                        exited_at=datetime.utcnow(),
                    )

        def _wait_side_effect(container_id, timeout=3600):
            return wait_results[container_id]

        mock_docker.wait_for_container.side_effect = _wait_side_effect

        # Spawner mock
        mock_spawner = MagicMock()
        mock_spawner.docker = mock_docker
        mock_spawn_fn = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = mock_spawn_fn

        return pipeline, mock_store, mock_spawner, mock_docker, phase_exec

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_all_containers_exit_successfully(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """When all containers exit with code 0, returns (0, logs)."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            _make_execution(AgentRole.DOCUMENTER, "doc-abc"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 0
        assert mock_docker.wait_for_container.call_count == 3

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_container_failure_returns_nonzero(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """When a container exits with non-zero code, returns (1, logs)."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
        ]

        wait_results = {
            "coder-abc": ContainerInfo(
                container_id="coder-abc",
                container_name="issue-999-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
            ),
            "tester-abc": ContainerInfo(
                container_id="tester-abc",
                container_name="issue-999-tester",
                status=ContainerStatus.FAILED,
                exit_code=1,
                exited_at=datetime.utcnow(),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker, _ = self._make_mocks(
            executions, wait_results=wait_results
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 1
        assert "tester" in logs

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_container_not_found_during_wait(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """When a container disappears during wait, returns failure."""
        from docker_client import ContainerNotFoundError

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]

        pipeline, mock_store, mock_spawner, mock_docker, _ = self._make_mocks(executions)
        mock_docker.wait_for_container.side_effect = ContainerNotFoundError("coder-abc")

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 1
        assert "coder" in logs

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_state_store_records_containers_and_agents(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """Pipeline state is updated with container/agent info after spawn and wait."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        # store.save_pipeline called at least twice: once after spawn recording,
        # once after wait/status update
        assert mock_store.save_pipeline.call_count >= 2

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_store_none_does_not_crash(self, MockExecutor, mock_build_prompt, mock_state_lock):
        """When store=None, state recording is skipped gracefully."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]
        pipeline, _, mock_spawner, mock_docker, _ = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=None,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 0


class TestPartialSpawnFailureCleanup:
    """Tests for stopping orphaned containers when some agents fail to spawn."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_partial_failure_stops_running_containers(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """When one agent fails to spawn, running containers are stopped."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = _make_phase_execution()
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_spawner = MagicMock()
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 1
        # Both running containers should have been stopped
        assert mock_docker.stop_container.call_count == 2
        stopped_ids = {call.args[0] for call in mock_docker.stop_container.call_args_list}
        assert stopped_ids == {"coder-abc", "tester-abc"}

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_stop_container_error_does_not_block_return(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """If stopping a container fails, partial failure still returns (1, logs)."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_failed_execution(AgentRole.TESTER),
        ]

        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = _make_phase_execution()
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.stop_container.side_effect = Exception("Docker socket error")
        mock_spawner = MagicMock()
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        # Should still return failure even though stop_container raised
        assert exit_code == 1
        assert mock_docker.stop_container.call_count == 1

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_all_spawns_fail_no_containers_to_stop(
        self, MockExecutor, mock_build_prompt, mock_state_lock
    ):
        """When all agents fail to spawn, no stop_container calls."""
        executions = [
            _make_failed_execution(AgentRole.CODER),
            _make_failed_execution(AgentRole.TESTER),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = _make_phase_execution()
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_spawner = MagicMock()
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor.return_value = mock_executor_instance

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            sandbox_env={},
            store=mock_store,
            certs_volume=None,
            worktree_repo_path=Path("/tmp/test-repo"),
        )

        assert exit_code == 1
        mock_docker.stop_container.assert_not_called()
