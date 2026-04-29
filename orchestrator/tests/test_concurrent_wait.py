"""Tests for _run_concurrent_phase wait/state-tracking and partial-failure cleanup.

Covers the container wait lifecycle, pipeline state recording/updating, and
the behavior when a subset of agents fail to spawn.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes_spawner import SpawnFailureError
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

# Common consensus result for tests that rely on container-exit fallback.
_NO_CONSENSUS = {"is_complete": False, "has_objections": False, "blocking_agents": []}


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
        except AttributeError, ValueError:
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
        started_at=datetime.now(UTC),
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
                by wait_for_container / get_container_info.
                Defaults to exit_code=0 for all.
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
                        exited_at=datetime.now(UTC),
                    )

        def _wait_side_effect(container_id, timeout=3600):
            return wait_results[container_id]

        def _info_side_effect(container_id):
            return wait_results[container_id]

        mock_docker.wait_for_container.side_effect = _wait_side_effect
        mock_docker.get_container_info.side_effect = _info_side_effect

        # Spawner mock
        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawn_fn = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = mock_spawn_fn

        return pipeline, mock_store, mock_spawner, mock_docker, phase_exec

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_all_containers_exit_without_consensus_returns_failure(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """When all containers exit with code 0 but no consensus, returns (1, logs)."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            _make_execution(AgentRole.DOCUMENTER, "doc-abc"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

        # Issue #1581: clean exit without consensus must return failure
        assert exit_code == 1
        assert mock_docker.get_container_info.call_count == 3

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_container_failure_returns_nonzero(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """When a container exits with non-zero code, returns (1, logs)."""
        mock_monotonic.return_value = 0.0

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
                exited_at=datetime.now(UTC),
            ),
            "tester-abc": ContainerInfo(
                container_id="tester-abc",
                container_name="issue-999-tester",
                status=ContainerStatus.FAILED,
                exit_code=1,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker, _ = self._make_mocks(
            executions, wait_results=wait_results
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_container_not_found_during_wait(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """When a container disappears during poll, returns failure."""
        mock_monotonic.return_value = 0.0

        from docker_client import ContainerNotFoundError

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]

        pipeline, mock_store, mock_spawner, mock_docker, _ = self._make_mocks(executions)
        mock_docker.get_container_info.side_effect = ContainerNotFoundError("coder-abc")

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_state_store_records_containers_and_agents(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """Pipeline state is updated with container/agent info after spawn and wait."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_recorded_container_preserves_k8s_metadata(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """When AgentExecution.container_info carries K8s fields (namespace,
        job_name, pod_name), the recorded phase_execution.containers[] entry
        preserves them instead of rebuilding a minimal ContainerInfo (#1841)."""
        mock_monotonic.return_value = 0.0

        k8s_info = ContainerInfo(
            container_id="uid-abc123",
            container_name="issue-999-coder",
            status=ContainerStatus.PENDING,
            namespace="egg-sandbox",
            job_name="issue-999-coder",
            pod_name="issue-999-coder-xyz",
        )
        execution = AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id="uid-abc123",
            container_info=k8s_info,
            started_at=datetime.now(UTC),
        )
        executions = [execution]

        # Wait results must key on the same container_id.
        wait_results = {
            "uid-abc123": ContainerInfo(
                container_id="uid-abc123",
                container_name="issue-999-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(
            executions, wait_results=wait_results
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

        assert len(phase_exec.containers) == 1
        recorded = phase_exec.containers[0]
        assert recorded.container_id == "uid-abc123"
        assert recorded.namespace == "egg-sandbox"
        assert recorded.job_name == "issue-999-coder"
        assert recorded.pod_name == "issue-999-coder-xyz"
        # model_copy initially overrides status to RUNNING, but the wait
        # loop updates it to EXITED once the container finishes — verify
        # the final state reflects the wait result.
        assert recorded.status == ContainerStatus.EXITED
        assert recorded.agent_role == AgentRole.CODER
        assert recorded.started_at is not None

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_recorded_container_fallback_when_info_missing(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """Docker-style AgentExecution with no container_info still produces
        a minimal ContainerInfo record (backward compatibility for #1841)."""
        mock_monotonic.return_value = 0.0

        executions = [_make_execution(AgentRole.CODER, "coder-abc")]
        # Precondition: no container_info set (docker-style execution).
        assert executions[0].container_info is None

        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

        assert len(phase_exec.containers) == 1
        recorded = phase_exec.containers[0]
        assert recorded.container_id == "coder-abc"
        assert recorded.container_name == "issue-999-coder"
        assert recorded.namespace is None
        assert recorded.job_name is None
        assert recorded.pod_name is None

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_store_none_does_not_crash(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """When store=None, state recording is skipped gracefully.

        Note: with the #1581 consensus gate fix, clean exit without
        consensus returns failure (1), but it must not crash when store
        is None.
        """
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]
        pipeline, _, mock_spawner, mock_docker, _ = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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

        # Issue #1581: clean exit without consensus returns failure, but must not crash
        assert exit_code == 1

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_review_feedback_forwarded_to_build_prompt(
        self, MockExecutor, mock_build_prompt, mock_state_lock, mock_monotonic, mock_sleep
    ):
        """review_feedback parameter is forwarded to _build_agent_prompt calls."""
        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
        ]
        pipeline, mock_store, mock_spawner, mock_docker, phase_exec = self._make_mocks(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
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
            review_feedback="Please fix the error handling",
        )

        # Verify _build_agent_prompt was called with review_feedback
        assert mock_build_prompt.call_count >= 1
        call_kwargs = mock_build_prompt.call_args_list[0]
        assert call_kwargs.kwargs.get("review_feedback") == "Please fix the error handling"


class TestPartialSpawnFailureCleanup:
    """Tests for stopping orphaned containers when some agents fail to spawn.

    Issue #1837: spawn failures raise SpawnFailureError (a KubernetesSpawnError
    subclass) rather than returning (1, logs), so pipeline.error distinguishes
    spawn failures from container exits. Survivor state must be written back to
    the pipeline store before the exception propagates.
    """

    def _invoke(self, executions, docker_side_effect=None):
        """Run _run_concurrent_phase with the given executions and return the
        raised SpawnFailureError plus the phase_exec used by the mock store.

        Note: ``mock_store.load_pipeline()`` always returns the same
        ``mock_pipeline_state`` object, so mutations made by the recording
        block (line ~7291) and the cleanup block (line ~7342) both land on
        the same ``phase_exec``.  This mirrors production where save→load
        round-trips through the same store within a single lock scope.
        """
        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        if docker_side_effect is not None:
            mock_docker.stop_container.side_effect = docker_side_effect
        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        MockExecutor = MagicMock(return_value=mock_executor_instance)

        mock_state_lock_cm = MagicMock()
        mock_state_lock_cm.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock_cm.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("concurrent_executor.ConcurrentPhaseExecutor", MockExecutor),
            patch("routes.pipelines._build_agent_prompt", return_value="test prompt"),
            patch("routes.pipelines.get_pipeline_state_lock", mock_state_lock_cm),
        ):
            with pytest.raises(SpawnFailureError) as excinfo:
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

        return excinfo.value, phase_exec, mock_docker, mock_store

    def test_partial_failure_stops_running_containers(self):
        """When one agent fails to spawn, running containers are stopped."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        err, phase_exec, mock_docker, _ = self._invoke(executions)

        assert mock_docker.stop_container.call_count == 2
        stopped_ids = {call.args[0] for call in mock_docker.stop_container.call_args_list}
        assert stopped_ids == {"coder-abc", "tester-abc"}
        assert err.failures == [(AgentRole.DOCUMENTER.value, "Spawn failed")]

    def test_survivor_state_marked_failed_before_raise(self):
        """Survivor agents and containers must be written back as FAILED so
        get_status agrees with list_containers."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        _, phase_exec, _, mock_store = self._invoke(executions)

        # State must be persisted: once for recording agents, once for cleanup.
        assert mock_store.save_pipeline.call_count >= 2

        survivor_agents = {
            a.role: a for a in phase_exec.agents if a.container_id in {"coder-abc", "tester-abc"}
        }
        assert survivor_agents[AgentRole.CODER].status == AgentExecutionStatus.FAILED
        assert survivor_agents[AgentRole.TESTER].status == AgentExecutionStatus.FAILED
        assert survivor_agents[AgentRole.CODER].error is not None
        assert survivor_agents[AgentRole.TESTER].error is not None
        assert survivor_agents[AgentRole.CODER].completed_at is not None
        assert survivor_agents[AgentRole.TESTER].completed_at is not None

        survivor_containers = {
            c.container_id: c
            for c in phase_exec.containers
            if c.container_id in {"coder-abc", "tester-abc"}
        }
        assert survivor_containers["coder-abc"].status == ContainerStatus.FAILED
        assert survivor_containers["tester-abc"].status == ContainerStatus.FAILED
        assert survivor_containers["coder-abc"].exited_at is not None
        assert survivor_containers["tester-abc"].exited_at is not None

    def test_spawn_failure_error_message_includes_roles_and_reasons(self):
        """SpawnFailureError.__str__ identifies failed roles and their reasons
        so pipeline.error is actionable rather than 'Container exited with code 1'."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_failed_execution(AgentRole.TESTER),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        err, _, _, _ = self._invoke(executions)

        message = str(err)
        assert "Spawn failed" in message
        assert "tester" in message
        assert "documenter" in message
        assert "coder" not in message  # coder survived and was stopped, not a spawn failure
        assert err.failures == [
            (AgentRole.TESTER.value, "Spawn failed"),
            (AgentRole.DOCUMENTER.value, "Spawn failed"),
        ]

    def test_stop_container_error_does_not_block_raise(self):
        """If stopping a container fails, SpawnFailureError still propagates."""
        executions = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_failed_execution(AgentRole.TESTER),
        ]

        err, _, mock_docker, _ = self._invoke(
            executions, docker_side_effect=Exception("Docker socket error")
        )

        assert mock_docker.stop_container.call_count == 1
        assert "tester" in str(err)

    def test_all_spawns_fail_no_containers_to_stop(self):
        """When all agents fail to spawn, no stop_container calls and error
        still names every failed role."""
        executions = [
            _make_failed_execution(AgentRole.CODER),
            _make_failed_execution(AgentRole.TESTER),
            _make_failed_execution(AgentRole.DOCUMENTER),
        ]

        err, _, mock_docker, _ = self._invoke(executions)

        mock_docker.stop_container.assert_not_called()
        assert err.failures == [
            (AgentRole.CODER.value, "Spawn failed"),
            (AgentRole.TESTER.value, "Spawn failed"),
            (AgentRole.DOCUMENTER.value, "Spawn failed"),
        ]


class TestPhaseLevelTransientRetry:
    """Phase coordinator retries transient spawn failures before aborting (#1879)."""

    def _make_transient_failed_execution(
        self, role: AgentRole, error: str = "GatewayError: Connection refused"
    ):
        return AgentExecution(role=role, status=AgentExecutionStatus.FAILED, error=error)

    def _make_permanent_failed_execution(
        self, role: AgentRole, error: str = "Repository not found"
    ):
        return AgentExecution(role=role, status=AgentExecutionStatus.FAILED, error=error)

    def _harness(
        self,
        spawn_all_result,
        spawn_specific_side_effect=None,
        expect_raise=True,
    ):
        """Run _run_concurrent_phase with a configurable retry scenario.

        Args:
            spawn_all_result: List of AgentExecution returned by the initial
                spawn_all call.
            spawn_specific_side_effect: A list of return values or side_effect
                callable for spawn_specific_roles. When None, the method is
                not expected to be called.
            expect_raise: If True, expect SpawnFailureError; if False, expect
                the phase to run the wait loop and return (exit_code, logs).
                When False, all container_ids in the final executions are
                given exit_code=0 wait results.

        Returns a dict with harness references for assertions.
        """
        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()
        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_gateway = MagicMock()
        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.gateway = mock_gateway
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # If the phase should reach the wait loop, wire up wait_for_container /
        # get_container_info to return exit_code=0 for every final container.
        final_executions_holder = {"value": list(spawn_all_result)}

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = spawn_all_result
        if spawn_specific_side_effect is not None:
            if callable(spawn_specific_side_effect):
                mock_executor_instance.spawn_specific_roles.side_effect = spawn_specific_side_effect
            else:
                mock_executor_instance.spawn_specific_roles.side_effect = list(
                    spawn_specific_side_effect
                )
        # When consensus is checked during the wait loop, return "not complete"
        # so the loop falls through to container-exit detection.
        mock_executor_instance.check_consensus.return_value = _NO_CONSENSUS
        MockExecutor = MagicMock(return_value=mock_executor_instance)

        # Wait loop stubs — used only on the recovery path.
        def _wait_side_effect(container_id, timeout=3600):
            return ContainerInfo(
                container_id=container_id,
                container_name=f"issue-999-{container_id}",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            )

        mock_docker.wait_for_container.side_effect = _wait_side_effect
        mock_docker.get_container_info.side_effect = _wait_side_effect

        mock_state_lock_cm = MagicMock()
        mock_state_lock_cm.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock_cm.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("concurrent_executor.ConcurrentPhaseExecutor", MockExecutor),
            patch("routes.pipelines._build_agent_prompt", return_value="test prompt"),
            patch("routes.pipelines.get_pipeline_state_lock", mock_state_lock_cm),
            patch("routes.pipelines.time.sleep"),
        ):
            raised = None
            result = None
            try:
                result = _run_concurrent_phase(
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
            except SpawnFailureError as e:
                raised = e

        if expect_raise:
            assert raised is not None, "Expected SpawnFailureError but phase returned"
        else:
            assert raised is None, f"Did not expect SpawnFailureError: {raised}"

        return {
            "raised": raised,
            "result": result,
            "executor": mock_executor_instance,
            "docker": mock_docker,
            "gateway": mock_gateway,
            "phase_exec": phase_exec,
            "final_executions_holder": final_executions_holder,
        }

    def test_transient_failure_retries_and_recovers(self):
        """One transient failure -> retry succeeds -> phase runs to completion."""
        initial = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            _make_execution(AgentRole.TESTER, "tester-abc"),
            self._make_transient_failed_execution(AgentRole.DOCUMENTER),
        ]
        # Retry recovers documenter with a fresh container_id.
        retry_result = [_make_execution(AgentRole.DOCUMENTER, "doc-xyz")]

        h = self._harness(initial, spawn_specific_side_effect=[retry_result], expect_raise=False)

        # Retry was attempted exactly once with the transient role.
        assert h["executor"].spawn_specific_roles.call_count == 1
        call = h["executor"].spawn_specific_roles.call_args
        assert call.args[0] == [AgentRole.DOCUMENTER]

        # Gateway cleared half-created worktree for failed role before retry.
        assert h["gateway"].delete_worktrees.call_count == 1
        assert h["gateway"].delete_worktrees.call_args.kwargs["container_id"] == (
            "issue-999-documenter"
        )

        # Survivors were NOT stopped during the retry window — the existing
        # abort block should not have run.
        h["docker"].stop_container.assert_not_called()

    def test_permanent_failure_skips_retry(self):
        """Permanent failure goes straight to the existing abort path."""
        initial = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            self._make_permanent_failed_execution(AgentRole.TESTER),
        ]

        h = self._harness(initial, spawn_specific_side_effect=None, expect_raise=True)

        # No retry attempted — classifier recognised a permanent failure.
        h["executor"].spawn_specific_roles.assert_not_called()
        h["gateway"].delete_worktrees.assert_not_called()

        # Existing abort path ran: survivor stopped + failure surfaced.
        assert h["docker"].stop_container.call_count == 1
        assert h["raised"].failures == [(AgentRole.TESTER.value, "Repository not found")]

    def test_retry_budget_exhausts_then_aborts(self):
        """Persistent transient failures exhaust the budget and then abort."""
        initial = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            self._make_transient_failed_execution(AgentRole.TESTER),
        ]
        # Every retry returns the same transient failure.
        retry_result = [self._make_transient_failed_execution(AgentRole.TESTER)]

        h = self._harness(
            initial,
            spawn_specific_side_effect=[retry_result, retry_result, retry_result],
            expect_raise=True,
        )

        # Default phase_spawn_max_retries=2 -> exactly 2 retry attempts.
        assert h["executor"].spawn_specific_roles.call_count == 2

        # Gateway was cleaned up before each retry.
        assert h["gateway"].delete_worktrees.call_count == 2

        # Abort path eventually ran once budget exhausted.
        assert h["docker"].stop_container.call_count == 1
        assert h["raised"].failures == [
            (AgentRole.TESTER.value, "GatewayError: Connection refused"),
        ]

    def test_mixed_transient_and_permanent_retries_only_what_can_recover(self):
        """Permanent + transient both fail first -> retry runs; transient recovers,
        permanent stays failed -> abort with just the permanent error."""
        initial = [
            _make_execution(AgentRole.CODER, "coder-abc"),
            self._make_transient_failed_execution(AgentRole.TESTER),
            self._make_permanent_failed_execution(AgentRole.DOCUMENTER),
        ]
        # Retry is called with BOTH failed roles (tester + documenter).  Tester
        # recovers, documenter fails permanently again.
        retry_result = [
            _make_execution(AgentRole.TESTER, "tester-xyz"),
            self._make_permanent_failed_execution(AgentRole.DOCUMENTER),
        ]

        h = self._harness(initial, spawn_specific_side_effect=[retry_result], expect_raise=True)

        # First retry attempt ran.
        assert h["executor"].spawn_specific_roles.call_count == 1
        retry_call_roles = set(h["executor"].spawn_specific_roles.call_args.args[0])
        assert retry_call_roles == {AgentRole.TESTER, AgentRole.DOCUMENTER}

        # Second retry was NOT attempted — only permanent remains after round 1.
        # (The loop breaks because no remaining failure is transient.)
        assert h["raised"].failures == [(AgentRole.DOCUMENTER.value, "Repository not found")]
