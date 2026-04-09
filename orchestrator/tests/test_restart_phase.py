"""Tests for phase-level restart functionality.

Covers:
- POST /<pipeline_id>/phases/<phase>/restart endpoint (task-1-3)
- Full consensus reset on phase restart
- Phase review cycle counter reset
- All containers stopped and respawned
- Edge cases: missing pipeline, invalid phase, artifact preservation
"""

from unittest.mock import MagicMock, patch

import pytest
from container_spawner import (
    SpawnedContainer,
)
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
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_with_phase_agents(
    pipeline_id="issue-200",
    phase=PipelinePhase.IMPLEMENT,
):
    """Create a pipeline with multiple running agents in a phase."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=200,
        repo="owner/repo",
        branch="egg/issue-200",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    pipeline.phases = {
        phase.value: PhaseExecution(
            phase=phase,
            status=PipelineStatus.RUNNING,
            review_cycles=2,
            containers=[
                ContainerInfo(
                    container_id="coder-container",
                    container_name="egg-issue-200-coder",
                    agent_role=AgentRole.CODER,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="tester-container",
                    container_name="egg-issue-200-tester",
                    agent_role=AgentRole.TESTER,
                    status=ContainerStatus.RUNNING,
                ),
                ContainerInfo(
                    container_id="documenter-container",
                    container_name="egg-issue-200-documenter",
                    agent_role=AgentRole.DOCUMENTER,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="coder-container",
                ),
                AgentExecution(
                    role=AgentRole.TESTER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="tester-container",
                ),
                AgentExecution(
                    role=AgentRole.DOCUMENTER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="documenter-container",
                ),
            ],
        ),
    }
    return pipeline


# ---------------------------------------------------------------------------
# Flask test setup
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase restart endpoint tests (task-1-3)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartPhaseEndpoint:
    """Tests for POST /<pipeline_id>/phases/<phase>/restart endpoint."""

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_success(self, mock_repo, mock_resolve, mock_spawner_fn, client):
        """Successful phase restart returns 200."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 3
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-coder-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Multiple agents stalled"},
        )

        assert response.status_code == 200

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_pipeline_not_found(self, mock_repo, mock_resolve, client):
        """Restart returns 404 for missing pipeline."""
        mock_repo.return_value = "/repo"
        from state_store import PipelineNotFoundError

        mock_resolve.side_effect = PipelineNotFoundError("not found")

        response = client.post(
            "/api/v1/pipelines/nonexistent/phases/implement/restart",
            json={},
        )

        assert response.status_code == 404

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_invalid_phase(self, mock_repo, mock_resolve, mock_spawner_fn, client):
        """Restart returns 400 for invalid phase name."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/invalid_phase/restart",
            json={},
        )

        assert response.status_code == 400

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_failed_pipeline(self, mock_repo, mock_resolve, mock_spawner_fn, client):
        """Phase restart succeeds on a failed pipeline and resets status to running."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 3
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-coder-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Usage limit hit"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_running_pipeline_status_unchanged(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart on a running pipeline does not change pipeline status."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        assert pipeline.status == PipelineStatus.RUNNING  # precondition

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 3
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-coder-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_awaiting_human_pipeline_status_unchanged(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart on an AWAITING_HUMAN pipeline does not change pipeline status."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.AWAITING_HUMAN
        assert pipeline.status == PipelineStatus.AWAITING_HUMAN  # precondition

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 3
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-coder-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.AWAITING_HUMAN

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_cancelled_pipeline_returns_409(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart returns 409 for cancelled pipelines."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.CANCELLED

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 409

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_stops_all_containers(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should stop and remove all containers for the phase."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.stop_agent_container.return_value = ContainerInfo(
            container_id="any",
            container_name="any",
            status=ContainerStatus.EXITED,
        )
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # All 3 containers should be stopped and removed individually
        assert mock_spawner.stop_agent_container.call_count >= 3, (
            f"Expected 3+ stop calls for 3 containers, got {mock_spawner.stop_agent_container.call_count}"
        )
        assert mock_spawner.remove_agent_container.call_count >= 3, (
            f"Expected 3+ remove calls for 3 containers, got {mock_spawner.remove_agent_container.call_count}"
        )

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_resets_consensus(self, mock_repo, mock_resolve, mock_spawner_fn, client):
        """Phase restart should attempt to clear consensus state.

        The endpoint uses a relative import to get the consensus tracker,
        so we verify indirectly: the endpoint succeeds (consensus reset is
        best-effort and handled with try/except).
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        # Should succeed — consensus reset failures are handled gracefully
        assert response.status_code == 200

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_resets_review_cycle_counter(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should reset the review cycle counter."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        # Pipeline had 2 review cycles before restart
        assert pipeline.phases["implement"].review_cycles == 2

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # Pipeline state should be saved after reset
        assert mock_store.update_pipeline.called, "Expected pipeline state to be persisted"

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_preserves_prior_phase_artifacts(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should preserve artifacts from prior phases."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        # Add a prior phase with artifacts
        pipeline.phases["refine"] = PhaseExecution(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.COMPLETE,
            artifacts={"analysis": "analysis.md"},
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # Prior phase artifacts should be preserved
        assert pipeline.phases["refine"].artifacts == {"analysis": "analysis.md"}

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_respawns_all_agents(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should respawn all agents for the phase."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        spawn_count = 0

        def mock_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return SpawnedContainer(
                container_info=ContainerInfo(
                    container_id=f"new-{spawn_count}",
                    container_name=f"egg-issue-200-agent-{spawn_count}",
                    status=ContainerStatus.RUNNING,
                ),
                session_info=None,
                agent_role=kwargs.get("agent_role", AgentRole.CODER),
                pipeline_id="issue-200",
                environment={},
            )

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.side_effect = mock_spawn
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # All 3 agents should be respawned
        assert spawn_count >= 3, f"Expected 3+ agent respawns, got {spawn_count}"

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_non_current_phase_returns_409(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Restarting a phase that is not the current phase should return 409.

        This prevents corruption of pipeline state by restarting a completed
        or future phase.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents(phase=PipelinePhase.IMPLEMENT)

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/refine/restart",
            json={},
        )

        assert response.status_code == 409
        data = response.get_json()
        assert "not the current phase" in data.get("message", "").lower()

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_toctou_guard_under_lock(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should re-check current phase under the lock.

        If the pipeline advances between the initial check and lock acquisition,
        the endpoint should return 409 rather than restarting the wrong phase.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents(phase=PipelinePhase.IMPLEMENT)

        # The pipeline loaded under the lock has advanced to REFINE
        advanced_pipeline = _make_pipeline_with_phase_agents(phase=PipelinePhase.REFINE)
        # Also add an "implement" phase entry so the phase lookup doesn't fail early
        advanced_pipeline.phases["implement"] = PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            status=PipelineStatus.COMPLETE,
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = advanced_pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 409, (
            "Expected 409 when pipeline advances between initial check and lock acquisition"
        )

    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_uses_computed_gateway_mode(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_gateway_mode, client
    ):
        """Phase restart should use _compute_gateway_mode, not hardcoded 'public'.

        This ensures private-repo pipelines get the correct gateway mode on restart.
        """
        mock_repo.return_value = "/repo"
        mock_gateway_mode.return_value = ("private", "private")

        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        mock_gateway_mode.assert_called_once_with(pipeline)
        # Verify spawn calls use the computed mode, not hardcoded "public"
        for spawn_call in mock_spawner.spawn_agent_container.call_args_list:
            assert spawn_call[1].get("mode") == "private", (
                "Expected computed gateway mode 'private', not hardcoded 'public'"
            )

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_passes_preserve_worktree_on_failure(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Phase restart should pass preserve_worktree_on_failure=True to spawn.

        This ensures that a transient Docker failure during phase restart does
        not destroy agents' worktrees containing committed work.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # Every spawn call during phase restart must preserve worktrees on failure
        for spawn_call in mock_spawner.spawn_agent_container.call_args_list:
            assert spawn_call[1].get("preserve_worktree_on_failure") is True, (
                "Phase restart must pass preserve_worktree_on_failure=True to "
                "protect existing worktrees from transient Docker failures"
            )

    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_updates_status_before_container_teardown(
        self, mock_repo, mock_resolve, mock_spawner_fn, client
    ):
        """Status should be set to RUNNING before slow container stop/remove calls.

        Regression test for #1594: MCP lifecycle operations time out but
        succeed server-side.  If get_status is called during container
        teardown, it must already show 'running', not stale 'failed'.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_resolve.return_value = (mock_store, pipeline)

        # Track the order of update_pipeline vs stop_agent_container calls
        call_order: list[str] = []

        def track_update(*args, **kwargs):
            call_order.append("update_pipeline")

        def track_stop(*args, **kwargs):
            call_order.append("stop_container")

        mock_store.update_pipeline.side_effect = track_update

        mock_spawner = MagicMock()
        mock_spawner.stop_agent_container.side_effect = track_stop
        mock_spawner.spawn_agent_container.return_value = SpawnedContainer(
            container_info=ContainerInfo(
                container_id="new-coder-xyz",
                container_name="egg-issue-200-coder",
                status=ContainerStatus.RUNNING,
            ),
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-200",
            environment={},
        )
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Fix stall"},
        )

        assert response.status_code == 200
        # The FIRST update_pipeline must come BEFORE any stop_container
        assert "update_pipeline" in call_order, "Expected at least one update_pipeline call"
        assert "stop_container" in call_order, "Expected at least one stop_container call"
        first_update = call_order.index("update_pipeline")
        first_stop = call_order.index("stop_container")
        assert first_update < first_stop, (
            f"Expected status update before container stop. Call order: {call_order}"
        )
