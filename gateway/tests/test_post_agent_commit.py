"""Tests for post_agent_commit.py auto-commit functionality.

Validates that auto_commit_worktree correctly detects uncommitted changes,
stages them, creates a WIP commit, filters phase-restricted files, and
handles error cases.
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


class TestAutoCommitWorktreeSuccess:
    """Cases where auto-commit should create a commit."""

    @patch("post_agent_commit.subprocess.run")
    def test_successful_commit_returns_sha(self, mock_run, tmp_path):
        """Dirty worktree -> stage -> commit -> return SHA."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            # git add -- file.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="abc1234def5678\n", stderr=""),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result == "abc1234def5678"

    @patch("post_agent_commit.subprocess.run")
    def test_stages_specific_files_not_all(self, mock_run, tmp_path):
        """Should use 'git add -- <files>' instead of 'git add -A'."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n?? new.txt\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        # Second call is git add -- file.py new.txt
        add_call = mock_run.call_args_list[1]
        add_cmd = add_call[0][0]
        assert "-A" not in add_cmd
        assert "--" in add_cmd
        assert "file.py" in add_cmd
        assert "new.txt" in add_cmd

    @patch("post_agent_commit.subprocess.run")
    def test_commit_message_includes_container_id(self, mock_run, tmp_path):
        """Commit message includes container ID."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M x.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="my-container-42")
        # Third call is commit
        commit_call = mock_run.call_args_list[2]
        commit_cmd = commit_call[0][0]
        msg_idx = commit_cmd.index("-m") + 1
        assert "my-container-42" in commit_cmd[msg_idx]

    @patch("post_agent_commit.subprocess.run")
    def test_commit_message_includes_role(self, mock_run, tmp_path):
        """Commit message includes agent role when provided."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="?? new.txt\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha456\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1", agent_role="coder")
        commit_cmd = mock_run.call_args_list[2][0][0]
        msg_idx = commit_cmd.index("-m") + 1
        assert "(coder)" in commit_cmd[msg_idx]

    @patch("post_agent_commit.subprocess.run")
    def test_commit_message_includes_pipeline_id(self, mock_run, tmp_path):
        """Commit message includes pipeline ID when provided."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="?? new.txt\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha789\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1", pipeline_id="issue-42")
        commit_cmd = mock_run.call_args_list[2][0][0]
        msg_idx = commit_cmd.index("-m") + 1
        assert "[issue-42]" in commit_cmd[msg_idx]

    @patch("post_agent_commit.subprocess.run")
    def test_author_is_egg(self, mock_run, tmp_path):
        """Commit uses egg as author."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M x.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha000\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        commit_cmd = mock_run.call_args_list[2][0][0]
        author_idx = commit_cmd.index("--author") + 1
        assert commit_cmd[author_idx] == "egg <egg@localhost>"

    @patch("post_agent_commit.subprocess.run")
    def test_commit_uses_no_verify(self, mock_run, tmp_path):
        """Commit includes --no-verify flag."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M x.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha000\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        commit_cmd = mock_run.call_args_list[2][0][0]
        assert "--no-verify" in commit_cmd

    @patch("post_agent_commit.subprocess.run")
    def test_git_uses_security_configs(self, mock_run, tmp_path):
        """All git commands include safe.directory and hooks prevention."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M x.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha000\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "safe.directory=*" in cmd
            assert "core.hooksPath=/dev/null" in cmd


class TestAutoCommitPhaseFiltering:
    """Tests for phase-based file restriction filtering."""

    @patch("post_agent_commit.subprocess.run")
    def test_blocked_files_restored_and_excluded(self, mock_run, tmp_path):
        """Files blocked by phase restrictions are restored and not staged."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M src/app.py\n M .egg-state/contracts/c.json\n",
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json (restore blocked)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git add -- src/app.py (only allowed file)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_filtered\n", stderr=""),
        ]

        # Mock check_phase_file_restrictions to block the contract file
        mock_result = MagicMock()
        mock_result.allowed = False
        mock_result.blocked_files = [".egg-state/contracts/c.json"]

        import sys
        import types

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_result)
        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            assert result == "sha_filtered"

            # Verify git checkout -- was called for blocked file
            checkout_call = mock_run.call_args_list[1]
            checkout_cmd = checkout_call[0][0]
            assert "checkout" in checkout_cmd
            assert ".egg-state/contracts/c.json" in checkout_cmd

            # Verify git add only includes allowed file
            add_call = mock_run.call_args_list[2]
            add_cmd = add_call[0][0]
            assert "src/app.py" in add_cmd
            assert ".egg-state/contracts/c.json" not in add_cmd
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)

    @patch("post_agent_commit.subprocess.run")
    def test_all_files_blocked_returns_none(self, mock_run, tmp_path):
        """When all files are blocked, no commit is made."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M .egg-state/drafts/plan.md\n",
                stderr="",
            ),
            # git checkout -- (restore blocked file)
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        mock_result = MagicMock()
        mock_result.allowed = False
        mock_result.blocked_files = [".egg-state/drafts/plan.md"]

        import sys
        import types

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_result)
        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            assert result is None
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)

    @patch("post_agent_commit.subprocess.run")
    def test_no_phase_skips_filtering(self, mock_run, tmp_path):
        """Without a phase, all files are committed without filtering."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha_no_phase\n", stderr=""),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result == "sha_no_phase"

    @patch("post_agent_commit.subprocess.run")
    def test_phase_filter_import_fails_gracefully(self, mock_run, tmp_path):
        """If phase_filter can't be imported, files are committed unfiltered."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha_fallback\n", stderr=""),
        ]
        # Ensure phase_filter is not importable
        import sys

        old = sys.modules.get("phase_filter")
        old_gw = sys.modules.get("gateway.phase_filter")
        sys.modules["phase_filter"] = None  # type: ignore[assignment]
        sys.modules["gateway.phase_filter"] = None  # type: ignore[assignment]
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            assert result == "sha_fallback"
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)
            if old_gw is not None:
                sys.modules["gateway.phase_filter"] = old_gw
            else:
                sys.modules.pop("gateway.phase_filter", None)

    @patch("post_agent_commit.subprocess.run")
    def test_allowed_result_commits_all(self, mock_run, tmp_path):
        """When phase filter allows all files, all are committed."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M src/app.py\n M src/lib.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha_all\n", stderr=""),
        ]

        mock_result = MagicMock()
        mock_result.allowed = True

        import sys
        import types

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_result)
        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            assert result == "sha_all"
            # All files should be staged
            add_cmd = mock_run.call_args_list[1][0][0]
            assert "src/app.py" in add_cmd
            assert "src/lib.py" in add_cmd
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)


