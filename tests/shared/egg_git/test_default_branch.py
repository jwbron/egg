"""Tests for shared egg_git default_branch module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from egg_git.default_branch import get_default_branch


class TestGetDefaultBranch:
    """Tests for get_default_branch function."""

    @patch("subprocess.run")
    def test_from_remote_show(self, mock_run):
        """Get default branch from git remote show."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="* remote origin\n  HEAD branch: main\n  Remote branches:\n",
        )
        result = get_default_branch("/path/to/repo")
        assert result == "main"

    @patch("subprocess.run")
    def test_from_remote_show_master(self, mock_run):
        """Detect master as default branch."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="* remote origin\n  HEAD branch: master\n",
        )
        result = get_default_branch("/path/to/repo")
        assert result == "master"

    @patch("subprocess.run")
    def test_fallback_to_branch_r(self, mock_run):
        """Fall back to git branch -r when remote show fails."""
        # First call (remote show) fails
        # Second call (branch -r) succeeds with master
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="error"),
            MagicMock(returncode=0, stdout="  origin/master\n  origin/feature\n"),
        ]
        result = get_default_branch("/path/to/repo")
        assert result == "master"

    @patch("subprocess.run")
    def test_fallback_to_main(self, mock_run):
        """Fall back to branch -r with main branch."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="  origin/main\n  origin/dev\n"),
        ]
        result = get_default_branch("/path/to/repo")
        assert result == "main"

    @patch("subprocess.run")
    def test_ultimate_fallback(self, mock_run):
        """Ultimate fallback returns 'main'."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=1, stdout=""),
        ]
        result = get_default_branch("/path/to/repo")
        assert result == "main"

    @patch("subprocess.run")
    def test_timeout_handling(self, mock_run):
        """Handle subprocess timeout."""
        import subprocess

        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="git", timeout=30),
            MagicMock(returncode=0, stdout="  origin/main\n"),
        ]
        result = get_default_branch("/path/to/repo")
        assert result == "main"

    @patch("subprocess.run")
    def test_accepts_string_path(self, mock_run):
        """Accepts string path argument."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  HEAD branch: main\n",
        )
        result = get_default_branch("/path/to/repo")
        assert result == "main"

    @patch("subprocess.run")
    def test_accepts_path_object(self, mock_run):
        """Accepts Path object argument."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  HEAD branch: develop\n",
        )
        result = get_default_branch(Path("/path/to/repo"))
        assert result == "develop"

    @patch("subprocess.run")
    def test_subprocess_error_handling(self, mock_run):
        """Handle SubprocessError gracefully."""
        import subprocess

        mock_run.side_effect = [
            subprocess.SubprocessError("Something went wrong"),
            subprocess.SubprocessError("Still wrong"),
        ]
        result = get_default_branch("/path/to/repo")
        assert result == "main"
