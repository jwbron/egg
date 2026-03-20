"""Tests for POST /<pipeline_id>/start — pipeline restart after failure."""

import json
import sys
import threading
from contextlib import contextmanager
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

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import (
    AgentExecution,
    DecisionStatus,
    HITLDecision,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)


@contextmanager
def _noop_lock(*args, **kwargs):
    """No-op context manager to replace get_pipeline_state_lock in tests."""
    yield


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
        execution.started_at = datetime.now(UTC)
        if phase_status in (PipelineStatus.FAILED, PipelineStatus.COMPLETE):
            execution.completed_at = datetime.now(UTC)
        if phase_status == PipelineStatus.FAILED:
            execution.error = "Container exited with code 1"
            execution.review_cycles = 1
            pipeline.error = "Container exited with code 1"
        # Add stale agent/artifact state to verify reset
        execution.agents = [AgentExecution(role="coder", container_id="old-container")]
        execution.artifacts = {"pr_url": "https://github.com/old/pr"}
        execution.containers = [MagicMock()]
    return pipeline


def _setup_mocks(mock_get_repo, mock_resolve, pipeline):
    """Configure common mocks for start-pipeline tests."""
    mock_get_repo.return_value = Path("/repo")
    mock_store = MagicMock()
    mock_store.repo_path = Path("/repo")
    mock_store.load_pipeline.return_value = pipeline
    mock_resolve.return_value = (mock_store, pipeline)
    return mock_store


