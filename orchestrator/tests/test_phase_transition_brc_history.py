"""Regressions for #1827: BRC history must be persisted by phase-transition
REST/MCP handlers before ``_clear_concurrent_state`` wipes the message store.

The in-thread advance (``_run_pipeline``) writes BRC history inline before
moving to the next phase, but external phase transitions
(``complete_phase`` / ``advance_phase``) historically skipped that write
and then cleared the message store — which silently dropped the consensus
transcript for the outgoing phase.  This was observed in the #1813 unstick
flow (``complete_phase`` + ``advance_phase --force``) against pipeline
#1759, where the plan-phase BRC transcript never landed on the branch.
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

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: E402

try:
    from flask import Flask
    from routes.phases import phases_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


def _make_pipeline(
    pipeline_id="issue-300",
    phase=PipelinePhase.PLAN,
    phase_status=PipelineStatus.COMPLETE,
):
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=300,
        repo="owner/repo",
        branch="egg/issue-300",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = phase_status
    phase_exec.completed_at = datetime.now(UTC)
    return pipeline


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


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestCompletePhasePersistsBrcHistory:
    """``complete_phase`` must save BRC history before clearing state (#1827)."""

    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_persist_runs_before_clear(self, mock_get_store, mock_clear, mock_persist, client):
        # Note: _collect_unresolved_phase_decisions is not mocked here — it
        # works because Pipeline defaults has_contract=False and decisions=[],
        # so the collection short-circuits.  If those defaults change, this
        # test will need an explicit mock for that helper.
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN, phase_status=PipelineStatus.RUNNING)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_get_store.return_value = (mock_store, pipeline)

        parent = MagicMock()
        parent.attach_mock(mock_persist, "persist")
        parent.attach_mock(mock_clear, "clear")

        response = client.post("/api/v1/pipelines/issue-300/phase/complete")

        assert response.status_code == 200
        mock_persist.assert_called_once_with(pipeline, mock_store, "plan")
        mock_clear.assert_called_once_with("issue-300")

        # Persist must run before the clear — otherwise the message store
        # is empty by the time history is written.
        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("persist") < call_names.index("clear")


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestAdvancePhasePersistsBrcHistory:
    """``advance_phase`` must save BRC history for the outgoing phase before
    clearing state (#1827).  Applies to both normal advances and
    ``force=true`` (the #1813 unstick path)."""

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_normal_advance_persists_outgoing_phase(
        self,
        mock_get_store,
        mock_clear,
        mock_persist,
        mock_get_lock,
        mock_thread_cls,
        client,
    ):
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        parent = MagicMock()
        parent.attach_mock(mock_persist, "persist")
        parent.attach_mock(mock_clear, "clear")

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        # Persist must be called with the OUTGOING phase ("plan"), not the
        # new current phase — otherwise we save history for a phase that
        # has no consensus transcript yet.
        mock_persist.assert_called_once_with(pipeline, mock_store, "plan")
        mock_clear.assert_called_once_with("issue-300")

        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("persist") < call_names.index("clear")

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.pipelines._persist_phase_brc_history")
    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_advance_persists_outgoing_phase(
        self,
        mock_get_store,
        mock_clear,
        mock_persist,
        mock_get_lock,
        mock_thread_cls,
        client,
    ):
        """The #1813 unstick path (force=true) must also persist history.

        Without this, the plan phase's BRC transcript is silently dropped
        whenever a stuck pipeline is advanced with --force.
        """
        pipeline = _make_pipeline(
            phase=PipelinePhase.PLAN,
            phase_status=PipelineStatus.RUNNING,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        parent = MagicMock()
        parent.attach_mock(mock_persist, "persist")
        parent.attach_mock(mock_clear, "clear")

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement", "force": True},
        )

        assert response.status_code == 200
        mock_persist.assert_called_once_with(pipeline, mock_store, "plan")
        mock_clear.assert_called_once_with("issue-300")

        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("persist") < call_names.index("clear")


class TestResolvePipelineWorktreePath:
    """Pure-unit tests for the worktree path resolution helper."""

    def test_prefers_worktree_when_it_exists(self, tmp_path):
        from routes.pipelines import WORKTREE_BASE_DIR, _resolve_pipeline_worktree_path

        pipeline = _make_pipeline()
        fallback = tmp_path / "main-repo"
        fallback.mkdir()

        worktree = tmp_path / "wt" / pipeline.id / "repo"
        worktree.mkdir(parents=True)

        with patch.object(sys.modules["routes.pipelines"], "WORKTREE_BASE_DIR", tmp_path / "wt"):
            resolved = _resolve_pipeline_worktree_path(pipeline, fallback)

        assert resolved == worktree
        # Sanity: confirm the constant is still what we patched in.
        assert WORKTREE_BASE_DIR == Path("/home/egg/.egg-worktrees")

    def test_falls_back_when_worktree_missing(self, tmp_path):
        from routes.pipelines import _resolve_pipeline_worktree_path

        pipeline = _make_pipeline()
        fallback = tmp_path / "main-repo"
        fallback.mkdir()

        with patch.object(
            sys.modules["routes.pipelines"],
            "WORKTREE_BASE_DIR",
            tmp_path / "nonexistent",
        ):
            resolved = _resolve_pipeline_worktree_path(pipeline, fallback)

        assert resolved == fallback
