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
        return source[idx : idx + 3000]

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

    def test_auto_advance_clears_concurrent_state(self):
        """Regression for #2502.

        The auto-advance block must drop the previous phase's in-memory
        consensus tracker (and message-store entries) before spawning the
        next phase's driver thread.  Otherwise ``_get_concurrent_status``
        keeps finding the prior phase's tracker keyed under the bare
        ``pipeline_id`` and reports its ``is_complete: True`` indefinitely
        — which is exactly what masked an in-progress implement-phase
        BRC stall in pipeline ``issue-2474`` (see issue #2502).

        Source-inspection in the same style as the sibling assertions: a
        behavioural test would need to drive the full phase loop through
        a mock harness.  Pairs with the explicit ``_clear_concurrent_state``
        calls already present in the ``advance_phase`` REST handler, the
        HITL-revision re-run path, and the ``recover_pipeline`` resume
        path.
        """
        block = self._auto_advance_block()
        assert "_clear_concurrent_state(pipeline_id)" in block, (
            "Auto-advance must call _clear_concurrent_state(pipeline_id) so "
            "the next phase's get_pipeline_snapshot / get_consensus_status "
            "calls don't surface the previous phase's tracker (#2502)."
        )


# ---------------------------------------------------------------------------
# Tests for #2502 (recover_pipeline advance branch): the AWAITING_HUMAN
# resume path must also drop the previous phase's tracker when the HITL
# decision approves a phase advance (cross-phase), mirroring the auto-
# advance block in ``_run_pipeline``.  The same-phase re-run branch
# (request_changes / change_approach) already clears under the lock for
# #1296.
# ---------------------------------------------------------------------------


class TestRecoverPipelineClearsConcurrentState:
    """Verify ``start_pipeline``'s AWAITING_HUMAN advance branch wipes the
    previous phase's consensus tracker before launching the runner thread.

    Source-inspection in the same style as
    ``TestAutoAdvanceRespawnsThread`` — a behavioural test would need a
    full Flask + state-store harness exercising the HITL resolution
    flow.  Pairs with the explicit ``_clear_concurrent_state`` calls in
    ``advance_phase``, the HITL-revision re-run branch, and the
    auto-advance block.
    """

    _BLOCK_MARKER = "TEST_MARKER: recover_advance_clear"

    def _recover_advance_block(self) -> str:
        """Extract the recover-advance clear block from start_pipeline."""
        from routes import pipelines

        source = inspect.getsource(pipelines.start_pipeline)
        assert self._BLOCK_MARKER in source, (
            f"Could not find {self._BLOCK_MARKER!r} in start_pipeline source. "
            "Tests rely on this token bracketing the recover_pipeline advance "
            "branch's clear call; if the block was moved or removed, update "
            "both source and tests."
        )
        idx = source.index(self._BLOCK_MARKER)
        # Window large enough to cover the comment + the conditional clear.
        return source[idx : idx + 1200]

    def test_recover_advance_calls_clear_concurrent_state(self):
        """Regression for #2502 (advance branch).

        Without this, an HITL-approved advance from plan→implement would
        leave the plan-phase tracker keyed under the bare ``pipeline_id``
        for ``_get_concurrent_status`` to find and report ``is_complete:
        True`` indefinitely — the same shape as the auto-advance bug.
        """
        block = self._recover_advance_block()
        assert "_clear_concurrent_state(pipeline_id)" in block, (
            "recover_pipeline's advance branch must call "
            "_clear_concurrent_state(pipeline_id) so the next phase's "
            "snapshot doesn't surface the previous phase's tracker (#2502)."
        )

    def test_recover_advance_clear_is_conditional_on_is_approved(self):
        """The clear must only fire on the advance branch, not the
        request_changes/change_approach branch (which already clears
        inside the lock for #1296)."""
        block = self._recover_advance_block()
        assert re.search(
            r"if is_approved:\s*\n(?:\s*(?:from|#).*\n)*\s*_clear_concurrent_state\(pipeline_id\)",
            block,
        ), (
            "The recover_pipeline clear must be guarded by `if is_approved:` "
            "so the request_changes branch's existing in-lock clear (#1296) "
            "is not duplicated."
        )


