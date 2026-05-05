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
