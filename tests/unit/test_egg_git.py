"""Unit tests for egg_git module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from shared.egg_git import get_default_branch


class TestGetDefaultBranch:
    """Tests for get_default_branch function."""

    def test_main_from_remote_show(self, tmp_path: Path):
        """Test detection via git remote show origin."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="* remote origin\n  HEAD branch: main\n",
            )
            result = get_default_branch(tmp_path)
            assert result == "main"

    def test_master_from_remote_show(self, tmp_path: Path):
        """Test detection of master via git remote show."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="* remote origin\n  HEAD branch: master\n",
            )
            result = get_default_branch(tmp_path)
            assert result == "master"

    def test_fallback_to_branch_list_master(self, tmp_path: Path):
        """Test fallback to git branch -r when remote show fails."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            # First call (remote show) fails
            # Second call (branch -r) succeeds
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=0, stdout="  origin/HEAD -> origin/master\n  origin/master\n"),
            ]
            result = get_default_branch(tmp_path)
            assert result == "master"

    def test_fallback_to_branch_list_main(self, tmp_path: Path):
        """Test fallback detecting main via branch list."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=0, stdout="  origin/HEAD -> origin/main\n  origin/main\n"),
            ]
            result = get_default_branch(tmp_path)
            assert result == "main"

    def test_ultimate_fallback_to_main(self, tmp_path: Path):
        """Test ultimate fallback to 'main' when all else fails."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=1, stdout=""),
            ]
            result = get_default_branch(tmp_path)
            assert result == "main"

    def test_timeout_handling(self, tmp_path: Path):
        """Test that timeouts are handled gracefully."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd="git", timeout=30),
                MagicMock(returncode=1, stdout=""),
            ]
            result = get_default_branch(tmp_path)
            assert result == "main"

    def test_subprocess_error_handling(self, tmp_path: Path):
        """Test that subprocess errors are handled gracefully."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.SubprocessError("test error"),
                subprocess.SubprocessError("test error"),
            ]
            result = get_default_branch(tmp_path)
            assert result == "main"

    def test_string_path_input(self, tmp_path: Path):
        """Test that string paths are converted to Path objects."""
        with patch("shared.egg_git.default_branch.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="* remote origin\n  HEAD branch: main\n",
            )
            # Pass string instead of Path
            result = get_default_branch(str(tmp_path))
            assert result == "main"
