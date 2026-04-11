"""Tests for advance_phase launching a _run_pipeline thread (#1672).

When advance_phase (force or normal) advances to a new phase, it must spawn
a background _run_pipeline thread — otherwise the pipeline sits in RUNNING
state with nothing processing the new phase.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing models
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    pipeline_id="issue-300",
    phase=PipelinePhase.PLAN,
    phase_status=PipelineStatus.COMPLETE,
):
    """Create a pipeline ready for phase advance."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=300,
        repo="owner/repo",
        branch="egg/issue-300",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    # Mark the current phase as complete so advance_phase accepts it
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = phase_status
    phase_exec.completed_at = datetime.now(UTC)
    return pipeline


# ---------------------------------------------------------------------------
# Flask test setup
# ---------------------------------------------------------------------------

try:
    from flask import Flask
    from routes.phases import phases_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


@pytest.fixture
def app():
    if not _HAS_FLASK:
        pytest.skip("Flask not available")
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests for #1672: advance_phase must launch _run_pipeline thread
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestAdvancePhaseLaunchesThread:
    """Tests that advance_phase spawns a _run_pipeline background thread (#1672)."""

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_launches_thread(
        self, mock_get_store, mock_get_lock, mock_thread_cls, client
    ):
        """advance_phase must launch a _run_pipeline thread after state update.

        This is the root cause of #1672: without a thread, the new phase
        never gets processed.
        """
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        # Verify thread was created and started
        mock_thread_cls.assert_called_once()
        call_kwargs = mock_thread_cls.call_args
        assert call_kwargs[1]["daemon"] is True
        assert "pipeline-issue-300" in call_kwargs[1]["name"]
        mock_thread_instance.start.assert_called_once()

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_force_launches_thread(
        self, mock_get_store, mock_get_lock, mock_thread_cls, client
    ):
        """force=true advance must also launch a thread."""
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            phase_status=PipelineStatus.RUNNING,
        )

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "pr", "force": True},
        )

        assert response.status_code == 200
        mock_thread_cls.assert_called_once()
        mock_thread_instance.start.assert_called_once()

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_bumps_run_epoch(
        self, mock_get_store, mock_get_lock, mock_thread_cls, client
    ):
        """advance_phase must bump run_epoch so stale threads exit."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        original_epoch = pipeline.run_epoch
        original_created_at = pipeline.created_at

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        # run_epoch must have been bumped
        assert pipeline.run_epoch is not None
        assert pipeline.run_epoch != original_epoch
        # Verify the saved pipeline has the bumped epoch
        saved_pipeline = mock_store.save_pipeline.call_args[0][0]
        assert saved_pipeline.run_epoch is not None
        assert saved_pipeline.run_epoch != original_epoch
        # created_at must NOT change
        assert pipeline.created_at == original_created_at

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_acquires_state_lock(
        self, mock_get_store, mock_get_lock, mock_thread_cls, client
    ):
        """advance_phase must acquire the pipeline state lock for atomicity."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)

        mock_lock = MagicMock()
        mock_get_lock.return_value = mock_lock
        mock_thread_cls.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        mock_get_lock.assert_called_once_with("issue-300")
        mock_lock.__enter__.assert_called_once()