class TestStartFailedPipeline:
    """Restarting a failed pipeline resets the failed phase."""

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_failed_pipeline_returns_200(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_resets_failed_phase_to_pending(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        # The failed phase should be reset
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert phase_exec.started_at is None
        assert phase_exec.work_started_at is None
        assert phase_exec.completed_at is None
        assert phase_exec.error is None
        assert phase_exec.review_cycles == 0
        assert phase_exec.hitl_review_cycles == 0

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_clears_pipeline_error(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.error is None
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_saves_pipeline_state(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        mock_store = _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        mock_store.save_pipeline.assert_called_once_with(pipeline)

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_clears_agents_and_artifacts(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.agents == []
        assert phase_exec.artifacts == {}
        assert phase_exec.containers == []

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_bumps_created_at(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        original_created_at = pipeline.created_at
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.created_at > original_created_at

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_calls_run_pipeline(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.FAILED,
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        # Join the background thread to avoid racing the mock assertion
        for t in threading.enumerate():
            if t.name == "pipeline-issue-42":
                t.join(timeout=1)
                break

        mock_run.assert_called_once_with("issue-42", Path("/repo"))


class TestStartFailedPipelineWithRunningPhase:
    """Pipeline-level failure with phase still in RUNNING state."""

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_restart_resets_running_phase_to_pending(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        pipeline = _make_pipeline(
            PipelineStatus.FAILED,
            phase=PipelinePhase.REFINE,
            phase_status=PipelineStatus.RUNNING,
        )
        # Simulate pipeline-level failure (pipeline.error set, but phase not FAILED)
        pipeline.error = "Unexpected orchestrator error"
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

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
    def test_complete_pipeline_returns_409(self, mock_get_repo, mock_resolve, client):
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
    def test_cancelled_pipeline_returns_409(self, mock_get_repo, mock_resolve, client):
        mock_get_repo.return_value = Path("/repo")
        pipeline = _make_pipeline(PipelineStatus.CANCELLED)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert "cancelled" in data["message"].lower()


# ---------------------------------------------------------------------------
# Helpers for AWAITING_HUMAN tests
# ---------------------------------------------------------------------------


def _make_awaiting_pipeline(
    phase=PipelinePhase.REFINE,
    pending_decisions=0,
    resolution='{"action": "approve"}',
    decision_type="phase_gate",
):
    """Create an AWAITING_HUMAN pipeline with configurable decisions."""
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        mode="issue",
        status=PipelineStatus.AWAITING_HUMAN,
        current_phase=phase,
    )
    # Mark current phase as COMPLETE (as it would be when HITL gate fires)
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = PipelineStatus.COMPLETE
    phase_exec.started_at = datetime.now(UTC)
    phase_exec.completed_at = datetime.now(UTC)
    phase_exec.agents = [AgentExecution(role="coder", container_id="old-container")]
    phase_exec.artifacts = {"pr_url": "https://github.com/old/pr"}

    # Add a resolved phase_gate decision
    pipeline.decisions.append(
        HITLDecision(
            id="decision-1",
            question="Approve phase?",
            decision_type=decision_type,
            status=DecisionStatus.RESOLVED,
            resolution=resolution,
        )
    )
    # Add pending decisions if requested
    for i in range(pending_decisions):
        pipeline.decisions.append(
            HITLDecision(
                id=f"decision-pending-{i + 1}",
                question="Approve?",
                decision_type="phase_gate",
                status=DecisionStatus.PENDING,
            )
        )
    return pipeline


class TestStartAwaitingHumanPipeline:
    """Recovery of AWAITING_HUMAN pipelines with no pending decisions."""

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_409_when_pending_decisions_exist(self, mock_get_repo, mock_resolve, mock_lock, client):
        """AWAITING_HUMAN with pending decisions returns 409."""
        pipeline = _make_awaiting_pipeline(pending_decisions=1)
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert "pending decision" in data["message"].lower()

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_approved_advances_phase(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Approved resolution advances to the next phase and starts runner."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "approve"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["data"]["status"] == "running"
        # REFINE → PLAN
        assert pipeline.current_phase == PipelinePhase.PLAN
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_request_changes_resets_phase(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """request_changes resolution resets the phase for re-run."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "request_changes", "feedback": "Fix tests"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        # Phase should be reset to PENDING
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert phase_exec.started_at is None
        assert phase_exec.agents == []
        assert phase_exec.artifacts == {}
        # Pipeline should still be on REFINE (not advanced)
        assert pipeline.current_phase == PipelinePhase.REFINE
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_bumps_created_at(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Recovery bumps created_at to signal old thread to skip cleanup."""
        pipeline = _make_awaiting_pipeline()
        original_created_at = pipeline.created_at
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.created_at > original_created_at

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_launches_runner_thread(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Recovery launches _run_pipeline in a background thread."""
        pipeline = _make_awaiting_pipeline()
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        for t in threading.enumerate():
            if t.name == "pipeline-issue-42":
                t.join(timeout=1)
                break

        mock_run.assert_called_once_with("issue-42", Path("/repo"))

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_terminal_phase_marks_complete(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Approved at terminal phase (PR) marks pipeline COMPLETE."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.PR,
            resolution='{"action": "approve"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["data"]["status"] == "complete"
        assert pipeline.status == PipelineStatus.COMPLETE

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_change_approach_resets_phase(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """change_approach resolution resets the phase for re-run (not approval)."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "change_approach", "feedback": "Try a different strategy"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        # Phase should be reset to PENDING (not advanced)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert phase_exec.started_at is None
        assert pipeline.current_phase == PipelinePhase.REFINE
        assert pipeline.status == PipelineStatus.RUNNING
        # Feedback should be preserved for the re-running agent
        assert phase_exec.hitl_feedback == "Try a different strategy"

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_bare_string_approved(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Bare-string 'approved' resolution advances the phase."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution="approved",
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        # REFINE → PLAN
        assert pipeline.current_phase == PipelinePhase.PLAN
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_bare_string_request_changes(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Bare-string 'request changes' resolution resets the phase."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution="request changes",
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.status == PipelineStatus.PENDING
        assert pipeline.current_phase == PipelinePhase.REFINE
        assert pipeline.status == PipelineStatus.RUNNING

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_preserves_feedback(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """request_changes feedback is stored in phase_execution.hitl_feedback."""
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "request_changes", "feedback": "Fix the tests"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        assert phase_exec.hitl_feedback == "Fix the tests"

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_request_changes_clears_concurrent_state(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """request_changes must clear message store and consensus trackers (#1296).

        Stale CONSENSUS_CONFIRMED messages from a previous run cause
        check_consensus() to short-circuit the re-run via its message-bus
        fallback. The recovery path must clear this state.
        """
        mock_msg_store = MagicMock()
        mock_evaluator = MagicMock()
        mock_remove_tracker = MagicMock()

        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "request_changes", "feedback": "Fix tests"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        with (
            patch("message_store.get_message_store", return_value=mock_msg_store),
            patch("peer_consensus.remove_peer_consensus_tracker", mock_remove_tracker),
            patch("consensus.get_consensus_evaluator", return_value=mock_evaluator),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        mock_msg_store.clear.assert_called_once_with("issue-42")
        mock_remove_tracker.assert_called_once_with("issue-42")
        mock_evaluator.clear.assert_called_once_with("issue-42")
