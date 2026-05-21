"""Tests for agent-level restart functionality.

Covers:
- ContainerSpawner.restart_agent_container() method (task-1-1)
- ContainerSpawner.get_restart_count() and reset_restart_counts() (task-1-1)
- POST /<pipeline_id>/agents/<role>/restart endpoint (task-1-2)
- Consensus state reset on restart
- Edge cases: missing pipeline, invalid role, restart limit exceeded
- Issue #1695 gaps: concurrency guard, pre-spawn count increment, mode=None,
  consensus reset ordering, lock cleanup
"""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
)
from docker_client import ContainerNotFoundError, ContainerOperationError
from gateway_client import GatewayHealth, SessionInfo
from kubernetes_client import JobOperationError
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    mock = MagicMock()
    mock.is_connected.return_value = True
    # get_container_info returns info for the existing container
    mock.get_container_info.return_value = ContainerInfo(
        container_id="old-container-abc",
        container_name="egg-agent-issue-100-coder",
        status=ContainerStatus.RUNNING,
    )

    # K8s create_container creates the Job atomically (no separate start)
    mock.create_container.return_value = ContainerInfo(
        container_id="new-container-123",
        container_name="egg-sandbox-egg-agent-issue-100-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.stop_container.return_value = ContainerInfo(
        container_id="old-container-abc",
        container_name="egg-agent-issue-100-coder",
        status=ContainerStatus.EXITED,
    )
    mock.list_containers.return_value = []

    return mock


