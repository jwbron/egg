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


def _make_spawner(fetch_ok: bool = True) -> MagicMock:
    """Create a mock spawner with gateway.fetch_worktree_branch."""
    spawner = MagicMock()
    spawner.gateway.fetch_worktree_branch.return_value = fetch_ok
    return spawner


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


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

    def test_skips_reset_when_local_ahead(self):
        """If local has commits not on remote, skip the reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should NOT proceed to step 4 (reset)
            assert mock_run.call_count == 3

    def test_skips_reset_when_local_diverged(self):
        """If local has diverged from remote (ahead AND behind), skip the reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 3 behind (diverged)
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should NOT proceed to step 4 (reset) — local_ahead > 0
            assert mock_run.call_count == 3

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
        with patch("routes.pipelines.subprocess.run") as mock_run, \
             patch("routes.pipelines.logger") as mock_logger:
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
