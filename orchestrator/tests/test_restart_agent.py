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

    def test_check_and_increment_restart_count_increments(self, spawner):
        """check_and_increment_restart_count bumps and returns the new count (#3244)."""
        assert spawner.check_and_increment_restart_count("issue-100", AgentRole.CODER) == 1
        assert spawner.check_and_increment_restart_count("issue-100", AgentRole.CODER) == 2
        assert spawner.get_restart_count("issue-100", "coder") == 2

    def test_check_and_increment_restart_count_enforces_cap(self, spawner):
        """Once the budget is exhausted, further calls raise instead of incrementing (#3244)."""
        spawner._restart_counts[("issue-100", "coder", None)] = 2
        with pytest.raises(ContainerSpawnError, match="Restart limit"):
            spawner.check_and_increment_restart_count("issue-100", AgentRole.CODER, max_restarts=2)
        # The rejected call must not burn an extra slot.
        assert spawner.get_restart_count("issue-100", "coder") == 2

    def test_check_and_increment_restart_count_slice_scoped(self, spawner):
        """Slice-scoped budgets are independent of the pipeline-level bucket (#3244)."""
        assert (
            spawner.check_and_increment_restart_count(
                "issue-100", AgentRole.CODER, slice_id="slice-2"
            )
            == 1
        )
        # Pipeline-level bucket untouched.
        assert spawner.get_restart_count("issue-100", "coder") == 0
        assert spawner.get_restart_count("issue-100", "coder", slice_id="slice-2") == 1


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

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_success(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
    ):
        """Successful agent restart returns 200 and delegates respawn (#3164).

        After #3164 ``restart_agent`` no longer spawns a resident pod —
        it resets consensus + kills the live one-shot Job and the event
        loop respawns. So the response carries no ``container_id``; it
        carries the ``respawn`` delegation marker. The pipeline here is
        RUNNING (live event loop), so the route must NOT relaunch a driver
        thread — doing so would race the live loop (#3244 review).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data.get("success") is True
        # #3164: no resident pod is spawned here.
        mock_spawner.restart_agent_container.assert_not_called()
        assert "container_id" not in data["data"]
        assert data["data"]["agent_role"] == "coder"
        assert data["data"]["respawn"] == "delegated to orchestrator event loop"
        # restart_count reflects the just-incremented budget (#3244), not 0.
        assert data["data"]["restart_count"] == 1
        # RUNNING pipeline: live event loop owns the respawn; no relaunch.
        mock_spawn_thread.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_rejected_when_budget_exhausted(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
    ):
        """Over-budget restart is rejected 429 without mutating state (#3244).

        The per-(pipeline, role, slice) restart cap that pre-#3164 lived in
        ``restart_agent_job`` is re-enforced on the route. When the budget is
        exhausted the route must reject loudly instead of resetting consensus
        / flipping status and returning a misleading success — an unbounded
        restart storm can actively prevent a converging phase from reaching
        consensus.
        """
        from kubernetes_spawner import KubernetesSpawnError

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.side_effect = KubernetesSpawnError(
            "Restart limit (2) exceeded for coder in pipeline issue-100 (restarted 2 times)"
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 429
        # No destructive action: consensus/Jobs untouched, no relaunch.
        mock_spawner.k8s.list_containers.assert_not_called()
        mock_spawn_thread.assert_not_called()

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

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_cancelled_pipeline_resumes(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
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
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
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
        # Dead event loop is restarted by relaunching the driver thread (#3244).
        mock_spawn_thread.assert_called_once()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_agent_failed_pipeline(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
    ):
        """Restart succeeds on a failed pipeline and resets status to running."""
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Usage limit hit"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING
        # Dead event loop is restarted by relaunching the driver thread (#3244).
        mock_spawn_thread.assert_called_once()

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_tolerates_live_job_listing_failure(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """A k8s failure listing live one-shot Jobs is best-effort (#3164).

        The respawn is owned by the event loop, so a failure tearing
        down the stuck pod must not fail the restart — consensus is
        still reset and the route returns 200.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.side_effect = RuntimeError("k8s API down")
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_failed_pipeline_transitions_to_running_no_revert(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
    ):
        """Restart of a FAILED pipeline transitions to RUNNING and respawns.

        After #3164 there is no resident spawn that can fail, so the
        early FAILED -> RUNNING transition is never reverted. But a
        FAILED pipeline's event loop and ``_run_pipeline`` driver thread
        are already dead, so resetting consensus alone would leave the
        pipeline RUNNING-but-idle (#3244 review). The route must relaunch
        a fresh driver thread to restart the event loop — assert that
        actually happens (the prior version of this test only checked the
        status flip and never that a respawn was driven).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING
        mock_spawner.restart_agent_container.assert_not_called()
        # The dead event loop is restarted by relaunching the driver thread
        # (mirrors restart_phase) — without this the respawn never happens.
        mock_spawn_thread.assert_called_once()
        assert mock_spawn_thread.call_args.args[0] == "issue-100"

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_cancelled_pipeline_transitions_to_running_no_revert(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_spawn_thread, client
    ):
        """Restart of a CANCELLED pipeline transitions to RUNNING and respawns.

        cancel_task(cleanup=false) leaves CANCELLED with state preserved;
        restart resumes it. With no resident spawn (#3164) there is no
        failure path that would revert to FAILED. As with the FAILED path,
        the dead event loop is restarted by relaunching the driver thread
        (#3244 review).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()
        pipeline.status = PipelineStatus.CANCELLED
        pipeline.phases["implement"].status = PipelineStatus.CANCELLED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING
        mock_spawner.restart_agent_container.assert_not_called()
        mock_spawn_thread.assert_called_once()

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
    def test_slice_id_query_param_scopes_live_job_lookup_and_count(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """``?slice_id=slice-2`` scopes the live-Job label filter + restart count (#2410/#3164).

        After #3164 there is no spawn; the slice scope instead narrows
        the ``list_containers`` label filter (so we only tear down the
        slice's stuck pod) and the per-slice restart-count bucket.
        """
        from kubernetes_client import LABEL_AGENT_ROLE, LABEL_PIPELINE_ID, LABEL_SLICE_ID

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={"reason": "Slice agent stalled"},
        )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()
        # The live one-shot Job lookup is found by LABEL (Jobs carry an
        # event-discriminator suffix in their name), scoped to the slice.
        list_call = mock_spawner.k8s.list_containers.call_args
        assert list_call.kwargs["labels"] == {
            LABEL_PIPELINE_ID: "issue-100",
            LABEL_AGENT_ROLE: "coder",
            LABEL_SLICE_ID: "slice-2",
        }
        # Enforcing/incrementing the restart budget after a slice-scoped
        # restart MUST target the per-slice budget bucket (#2410/#3244).
        get_count_call = mock_spawner.check_and_increment_restart_count.call_args
        assert get_count_call.kwargs.get("slice_id") == "slice-2"
        assert response.get_json()["data"]["slice_id"] == "slice-2"

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_slice_id_body_field_scopes_live_job_lookup_and_count(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """``{"slice_id": "slice-2"}`` in the body scopes the label filter + count."""
        from kubernetes_client import LABEL_AGENT_ROLE, LABEL_PIPELINE_ID, LABEL_SLICE_ID

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Slice agent stalled", "slice_id": "slice-2"},
        )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()
        list_call = mock_spawner.k8s.list_containers.call_args
        assert list_call.kwargs["labels"] == {
            LABEL_PIPELINE_ID: "issue-100",
            LABEL_AGENT_ROLE: "coder",
            LABEL_SLICE_ID: "slice-2",
        }
        get_count_call = mock_spawner.check_and_increment_restart_count.call_args
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
    def test_no_slice_id_omits_slice_label_and_uses_none_count(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """Pipeline-level callers omit the slice label and read the None count bucket."""
        from kubernetes_client import LABEL_AGENT_ROLE, LABEL_PIPELINE_ID, LABEL_SLICE_ID

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart",
            json={"reason": "Pipeline-level restart"},
        )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()
        list_call = mock_spawner.k8s.list_containers.call_args
        assert list_call.kwargs["labels"] == {
            LABEL_PIPELINE_ID: "issue-100",
            LABEL_AGENT_ROLE: "coder",
        }
        assert LABEL_SLICE_ID not in list_call.kwargs["labels"]
        get_count_call = mock_spawner.check_and_increment_restart_count.call_args
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
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/coder/restart?slice_id=slice-2",
            json={},
        )

        assert response.status_code == 200
        # Lock in the "gate ran AND accepted" intent: without this assertion
        # the test would pass identically whether the gate executed or fell
        # through silently on a fast-path skip.
        mock_load_contract.assert_any_call(100, Path("/repo"))
        # #3164: validation accepted, no resident spawn; the per-slice
        # count bucket is read.
        mock_spawner.restart_agent_container.assert_not_called()
        assert (
            mock_spawner.check_and_increment_restart_count.call_args.kwargs.get("slice_id")
            == "slice-2"
        )

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

        Pipelines without a contract are not slice-aware (no contract =
        no slices), so any non-``None`` ``slice_id`` against them is by
        definition unknown. Rejecting outright also avoids a wasted
        ``resolve_worktree_path`` + ``load_contract`` call that would
        always raise ``ContractNotFoundError``.
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


# ---------------------------------------------------------------------------
# Issue #2759: omitted slice_id is derived from the phase's agent records
# ---------------------------------------------------------------------------


def _make_pipeline_with_slice_agents(role, slice_records, pipeline_id="issue-100"):
    """Pipeline whose current phase carries per-slice ``AgentExecution`` records.

    ``slice_records`` is a list of ``(slice_id, status)`` tuples — one per
    slice that has run (or is running) ``role``. Exercises the #2759 slice
    auto-derivation in ``restart_agent``.
    """
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    pipeline.phases = {
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.RUNNING,
            agents=[
                AgentExecution(
                    role=role,
                    status=status,
                    container_id=f"container-{sid}",
                    slice_id=sid,
                )
                for sid, status in slice_records
            ],
        ),
    }
    return pipeline


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartAgentEndpointSliceDerivation:
    """Omitted ``slice_id`` is derived from phase agent records (#2759).

    A slice-mode restart that omits ``slice_id`` would otherwise spawn the
    agent pipeline-level — ``EGG_SLICE_ID`` unset — so its BRC signals
    route to the bare pipeline tracker instead of the slice's tracker and
    the slice's consensus wedges with no message-bus recovery path.
    """

    @patch("egg_contracts.loader.load_contract")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_omitted_slice_id_derived_from_single_non_complete_record(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, mock_load_contract, client
    ):
        """One non-complete slice record → its slice_id is derived and forwarded."""
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        # slice-1 finished cleanly; slice-2's reviewer crashed.
        pipeline = _make_pipeline_with_slice_agents(
            AgentRole.REVIEWER_CODE_HOLISTIC,
            [
                ("slice-1", AgentExecutionStatus.COMPLETE),
                ("slice-2", AgentExecutionStatus.FAILED),
            ],
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_load_contract.return_value = Contract(
            pipeline_id="issue-100",
            slices=[Slice(id="slice-1", name="First"), Slice(id="slice-2", name="Second")],
        )

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/reviewer_code_holistic/restart",
            json={"reason": "container crashed"},
        )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()
        # The crashed slice is derived: consensus reset + the live-Job
        # label filter + the restart-count bucket are all scoped to
        # slice-2, so the event loop respawns the slice-2-scoped agent
        # and it rejoins slice-2's BRC tracker rather than the bare one.
        from kubernetes_client import LABEL_SLICE_ID

        assert (
            mock_spawner.k8s.list_containers.call_args.kwargs["labels"][LABEL_SLICE_ID] == "slice-2"
        )
        assert (
            mock_spawner.check_and_increment_restart_count.call_args.kwargs.get("slice_id")
            == "slice-2"
        )
        assert response.get_json()["data"]["slice_id"] == "slice-2"

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_omitted_slice_id_ambiguous_rejected(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Multiple non-complete slice records → 400 with the candidate list."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_slice_agents(
            AgentRole.REVIEWER_CODE_HOLISTIC,
            [
                ("slice-1", AgentExecutionStatus.COMPLETE),
                ("slice-2", AgentExecutionStatus.FAILED),
                ("slice-3", AgentExecutionStatus.RUNNING),
            ],
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/reviewer_code_holistic/restart",
            json={},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert body["reason"] == "slice_id_required"
        assert body["details"]["restart_candidates"] == ["slice-2", "slice-3"]
        assert body["details"]["known_slices"] == ["slice-1", "slice-2", "slice-3"]
        # Never silently spawn an unscoped agent on an ambiguous restart.
        mock_spawner.restart_agent_container.assert_not_called()

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_omitted_slice_id_all_complete_rejected(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """All slice records complete → 400 (no unambiguous restart target)."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_slice_agents(
            AgentRole.REVIEWER_CODE_HOLISTIC,
            [
                ("slice-1", AgentExecutionStatus.COMPLETE),
                ("slice-2", AgentExecutionStatus.COMPLETE),
            ],
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)
        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-100/agents/reviewer_code_holistic/restart",
            json={},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["reason"] == "slice_id_required"
        assert body["details"]["restart_candidates"] == []
        mock_spawner.restart_agent_container.assert_not_called()

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_explicit_slice_id_bypasses_derivation(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """An explicit slice_id wins even when records would derive otherwise.

        Derivation runs only when ``slice_id`` is absent; an explicit
        value (even one with no agent record) is forwarded as-is, so this
        path does not regress the #2410 explicit-scope contract.
        """
        from egg_contracts.models import Contract, Slice

        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_slice_agents(
            AgentRole.REVIEWER_CODE_HOLISTIC,
            [("slice-2", AgentExecutionStatus.FAILED)],
        )
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        with patch("egg_contracts.loader.load_contract") as mock_load_contract:
            mock_load_contract.return_value = Contract(
                pipeline_id="issue-100",
                slices=[Slice(id="slice-1", name="First"), Slice(id="slice-2", name="Second")],
            )
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/reviewer_code_holistic/restart?slice_id=slice-1",
                json={},
            )

        assert response.status_code == 200
        mock_spawner.restart_agent_container.assert_not_called()
        # The explicit slice_id wins over derivation: it scopes the count
        # bucket and the response.
        assert (
            mock_spawner.check_and_increment_restart_count.call_args.kwargs.get("slice_id")
            == "slice-1"
        )
        assert response.get_json()["data"]["slice_id"] == "slice-1"


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
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
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
        # #3164: the restarted record's container_id is cleared to None —
        # the event loop owns the respawn and sets the live container_id.
        assert slice3_persisted["container_id"] is None
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
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
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
        # #3164: appended record has no resident container — event loop respawns.
        assert slice3_rows[0]["container_id"] is None
        assert slice3_rows[0]["status"] == AgentExecutionStatus.RUNNING.value


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
    """Consensus reset is orchestrator-native and unconditional (#3164).

    The old spawn-then-reset ordering (issue #1695 item 5) is moot: there
    is no resident spawn that can fail, so consensus is always reset and
    the event loop respawns from the cleared state.
    """

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_live_job_deletion_failure_still_resets_consensus(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """A best-effort live-Job teardown failure does not block consensus reset.

        After #3164 the only "failure" before the consensus reset is the
        best-effort Job teardown. It must be swallowed so the consensus
        reset (and thus the event-loop respawn) still happens — the
        opposite of the old "preserve consensus on spawn failure" rule.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.side_effect = RuntimeError("k8s API down")
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        mock_tracker = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                ),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

            assert response.status_code == 200
            mock_spawner.restart_agent_container.assert_not_called()
            mock_tracker.remove_agent.assert_called_once_with("coder")

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_resets_consensus(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """A restart resets the agent's BRC consensus state (#3164).

        Only the peer-consensus (BRC) tracker is reset; the legacy
        ``consensus`` evaluator reset was removed in #2777 (slice-2).
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        mock_tracker = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": MagicMock(
                    get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                ),
            },
        ):
            response = client.post(
                "/api/v1/pipelines/issue-100/agents/coder/restart",
                json={"reason": "Agent stalled"},
            )

            assert response.status_code == 200
            mock_spawner.restart_agent_container.assert_not_called()
            mock_tracker.remove_agent.assert_called_once_with("coder")


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
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
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
        """``restart_agent`` must overwrite ``agent.started_at`` with the
        restart timestamp so ``_get_concurrent_status`` reports an
        ``elapsed_seconds`` anchored on the respawn, not the dead container
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
        mock_spawner.k8s.list_containers.return_value = []
        mock_spawner.check_and_increment_restart_count.return_value = 1
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
            "restart_agent must refresh started_at to the restart time so "
            "_get_concurrent_status reports an elapsed_seconds anchored on "
            "the respawn instead of the dead container."
        )
        # The refreshed timestamp should be very recent (the test just ran).
        assert (datetime.now(UTC) - agent.started_at) < timedelta(seconds=30)

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_live_job_deletion_failure_still_resets_health(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_lock_fn, client
    ):
        """A best-effort live-Job teardown failure does not block the health reset (#3164).

        The respawn is owned by the event loop, so the health-monitor
        anchor must be cleared unconditionally — a stuck-pod teardown
        error must not leave the dead container's clock anchored against
        the respawn (issue #2084). Replaces the old
        ``test_failed_spawn_skips_reset_agent`` whose spawn-failure path
        no longer exists.
        """
        mock_repo.return_value = "/repo"
        mock_lock_fn.return_value = MagicMock()
        pipeline = _make_pipeline_with_running_agent()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.k8s.list_containers.side_effect = RuntimeError("k8s API down")
        mock_spawner.check_and_increment_restart_count.return_value = 1
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

            assert response.status_code == 200
            mock_spawner.restart_agent_container.assert_not_called()
            mock_hm.reset_agent.assert_called_once_with("coder")
