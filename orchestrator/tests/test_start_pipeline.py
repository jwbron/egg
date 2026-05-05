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
    def test_restart_bumps_run_epoch(
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

        assert pipeline.run_epoch is not None
        assert pipeline.created_at == original_created_at

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
    def test_recovery_bumps_run_epoch(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Recovery bumps run_epoch to signal old thread to skip cleanup."""
        pipeline = _make_awaiting_pipeline()
        original_created_at = pipeline.created_at
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        client.post("/api/v1/pipelines/issue-42/start")

        assert pipeline.run_epoch is not None
        assert pipeline.created_at == original_created_at

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

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_routes_phase_gate_persistence_through_worktree(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Regression for #2357.

        The contract and phase draft both live under the per-pipeline
        worktree (``<worktree>/.egg-state/``), not the orchestrator's
        main repo. When the AWAITING_HUMAN recovery branch persists a
        phase gate resolution, it must resolve the worktree path and
        forward it to ``_persist_phase_gate_resolution``,
        ``_commit_statefiles_to_worktree``, and ``push_worktree_branch``
        — same shape as the #2345 conflation in
        ``_sync_pipeline_decisions_to_contract``.
        """
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "approve", "context": "Use adapter pattern"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/repo")
        mock_spawner = MagicMock()

        with (
            patch(
                "routes.pipelines._resolve_pipeline_worktree_path",
                return_value=worktree_path,
            ) as mock_resolve_wt,
            patch("routes.pipelines._persist_phase_gate_resolution") as mock_persist,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
            patch("routes.pipelines._get_spawner", return_value=mock_spawner),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200

        mock_resolve_wt.assert_called_once_with(pipeline, Path("/repo"))

        persist_args, _ = mock_persist.call_args
        assert persist_args[0] == worktree_path, (
            f"_persist_phase_gate_resolution must be called with the "
            f"worktree path, got {persist_args[0]}"
        )

        commit_args, _ = mock_commit.call_args
        assert commit_args[0] == worktree_path, (
            f"_commit_statefiles_to_worktree must be called with the "
            f"worktree path, got {commit_args[0]}"
        )

        push_kwargs = mock_spawner.gateway.push_worktree_branch.call_args.kwargs
        assert push_kwargs["repo_path"] == str(worktree_path), (
            f"push_worktree_branch must be called with the worktree path, "
            f"got {push_kwargs['repo_path']}"
        )

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_phase_gate_commit_swallows_timeout(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """Recovery's commit handler must catch broad exceptions (#2219, follow-up to #2357).

        ``_commit_statefiles_to_worktree`` calls ``subprocess.run(...,
        timeout=30)`` which can raise ``TimeoutExpired`` or ``OSError``.
        Pre-fix the recovery branch's narrow
        ``except subprocess.CalledProcessError`` was unreachable because
        ``repo_path`` short-circuited the helper at its ``state_dir.exists()``
        guard. Now that the helper actually executes git against the
        worktree, the narrow handler would let those exceptions escape
        as a 500. Mirror the inline path's broadened handler.
        """
        import subprocess

        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "approve"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/repo")

        with (
            patch(
                "routes.pipelines._resolve_pipeline_worktree_path",
                return_value=worktree_path,
            ),
            patch("routes.pipelines._persist_phase_gate_resolution"),
            patch(
                "routes.pipelines._commit_statefiles_to_worktree",
                side_effect=subprocess.TimeoutExpired(cmd="git commit", timeout=30),
            ),
            patch("routes.pipelines._get_spawner", return_value=MagicMock()),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200, (
            "TimeoutExpired from _commit_statefiles_to_worktree must be "
            "caught and logged, not propagate as a 500"
        )

    @patch("routes.pipelines.get_pipeline_state_lock", side_effect=_noop_lock)
    @patch("routes.pipelines._run_pipeline")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_recovery_skips_push_when_worktree_resolves_to_main_repo(
        self, mock_get_repo, mock_resolve, mock_run, mock_lock, client
    ):
        """When no worktree is materialised, recovery must skip the push.

        ``_resolve_pipeline_worktree_path`` falls back to the supplied
        path when no worktree directory exists (e.g. cleaned up between
        AWAITING_HUMAN entry and recovery, or after an orchestrator
        restart). Pushing the orchestrator's main repo would target the
        wrong working tree — mirror the inline path's
        ``worktree_repo_path != repo_path`` guard at pipelines.py:16044.
        """
        pipeline = _make_awaiting_pipeline(
            phase=PipelinePhase.REFINE,
            resolution='{"action": "approve"}',
        )
        _setup_mocks(mock_get_repo, mock_resolve, pipeline)

        repo_path = Path("/repo")  # matches _setup_mocks
        mock_spawner = MagicMock()

        with (
            patch(
                "routes.pipelines._resolve_pipeline_worktree_path",
                return_value=repo_path,  # fallback case: no worktree found
            ),
            patch("routes.pipelines._persist_phase_gate_resolution"),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines._get_spawner", return_value=mock_spawner),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/start")

        assert resp.status_code == 200
        mock_spawner.gateway.push_worktree_branch.assert_not_called()


# -----------------------------------------------------------------------------
# Issue #1556 — Jira ticket plumbing
# -----------------------------------------------------------------------------


class TestPipelineJiraTicketField:
    """``Pipeline.jira_ticket`` round-trips cleanly and validates."""

    def test_default_is_none(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
        )
        assert pipeline.jira_ticket is None

    def test_accepts_valid_ticket(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="ENG-123",
        )
        assert pipeline.jira_ticket == "ENG-123"

    def test_strips_surrounding_whitespace(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="  ENG-123  ",
        )
        assert pipeline.jira_ticket == "ENG-123"

    def test_empty_string_normalised_to_none(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="   ",
        )
        assert pipeline.jira_ticket is None

    @pytest.mark.parametrize(
        "bad",
        [
            "foo-1",  # lowercase
            "FOO",  # missing -<digits>
            "FOO-",  # missing digits
            "FOO-abc",  # non-digit trailing
            "foo bar",
            "ENG_123",  # missing hyphen
        ],
    )
    def test_rejects_malformed_ticket(self, bad: str):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Pipeline(
                id="issue-1",
                issue_number=1,
                repo="owner/repo",
                branch="egg/test",
                mode="issue",
                status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.REFINE,
                jira_ticket=bad,
            )

    def test_round_trip_via_model_dump(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="ENG-123",
        )
        dumped = pipeline.model_dump()
        assert dumped["jira_ticket"] == "ENG-123"
        restored = Pipeline.model_validate(dumped)
        assert restored.jira_ticket == "ENG-123"

    def test_round_trip_with_none(self):
        """Legacy pipelines without the field deserialize cleanly."""
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
        )
        dumped = pipeline.model_dump()
        assert dumped["jira_ticket"] is None
        restored = Pipeline.model_validate(dumped)
        assert restored.jira_ticket is None

    def test_legacy_dict_without_jira_ticket_deserializes(self):
        """A dict saved BEFORE issue #1556 must still load — jira_ticket
        defaults to None."""
        legacy = {
            "id": "issue-1",
            "issue_number": 1,
            "repo": "owner/repo",
            "branch": "egg/test",
            "mode": "issue",
            "status": PipelineStatus.RUNNING.value,
            "current_phase": PipelinePhase.REFINE.value,
            "phases": {},
        }
        restored = Pipeline.model_validate(legacy)
        assert restored.jira_ticket is None


class TestSandboxJiraEnvBuilder:
    """Mirror the inline env-builder snippet in ``orchestrator/routes/pipelines.py``.

    The snippet is:

        jira_ticket_value = (getattr(pipeline, "jira_ticket", None) or "")
        sandbox_env["EGG_JIRA_TICKET"] = jira_ticket_value
        if jira_ticket_value and "-" in jira_ticket_value:
            sandbox_env["EGG_JIRA_PROJECT"] = jira_ticket_value.split("-", 1)[0]
        else:
            sandbox_env["EGG_JIRA_PROJECT"] = ""

    We reproduce it here so a regression that drops or mangles the env
    export fails the test.  This is a focused unit check; the full spawn
    path is covered in orchestrator/tests/test_run_pipeline*.py.
    """

    @staticmethod
    def _build_env(pipeline) -> dict[str, str]:
        sandbox_env: dict[str, str] = {}
        jira_ticket_value = getattr(pipeline, "jira_ticket", None) or ""
        sandbox_env["EGG_JIRA_TICKET"] = jira_ticket_value
        if jira_ticket_value and "-" in jira_ticket_value:
            sandbox_env["EGG_JIRA_PROJECT"] = jira_ticket_value.split("-", 1)[0]
        else:
            sandbox_env["EGG_JIRA_PROJECT"] = ""
        return sandbox_env

    def test_populated_ticket_exports_both(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="ENG-123",
        )
        env = self._build_env(pipeline)
        assert env["EGG_JIRA_TICKET"] == "ENG-123"
        assert env["EGG_JIRA_PROJECT"] == "ENG"

    def test_absent_ticket_exports_empty_strings(self):
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
        )
        env = self._build_env(pipeline)
        assert env["EGG_JIRA_TICKET"] == ""
        assert env["EGG_JIRA_PROJECT"] == ""

    def test_zero_credentials_invariant(self):
        """Risk R7: the env builder must NOT export any of the Atlassian
        credential keys to the sandbox.  This snapshot-tests the current
        snippet; if someone extends the builder to plumb secrets through,
        the test fails."""
        pipeline = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.REFINE,
            jira_ticket="ENG-123",
        )
        env = self._build_env(pipeline)
        for forbidden in ("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"):
            assert forbidden not in env, (
                f"sandbox env must never carry {forbidden} — credentials must "
                "remain on the gateway (issue #1556 risk R7)."
            )


class TestSandboxJiraEnvBuilderSourceSnippet:
    """Guard against drift: the snippet this file reproduces MUST match
    what ``orchestrator/routes/pipelines.py`` actually does.

    If the live source changes shape (new keys, different sentinel), the
    copy above in ``TestSandboxJiraEnvBuilder`` is silently stale.  This
    test reads the source file and asserts the key markers are present so
    a reviewer catches drift early.
    """

    def test_source_exports_egg_jira_ticket_and_project(self):
        src = (Path(__file__).parent.parent / "routes" / "pipelines.py").read_text()
        assert 'sandbox_env["EGG_JIRA_TICKET"]' in src
        assert 'sandbox_env["EGG_JIRA_PROJECT"]' in src

    def test_source_never_exports_jira_secrets(self):
        """A regression check: the spawn-env assembly must never put
        ``JIRA_BASE_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN`` into
        ``sandbox_env`` (risk R7)."""
        src = (Path(__file__).parent.parent / "routes" / "pipelines.py").read_text()
        for forbidden in ("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"):
            # Scan for any write to sandbox_env[<forbidden>].
            assert f'sandbox_env["{forbidden}"]' not in src, (
                f"orchestrator/routes/pipelines.py writes {forbidden} into "
                "sandbox_env; this violates the zero-credential invariant "
                "(issue #1556 risk R7)."
            )
