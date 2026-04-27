"""Tests for spurious PipelineNotFoundError recovery in _run_pipeline (#2155).

When `_run_pipeline` hits a transient `PipelineNotFoundError` mid-execution
(e.g., empty content during a concurrent commit on the state worktree),
the outer `except PipelineNotFoundError` block must:

1. Re-verify the pipeline really is gone before treating the exception as
   deletion — a transient is recovered if the pipeline still exists on
   retry, and ``StateValidationError`` (corrupt-but-present) is treated
   as transient too.
2. On spurious failure: bump ``run_epoch`` so the finally cleanup detects
   the thread as superseded and skips the destructive worktree teardown.
3. Relaunch a fresh ``_run_pipeline`` thread so the next phase keeps
   making progress without operator intervention — but only if the
   ``run_epoch`` bump succeeded, so the old thread doesn't race the
   new one into worktree teardown.
4. Cap the respawn cascade so a persistent transient can't leak threads,
   overseer containers, and state-branch commits unboundedly — after
   ``_PNFE_RESPAWN_MAX_ATTEMPTS`` consecutive respawns, mark the
   pipeline FAILED so an operator can investigate.
5. On a genuine deletion: log with ``exc_info`` and skip the respawn so
   worktree cleanup runs normally.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from models import Pipeline, PipelinePhase, PipelineStatus
from state_store import PipelineNotFoundError, StateValidationError


def _make_pipeline():
    return Pipeline(
        id="issue-2155",
        issue_number=2155,
        repo="owner/repo",
        branch="egg/issue-2155",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
        network_mode="public",
    )


_PATCHES = [
    "routes.pipelines._emit_pipeline_event",
    "routes.pipelines.get_container_spawner",
    "routes.pipelines.get_state_store",
    "routes.pipelines.get_pipeline_state_lock",
    "routes.pipelines.report_pipeline_status",
    "routes.pipelines.threading.Thread",
    "routes.pipelines.time.sleep",
]


def _respawn_calls(mock_thread_cls):
    return [
        c
        for c in mock_thread_cls.call_args_list
        if "respawn" in (c.kwargs.get("name") or "")
    ]


class TestSpuriousPNFERecovery:
    """When PipelineNotFoundError fires but the pipeline still exists,
    recover by bumping run_epoch and relaunching the driver thread."""

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_spurious_pnfe_relaunches_thread_and_skips_worktree_cleanup(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_state_lock,
        mock_report,
        mock_thread_cls,
        mock_sleep,
    ):
        from routes.pipelines import _run_pipeline

        pipeline = _make_pipeline()
        original_epoch = pipeline.run_epoch or pipeline.created_at

        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        # Sequence: initial load (line ~11392) → PNFE; verify retry
        # succeeds; bump-load returns pipeline; finally cleanup load
        # returns pipeline (now with bumped epoch in-place).
        mock_store.load_pipeline.side_effect = [
            PipelineNotFoundError("transient"),
            pipeline,
            pipeline,
            pipeline,
        ]
        mock_get_store.return_value = mock_store

        mock_spawner = MagicMock()
        mock_get_spawner.return_value = mock_spawner
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        _run_pipeline("issue-2155", Path("/repo"))

        # A respawn thread was launched with attempt counter incremented.
        respawn = _respawn_calls(mock_thread_cls)
        assert len(respawn) == 1, (
            f"Expected exactly one respawn thread; got threads with names: "
            f"{[c.kwargs.get('name') for c in mock_thread_cls.call_args_list]}"
        )
        assert respawn[0].kwargs["daemon"] is True
        assert respawn[0].kwargs["args"] == ("issue-2155", Path("/repo"))
        assert respawn[0].kwargs["kwargs"] == {"_respawn_attempt": 1}
        mock_thread_instance.start.assert_called()

        # save_pipeline was called to persist the bumped run_epoch.
        assert mock_store.save_pipeline.called, "Expected run_epoch to be persisted"
        bumped_pipeline = mock_store.save_pipeline.call_args.args[0]
        assert bumped_pipeline.run_epoch is not None
        assert bumped_pipeline.run_epoch != original_epoch

        # Central correctness claim: worktrees are NOT torn down on the
        # spurious branch.  The finally guard sees ``run_epoch`` (the
        # captured pre-bump epoch) differ from the on-disk bumped epoch
        # and skips cleanup.
        mock_spawner.gateway.delete_worktrees.assert_not_called()

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_state_validation_error_during_verify_treated_as_transient(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_state_lock,
        mock_report,
        mock_thread_cls,
        mock_sleep,
    ):
        from routes.pipelines import _run_pipeline

        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        # Initial load PNFE; verify retry hits StateValidationError
        # (corrupt-but-present); bump-load + finally-load succeed.
        mock_store.load_pipeline.side_effect = [
            PipelineNotFoundError("transient"),
            StateValidationError("invalid JSON"),
            pipeline,
            pipeline,
        ]
        mock_get_store.return_value = mock_store

        mock_spawner = MagicMock()
        mock_get_spawner.return_value = mock_spawner
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_cls.return_value = MagicMock()

        _run_pipeline("issue-2155", Path("/repo"))

        # StateValidationError is corrupt-but-present, not deletion: the
        # recovery path must respawn and preserve worktrees.
        assert len(_respawn_calls(mock_thread_cls)) == 1
        mock_spawner.gateway.delete_worktrees.assert_not_called()

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_bump_failure_does_not_relaunch_thread(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_state_lock,
        mock_report,
        mock_thread_cls,
        mock_sleep,
    ):
        """If the run_epoch bump fails, we must NOT respawn — otherwise
        the old thread races the new one into worktree teardown."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        # Initial PNFE; verify succeeds; bump-load succeeds but
        # save_pipeline (in the bump) fails.
        mock_store.load_pipeline.side_effect = [
            PipelineNotFoundError("transient"),
            pipeline,
            pipeline,
            pipeline,
        ]
        mock_store.save_pipeline.side_effect = RuntimeError("git index lock")
        mock_get_store.return_value = mock_store

        mock_get_spawner.return_value = MagicMock()
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_cls.return_value = MagicMock()

        _run_pipeline("issue-2155", Path("/repo"))

        # No respawn when bump fails — the old thread must not race the
        # new one into the destructive cleanup path.
        assert len(_respawn_calls(mock_thread_cls)) == 0

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_respawn_cascade_is_bounded_and_marks_failed(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_state_lock,
        mock_report,
        mock_thread_cls,
        mock_sleep,
    ):
        """A persistent transient must not cascade into an unbounded
        thread/overseer/commit storm.  After the cap, the pipeline is
        marked FAILED and no further respawn is launched."""
        from routes.pipelines import _PNFE_RESPAWN_MAX_ATTEMPTS, _run_pipeline

        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        # Initial PNFE; verify succeeds; FAILED-mark load; finally load.
        mock_store.load_pipeline.side_effect = [
            PipelineNotFoundError("transient"),
            pipeline,
            pipeline,
            pipeline,
        ]
        mock_get_store.return_value = mock_store

        mock_get_spawner.return_value = MagicMock()
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_cls.return_value = MagicMock()

        _run_pipeline(
            "issue-2155",
            Path("/repo"),
            _respawn_attempt=_PNFE_RESPAWN_MAX_ATTEMPTS,
        )

        # No respawn at the cap.
        assert len(_respawn_calls(mock_thread_cls)) == 0

        # Pipeline was marked FAILED with a clear error.
        save_calls = [
            c for c in mock_store.save_pipeline.call_args_list
            if c.args and c.args[0].status == PipelineStatus.FAILED
        ]
        assert save_calls, "Expected pipeline to be saved with FAILED status"
        failed_pipeline = save_calls[0].args[0]
        assert "exhausted" in (failed_pipeline.error or "").lower()

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_real_deletion_does_not_relaunch_and_runs_cleanup(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_state_lock,
        mock_report,
        mock_thread_cls,
        mock_sleep,
    ):
        from routes.pipelines import _run_pipeline

        mock_store = MagicMock()
        mock_store.repo_path = Path("/repo")
        # All loads (initial + verify retries + finally) raise PNFE → genuine deletion.
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("deleted")
        mock_get_store.return_value = mock_store

        mock_spawner = MagicMock()
        mock_get_spawner.return_value = mock_spawner
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_cls.return_value = MagicMock()

        _run_pipeline("issue-2155", Path("/repo"))

        # No respawn thread launched on genuine deletion.
        assert len(_respawn_calls(mock_thread_cls)) == 0

        # Genuine deletion → finally cleanup runs delete_worktrees so
        # the pipeline's worktrees aren't leaked.  (The cleanup also
        # loops over per-agent containers as a safety-net, so we assert
        # the pipeline-level call specifically rather than call count.)
        delete_calls = mock_spawner.gateway.delete_worktrees.call_args_list
        pipeline_level = [
            c for c in delete_calls if c.kwargs.get("container_id") == "issue-2155"
        ]
        assert len(pipeline_level) == 1, (
            f"Expected one pipeline-level delete_worktrees call; got: "
            f"{[c.kwargs.get('container_id') for c in delete_calls]}"
        )