@pytest.fixture
def mock_gateway_client():
    """Create a mock Gateway client."""
    mock = MagicMock()

    mock.check_health.return_value = GatewayHealth(
        healthy=True,
        status="healthy",
        version="0.1.0",
    )
    mock.register_session.return_value = SessionInfo(
        session_token="new-token-67890",
        container_id="new-container-123",
        container_ip="172.32.0.51",
        mode="public",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    return mock


@pytest.fixture
def spawner(mock_docker_client, mock_gateway_client):
    """Create a container spawner with mocked clients."""
    return ContainerSpawner(
        docker_client=mock_docker_client,
        gateway_client=mock_gateway_client,
    )


# ---------------------------------------------------------------------------
# ContainerSpawner.restart_agent_container tests (task-1-1)
# ---------------------------------------------------------------------------


class TestRestartAgentContainer:
    """Tests for the restart_agent_container spawner method."""

    def test_restart_returns_spawned_container(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Restart should return a SpawnedContainer with new container info."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-100"
        assert result.container_info.container_id == "new-container-123"
        assert result.session_info is not None

    def test_restart_stops_existing_container(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Restart should remove the old Job before spawning a new one."""
        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )

        # K8s restart calls delete_job directly (#2070): remove_agent_job
        # would route both the k8s and gateway calls through one identifier,
        # but k8s wants the prefixed form and the gateway session is keyed
        # by the unprefixed form.
        mock_docker_client.delete_job.assert_called()

    def test_restart_spawns_new_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should create a new Job."""
        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )

        # K8s Job creation is atomic (create_container creates the Job)
        mock_docker_client.create_container.assert_called()

    def test_restart_tracks_count(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should increment the restart count."""
        assert spawner.get_restart_count("issue-100", "coder") == 0

        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )
        assert spawner.get_restart_count("issue-100", "coder") == 1

    def test_restart_limit_exceeded_raises(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should raise ContainerSpawnError when limit is exceeded."""
        # Pre-set restart count to the limit
        spawner._restart_counts[("issue-100", "coder", None)] = 2

        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
                max_restarts=2,
            )

    def test_restart_custom_max_restarts(self, spawner, mock_docker_client, mock_gateway_client):
        """Custom max_restarts should be respected."""
        spawner._restart_counts[("issue-100", "coder", None)] = 5

        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
                max_restarts=5,
            )

    def test_restart_handles_stop_failure_gracefully(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If deleting the old Job fails with a real k8s error, restart should still proceed."""
        # Restart-side delete (line ~1054) fails first; spawn_agent_job's
        # subsequent cleanup delete (line ~404) succeeds.
        mock_docker_client.delete_job.side_effect = [JobOperationError("api timeout"), None]

        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )

        # Should still succeed — the method swallows JobOperationError on best-effort cleanup
        assert isinstance(result, SpawnedContainer)
        # And the failing delete_job call really happened against the prefixed k8s name
        assert mock_docker_client.delete_job.call_args_list[0].args[0] == (
            "egg-sandbox-egg-agent-issue-100-coder"
        )

    def test_restart_handles_container_not_found(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If the old container is already gone, restart should still proceed."""
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError("gone")

        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )

        assert isinstance(result, SpawnedContainer)

    def test_restart_raises_on_spawn_failure(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If spawning the new container fails, restart should raise."""
        mock_docker_client.create_container.side_effect = ContainerOperationError("no space")

        with pytest.raises(ContainerSpawnError):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
            )

    def test_restart_with_extra_env(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should pass extra environment variables to spawn."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
            extra_env={"RESTART_REASON": "stall detected"},
        )

        assert isinstance(result, SpawnedContainer)
        assert result.container_info.container_id == "new-container-123"

    def test_restart_with_reason(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should accept a reason string and succeed."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
            reason="Agent stalled after Edit tool error",
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER

    def test_restart_passes_preserve_worktree_on_failure(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Restart should pass preserve_worktree_on_failure=True to spawn_agent_container.

        This ensures that a transient Docker failure during restart does not
        destroy the agent's worktree containing committed work.
        """
        with patch.object(spawner, "spawn_agent_job", wraps=spawner.spawn_agent_job) as mock_spawn:
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args[1]
            assert call_kwargs.get("preserve_worktree_on_failure") is True, (
                "restart_agent_container must pass preserve_worktree_on_failure=True "
                "to protect existing worktree from transient failures"
            )


class TestRestartCountManagement:
    """Tests for restart count tracking."""

    def test_get_restart_count_default_zero(self, spawner):
        """Default restart count should be 0."""
        assert spawner.get_restart_count("issue-100", "coder") == 0

    def test_get_restart_count_after_restart(self, spawner):
        """Count should increment after manual tracking."""
        spawner._restart_counts[("issue-100", "coder", None)] = 3
        assert spawner.get_restart_count("issue-100", "coder") == 3

    def test_reset_restart_counts_clears_pipeline(self, spawner):
        """reset_restart_counts should clear all counts for a pipeline."""
        spawner._restart_counts[("issue-100", "coder", None)] = 2
        spawner._restart_counts[("issue-100", "tester", None)] = 1
        spawner._restart_counts[("issue-200", "coder", None)] = 3

        spawner.reset_restart_counts("issue-100")

        assert spawner.get_restart_count("issue-100", "coder") == 0
        assert spawner.get_restart_count("issue-100", "tester") == 0
        # Other pipeline's counts should be unaffected
        assert spawner.get_restart_count("issue-200", "coder") == 3

    def test_restart_counts_initialized_empty(self):
        """Restart counts dict should be initialized in constructor."""
        spawner = ContainerSpawner()
        assert hasattr(spawner, "_restart_counts")
        assert spawner._restart_counts == {}


# ---------------------------------------------------------------------------
# REST API endpoint tests (task-1-2)
# ---------------------------------------------------------------------------


def _make_pipeline_with_running_agent(
    pipeline_id="issue-100",
    agent_role=AgentRole.CODER,
):
    """Create a pipeline with a running agent for restart tests."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    pipeline.phases = {
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="container-abc",
                    container_name=f"egg-issue-100-{agent_role.value}",
                    agent_role=agent_role,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-abc",
                ),
            ],
        ),
    }
    return pipeline


try:
    from flask import Flask
    from routes.pipelines import pipelines_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    if not _HAS_FLASK:
        pytest.skip("Flask not available")
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentEndpoint:
    """Tests for POST /<pipeline_id>/agents/<role>/restart endpoint."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_success(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Successful agent restart returns 200 with new container ID."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data.get("success") is True
        assert data["data"]["container_id"] == "new-container-xyz"
        assert data["data"]["agent_role"] == "coder"

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_pipeline_not_found(self, mock_repo, mock_resolve, client):
        """Restart returns 404 when pipeline doesn't exist."""
        mock_repo.return_value = "/repo"
        from state_store import PipelineNotFoundError

        mock_resolve.side_effect = PipelineNotFoundError("not found")

        response = client.post(
            "/api/v1/pipelines/issue-999/agents/coder/restart",
            json={},
        )

        assert response.status_code == 404

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_invalid_role(self, mock_repo, mock_resolve, client):
        """Restart returns 400 for invalid agent role."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/invalid_role/restart",
            json={},
        )

        assert response.status_code == 400

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_pipeline_not_running(self, mock_repo, mock_resolve, client):
        """Restart returns 409 when pipeline is complete."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 409

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_cancelled_pipeline_resumes(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Restart succeeds on a cancelled pipeline and resets status to running.

        cancel_task(cleanup=false) leaves the pipeline in CANCELLED with
        all state preserved; restart_agent should be able to resume it
        rather than requiring a full resubmission (see #1725).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.CANCELLED
        pipeline.phases["implement"].status = PipelineStatus.CANCELLED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Resuming after fix landed"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING
        # Verify the CANCELLED -> RUNNING transition was persisted
        assert mock_store.update_pipeline.call_count >= 1

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_failed_pipeline(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Restart succeeds on a failed pipeline and resets status to running."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Usage limit hit"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_spawner_failure_returns_500(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Restart returns 500 when spawner fails."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.side_effect = ContainerSpawnError("Failed")
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 500

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_spawner_failure_reverts_status_to_failed(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Spawn failure must revert pipeline status back to FAILED.

        Regression test for review feedback on #1594: the early status update
        sets RUNNING before the spawn attempt. If the spawn raises
        ContainerSpawnError, the status must be reverted so monitoring doesn't
        see a 'running' pipeline with no running agent.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.side_effect = ContainerSpawnError("Failed")
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 500
        # Verify status was reverted to FAILED after spawn failure
        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.phases["implement"].status == PipelineStatus.FAILED
        # Verify update_pipeline was called at least twice:
        # once for the early RUNNING update, once for the FAILED revert
        assert mock_store.update_pipeline.call_count >= 2

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_cancelled_spawner_failure_reverts_status_to_failed(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Spawn failure on a CANCELLED pipeline reverts status to FAILED.

        When restart_agent is called on a CANCELLED pipeline and the spawn
        fails, the revert unconditionally sets FAILED (not back to CANCELLED).
        This is intentional: FAILED is also restartable, and it accurately
        reflects that the restart attempt failed.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.CANCELLED
        pipeline.phases["implement"].status = PipelineStatus.CANCELLED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.side_effect = ContainerSpawnError("Failed")
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 500
        # Spawn failure reverts to FAILED (not back to CANCELLED)
        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.phases["implement"].status == PipelineStatus.FAILED
        assert mock_store.update_pipeline.call_count >= 2

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_invalid_pipeline_id_format(self, mock_repo, mock_resolve, client):
        """Restart returns 400 for invalid pipeline ID format."""
        mock_repo.return_value = "/repo"
        from state_store import InvalidPipelineIdError

        mock_resolve.side_effect = InvalidPipelineIdError("bad format")

        response = client.post(
            "/api/v1/pipelines/bad-format-id/agents/coder/restart",
            json={},
        )

        assert response.status_code == 400

    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_uses_computed_gateway_mode(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_gateway_mode, client
    ):
        """Agent restart should use _compute_gateway_mode, not hardcoded 'public'.

        This ensures private-repo pipelines get the correct gateway mode on restart.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        mock_gateway_mode.return_value = ("private", "private")

        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 200
        # Verify _compute_gateway_mode was called with the pipeline
        mock_gateway_mode.assert_called_once_with(pipeline)
        # Verify the computed mode was passed to the spawner
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call[1].get("mode") == "private", (
            "Expected computed gateway mode 'private', not hardcoded 'public'"
        )


# ---------------------------------------------------------------------------
# Issue #2410: slice_id forwarding through the operator restart route
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentEndpointSliceScope:
    """``slice_id`` query / body parameter is validated and forwarded (#2410)."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_id_query_param_forwarded_to_spawner(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """``?slice_id=slice-2`` reaches ``restart_agent_container``."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Slice agent stalled"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["slice_id"] == "slice-2"
        # Reading the restart count after a slice-scoped restart MUST
        # query the per-slice budget bucket (#2410). Without this, the
        # JSON response and audit log report the pipeline-level count
        # (typically 0) and operators can't trust "you've burned N of M
        # restarts" telemetry.
        get_count_call = mock_spawner.get_restart_count.call_args
        assert get_count_call.kwargs.get("slice_id") == "slice-2"

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_id_body_field_forwarded_to_spawner(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """``{"slice_id": "slice-2"}`` in the body reaches the spawner."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Slice agent stalled", "slice_id": "slice-2"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["slice_id"] == "slice-2"
        get_count_call = mock_spawner.get_restart_count.call_args
        assert get_count_call.kwargs.get("slice_id") == "slice-2"

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_invalid_slice_id_returns_400(self, mock_repo, mock_resolve, client):
        """Non-canonical slice_id values are rejected with 400 before spawn."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        # Path-separator and shell-metacharacter values must not reach
        # the spawner — defense-in-depth against a future caller that
        # forgets the upstream regex.
        for bad in ("phase-2", "slice-2/etc", "../slice-2", "slice-"):
            response = client.post(
                f"/api/v1/pipelines/issue-100/agents/coder/restart?slice_id={bad}",
                json={},
            )
            assert response.status_code == 400, f"slice_id={bad!r} should be rejected"

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_no_slice_id_forwards_none(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Pipeline-level callers (no slice_id) get ``slice_id=None`` forwarded."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Pipeline-level restart"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["slice_id"] is None
        get_count_call = mock_spawner.get_restart_count.call_args
        assert get_count_call.kwargs.get("slice_id") is None

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_unknown_slice_id_returns_404(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_load_contract,
        client,
    ):
        """``slice_id`` not present in the pipeline's contract is rejected (#2421).

        Without this gate a well-formed but unknown ``slice-<N>`` would
        spawn an orphan Job + worktree the rest of the system has no
        record of.
        """
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[Slice(id="slice-1", name="Only known slice")],
        )

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-99",
            json={},
        )

        assert response.status_code == 404
        body = response.get_json()
        assert body["success"] is False
        assert "slice-99" in body["message"]
        assert body["details"]["slice_id"] == "slice-99"
        assert body["details"]["known_slices"] == ["slice-1"]
        mock_spawner.restart_agent_container.assert_not_called()

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_known_slice_id_passes_validation(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """A ``slice_id`` matching the contract still reaches the spawner (#2421)."""
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[
                Slice(id="slice-1", name="First"),
                Slice(id="slice-2", name="Second"),
            ],
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={},
        )

        assert response.status_code == 200
        # Lock in the "gate ran AND accepted" intent: without this assertion
        # the test would pass identically whether the gate executed or fell
        # through silently on a fast-path skip. ``load_contract`` may also
        # be invoked downstream by the spawner flow, so we verify the gate's
        # specific call rather than the total count.
        mock_load_contract.assert_any_call(100, Path("/repo"))
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["slice_id"] == "slice-2"

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_id_rejected_for_pipeline_without_contract(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_load_contract,
        client,
    ):
        """``slice_id`` is rejected for pipelines with ``has_contract=False`` (#2421).

        CUSTOM+PR pipelines are not slice-aware (no contract = no
        slices), so any non-``None`` ``slice_id`` against
        them is by definition unknown. Rejecting outright also avoids a
        wasted ``resolve_worktree_path`` + ``load_contract`` call that
        would always raise ``ContractNotFoundError``.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()
        pipeline.has_contract = False
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-1",
            json={},
        )

        assert response.status_code == 404
        body = response.get_json()
        assert body["success"] is False
        assert "slice-1" in body["message"]
        assert body["details"]["slice_id"] == "slice-1"
        assert body["details"]["known_slices"] == []
        # Fast-path: no contract load attempted, no spawner call.
        mock_load_contract.assert_not_called()
        mock_spawner.restart_agent_container.assert_not_called()

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_restart_passes_slice_integration_branch(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Slice-scoped restart must target the slice integration branch (#2428).

        The gateway registers the new session with ``assigned_branch =
        branch`` and rejects every push that doesn't match. If the
        restart route forwards the pipeline tip instead, the restarted
        agent's pushes are rejected the same way the slice-coder
        spawn-side bug rejected them. The slice integration branch is
        ``<namespace_root>/<slice_id>``, the same shape the slice
        scheduler uses for the initial spawn.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        # Use a /work-suffixed pipeline branch so the namespace root is
        # exercised (post-#2399 shape).
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Slice agent stalled"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["branch"] == "egg/issue-100/slice-2"

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_pipeline_level_restart_passes_pipeline_branch(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Without slice_id, restart still forwards the pipeline branch unchanged."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Pipeline-level restart"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["branch"] == "egg/issue-100"


# ---------------------------------------------------------------------------
# Issue #2439: spawner ``base_branch`` matches the slice forest's parent edge
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentEndpointBaseBranch:
    """Restart route forwards the right ``base_branch`` to the spawner (#2439).

    ``gateway.create_worktrees`` is idempotent and ``restart_agent_job``
    passes ``preserve_worktree_on_failure=True``, so most restarts hit a
    pre-existing worktree and the wrong ``base_branch`` is never
    consulted — but a worktree-absent restart (cleanup race, manual
    gateway prune, container hostpath wiped) reaches the worktree
    creation path. Forking from ``pipeline.branch`` (the integration
    tip) at that point pulls sibling slices' commits into the rebuilt
    worktree.
    """

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_pipeline_level_restart_uses_pipeline_base_branch(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Pipeline-level restart forwards ``pipeline.base_branch`` (not ``pipeline.branch``).

        Every other call site in the codebase uses ``pipeline.base_branch``
        for spawner ``base_branch``; the operator-triggered restart route
        was the lone outlier (#2439).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.base_branch = "main"

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Pipeline-level restart"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["base_branch"] == "main"
        # Defensive: the integration tip must NEVER be forwarded as
        # ``base_branch``. If the worktree is rebuilt, forking from
        # the tip leaks tip commits into the per-agent worktree.
        assert restart_call.kwargs["base_branch"] != pipeline.branch

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_root_slice_restart_uses_pipeline_base_branch(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """A root slice (no parent in the slice forest) falls back to ``pipeline.base_branch``."""
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"
        pipeline.base_branch = "main"
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            # slice-1 has no dependencies → root of the forest.
            slices=[Slice(id="slice-1", name="Root slice")],
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-1-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-1",
            json={"reason": "Root slice agent stalled"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["base_branch"] == "main"

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_child_slice_restart_uses_parent_slice_integration_branch(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """A child slice's restart forks from its parent slice's integration branch.

        Mirrors :func:`_run_one_slice_inner`'s ``parent_branch``
        derivation: when ``slice-2`` lists ``slice-1`` as its
        dependency, the parent integration branch is
        ``<namespace_root>/slice-1``. Without this, a worktree-absent
        restart of ``slice-2`` would re-fork from the pipeline tip
        and pull sibling-slice commits into ``slice-2``'s rebuilt
        worktree (#2439).
        """
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"
        pipeline.base_branch = "main"
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[
                Slice(id="slice-1", name="Parent slice"),
                Slice(
                    id="slice-2",
                    name="Child slice",
                    dependencies=["slice-1"],
                ),
            ],
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Child slice agent stalled"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        # The agent's assigned branch is still its OWN integration
        # branch (slice-2). It's the spawner's ``base_branch`` — the
        # ref the per-agent worktree is forked off of when rebuilt —
        # that should reference the parent slice's integration branch.
        assert restart_call.kwargs["branch"] == "egg/issue-100/slice-2"
        assert restart_call.kwargs["base_branch"] == "egg/issue-100/slice-1"

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_restart_with_unloadable_contract_falls_back_to_pipeline_base(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """When the contract can't be loaded, slice restart falls back to ``pipeline.base_branch``.

        The slice-id existence check (#2421) intentionally falls
        through silently when the contract can't be loaded so legitimate
        restarts on a pruned worktree aren't gated. The base-branch
        fix (#2439) inherits that policy: with no contract to consult
        for the parent edge, the spawner gets ``pipeline.base_branch``
        — the same fallback the other call sites use.
        """
        from egg_contracts.loader import ContractNotFoundError

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"
        pipeline.base_branch = "main"
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.side_effect = ContractNotFoundError(
            "issue-100", Path("/repo/.egg-state/contracts/issue-100.json")
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Slice restart with no contract"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        assert restart_call.kwargs["base_branch"] == "main"

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_child_slice_restart_prefers_parent_branch_at_creation(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """Restart prefers the recorded ``parent_branch_at_creation`` over reconstruction.

        Set by ``_run_one_slice_inner`` when the slice was provisioned
        (#2137 TASK-4-2); per the docstring on
        ``Slice.parent_branch_at_creation`` it's *the* recorded fact
        about how the slice was forked. Reconstructing
        ``f"{_issue_branch}/{parent_slice_id}"`` would silently drift
        if a future qualifier-suffix or namespacing change lands in
        ``_run_one_slice_inner`` but not in the restart route — the
        recorded value would not (#2460 review observation 2).
        """
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"
        pipeline.base_branch = "main"
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        # The recorded value differs from what reconstruction would
        # produce (a hypothetical future qualifier suffix). The route
        # must prefer the recorded value over reconstruction.
        recorded_parent_branch = "egg/issue-100/slice-1-future-qualifier"
        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[
                Slice(id="slice-1", name="Parent slice"),
                Slice(
                    id="slice-2",
                    name="Child slice",
                    dependencies=["slice-1"],
                    parent_branch_at_creation=recorded_parent_branch,
                ),
            ],
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Child slice agent stalled"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        # The recorded parent branch wins over reconstructed
        # ``egg/issue-100/slice-1``.
        assert restart_call.kwargs["base_branch"] == recorded_parent_branch

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_child_slice_restart_falls_back_when_parent_complete(
        self,
        mock_repo,
        mock_resolve,
        mock_spawner_fn,
        mock_lock_fn,
        mock_load_contract,
        client,
    ):
        """Restart falls back to ``pipeline.base_branch`` when the parent slice is complete (#2470).

        When the parent slice has reached ``SliceStatus.COMPLETE``,
        its PR has plausibly been merged and the head branch deleted
        by GitHub's standard auto-cleanup. The gateway's per-repo
        ``git fetch origin <parent_branch>`` would then wedge the
        restart on a missing-branch fetch error. The route detects
        this via the contract's slice status and falls back to
        ``pipeline.base_branch`` — equivalent to the
        contract-unloadable fall-through (#2421/#2439): prefer
        letting the restart proceed over over-strict gating.

        The recorded ``parent_branch_at_creation`` is set on the
        child slice here to confirm the fallback wins even when the
        recorded value is available (the issue here is the *branch's*
        existence, not the route's knowledge of its name).
        """
        from egg_contracts.models import Contract, Slice, SliceStatus

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.branch = "egg/issue-100/work"
        pipeline.base_branch = "main"
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[
                Slice(
                    id="slice-1",
                    name="Parent slice (complete, branch likely deleted on origin)",
                    status=SliceStatus.COMPLETE,
                ),
                Slice(
                    id="slice-2",
                    name="Child slice",
                    dependencies=["slice-1"],
                    parent_branch_at_creation="egg/issue-100/slice-1",
                ),
            ],
        )

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-slice-2-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Child slice restart after parent merged"},
        )

        assert response.status_code == 200
        restart_call = mock_spawner.restart_agent_container.call_args
        # Fallback wins over both the recorded parent branch and
        # reconstructed ``egg/issue-100/slice-1``.
        assert restart_call.kwargs["base_branch"] == "main"
        # Agent's assigned branch is unaffected — still its own
        # integration branch.
        assert restart_call.kwargs["branch"] == "egg/issue-100/slice-2"


# ---------------------------------------------------------------------------
# Issue #2422: in-place agent-state mutation matches on (role, slice_id)
# ---------------------------------------------------------------------------


def _make_pipeline_with_two_slice_coders():
    """Pipeline with concurrent slice-2 + slice-3 coder records.

    The two ``AgentExecution`` records share ``role=CODER`` and only
    differ on ``slice_id``. Pre-#2422 the restart route walked
    ``phase_exec.agents`` looking for ``agent.role == role`` and mutated
    the first match — so a slice-3 restart would clobber slice-2's
    record. This fixture lets the test assert the new ``(role,
    slice_id)`` predicate keeps the unrelated slice's row untouched.
    """
    pipeline = Pipeline(
        id="issue-100",
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    pipeline.phases = {
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="container-slice-2",
                    container_name="egg-issue-100-slice-2-coder",
                    agent_role=AgentRole.CODER,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="container-slice-3",
                    container_name="egg-issue-100-slice-3-coder",
                    agent_role=AgentRole.CODER,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-slice-2",
                    slice_id="slice-2",
                ),
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="container-slice-3",
                    slice_id="slice-3",
                ),
            ],
        ),
    }
    return pipeline


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentSliceMatching:
    """``restart_agent`` mutation predicate matches on ``(role, slice_id)`` (#2422)."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_3_restart_does_not_mutate_slice_2_record(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Restarting slice-3 coder leaves slice-2 coder's AgentExecution intact."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_two_slice_coders()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="container-slice-3-RESTARTED",
                container_name="egg-issue-100-slice-3-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        slice2_before = next(
            a for a in pipeline.phases["implement"].agents if a.slice_id == "slice-2"
        )
        slice2_container_before = slice2_before.container_id

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-3",
            json={"reason": "slice-3 stall"},
        )

        assert response.status_code == 200, response.get_json()

        # Assert on the *persisted* state, not just the in-memory pipeline.
        # The route serialises the mutated pipeline via
        # ``store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))``;
        # inspecting the call_args guards against a future refactor where
        # ``_resolve_pipeline`` returns a copy and the route forgets to
        # save the mutation back.
        mock_store.update_pipeline.assert_called()
        persisted = mock_store.update_pipeline.call_args[0][1]
        persisted_agents = persisted["phases"]["implement"]["agents"]
        slice2_persisted = next(a for a in persisted_agents if a["slice_id"] == "slice-2")
        slice3_persisted = next(a for a in persisted_agents if a["slice_id"] == "slice-3")

        assert slice2_persisted["container_id"] == slice2_container_before, (
            "slice-2 container_id must not change when slice-3 is restarted"
        )
        assert slice3_persisted["container_id"] == "container-slice-3-RESTARTED"
        assert slice3_persisted["status"] == AgentExecutionStatus.RUNNING.value

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_3_restart_with_no_existing_record_appends_with_slice_id(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Fall-through ``AgentExecution`` append carries the route's slice_id."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()

        # Pipeline has only slice-2 coder; slice-3 restart should append
        # a new slice-3 row rather than mutating slice-2's record.
        pipeline = Pipeline(
            id="issue-100",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
                containers=[
                    ContainerInfo(
                        container_id="container-slice-2",
                        container_name="egg-issue-100-slice-2-coder",
                        agent_role=AgentRole.CODER,
                        status=ContainerStatus.RUNNING,
                    ),
                ],
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        status=AgentExecutionStatus.RUNNING,
                        container_id="container-slice-2",
                        slice_id="slice-2",
                    ),
                ],
            ),
        }

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="container-slice-3-NEW",
                container_name="egg-issue-100-slice-3-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-3",
            json={"reason": "slice-3 first restart"},
        )

        assert response.status_code == 200
        # Assert on the *persisted* state — same robustness rationale as
        # the slice-3-does-not-mutate-slice-2 test above.
        mock_store.update_pipeline.assert_called()
        persisted = mock_store.update_pipeline.call_args[0][1]
        persisted_agents = persisted["phases"]["implement"]["agents"]
        # slice-2 untouched, slice-3 appended
        assert any(
            a["slice_id"] == "slice-2" and a["container_id"] == "container-slice-2"
            for a in persisted_agents
        )
        slice3_rows = [a for a in persisted_agents if a["slice_id"] == "slice-3"]
        assert len(slice3_rows) == 1
        assert slice3_rows[0]["container_id"] == "container-slice-3-NEW"


# ---------------------------------------------------------------------------
# Issue #1695: mode=None raises ValueError (issue 7)
# ---------------------------------------------------------------------------


class TestModeParameterValidation:
    """Tests that mode=None raises ValueError (issue #1695, item 7)."""

    def test_restart_mode_none_raises_value_error(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """restart_agent_container must raise ValueError when mode is None."""
        with pytest.raises(ValueError, match="mode must be explicitly provided"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode=None,
            )

    def test_restart_mode_none_does_not_increment_count(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """ValueError from mode=None must not burn a restart budget slot."""
        with pytest.raises(ValueError):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode=None,
            )
        assert spawner.get_restart_count("issue-100", "coder") == 0

    def test_restart_mode_explicit_public_works(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Passing mode='public' explicitly should succeed."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="public",
        )
        assert isinstance(result, SpawnedContainer)

    def test_restart_mode_explicit_private_works(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Passing mode='private' explicitly should succeed."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
            mode="private",
        )
        assert isinstance(result, SpawnedContainer)


# ---------------------------------------------------------------------------
# Issue #1695: Failed spawn still increments restart count (issue 4)
# ---------------------------------------------------------------------------


class TestPreSpawnCountIncrement:
    """Tests that restart count is incremented before spawn attempt (issue #1695, item 4)."""

    def test_failed_spawn_increments_count(self, spawner, mock_docker_client, mock_gateway_client):
        """If spawn_agent_container raises, restart count must still be incremented.

        This prevents infinite retry loops when Docker consistently fails.
        """
        mock_docker_client.create_container.side_effect = ContainerOperationError("out of disk")

        with pytest.raises(ContainerSpawnError):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
            )

        # Count should be 1 even though spawn failed
        assert spawner.get_restart_count("issue-100", "coder") == 1

    def test_failed_spawn_eventually_hits_limit(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Repeated spawn failures should eventually exhaust the restart budget."""
        mock_docker_client.create_container.side_effect = ContainerOperationError("out of disk")

        # First attempt — should fail but increment count
        with pytest.raises(ContainerSpawnError, match="out of disk|Failed"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
                max_restarts=2,
            )
        assert spawner.get_restart_count("issue-100", "coder") == 1

        # Second attempt — should fail but increment count
        with pytest.raises(ContainerSpawnError, match="out of disk|Failed"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
                max_restarts=2,
            )
        assert spawner.get_restart_count("issue-100", "coder") == 2

        # Third attempt — should be rejected by limit check
        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                mode="public",
                max_restarts=2,
            )


# ---------------------------------------------------------------------------
# Issue #1695: Concurrency guard (issue 1)
# ---------------------------------------------------------------------------


class TestRestartConcurrencyGuard:
    """Tests that concurrent restart_agent_container calls are serialised (issue #1695, item 1)."""

    def test_concurrent_restarts_both_succeed_within_limit(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Two threads restarting different agents should both succeed."""
        results = {}
        errors = {}

        def restart_agent(role, key):
            try:
                result = spawner.restart_agent_container(
                    pipeline_id="issue-100",
                    agent_role=role,
                    issue_number=100,
                    mode="public",
                )
                results[key] = result
            except Exception as e:
                errors[key] = e

        t1 = threading.Thread(target=restart_agent, args=(AgentRole.CODER, "coder"))
        t2 = threading.Thread(target=restart_agent, args=(AgentRole.TESTER, "tester"))

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Both should succeed since they are different agents
        assert "coder" in results, f"Coder restart failed: {errors.get('coder')}"
        assert "tester" in results, f"Tester restart failed: {errors.get('tester')}"
        assert spawner.get_restart_count("issue-100", "coder") == 1
        assert spawner.get_restart_count("issue-100", "tester") == 1

    def test_concurrent_restarts_same_agent_serialised(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Two threads restarting the same agent should be serialised by the lock.

        With max_restarts=2, both should succeed but the count should be 2.
        """
        results = []
        errors = []
        barrier = threading.Barrier(2, timeout=5)

        def restart_agent():
            try:
                barrier.wait()
                result = spawner.restart_agent_container(
                    pipeline_id="issue-100",
                    agent_role=AgentRole.CODER,
                    issue_number=100,
                    mode="public",
                    max_restarts=2,
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=restart_agent)
        t2 = threading.Thread(target=restart_agent)

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Both should succeed (under the lock, each sees the count before/after the other)
        assert len(results) == 2, f"Expected 2 successes, got errors: {errors}"
        assert spawner.get_restart_count("issue-100", "coder") == 2

    def test_concurrent_restarts_one_past_limit(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If one thread consumes the last restart, the other should get limit error."""
        # Pre-set count to 1 with max_restarts=2 — only one more slot
        spawner._restart_counts[("issue-100", "coder", None)] = 1

        results = []
        errors = []
        barrier = threading.Barrier(2, timeout=5)

        def restart_agent():
            try:
                barrier.wait()
                result = spawner.restart_agent_container(
                    pipeline_id="issue-100",
                    agent_role=AgentRole.CODER,
                    issue_number=100,
                    mode="public",
                    max_restarts=2,
                )
                results.append(result)
            except ContainerSpawnError as e:
                errors.append(e)

        t1 = threading.Thread(target=restart_agent)
        t2 = threading.Thread(target=restart_agent)

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # One should succeed, one should fail with limit exceeded
        assert len(results) == 1, f"Expected 1 success, got {len(results)}"
        assert len(errors) == 1, f"Expected 1 error, got {len(errors)}"
        assert "Restart limit" in str(errors[0])

    def test_restart_lock_created_per_key(self, spawner):
        """Each (pipeline_id, agent_role, slice_id) tuple should get its own lock."""
        key1 = ("issue-100", "coder", None)
        key2 = ("issue-100", "tester", None)
        key3 = ("issue-200", "coder", None)
        # Slice scope splits the key further (#2410).
        key4 = ("issue-100", "coder", "slice-2")

        lock1 = spawner._get_restart_lock(key1)
        lock2 = spawner._get_restart_lock(key2)
        lock3 = spawner._get_restart_lock(key3)
        lock4 = spawner._get_restart_lock(key4)

        assert lock1 is not lock2, "Different agents should have different locks"
        assert lock1 is not lock3, "Different pipelines should have different locks"
        assert lock1 is not lock4, "Different slice scopes should have different locks"

        # Same key should return the same lock
        assert spawner._get_restart_lock(key1) is lock1


# ---------------------------------------------------------------------------
# Issue #1695: Lock cleanup on reset_restart_counts
# ---------------------------------------------------------------------------


class TestRestartLockCleanup:
    """Tests lock behavior in reset_restart_counts."""

    def test_reset_clears_counts_retains_locks(self, spawner):
        """reset_restart_counts should clear counts but retain locks.

        Locks are intentionally kept to prevent a race where
        restart_agent_job holds a per-key lock, reset_restart_counts
        deletes it from the dict, and _get_restart_lock creates a new
        lock for the same key — breaking mutual exclusion.
        """
        # Create some locks by accessing them
        spawner._get_restart_lock(("issue-100", "coder", None))
        spawner._get_restart_lock(("issue-100", "tester", None))
        spawner._get_restart_lock(("issue-200", "coder", None))

        # Set some counts
        spawner._restart_counts[("issue-100", "coder", None)] = 2
        spawner._restart_counts[("issue-100", "tester", None)] = 1
        spawner._restart_counts[("issue-200", "coder", None)] = 3

        spawner.reset_restart_counts("issue-100")

        # Counts for issue-100 should be cleared
        assert spawner._restart_counts.get(("issue-100", "coder", None), 0) == 0
        assert spawner._restart_counts.get(("issue-100", "tester", None), 0) == 0

        # Locks for issue-100 should be retained (not deleted)
        assert ("issue-100", "coder", None) in spawner._restart_locks
        assert ("issue-100", "tester", None) in spawner._restart_locks

        # issue-200 should be untouched
        assert spawner._restart_counts.get(("issue-200", "coder", None), 0) == 3
        assert ("issue-200", "coder", None) in spawner._restart_locks

    def test_restart_lock_initialization(self):
        """Restart lock dicts should be initialised in constructor."""
        spawner = ContainerSpawner()
        assert hasattr(spawner, "_restart_locks")
        assert spawner._restart_locks == {}
        assert hasattr(spawner, "_restart_locks_lock")


# ---------------------------------------------------------------------------
# Issue #1695: Consensus reset ordering (issue 5)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestConsensusResetOrdering:
    """Tests that consensus is reset AFTER successful spawn, not before (issue #1695, item 5)."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_spawn_failure_preserves_consensus(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """If spawner raises ContainerSpawnError, consensus state must NOT be reset.

        Regression test for issue #1695 item 5: consensus reset was previously
        done before the spawn attempt, leaving orphaned consensus deletion on
        spawn failure.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.side_effect = ContainerSpawnError("Docker error")
        mock_spawner_fn.return_value = mock_spawner

        # Mock the consensus modules via sys.modules so the inline imports
        # inside the route handler resolve correctly (no create=True needed).
        mock_tracker = MagicMock()
        mock_evaluator = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                ),
                "consensus": MagicMock(
                    get_consensus_evaluator=MagicMock(return_value=mock_evaluator)
                ),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

            assert response.status_code == 500

            # Consensus should NOT have been reset since spawn failed
            mock_tracker.remove_agent.assert_not_called()
            mock_evaluator.remove_agent.assert_not_called()

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_successful_spawn_resets_consensus(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """On successful spawn, consensus state should be reset for the agent."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        # Patch the consensus imports inside the route handler
        mock_tracker = MagicMock()
        mock_evaluator = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                ),
                "consensus": MagicMock(
                    get_consensus_evaluator=MagicMock(return_value=mock_evaluator)
                ),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

            assert response.status_code == 200

            # Consensus should have been reset after successful spawn
            mock_tracker.remove_agent.assert_called_once_with("coder")
            mock_evaluator.remove_agent.assert_called_once_with("issue-100", "coder")


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentResetsHealthMonitor:
    """Issue #2084: ``restart_agent`` must clear the Tier-1 heartbeat anchor
    so the respawned container is not judged against the dead container's
    clock."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_successful_restart_calls_reset_agent(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        mock_hm = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=MagicMock())
                ),
                "consensus": MagicMock(get_consensus_evaluator=MagicMock(return_value=MagicMock())),
                "health_monitor": MagicMock(get_health_monitor=MagicMock(return_value=mock_hm)),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

            assert response.status_code == 200
            mock_hm.reset_agent.assert_called_once_with("coder")

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_successful_restart_refreshes_started_at(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """``restart_agent`` must overwrite ``agent.started_at`` with the new
        spawn timestamp so ``_get_concurrent_status`` reports an
        ``elapsed_seconds`` anchored on the live container, not the dead one
        (issue #2084)."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        # Pin the existing agent's ``started_at`` to a moment well before the
        # restart so a refreshed value is unambiguously newer.
        original_started_at = datetime.now(UTC) - timedelta(hours=1)
        pipeline.phases["implement"].agents[0].started_at = original_started_at

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        new_container = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-container-xyz",
                container_name="egg-issue-100-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-100",
            environment={},
        )
        mock_spawner.restart_agent_container.return_value = new_container
        mock_spawner.get_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=MagicMock())
                ),
                "consensus": MagicMock(get_consensus_evaluator=MagicMock(return_value=MagicMock())),
                "health_monitor": MagicMock(get_health_monitor=MagicMock(return_value=MagicMock())),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

        assert response.status_code == 200

        agent = pipeline.phases["implement"].agents[0]
        assert agent.started_at is not None
        assert agent.started_at > original_started_at, (
            "restart_agent must refresh started_at to the new container's "
            "spawn time so _get_concurrent_status reports an elapsed_seconds "
            "anchored on the live container instead of the dead one."
        )
        # The refreshed timestamp should be very recent (the test just ran).
        assert (datetime.now(UTC) - agent.started_at) < timedelta(seconds=30)

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_failed_spawn_skips_reset_agent(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """If the spawn fails, the heartbeat anchor must be preserved so the
        old container's stall signal isn't silently swallowed."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.restart_agent_container.side_effect = ContainerSpawnError("boom")
        mock_spawner_fn.return_value = mock_spawner

        mock_hm = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=MagicMock())
                ),
                "consensus": MagicMock(get_consensus_evaluator=MagicMock(return_value=MagicMock())),
                "health_monitor": MagicMock(get_health_monitor=MagicMock(return_value=mock_hm)),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "stalled"},
            )

            assert response.status_code == 500
            mock_hm.reset_agent.assert_not_called()
