"""Tests for agent-level restart functionality.

Covers:
- ContainerSpawner.restart_agent_container() method (task-1-1)
- ContainerSpawner.get_restart_count() and reset_restart_counts() (task-1-1)
- POST /<pipeline_id>/agents/<role>/restart endpoint (task-1-2)
- Consensus state reset on restart
- Edge cases: missing pipeline, invalid role, restart limit exceeded
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    ContainerSpawner,
    ContainerSpawnError,
    SpawnedContainer,
)
from docker_client import ContainerNotFoundError, ContainerOperationError
from gateway_client import GatewayHealth, SessionInfo
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
    mock.CONTAINER_PREFIX = "egg-sandbox-"

    # get_container_info returns info for the existing container
    mock.get_container_info.return_value = ContainerInfo(
        container_id="old-container-abc",
        container_name="egg-sandbox-egg-issue-100-coder",
        status=ContainerStatus.RUNNING,
    )

    mock.create_container.return_value = ContainerInfo(
        container_id="new-container-123",
        container_name="egg-issue-100-coder",
        status=ContainerStatus.PENDING,
    )
    mock.start_container.return_value = ContainerInfo(
        container_id="new-container-123",
        container_name="egg-issue-100-coder",
        status=ContainerStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    mock.stop_container.return_value = ContainerInfo(
        container_id="old-container-abc",
        container_name="egg-issue-100-coder",
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
        )

        assert isinstance(result, SpawnedContainer)
        assert result.agent_role == AgentRole.CODER
        assert result.pipeline_id == "issue-100"
        assert result.container_info.container_id == "new-container-123"
        assert result.session_info is not None

    def test_restart_stops_existing_container(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Restart should stop the old container before spawning a new one."""
        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
        )

        # Should call stop on the old container
        mock_docker_client.stop_container.assert_called()

    def test_restart_removes_existing_container(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """Restart should force-remove the old container."""
        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
        )

        # Force removal should be called
        mock_docker_client.remove_container.assert_called()

    def test_restart_spawns_new_container(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should create and start a new container."""
        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
        )

        mock_docker_client.create_container.assert_called()
        mock_docker_client.start_container.assert_called()

    def test_restart_tracks_count(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should increment the restart count."""
        assert spawner.get_restart_count("issue-100", "coder") == 0

        spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
        )
        assert spawner.get_restart_count("issue-100", "coder") == 1

    def test_restart_limit_exceeded_raises(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should raise ContainerSpawnError when limit is exceeded."""
        # Pre-set restart count to the limit
        spawner._restart_counts[("issue-100", "coder")] = 2

        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                max_restarts=2,
            )

    def test_restart_custom_max_restarts(self, spawner, mock_docker_client, mock_gateway_client):
        """Custom max_restarts should be respected."""
        spawner._restart_counts[("issue-100", "coder")] = 5

        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
                max_restarts=5,
            )

    def test_restart_handles_stop_failure_gracefully(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If stopping the old container fails, restart should still proceed."""
        mock_docker_client.stop_container.side_effect = ContainerOperationError("timeout")

        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
        )

        # Should still succeed — the method handles stop failures gracefully
        assert isinstance(result, SpawnedContainer)

    def test_restart_handles_container_not_found(
        self, spawner, mock_docker_client, mock_gateway_client
    ):
        """If the old container is already gone, restart should still proceed."""
        mock_docker_client.get_container_info.side_effect = ContainerNotFoundError("gone")

        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
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
            )

    def test_restart_with_extra_env(self, spawner, mock_docker_client, mock_gateway_client):
        """Restart should pass extra environment variables to spawn."""
        result = spawner.restart_agent_container(
            pipeline_id="issue-100",
            agent_role=AgentRole.CODER,
            issue_number=100,
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
        with patch.object(
            spawner, "spawn_agent_container", wraps=spawner.spawn_agent_container
        ) as mock_spawn:
            spawner.restart_agent_container(
                pipeline_id="issue-100",
                agent_role=AgentRole.CODER,
                issue_number=100,
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args[1]
            assert call_kwargs.get("preserve_worktree_on_failure") is True, (
                "restart_agent_container must pass preserve_worktree_on_failure=True "
                "to protect existing worktree from transient Docker failures"
            )


class TestRestartCountManagement:
    """Tests for restart count tracking."""

    def test_get_restart_count_default_zero(self, spawner):
        """Default restart count should be 0."""
        assert spawner.get_restart_count("issue-100", "coder") == 0

    def test_get_restart_count_after_restart(self, spawner):
        """Count should increment after manual tracking."""
        spawner._restart_counts[("issue-100", "coder")] = 3
        assert spawner.get_restart_count("issue-100", "coder") == 3

    def test_reset_restart_counts_clears_pipeline(self, spawner):
        """reset_restart_counts should clear all counts for a pipeline."""
        spawner._restart_counts[("issue-100", "coder")] = 2
        spawner._restart_counts[("issue-100", "tester")] = 1
        spawner._restart_counts[("issue-200", "coder")] = 3

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

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_cancelled_pipeline_returns_409(self, mock_repo, mock_resolve, client):
        """Restart returns 409 for cancelled pipelines."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.CANCELLED

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
