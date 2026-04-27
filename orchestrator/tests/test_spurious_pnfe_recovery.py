"""Tests for spurious PipelineNotFoundError recovery in _run_pipeline (#2155).

When `_run_pipeline` hits a transient `PipelineNotFoundError` mid-execution
(e.g., empty content during a concurrent commit on the state worktree),
the outer `except PipelineNotFoundError` block must:

1. Re-verify the pipeline really is gone before treating the exception as
   deletion — a transient is recovered if the pipeline still exists on
   retry.
2. On spurious failure: bump ``run_epoch`` so the finally cleanup detects
   the thread as superseded and skips the destructive worktree teardown.
3. Relaunch a fresh ``_run_pipeline`` thread so the next phase keeps
   making progress without operator intervention.
4. On a genuine deletion: log with ``exc_info`` so the raise site is
   recoverable from logs.
"""

import sys
from datetime import UTC, datetime
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
from state_store import PipelineNotFoundError


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
    def test_spurious_pnfe_relaunches_thread_and_bumps_epoch(
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
        # First load (deep in _run_pipeline) raises PNFE.
        # The verify retry inside the except handler succeeds.
        # The bump-then-save load also succeeds.
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

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        _run_pipeline("issue-2155", Path("/repo"))

        # A respawn thread was launched.
        respawn_calls = [
            c for c in mock_thread_cls.call_args_list if "respawn" in (c.kwargs.get("name") or "")
        ]
        assert len(respawn_calls) == 1, (
            f"Expected exactly one respawn thread; got threads with names: "
            f"{[c.kwargs.get('name') for c in mock_thread_cls.call_args_list]}"
        )
        respawn = respawn_calls[0]
        assert respawn.kwargs["daemon"] is True
        assert respawn.kwargs["args"] == ("issue-2155", Path("/repo"))
        mock_thread_instance.start.assert_called()

        # save_pipeline was called to persist the bumped run_epoch.
        assert mock_store.save_pipeline.called, "Expected run_epoch to be persisted"
        bumped_pipeline = mock_store.save_pipeline.call_args.args[0]
        assert bumped_pipeline.run_epoch is not None
        # run_epoch should be a recent UTC timestamp.
        delta = datetime.now(UTC) - bumped_pipeline.run_epoch
        assert delta.total_seconds() < 5, f"Expected run_epoch within 5s of now, got delta={delta}"

    @patch(_PATCHES[6])
    @patch(_PATCHES[5])
    @patch(_PATCHES[4])
    @patch(_PATCHES[3])
    @patch(_PATCHES[2])
    @patch(_PATCHES[1])
    @patch(_PATCHES[0])
    def test_real_deletion_does_not_relaunch_thread(
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
        # All loads (initial + 3 retries) raise PNFE → genuine deletion.
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("deleted")
        mock_get_store.return_value = mock_store

        mock_get_spawner.return_value = MagicMock()
        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        _run_pipeline("issue-2155", Path("/repo"))

        # No respawn thread should be launched.
        respawn_calls = [
            c for c in mock_thread_cls.call_args_list if "respawn" in (c.kwargs.get("name") or "")
        ]
        assert len(respawn_calls) == 0, (
            f"Expected no respawn thread for genuine deletion; got: "
            f"{[c.kwargs.get('name') for c in mock_thread_cls.call_args_list]}"
        )