class TestPostBrcBandSwallowsErrors:
    """Verify the band between BRC return and auto-advance never propagates
    a sub-call exception out of ``_run_pipeline``.

    Source-inspection assertions in the same style as
    ``TestAutoAdvanceRespawnsThread`` — full behavioural coverage would
    need a phase-loop harness, but these guarantee the structural
    invariants that close the wedge in #2219.
    """

    def _run_pipeline_source(self) -> str:
        from routes import pipelines

        return inspect.getsource(pipelines._run_pipeline)

    def test_sync_worktree_with_remote_is_wrapped(self):
        """``_sync_worktree_with_remote`` was unwrapped — a gateway HTTP
        error or git failure inside it propagated to the outer Exception
        handler and (when FAILED-marking also failed) stranded the pipeline.
        """
        source = self._run_pipeline_source()
        # The post-phase call site (after BRC return) must sit inside a
        # ``try`` whose ``except`` matches ``Exception`` so any failure
        # mode is swallowed with a warning rather than killing the thread.
        # Indentation-tolerant: ``\s+`` between ``try:`` and the call.
        assert re.search(
            r"try:\s*\n\s*_sync_worktree_with_remote\(",
            source,
        ), (
            "_sync_worktree_with_remote(...) call after BRC return must be "
            "wrapped in try/except so a sub-call failure can't strand the "
            "pipeline (#2219)."
        )

    def test_commit_statefiles_handler_catches_broadly(self):
        """``_commit_statefiles_to_worktree`` raises ``TimeoutExpired`` and
        ``OSError`` paths that a ``CalledProcessError``-only ``except``
        does not catch.  The fix broadens the handler to ``Exception``.
        """
        source = self._run_pipeline_source()
        # Both call sites (post-phase and post-HITL-resolution) must use
        # the broader handler.  Find every call to the helper and assert
        # the immediately-following ``except`` clause is ``Exception``.
        call_sites = list(re.finditer(r"_commit_statefiles_to_worktree\(", source))
        # Five known call sites in ``_run_pipeline``: initial statefile
        # commit, pre-PR commit, pre-sync commit (#2488), post-phase
        # commit, post-HITL-resolution commit.  Pin the count so a future
        # move/delete is caught rather than silently degrading coverage.
        assert len(call_sites) == 5, (
            f"Expected 5 _commit_statefiles_to_worktree call sites in "
            f"_run_pipeline, found {len(call_sites)}.  If a call was "
            f"intentionally added/removed, update this count and the "
            f"comment above."
        )
        for match in call_sites:
            # Look at the next ~500 chars for the matching except clause.
            window = source[match.end() : match.end() + 500]
            except_match = re.search(r"except\s+([^\s:]+)", window)
            assert except_match is not None, (
                f"_commit_statefiles_to_worktree call at offset "
                f"{match.start()} has no matching except clause"
            )
            caught = except_match.group(1)
            assert caught == "Exception", (
                f"_commit_statefiles_to_worktree call at offset "
                f"{match.start()} catches {caught!r}, but must catch "
                f"Exception so TimeoutExpired/OSError can't strand the "
                f"pipeline (#2219)."
            )

    def test_failed_marking_handler_logs_on_failure(self):
        """If the FAILED-marking step itself raises (state-store
        contention, lock timeout), the original ``except Exception: pass``
        silently dropped both errors — the thread died and the pipeline
        stayed ``running`` with no error recorded.  The handler must log
        instead.
        """
        source = self._run_pipeline_source()
        # The outer Exception handler's inner try/except must NOT end in
        # a bare ``pass`` — it must log so future occurrences are visible.
        # Match the structural shape: ``except Exception as fail_err:``
        # immediately followed (within ~500 chars, indentation-tolerant)
        # by the new log line.  This pins the assertion to the invariant
        # rather than just any occurrence of the string.
        assert re.search(
            r"except\s+Exception\s+as\s+fail_err:[\s\S]{0,500}?"
            r"Failed to mark pipeline FAILED after exception",
            source,
        ), (
            "Outer Exception handler must log inside an "
            "``except Exception as fail_err:`` block when FAILED-marking "
            "itself fails so silent wedges (#2219) become visible in the "
            "log."
        )
