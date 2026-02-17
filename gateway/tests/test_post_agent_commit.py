"""Tests for post_agent_commit.py auto-commit functionality.

Validates that auto_commit_worktree correctly detects uncommitted changes,
stages them, creates a WIP commit, and handles error cases.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from post_agent_commit import auto_commit_worktree


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
            # git add -A
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="abc1234def5678\n", stderr=""),
        ]
        result = auto_commit_worktree(str(tmp_path), container_id="c1")
        assert result == "abc1234def5678"

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
        auto_commit_worktree(
            str(tmp_path), container_id="c1", agent_role="coder"
        )
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
        auto_commit_worktree(
            str(tmp_path), container_id="c1", pipeline_id="issue-42"
        )
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
