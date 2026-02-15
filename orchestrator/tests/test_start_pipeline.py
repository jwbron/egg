"""Tests for POST /<pipeline_id>/start — pipeline restart after failure."""

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import AgentExecution, Pipeline, PipelinePhase, PipelineStatus


@pytest.fixture
def app():
    from flask import Flask
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(status, phase=PipelinePhase.REFINE, phase_status=None):
    """Create a Pipeline with the given status and phase state."""
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        mode="issue",
        status=status,
        current_phase=phase,
    )
    if phase_status is not None:
        execution = pipeline.get_phase_execution(phase)
        execution.status = phase_status
        execution.started_at = datetime.utcnow()
        if phase_status in (PipelineStatus.FAILED, PipelineStatus.COMPLETE):
            execution.completed_at = datetime.utcnow()
        if phase_status == PipelineStatus.FAILED:
            execution.error = "Container exited with code 1"
            execution.review_cycles = 1
            pipeline.error = "Container exited with code 1"
        # Add stale agent/artifact state to verify reset
        execution.agents = [
            AgentExecution(role="coder", container_id="old-container")
        ]
        execution.artifacts = {"pr_url": "https://github.com/old/pr"}
        execution.containers = [MagicMock()]
    return pipeline


class TestStartFailedPipeline:
    """Restarting a failed pipeline resets the failed phase."""

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_failed_pipeline_returns_200(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_resets_failed_phase_to_pending(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        # The failed phase should be reset
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert phase_exec.started_at is None
        assert phase_exec.completed_at is None
        assert phase_exec.error is None
        assert phase_exec.review_cycles == 0
        assert phase_exec.hitl_review_cycles == 0

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_clears_pipeline_error(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.error is None
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_saves_pipeline_state(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        mock_store.save_pipeline.assert_called_once_with(pipeline)


    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_clears_agents_and_artifacts(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.agents == []
        assert phase_exec.artifacts == {}
        assert phase_exec.containers == []

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_bumps_created_at(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        original_created_at = pipeline.created_at
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.created_at > original_created_at

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_calls_run_pipeline(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        # Join the background thread to avoid racing the mock assertion
        for t in threading.enumerate():
            if t.name == "pipeline-issue-42":
                t.join(timeout=1)
                break

        mock_run.assert_called_once_with("issue-42", Path("/repo"))


class TestStartFailedPipelineWithRunningPhase:
    """Pipeline-level failure with phase still in RUNNING state."""

    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_resets_running_phase_to_pending(
        self, mock_get_repo, mock_resolve, mock_run, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.RUNNING,
        )
        # Simulate pipeline-level failure (pipeline.error set, but phase not FAILED)
        pipeline.error = "Unexpected orchestrator error"
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert phase_exec.started_at is None
        assert phase_exec.agents == []
        assert phase_exec.artifacts == {}


class TestStartCompletePipeline:
    """Completed pipelines cannot be restarted."""

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_complete_pipeline_returns_409(
        self, mock_get_repo, mock_resolve, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(PipelineStatus.COMPLETE)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 409


class TestStartCancelledPipeline:
    """Cancelled pipelines cannot be restarted."""

    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancelled_pipeline_returns_409(
        self, mock_get_repo, mock_resolve, client
    ):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(PipelineStatus.CANCELLED)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert "cancelled" in data["message"].lower()
