"""Tests for advance_phase launching a _run_pipeline thread (#1672, #2165).

Both manual ``advance_phase`` and the auto-advance block inside
``_run_pipeline`` must spawn a fresh ``_run_pipeline`` driver thread for
the next phase.  Otherwise the pipeline sits in RUNNING state with nothing
processing the new phase (#1672), and any exception in the new phase's
first iteration takes down the whole pipeline along with the dying thread
(#2165).
"""

import inspect
import re
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

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_launches_thread(self, mock_get_store, mock_get_lock, mock_spawn, client):
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

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        mock_spawn.assert_called_once()
        # Helper is invoked with (pipeline_id, repo_path, run_epoch)
        args, kwargs = mock_spawn.call_args
        assert args[0] == "issue-300"
        assert args[1] == Path("/tmp/repo")
        assert args[2] == pipeline.run_epoch

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_force_launches_thread(
        self, mock_get_store, mock_get_lock, mock_spawn, client
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

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "pr", "force": True},
        )

        assert response.status_code == 200
        mock_spawn.assert_called_once()

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_bumps_run_epoch(self, mock_get_store, mock_get_lock, mock_spawn, client):
        """advance_phase must bump run_epoch so stale threads exit."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        original_epoch = pipeline.run_epoch
        original_created_at = pipeline.created_at

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()

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

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_acquires_state_lock(
        self, mock_get_store, mock_get_lock, mock_spawn, client
    ):
        """advance_phase must acquire the pipeline state lock for atomicity."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)

        mock_lock = MagicMock()
        mock_get_lock.return_value = mock_lock

        response = client.post(
            "/api/v1/pipelines/issue-300/phase",
            json={"target_phase": "implement"},
        )

        assert response.status_code == 200
        mock_get_lock.assert_called_once_with("issue-300")
        mock_lock.__enter__.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for the _spawn_pipeline_run_thread helper itself
# ---------------------------------------------------------------------------


class TestSpawnPipelineRunThread:
    """Verify the shared helper used by both advance paths."""

    def test_spawns_daemon_thread_with_epoch_in_name(self):
        from routes.pipelines import _spawn_pipeline_run_thread

        run_epoch = datetime.now(UTC)
        with patch("routes.pipelines.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

            result = _spawn_pipeline_run_thread("issue-42", Path("/repo"), run_epoch)

        mock_thread_cls.assert_called_once()
        kwargs = mock_thread_cls.call_args.kwargs
        assert kwargs["daemon"] is True
        assert kwargs["name"] == f"pipeline-issue-42-{int(run_epoch.timestamp())}"
        assert kwargs["args"] == ("issue-42", Path("/repo"))
        mock_thread.start.assert_called_once()
        assert result is mock_thread


# ---------------------------------------------------------------------------
# Tests for #2165: auto-advance must also respawn a fresh thread
# ---------------------------------------------------------------------------


class TestAutoAdvanceRespawnsThread:
    """Verify _run_pipeline's auto-advance block matches advance_phase's pattern.

    The auto-advance block must (1) bump run_epoch, (2) call
    ``_spawn_pipeline_run_thread`` for the next phase, and (3) ``return``
    so the dying thread's finally block detects the epoch mismatch and
    skips worktree teardown.

    These are structural assertions on _run_pipeline's source: full
    behavioral coverage would require a heavy mock harness for the entire
    phase loop.  Pairs with TestSpawnPipelineRunThread (which covers the
    helper itself) and the pre-existing advance_phase tests (which cover
    the manual path end-to-end via Flask).
    """

    # Explicit token that brackets the auto-advance block.  Tests grep
    # for this rather than free-text comment prose so refactoring the
    # surrounding wording does not silently lose coverage.  If the block
    # is moved or removed, update both the source marker and this token.
    _BLOCK_MARKER = "TEST_MARKER: auto_advance_block"

    def _auto_advance_block(self) -> str:
        """Extract the auto-advance block from _run_pipeline's source."""
        from routes import pipelines

        source = inspect.getsource(pipelines._run_pipeline)
        assert self._BLOCK_MARKER in source, (
            f"Could not find {self._BLOCK_MARKER!r} in _run_pipeline source. "
            "Tests rely on this token bracketing the auto-advance block; "
            "if the block was moved or removed, update both source and tests."
        )
        idx = source.index(self._BLOCK_MARKER)
        # Take a generous window so the block including the return is included.
        return source[idx : idx + 2000]

    def test_auto_advance_bumps_run_epoch(self):
        block = self._auto_advance_block()
        assert "pipeline.run_epoch = datetime.now(UTC)" in block, (
            "Auto-advance must bump run_epoch so the dying thread's finally "
            "block detects itself as superseded and skips worktree cleanup."
        )

    def test_auto_advance_calls_spawn_helper(self):
        block = self._auto_advance_block()
        assert "_spawn_pipeline_run_thread(" in block, (
            "Auto-advance must spawn a fresh _run_pipeline driver thread via the shared helper."
        )

    def test_auto_advance_returns_after_spawn(self):
        block = self._auto_advance_block()
        # Indentation-tolerant: spawn call followed by a bare ``return`` on
        # the next non-blank line at any matching indent level.
        assert re.search(
            r"_spawn_pipeline_run_thread\([^)]*\)\s*\n\s*return\b",
            block,
        ), (
            "Auto-advance must return after spawning so the current "
            "thread's finally block runs (and skips worktree cleanup via "
            "epoch mismatch)."
        )

    def test_auto_advance_does_not_call_set_current_phase(self):
        """The dying thread no longer needs to update its health monitor."""
        block = self._auto_advance_block()
        assert "set_current_phase" not in block, (
            "The auto-advance block must not update the dying thread's "
            "health monitor — the new thread builds its own."
        )
