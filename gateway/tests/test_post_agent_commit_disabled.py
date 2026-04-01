"""Tests for disabled auto-commit behavior in post_agent_commit.py.

With per-agent worktree isolation (#1481), auto_commit_worktree() is a
logged no-op that always returns None.  These tests verify the new
behavior: no git commits are created, uncommitted changes are logged,
and the function signature remains backward-compatible.
"""

from unittest.mock import MagicMock, patch

from post_agent_commit import auto_commit_worktree


class TestAutoCommitDisabledWithChanges:
    """auto_commit_worktree with uncommitted changes returns None (not a SHA)."""

    @patch("post_agent_commit.subprocess.run")
    def test_uncommitted_changes_returns_none(self, mock_run, tmp_path):
        """Dirty worktree should return None -- no commit is created."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n?? new.txt\n", stderr=""
        )
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            agent_role="coder",
            pipeline_id="issue-42",
            phase="implement",
        )
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_uncommitted_changes_with_session_credentials_returns_none(self, mock_run, tmp_path):
        """Even with session token and gateway URL, should still return None."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n", stderr="")
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
        )
        assert result is None


class TestAutoCommitDisabledCleanWorktree:
    """auto_commit_worktree with clean worktree returns None."""

    @patch("post_agent_commit.subprocess.run")
    def test_clean_worktree_returns_none(self, mock_run, tmp_path):
        """Empty porcelain output means clean tree -- returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None


class TestAutoCommitDisabledNonExistent:
    """auto_commit_worktree with non-existent path returns None."""

    def test_nonexistent_worktree_returns_none(self, tmp_path):
        result = auto_commit_worktree(str(tmp_path / "nonexistent"), container_id="c1")
        assert result is None


class TestNoGitCommitExecuted:
    """Verify no git commit is executed -- only git status is called."""

    @patch("post_agent_commit.subprocess.run")
    def test_no_commit_call_with_changes(self, mock_run, tmp_path):
        """With uncommitted changes, only git status should be called."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n", stderr="")
        auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            agent_role="coder",
        )

        # Verify subprocess.run was called (for git status)
        assert mock_run.called

        # Verify no call contains "commit" in its arguments
        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get("args", [])
            assert "commit" not in args, f"git commit should not be called, but found: {args}"

    @patch("post_agent_commit.subprocess.run")
    def test_no_add_call_with_changes(self, mock_run, tmp_path):
        """No git add should be called -- we are not staging anything."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=" M file.py\n?? new.txt\n", stderr=""
        )
        auto_commit_worktree(str(tmp_path), container_id="c1")

        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get("args", [])
            assert "add" not in args, f"git add should not be called, but found: {args}"

    @patch("post_agent_commit.subprocess.run")
    def test_no_push_call_with_changes(self, mock_run, tmp_path):
        """No push should be attempted even with session credentials."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n", stderr="")
        auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
        )

        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get("args", [])
            assert "push" not in args, f"git push should not be called, but found: {args}"


class TestUncommittedChangesLogged:
    """Verify uncommitted changes are still logged for visibility."""

    @patch("post_agent_commit.logger")
    @patch("post_agent_commit.subprocess.run")
    def test_changes_logged_with_info(self, mock_run, mock_logger, tmp_path):
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

        # Find the info call with the disabled event
        info_calls = mock_logger.info.call_args_list
        assert len(info_calls) > 0, "Expected at least one logger.info call"

        # Check that the log message indicates auto-commit is disabled
        found = False
        for c in info_calls:
            msg = c[0][0] if c[0] else ""
            if "disabled" in msg.lower() or "auto_commit_disabled" in str(c):
                found = True
                break
        assert found, f"Expected log about auto-commit being disabled, got: {info_calls}"

    @patch("post_agent_commit.logger")
    @patch("post_agent_commit.subprocess.run")
    def test_clean_worktree_logged_at_debug(self, mock_run, mock_logger, tmp_path):
        """Clean worktree should be logged at DEBUG level."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        auto_commit_worktree(str(tmp_path), container_id="c1")

        debug_calls = mock_logger.debug.call_args_list
        assert len(debug_calls) > 0, "Expected at least one logger.debug call"


class TestBackwardCompatibility:
    """Verify function signature is backward-compatible."""

    def test_all_parameters_accepted(self, tmp_path):
        """All original parameters should be accepted without error."""
        result = auto_commit_worktree(
            worktree_path=str(tmp_path),
            container_id="c1",
            agent_role="coder",
            pipeline_id="issue-42",
            phase="implement",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
            consensus_confirmed=True,
        )
        assert result is None

    def test_minimal_parameters(self, tmp_path):
        """Only required parameters should work."""
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None
