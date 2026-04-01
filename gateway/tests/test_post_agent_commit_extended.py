"""Extended tests for post_agent_commit.py.

With per-agent worktree isolation (#1481), auto_commit_worktree() is a
logged no-op.  Tests for the old commit/push/phase-filter behavior have
been replaced with tests confirming the no-op.
"""

from unittest.mock import MagicMock, patch

from post_agent_commit import _parse_changed_files, auto_commit_worktree


class TestAutoCommitDisabledPhaseFiltering:
    """Phase filtering no longer applies since auto-commit is disabled (#1481)."""

    @patch("post_agent_commit.subprocess.run")
    def test_blocked_files_not_restored(self, mock_run, tmp_path):
        """No git checkout is called for blocked files -- auto-commit is disabled."""
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
        # Only one git call (status), no checkout/add/commit
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert "status" in cmd

    @patch("post_agent_commit.subprocess.run")
    def test_multiple_files_no_commit(self, mock_run, tmp_path):
        """Multiple changed files still result in no commit."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                " M src/app.py\n"
                " M .egg-state/contracts/c.json\n"
                " M .egg-state/drafts/plan.md\n"
                " M .egg-state/reviews/r.json\n"
            ),
            stderr="",
        )
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            phase="implement",
        )
        assert result is None


class TestAutoCommitDisabledPush:
    """Push-related behavior is disabled along with auto-commit (#1481)."""

    @patch("post_agent_commit.subprocess.run")
    def test_no_push_with_credentials(self, mock_run, tmp_path):
        """Push is never attempted even with session credentials."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n", stderr="")
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok",
            gateway_url="http://gw:9848",
        )
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_no_salvage_branch_on_main(self, mock_run, tmp_path):
        """No salvage branch creation even when on main."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py\n", stderr="")
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok",
            gateway_url="http://gw:9848",
        )
        assert result is None
        # Only one subprocess call (git status), no checkout -b
        assert mock_run.call_count == 1


class TestParseChangedFilesExtended:
    """Extended tests for _parse_changed_files()."""

    def test_deleted_file(self):
        assert _parse_changed_files(" D deleted.py\n") == ["deleted.py"]

    def test_added_file(self):
        assert _parse_changed_files("A  added.py\n") == ["added.py"]

    def test_mixed_statuses(self):
        output = " M modified.py\n D deleted.py\nA  added.py\n?? untracked.py\n"
        result = _parse_changed_files(output)
        assert len(result) == 4

    def test_file_with_spaces(self):
        result = _parse_changed_files(" M path with spaces/file.py\n")
        assert result == ["path with spaces/file.py"]

    def test_file_in_deep_directory(self):
        result = _parse_changed_files(" M a/b/c/d/e/f.py\n")
        assert result == ["a/b/c/d/e/f.py"]

    def test_rename_with_directories(self):
        result = _parse_changed_files("R  old/path.py -> new/path.py\n")
        assert result == ["new/path.py"]
