"""Tests for _run_pipeline failure-path behavior.

Verifies that when a phase fails:
1. _emit_pipeline_event is called with "pipeline.failed"
2. push_worktree_branch is called when pipeline.branch is set
3. Worktree cleanup is skipped (skip_cleanup) when pipeline status is FAILED
"""

import os
import sys
from datetime import datetime
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


def _make_running_pipeline(branch="egg/issue-42"):
    """Create a Pipeline in RUNNING state with its current phase also RUNNING."""
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.REFINE,
    )
    pipeline.contract_synced = True  # Skip contract creation
    execution = pipeline.get_phase_execution(PipelinePhase.REFINE)
    execution.status = PipelineStatus.RUNNING
    execution.started_at = datetime.utcnow()
    return pipeline


# All tests in this module share a common set of patches to get past the
# extensive setup in _run_pipeline.  The critical patches are:
#
# - get_state_store: returns a mock store whose load_pipeline yields the
#   test pipeline (mutable — inner code sets .status to FAILED)
# - get_container_spawner: provides mock gateway for push_worktree_branch
# - _spawn_and_wait: returns non-zero to trigger the failure path
# - _build_phase_prompt: avoids filesystem access for prompt building
# - get_pipeline_state_lock: returns a no-op context manager
# - report_pipeline_status: no-op stub
# - _emit_pipeline_event: mock to verify calls
# - _read_phase_draft: avoids filesystem access
# - os.environ: avoids reading real env vars

_COMMON_PATCHES = [
    "routes.pipelines._emit_pipeline_event",
    "routes.pipelines.get_container_spawner",
    "routes.pipelines.get_state_store",
    "routes.pipelines._spawn_and_wait",
    "routes.pipelines.get_pipeline_state_lock",
    "routes.pipelines._build_phase_prompt",
    "routes.pipelines._read_phase_draft",
    "routes.pipelines.report_pipeline_status",
]


def _setup_mocks(
    mock_report,
    mock_read_draft,
    mock_build_prompt,
    mock_state_lock,
    mock_spawn_wait,
    mock_get_store,
    mock_get_spawner,
    mock_emit,
    pipeline,
):
    """Wire up the common mocks for _run_pipeline failure-path tests."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = pipeline
    mock_store.repo_path = Path("/repo")
    mock_get_store.return_value = mock_store

    mock_gateway = MagicMock()
    mock_gateway.create_worktrees.return_value = MagicMock(
        success=True, worktrees={"repo": "/tmp/wt/repo"}, errors=[]
    )
    mock_spawner = MagicMock()
    mock_spawner.gateway = mock_gateway
    mock_get_spawner.return_value = mock_spawner

    # Simulate container failure
    mock_spawn_wait.return_value = (1, "error log")

    # No-op context manager for state lock
    mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
    mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

    mock_build_prompt.return_value = "test prompt"
    mock_read_draft.return_value = None

    return mock_store, mock_gateway


class TestFailurePathEmitsPipelineEvent:
    """Verify _emit_pipeline_event is called on phase failure."""

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_emit_pipeline_failed_on_phase_failure(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """When _spawn_and_wait returns non-zero, _emit_pipeline_event must be
        called with 'pipeline.failed'."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # Verify _emit_pipeline_event was called with "pipeline.failed"
        failed_calls = [
            c
            for c in mock_emit.call_args_list
            if len(c.args) >= 2 and c.args[1] == "pipeline.failed"
        ]
        assert len(failed_calls) >= 1, (
            f"Expected _emit_pipeline_event called with 'pipeline.failed', "
            f"got calls: {mock_emit.call_args_list}"
        )


