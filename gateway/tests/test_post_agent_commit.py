"""Tests for post_agent_commit.py.

With per-agent worktree isolation (#1481), auto_commit_worktree() is a
logged no-op that always returns None.  These tests verify the helper
functions still work correctly and that auto_commit_worktree() never
creates commits or pushes.
"""

import subprocess
from unittest.mock import MagicMock, patch

from post_agent_commit import _parse_changed_files, auto_commit_worktree


class TestParseChangedFiles:
    """Tests for _parse_changed_files helper."""

    def test_modified_file(self):
        assert _parse_changed_files(" M file.py\n") == ["file.py"]

    def test_new_file(self):
        assert _parse_changed_files("?? new.txt\n") == ["new.txt"]

    def test_renamed_file_keeps_destination(self):
        assert _parse_changed_files("R  old.py -> new.py\n") == ["new.py"]

    def test_multiple_files(self):
        output = " M a.py\n?? b.txt\n M c.js\n"
        assert _parse_changed_files(output) == ["a.py", "b.txt", "c.js"]

    def test_empty_output(self):
        assert _parse_changed_files("") == []

    def test_short_lines_ignored(self):
        assert _parse_changed_files("ab\n") == []


class TestAutoCommitWorktreeNoChanges:
    """Cases where auto-commit should return None without committing."""

    def test_nonexistent_worktree_returns_none(self, tmp_path):
        """Non-existent path should be skipped."""
        result = auto_commit_worktree(
            str(tmp_path / "nonexistent"),
            container_id="c1",
        )
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_clean_worktree_returns_none(self, mock_run, tmp_path):
        """Empty porcelain output means clean tree."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_status_failure_returns_none(self, mock_run, tmp_path):
        """git status failure should return None."""
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="error")
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_whitespace_only_status_returns_none(self, mock_run, tmp_path):
        """Whitespace-only output is treated as clean."""
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n  ", stderr="")
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None


class TestAutoCommitDisabled:
    """Auto-commit is now a no-op (#1481) -- always returns None."""

    @patch("post_agent_commit.subprocess.run")
    def test_dirty_worktree_returns_none(self, mock_run, tmp_path):
        """Dirty worktree no longer creates a commit -- returns None."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n", stderr=""
        )
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_no_commit_call(self, mock_run, tmp_path):
        """No git commit should be executed."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n?? new.txt\n", stderr=""
        )
        auto_commit_worktree(str(tmp_path), container_id="c1")
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "commit" not in cmd

    @patch("post_agent_commit.subprocess.run")
    def test_no_add_call(self, mock_run, tmp_path):
        """No git add should be executed."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n", stderr=""
        )
        auto_commit_worktree(str(tmp_path), container_id="c1")
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "add" not in cmd

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_no_push_even_with_credentials(self, mock_run, mock_push, tmp_path):
        """No push should be attempted even with session credentials."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n", stderr=""
        )
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
        )
        assert result is None
        mock_push.assert_not_called()

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_consensus_confirmed_still_returns_none(self, mock_run, mock_push, tmp_path):
        """Even with consensus_confirmed=True, returns None (no commit)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n", stderr=""
        )
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
            consensus_confirmed=True,
        )
        assert result is None
        mock_push.assert_not_called()

    @patch("post_agent_commit.subprocess.run")
    def test_phase_parameter_accepted_but_ignored(self, mock_run, tmp_path):
        """Phase parameter is accepted for backward compat but no filtering occurs."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=" M src/app.py\n M .egg-state/contracts/c.json\n",
            stderr="",
        )
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
        )
        assert result is None
        # Only one git call (status), no checkout/add/commit calls
        assert mock_run.call_count == 1

    @patch("post_agent_commit.subprocess.run")
    def test_git_uses_security_configs(self, mock_run, tmp_path):
        """Git status command includes safe.directory and hooks prevention."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        auto_commit_worktree(str(tmp_path), container_id="c1")
        cmd = mock_run.call_args[0][0]
        assert "safe.directory=*" in cmd
        assert "core.hooksPath=/dev/null" in cmd


class TestAutoCommitWorktreeErrors:
    """Error handling during auto-commit status check."""

    @patch("post_agent_commit.subprocess.run")
    def test_timeout_returns_none(self, mock_run, tmp_path):
        """subprocess.TimeoutExpired should return None."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_unexpected_exception_returns_none(self, mock_run, tmp_path):
        """Unexpected exceptions should return None."""
        mock_run.side_effect = OSError("disk full")
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_cwd_is_worktree_path(self, mock_run, tmp_path):
        """Git status command should use the worktree path as cwd."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        auto_commit_worktree(str(tmp_path), container_id="c1")
        assert mock_run.call_args[1]["cwd"] == str(tmp_path)


class TestAutoCommitLogging:
    """Verify logging behavior for the disabled auto-commit."""

    @patch("post_agent_commit.logger")
    @patch("post_agent_commit.subprocess.run")
    def test_uncommitted_changes_logged(self, mock_run, mock_logger, tmp_path):
        """Uncommitted changes should be logged at INFO level."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n?? new.txt\n", stderr=""
        )
        auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            agent_role="coder",
            pipeline_id="issue-42",
        )
        assert mock_logger.info.called
        call_kwargs = mock_logger.info.call_args
        assert "disabled" in call_kwargs[0][0].lower() or \
               "auto_commit_disabled" in str(call_kwargs)

    @patch("post_agent_commit.logger")
    @patch("post_agent_commit.subprocess.run")
    def test_clean_worktree_logged_at_debug(self, mock_run, mock_logger, tmp_path):
        """Clean worktree should be logged at DEBUG level."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        auto_commit_worktree(str(tmp_path), container_id="c1")
        assert mock_logger.debug.called
