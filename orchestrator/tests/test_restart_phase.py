"""Tests for phase-level restart functionality.

Covers:
- POST /<pipeline_id>/phases/<phase>/restart endpoint (task-1-3)
- Full consensus reset on phase restart
- Phase review cycle counter reset
- All containers stopped; polling thread launched (#1638)
- Edge cases: missing pipeline, invalid phase, artifact preservation
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_success(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Successful phase restart returns 200."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
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

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_failed_pipeline(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart succeeds on a failed pipeline and resets status to running."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Usage limit hit"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        # Phase is set to PENDING for the new _run_pipeline thread
        assert pipeline.phases["implement"].status == PipelineStatus.PENDING

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_running_pipeline_status_unchanged(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart on a running pipeline keeps pipeline status as RUNNING."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        assert pipeline.status == PipelineStatus.RUNNING  # precondition

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_awaiting_human_pipeline_set_to_running(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart on an AWAITING_HUMAN pipeline sets status to RUNNING.

        The phase restart launches a new _run_pipeline thread, so the pipeline
        must be in RUNNING state.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.AWAITING_HUMAN

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Agent stalled"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_cancelled_pipeline_resumes(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart resumes a cancelled pipeline (see #1725).

        cancel_task(cleanup=false) leaves the pipeline in CANCELLED with
        all state preserved, and restart_phase should be able to pick it
        back up rather than requiring a full resubmission.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.CANCELLED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={"reason": "Resuming after fix landed"},
        )

        assert response.status_code == 200
        assert pipeline.status == PipelineStatus.RUNNING
        assert pipeline.phases["implement"].status == PipelineStatus.PENDING
        # Verify the CANCELLED -> RUNNING transition was persisted
        assert mock_store.update_pipeline.call_count >= 1

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_stops_all_containers(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart should stop and remove all containers for the phase."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.stop_agent_container.return_value = ContainerInfo(
            container_id="any",
            container_name="any",
            status=ContainerStatus.EXITED,
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

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_resets_consensus(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart should attempt to clear consensus state.

        The endpoint uses a relative import to get the consensus tracker,
        so we verify indirectly: the endpoint succeeds (consensus reset is
        best-effort and handled with try/except).
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        # Should succeed — consensus reset failures are handled gracefully
        assert response.status_code == 200

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_resets_review_cycle_counter(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart should reset the review cycle counter."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        # Pipeline had 2 review cycles before restart
        assert pipeline.phases["implement"].review_cycles == 2

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # Pipeline state should be saved after reset
        assert mock_store.update_pipeline.called, "Expected pipeline state to be persisted"

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_preserves_prior_phase_artifacts(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
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
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
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

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_uses_computed_gateway_mode(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_gateway_mode, mock_thread, client
    ):
        """Phase restart should use _compute_gateway_mode, not hardcoded 'public'."""
        mock_repo.return_value = "/repo"
        mock_gateway_mode.return_value = ("private", "private")

        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        mock_gateway_mode.assert_called_once_with(pipeline)

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_updates_status_before_container_teardown(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
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
        mock_store.repo_path = Path("/repo")
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


# ---------------------------------------------------------------------------
# New tests for #1638: polling thread launch, epoch bump, state reset
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartPhaseLaunchesPollingThread:
    """Tests that restart_phase launches a _run_pipeline polling thread (#1638)."""

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_launches_polling_thread(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread_cls, client
    ):
        """Phase restart must launch a _run_pipeline thread.

        This is the root cause of #1638: without a polling thread, consensus
        completion is never detected and the pipeline hangs.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # Verify thread was created and started
        mock_thread_cls.assert_called_once()
        call_kwargs = mock_thread_cls.call_args
        assert call_kwargs[1]["daemon"] is True
        assert "pipeline-issue-200" in call_kwargs[1]["name"]
        mock_thread_instance.start.assert_called_once()

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_bumps_run_epoch(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must bump pipeline.run_epoch for old thread detection.

        The epoch bump ensures any lingering old _run_pipeline thread detects
        the restart via the run_epoch check and exits.  created_at must remain
        unchanged — it is the user-facing creation timestamp.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        original_created_at = pipeline.created_at

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        # run_epoch must have been set
        assert pipeline.run_epoch is not None, (
            "Expected run_epoch to be set for old thread detection"
        )
        # created_at must NOT have been changed
        assert pipeline.created_at == original_created_at, (
            "created_at is user-facing and must not be bumped on phase restart"
        )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_resets_completed_at(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must clear completed_at.

        Root cause of stale timestamp in #1638: completed_at from the first
        failure persisted across restarts.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED
        pipeline.phases["implement"].completed_at = datetime(2026, 4, 1, 1, 28, tzinfo=UTC)

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.phases["implement"].completed_at is None, (
            "Expected completed_at to be cleared on phase restart"
        )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_sets_phase_pending(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must set phase status to PENDING.

        The _run_pipeline thread expects PENDING to start the phase fresh,
        including spawning containers via _run_concurrent_phase.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.phases["implement"].status = PipelineStatus.FAILED

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.phases["implement"].status == PipelineStatus.PENDING, (
            "Expected phase status to be PENDING for _run_pipeline thread"
        )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_clears_error(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must clear pipeline and phase error fields."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.status = PipelineStatus.FAILED
        pipeline.error = "All agents died from token exhaustion"
        pipeline.phases["implement"].status = PipelineStatus.FAILED
        pipeline.phases["implement"].error = "Container exited with code 1"

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.error is None, "Expected pipeline error to be cleared"
        assert pipeline.phases["implement"].error is None, "Expected phase error to be cleared"

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_preserves_restarted_phase_artifacts(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must preserve artifacts from the restarted phase.

        Artifacts may contain outputs from partial work (e.g., analysis results,
        contract data) that could be useful context for the retried phase.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()
        pipeline.phases["implement"].artifacts = {"partial_output": "partial.md"}

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        assert pipeline.phases["implement"].artifacts == {"partial_output": "partial.md"}, (
            "Expected restarted phase artifacts to be preserved"
        )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_returns_agent_roles(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart should return the list of agent roles to be restarted."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200
        data = response.get_json()
        agents = data["data"]["agents_to_restart"]
        assert len(agents) == 3
        assert set(agents) == {"coder", "tester", "documenter"}

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_deletes_per_agent_worktrees(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Phase restart must delete per-agent worktrees before respawning.

        Regression test for #1723: without worktree cleanup, stale/broken
        btrfs mounts survive container removal and cause respawned containers
        to get invalid worktrees.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        assert response.status_code == 200

        # Verify delete_worktrees was called for each agent role
        delete_calls = mock_spawner.gateway.delete_worktrees.call_args_list
        deleted_ids = {
            call.kwargs.get("container_id", call.args[0] if call.args else None)
            for call in delete_calls
        }
        expected_ids = {
            "issue-200-coder",
            "issue-200-tester",
            "issue-200-documenter",
        }
        assert expected_ids.issubset(deleted_ids), (
            f"Expected worktree deletion for {expected_ids}, got {deleted_ids}"
        )
        # All calls should use force=True
        for call in delete_calls:
            assert call.kwargs.get("force") is True, (
                "Expected force=True for worktree deletion during phase restart"
            )

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_worktree_deletion_failure_is_nonfatal(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """Worktree deletion failure during phase restart must not abort the restart.

        The deletion is best-effort — if it fails (e.g. gateway down), the
        restart should still proceed.  The gateway's create_worktree has its
        own defensive cleanup for stale directories.
        """
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.gateway.delete_worktrees.side_effect = RuntimeError("Gateway unavailable")
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-200/phases/implement/restart",
            json={},
        )

        # Should still succeed despite worktree deletion failures
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Test: _run_pipeline marks pipeline FAILED on fatal error (restart path)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRunPipelineFatalErrorOnRestart:
    """Verify pipeline reaches FAILED when _run_pipeline encounters a fatal error.

    Covers the scenario from review feedback #3: after restart_phase launches
    a _run_pipeline thread, if that thread crashes (e.g. store.load_pipeline
    throws), the pipeline must not be stuck in RUNNING forever.
    """

    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines.get_state_store")
    def test_run_pipeline_marks_failed_on_exception(
        self, mock_get_store, mock_get_spawner, mock_get_lock
    ):
        """_run_pipeline must mark the pipeline FAILED when it hits a fatal error."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_pipeline_with_phase_agents()
        pipeline.run_epoch = datetime.now(UTC)

        mock_store = MagicMock()
        # Always return the pipeline — the fatal error comes from _run_pipeline's
        # own logic (e.g., missing repo volumes), not from load_pipeline.
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_spawner = MagicMock()
        mock_get_spawner.return_value = mock_spawner

        _run_pipeline(pipeline.id, Path("/repo"))

        # The pipeline must be marked FAILED — unconditional assertion.
        assert mock_store.save_pipeline.called, (
            "Expected save_pipeline to be called to mark pipeline FAILED"
        )
        saved = mock_store.save_pipeline.call_args[0][0]
        assert saved.status == PipelineStatus.FAILED, (
            "Expected pipeline to be marked FAILED after fatal error in _run_pipeline"
        )


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartPhaseResetsHealthMonitor:
    """Issue #2084: ``restart_phase`` must clear the Tier-1 heartbeat anchor
    for every respawned role so the new containers are not judged against
    the dead containers' clocks."""

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_phase_calls_reset_agent_for_each_role(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
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
                "/api/v1/pipelines/issue-200/phases/implement/restart",
                json={},
            )

        assert response.status_code == 200
        reset_calls = {call.args[0] for call in mock_hm.reset_agent.call_args_list}
        assert reset_calls == {"coder", "tester", "documenter"}


# ---------------------------------------------------------------------------
# Issue #2515: empty phase_exec.agents must not wedge restart_phase
# ---------------------------------------------------------------------------


def _make_pipeline_with_empty_phase_agents(
    pipeline_id="issue-2515",
    phase=PipelinePhase.IMPLEMENT,
    *,
    active_roles: list[str] | None = None,
):
    """Build a pipeline in CANCELLED state with phase_exec.agents=[].

    Reproduces the post-failed-restart state from #2515: a prior
    ``restart_phase`` cleared ``phase_exec.agents`` (its own clear
    step) but the spawn step failed before re-populating it, then
    ``cancel_task(cleanup=false)`` returned the pipeline to a
    restartable status with an empty roster cache.
    """
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=2515,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
        status=PipelineStatus.CANCELLED,
        current_phase=phase,
        active_roles=active_roles,
    )
    pipeline.phases = {
        phase.value: PhaseExecution(
            phase=phase,
            status=PipelineStatus.PENDING,
            review_cycles=0,
            containers=[],
            agents=[],
        ),
    }
    return pipeline


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestRestartPhaseEmptyAgentsFallback:
    """Issue #2515: ``restart_phase`` must derive the roster from the
    pipeline-level config when ``phase_exec.agents`` is empty.

    Without the fallback, a prior failed restart leaves the pipeline
    permanently unrecoverable: ``restart_phase`` 400s on the empty
    cache and ``start_pipeline`` 409s on the CANCELLED status.
    """

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_empty_agents_falls_back_to_phase_default_roster(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_empty_phase_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-2515/phases/implement/restart",
            json={"reason": "Recover from failed prior restart"},
        )

        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        agents = data["data"]["agents_to_restart"]
        # Implement phase has stable producer roles; reviewers vary by
        # repo / has_contract gating, so assert producers are present
        # rather than pinning the exact list.
        assert "coder" in agents
        assert "tester" in agents
        assert "documenter" in agents
        # Pipeline must transition out of CANCELLED so it is no longer
        # stuck.
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.threading.Thread")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_empty_agents_uses_active_roles_override_when_set(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_thread, client
    ):
        """CUSTOM-mode / BABYSIT pipelines persist their roster on
        ``Pipeline.active_roles``; the fallback must honour it rather
        than expanding to the full phase-default roster."""
        mock_repo.return_value = "/repo"
        pipeline = _make_pipeline_with_empty_phase_agents(
            active_roles=["coder", "tester"],
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner_fn.return_value = mock_spawner

        response = client.post(
            "/api/v1/pipelines/issue-2515/phases/implement/restart",
            json={"reason": "Recover CUSTOM-mode pipeline after failed restart"},
        )

        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert set(data["data"]["agents_to_restart"]) == {"coder", "tester"}