class TestFailurePathPushesWorktreeBranch:
    """Verify push_worktree_branch is called when pipeline.branch is set and
    the worktree path differs from the repo path."""

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_push_worktree_branch_called_when_worktree_exists(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """When phase fails with a worktree, push_worktree_branch should be called."""
        from routes.pipelines import WORKTREE_BASE_DIR, _run_pipeline

        pipeline = _make_running_pipeline(branch="egg/issue-42")
        mock_store, mock_gateway = _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        # Make create_worktrees return a worktree so worktree_repo_path differs
        # from repo_path (which triggers the push).
        worktree_dir = WORKTREE_BASE_DIR / "issue-42" / "repo"
        mock_gateway.create_worktrees.return_value = MagicMock(
            success=True,
            worktrees={"repo": str(worktree_dir)},
            errors=[],
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        mock_gateway.push_worktree_branch.assert_called_once_with(
            pipeline_id="issue-42",
            repo_path=str(worktree_dir),
            branch="egg/issue-42",
        )

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_push_skipped_when_no_branch(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """When pipeline.branch is not set, push_worktree_branch should not be called."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline(branch=None)
        mock_store, mock_gateway = _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        mock_gateway.push_worktree_branch.assert_not_called()


class TestFailurePathPreservesWorktrees:
    """Verify skip_cleanup is set when pipeline status is FAILED in finally block."""

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_worktree_cleanup_skipped_on_failure(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """When pipeline status is FAILED, delete_worktrees should NOT be called."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        mock_store, mock_gateway = _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # Pipeline should now be in FAILED state (set by the exit_code != 0 handler)
        assert pipeline.status == PipelineStatus.FAILED

        # delete_worktrees should NOT be called since pipeline is FAILED
        mock_gateway.delete_worktrees.assert_not_called()


class TestWorktreeCreationFailure:
    """Verify pipeline fails when worktree creation returns empty worktrees."""

    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_pipeline_fails_on_empty_worktrees(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
    ):
        """When create_worktrees returns empty worktrees, pipeline should fail."""
        from routes.pipelines import _run_pipeline

        pipeline = _make_running_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")
        mock_get_store.return_value = mock_store

        mock_gateway = MagicMock()
        mock_gateway.create_worktrees.return_value = MagicMock(
            success=False, worktrees={}, errors=["gateway unavailable"]
        )
        mock_spawner = MagicMock()
        mock_spawner.gateway = mock_gateway
        mock_get_spawner.return_value = mock_spawner

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_build_prompt.return_value = "test prompt"
        mock_read_draft.return_value = None

        with patch.dict(
            os.environ,
            {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'},
            clear=False,
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # Pipeline should be FAILED because worktree creation is now mandatory
        assert pipeline.status == PipelineStatus.FAILED

        # _spawn_and_wait should NOT have been called — we failed before reaching it
        mock_spawn_wait.assert_not_called()


class TestSuccessPathPushesStatefiles:
    """Verify push_worktree_branch is called after successful phase completion
    to push .egg-state/ files to the remote before the next phase begins."""

    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_push_after_successful_phase(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
        mock_commit_statefiles,
    ):
        """When a phase succeeds, push_worktree_branch should be called to push
        statefiles to the remote so the next phase's agents don't see unpushed
        .egg-state/ files in their diff."""
        from routes.pipelines import WORKTREE_BASE_DIR, _run_pipeline

        # Use PR phase (terminal) so the pipeline completes after one iteration
        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PR,
        )
        pipeline.contract_synced = True
        execution = pipeline.get_phase_execution(PipelinePhase.PR)
        execution.status = PipelineStatus.RUNNING
        execution.started_at = datetime.utcnow()

        mock_store, mock_gateway = _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        # Phase succeeds (exit code 0)
        mock_spawn_wait.return_value = (0, "success")

        worktree_dir = WORKTREE_BASE_DIR / "issue-42" / "repo"
        mock_gateway.create_worktrees.return_value = MagicMock(
            success=True,
            worktrees={"repo": str(worktree_dir)},
            errors=[],
        )

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        # Pipeline should complete successfully (PR is terminal phase)
        assert pipeline.status == PipelineStatus.COMPLETE

        # push_worktree_branch should have been called after phase completion
        mock_gateway.push_worktree_branch.assert_called_with(
            pipeline_id="issue-42",
            repo_path=str(worktree_dir),
            branch="egg/issue-42",
        )

    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch(_COMMON_PATCHES[7])
    @patch(_COMMON_PATCHES[6])
    @patch(_COMMON_PATCHES[5])
    @patch(_COMMON_PATCHES[4])
    @patch(_COMMON_PATCHES[3])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[0])
    def test_push_not_called_without_branch(
        self,
        mock_emit,
        mock_get_spawner,
        mock_get_store,
        mock_spawn_wait,
        mock_state_lock,
        mock_build_prompt,
        mock_read_draft,
        mock_report,
        mock_commit_statefiles,
    ):
        """When pipeline.branch is not set, push_worktree_branch should not be
        called even on success."""
        from routes.pipelines import _run_pipeline

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch=None,
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PR,
        )
        pipeline.contract_synced = True
        execution = pipeline.get_phase_execution(PipelinePhase.PR)
        execution.status = PipelineStatus.RUNNING
        execution.started_at = datetime.utcnow()

        mock_store, mock_gateway = _setup_mocks(
            mock_report,
            mock_read_draft,
            mock_build_prompt,
            mock_state_lock,
            mock_spawn_wait,
            mock_get_store,
            mock_get_spawner,
            mock_emit,
            pipeline,
        )

        mock_spawn_wait.return_value = (0, "success")

        with (
            patch.dict(os.environ, {"EGG_HOST_REPO_MAP": '{"repo": "/host/repo"}'}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _run_pipeline("issue-42", Path("/repo"))

        mock_gateway.push_worktree_branch.assert_not_called()
