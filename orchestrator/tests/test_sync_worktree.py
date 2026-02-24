"""Tests for _sync_worktree_with_remote."""

import subprocess
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

from routes.pipelines import _sync_worktree_with_remote


def _make_spawner(fetch_ok: bool = True, push_ok: bool = True) -> MagicMock:
    """Create a mock spawner with gateway.fetch_worktree_branch."""
    spawner = MagicMock()
    spawner.gateway.fetch_worktree_branch.return_value = fetch_ok
    spawner.gateway.push_worktree_branch.return_value = push_ok
    return spawner


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestSyncWorktreeWithRemote:
    """Tests for _sync_worktree_with_remote."""

    def test_returns_early_when_fetch_fails(self):
        """If gateway fetch fails, function returns without running git commands."""
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_run.assert_not_called()

    def test_returns_early_on_detached_head(self):
        """If branch --show-current returns empty, function returns."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            # Step 2: empty branch (detached HEAD)
            mock_run.return_value = _make_subprocess_result(stdout="")
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Only step 2 should have been called
            assert mock_run.call_count == 1

    def test_returns_early_when_remote_branch_missing(self):
        """If origin/{branch} does not exist, function returns."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse fails (remote branch missing)
                _make_subprocess_result(returncode=128),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 2

    def test_local_ahead_prior_succeeded_pushes_then_resets(self):
        """(a) local ahead + prior phase succeeded → pushes then resets."""
        spawner = _make_spawner(push_ok=True)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner, "pipe-1", Path("/tmp/repo"),
                prior_phase_succeeded=True,
            )
            # Should have pushed to remote
            spawner.gateway.push_worktree_branch.assert_called_once()
            # Should have proceeded to reset (step 4)
            assert mock_run.call_count == 4
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]
            assert "--hard" in reset_call[0][0]

    def test_local_ahead_prior_failed_discards_and_resets(self):
        """(b) local ahead + prior phase failed → resets without pushing."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner, "pipe-1", Path("/tmp/repo"),
                prior_phase_succeeded=False,
            )
            # Should NOT have pushed
            spawner.gateway.push_worktree_branch.assert_not_called()
            # Should have proceeded to reset (step 4)
            assert mock_run.call_count == 4
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]

    def test_local_behind_remote_fetches_then_resets(self):
        """(c) local behind remote → fetches then resets (existing behavior)."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 3 behind
                _make_subprocess_result(stdout="0\t3\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 4
            # Verify step 4 was git reset --hard
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]
            assert "--hard" in reset_call[0][0]

    def test_local_in_sync_no_push_needed(self):
        """(d) local in sync → no push, reset is a no-op."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 0 behind
                _make_subprocess_result(stdout="0\t0\n"),
                # Step 4: reset succeeds (no-op when in sync)
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            spawner.gateway.push_worktree_branch.assert_not_called()
            assert mock_run.call_count == 4

    def test_push_fails_continues_with_reset(self):
        """(e) push fails → logs warning, continues with reset."""
        spawner = _make_spawner(push_ok=False)
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
                # Step 4: reset succeeds (after push failure)
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner, "pipe-1", Path("/tmp/repo"),
                prior_phase_succeeded=True,
            )
            # Push was attempted but failed
            spawner.gateway.push_worktree_branch.assert_called_once()
            # Warning logged about push failure
            mock_logger.warning.assert_called()
            # Reset still happened
            assert mock_run.call_count == 4

    def test_diverged_attempts_merge(self):
        """(f) diverged → attempts fast-forward merge."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 3 behind (diverged)
                _make_subprocess_result(stdout="2\t3\n"),
                # Merge succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should have attempted merge
            assert mock_run.call_count == 4
            merge_call = mock_run.call_args_list[3]
            assert "merge" in merge_call[0][0]
            assert "--ff-only" in merge_call[0][0]

    def test_diverged_merge_fails_signals_error(self):
        """(g) diverged + merge fails → signals error."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 3 behind (diverged)
                _make_subprocess_result(stdout="2\t3\n"),
                # Merge fails (non-fast-forward)
                _make_subprocess_result(returncode=1, stderr="fatal: Not possible to fast-forward"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Error should be logged
            mock_logger.error.assert_called()
            error_msg = mock_logger.error.call_args[0][0]
            assert "fast-forward" in error_msg.lower() or "merge" in error_msg.lower() or "diverged" in error_msg.lower()

    def test_successful_reset(self):
        """Happy path: fetch, detect branch, verify remote, reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 3 behind
                _make_subprocess_result(stdout="0\t3\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 4
            # Verify step 4 was git reset --hard
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]
            assert "--hard" in reset_call[0][0]

    def test_logs_warning_on_failed_reset(self):
        """If git reset --hard fails, a warning is logged."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 1 behind
                _make_subprocess_result(stdout="0\t1\n"),
                # Step 4: reset fails
                _make_subprocess_result(returncode=1, stderr="permission denied"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "Failed to reset" in warning_msg

    def test_handles_subprocess_timeout(self):
        """If subprocess raises TimeoutExpired, function handles gracefully."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
            # Should not raise
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