class TestAutoCommitPushViaGateway:
    """Tests for push-via-gateway functionality."""

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_push_called_with_credentials(self, mock_run, mock_push, tmp_path):
        """When session_token and gateway_url are provided, push is attempted."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
            # git rev-parse --abbrev-ref HEAD
            MagicMock(returncode=0, stdout="egg/my-branch\n", stderr=""),
        ]
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
        )
        assert result == "sha123"
        mock_push.assert_called_once_with(
            str(tmp_path),
            "tok-123",
            "http://localhost:9848",
            "egg/my-branch",
        )

    @patch("post_agent_commit._push_via_gateway", return_value=False)
    @patch("post_agent_commit.subprocess.run")
    def test_push_failure_still_returns_sha(self, mock_run, mock_push, tmp_path):
        """Push failure does not prevent returning the commit SHA."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha456\n", stderr=""),
            MagicMock(returncode=0, stdout="egg/branch\n", stderr=""),
        ]
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok-123",
            gateway_url="http://localhost:9848",
        )
        assert result == "sha456"

    @patch("post_agent_commit.subprocess.run")
    def test_no_push_without_credentials(self, mock_run, tmp_path):
        """Without session_token, no push is attempted."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha789\n", stderr=""),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result == "sha789"
        # Only 4 git calls (no rev-parse --abbrev-ref for push)
        assert len(mock_run.call_args_list) == 4


class TestAutoCommitWorktreeErrors:
    """Error handling during auto-commit."""

    @patch("post_agent_commit.subprocess.run")
    def test_add_failure_returns_none(self, mock_run, tmp_path):
        """git add failure should return None."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="add failed"),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_commit_failure_returns_none(self, mock_run, tmp_path):
        """git commit failure should return None."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=128, stdout="", stderr="commit failed"),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result is None

    @patch("post_agent_commit.subprocess.run")
    def test_rev_parse_failure_returns_unknown(self, mock_run, tmp_path):
        """rev-parse failure should still return a SHA (unknown)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=128, stdout="", stderr="rev-parse failed"),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result == "unknown"

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
        """All git commands should use the worktree path as cwd."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        assert mock_run.call_args[1]["cwd"] == str(tmp_path)
