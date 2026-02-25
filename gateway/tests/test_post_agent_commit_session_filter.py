"""Tests for per-session file restriction filtering in post_agent_commit.

Validates that auto_commit_worktree respects session-level allowed_files:
- Files outside the session allowlist are restored (not committed)
- Session filtering combines with phase filtering (both applied)
- Graceful fallback when session or phase_filter unavailable
- Clear logging for session-blocked files
"""

from unittest.mock import MagicMock, patch

from post_agent_commit import auto_commit_worktree


class TestSessionFilterBasic:
    """Tests for session-level file restriction in auto_commit_worktree."""

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_session_allowed_files_filter_blocks_out_of_scope(
        self, mock_run, mock_load_session, tmp_path
    ):
        """Files outside session allowlist are restored, not committed."""
        # git status returns two files: one in scope, one out
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M src/auth/login.py\n M src/other/secret.py\n",
                stderr="",
            ),
            # git checkout -- src/other/secret.py (restore blocked file)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git add -- src/auth/login.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_session_filter\n", stderr=""),
        ]

        mock_load_session.return_value = ["src/auth/*"]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
            session_token="tok-123",
        )

        assert result == "sha_session_filter"

        # Verify checkout was called for the blocked file
        checkout_call = mock_run.call_args_list[1]
        cmd = checkout_call[0][0]
        assert "checkout" in cmd
        assert "src/other/secret.py" in cmd

        # Verify only allowed file was staged
        add_call = mock_run.call_args_list[2]
        add_cmd = add_call[0][0]
        assert "src/auth/login.py" in add_cmd
        assert "src/other/secret.py" not in add_cmd

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_no_session_token_skips_session_filter(self, mock_run, mock_load_session, tmp_path):
        """Without session_token, session filtering is skipped."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M src/any.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="sha_no_session\n", stderr=""),
        ]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
        )

        assert result == "sha_no_session"
        mock_load_session.assert_not_called()

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_no_phase_skips_session_filter(self, mock_run, mock_load_session, tmp_path):
        """Without phase, session filtering is skipped."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M src/any.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="sha_no_phase\n", stderr=""),
        ]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
        )

        assert result == "sha_no_phase"
        mock_load_session.assert_not_called()

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_session_returns_none_allows_all(self, mock_run, mock_load_session, tmp_path):
        """When session has no allowed_files (None), all files pass."""
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=" M src/any.py\n M src/other.py\n",
                stderr="",
            ),
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="sha_no_allowlist\n", stderr=""),
        ]

        mock_load_session.return_value = None

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
            session_token="tok-123",
        )

        assert result == "sha_no_allowlist"
        # All files should be staged (no checkout restore)
        add_call = mock_run.call_args_list[1]
        add_cmd = add_call[0][0]
        assert "src/any.py" in add_cmd
        assert "src/other.py" in add_cmd


class TestSessionAndPhaseFilterCombination:
    """Tests for combined phase + session filtering."""

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_both_phase_and_session_block_files(self, mock_run, mock_load_session, tmp_path):
        """Phase blocks .egg-state, session blocks out-of-scope code files."""
        mock_run.side_effect = [
            # git status: 3 files
            MagicMock(
                returncode=0,
                stdout=(
                    " M src/auth/login.py\n M src/other/secret.py\n M .egg-state/contracts/c.json\n"
                ),
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json (phase blocked)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- src/other/secret.py (session blocked)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git add -- src/auth/login.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_combined\n", stderr=""),
        ]

        mock_load_session.return_value = ["src/auth/*"]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
            session_token="tok-123",
        )

        assert result == "sha_combined"

    @patch("post_agent_commit._load_session_allowed_files")
    @patch("post_agent_commit.subprocess.run")
    def test_all_files_blocked_by_session_skips_commit(self, mock_run, mock_load_session, tmp_path):
        """When session blocks all files, no commit is made."""
        mock_run.side_effect = [
            # git status
            MagicMock(
                returncode=0,
                stdout=" M src/other/a.py\n M src/other/b.py\n",
                stderr="",
            ),
            # git checkout -- src/other/a.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- src/other/b.py
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        mock_load_session.return_value = ["src/auth/*"]  # Nothing matches

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
            session_token="tok-123",
        )

        assert result is None  # No commit made


class TestLoadSessionAllowedFiles:
    """Tests for _load_session_allowed_files helper."""

    def test_returns_allowed_files_from_session(self):
        """Returns session's allowed_files when session found."""
        from post_agent_commit import _load_session_allowed_files

        mock_session = MagicMock()
        mock_session.allowed_files = ["src/*", "tests/*"]

        mock_manager = MagicMock()
        mock_manager.get_session.return_value = mock_session

        with patch("session_manager.get_session_manager", return_value=mock_manager):
            result = _load_session_allowed_files("test-token")

        assert result == ["src/*", "tests/*"]

    def test_returns_none_when_session_not_found(self):
        """Returns None when session doesn't exist."""
        from post_agent_commit import _load_session_allowed_files

        mock_manager = MagicMock()
        mock_manager.get_session.return_value = None

        with patch("session_manager.get_session_manager", return_value=mock_manager):
            result = _load_session_allowed_files("nonexistent-token")

        assert result is None

    def test_returns_none_when_session_has_no_allowed_files(self):
        """Returns None when session exists but has no allowed_files."""
        from post_agent_commit import _load_session_allowed_files

        mock_session = MagicMock(spec=[])  # No attributes
        mock_manager = MagicMock()
        mock_manager.get_session.return_value = mock_session

        with patch("session_manager.get_session_manager", return_value=mock_manager):
            result = _load_session_allowed_files("test-token")

        assert result is None

    def test_returns_none_on_import_error(self):
        """Returns None gracefully when session_manager can't be imported."""
        import sys

        from post_agent_commit import _load_session_allowed_files

        # Temporarily make session_manager unimportable
        old = sys.modules.get("session_manager")
        old_gw = sys.modules.get("gateway.session_manager")
        sys.modules["session_manager"] = None  # type: ignore[assignment]
        sys.modules["gateway.session_manager"] = None  # type: ignore[assignment]
        try:
            result = _load_session_allowed_files("test-token")
            assert result is None
        finally:
            if old is not None:
                sys.modules["session_manager"] = old
            else:
                sys.modules.pop("session_manager", None)
            if old_gw is not None:
                sys.modules["gateway.session_manager"] = old_gw
            else:
                sys.modules.pop("gateway.session_manager", None)
