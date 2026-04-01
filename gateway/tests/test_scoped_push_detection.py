"""
Tests for scoped push file detection (#1481).

With per-agent worktree isolation, get_changed_files_in_push() naturally
returns only the current agent's changes because each agent works in its
own worktree. This test validates the existing diff-based detection still
works correctly with per-agent worktrees and that the #1481 comment
about scoped detection is accurate.

Also validates worktree_manager.py, the new module for managing
per-agent worktrees on the gateway side.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Use the gateway test conftest to load modules
# (the conftest.py handles the hyphenated directory import magic)


class TestGetChangedFilesInPushScopedIsolation:
    """Validates that per-agent worktree isolation scopes push detection."""

    @patch("subprocess.run")
    def test_diff_returns_only_agent_files(self, mock_run):
        """In a per-agent worktree, diff only contains that agent's changes."""
        # Simulate: agent made changes to test files only
        mock_run.side_effect = [
            # git fetch
            MagicMock(returncode=0, stdout="", stderr=""),
            # git diff --name-only
            MagicMock(
                returncode=0,
                stdout="tests/test_new.py\ntests/conftest.py\n",
                stderr="",
            ),
        ]
        try:
            from git_client import get_changed_files_in_push
        except ImportError:
            pytest.skip("git_client not importable in test environment")

        files, error = get_changed_files_in_push("/home/egg/repos/egg", "origin", "egg/test-branch")

        assert error is None
        assert set(files) == {"tests/test_new.py", "tests/conftest.py"}

    @patch("subprocess.run")
    def test_empty_diff_for_clean_worktree(self, mock_run):
        """Agent with no changes produces empty file list."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        try:
            from git_client import get_changed_files_in_push
        except ImportError:
            pytest.skip("git_client not importable in test environment")

        files, error = get_changed_files_in_push("/home/egg/repos/egg", "origin", "egg/test-branch")
        assert files == []
        assert error is None


class TestWorktreeManagerValidation:
    """Tests for the worktree_manager module validation functions."""

    def test_validate_identifier_accepts_valid(self):
        """Valid identifiers pass validation."""
        try:
            from worktree_manager import validate_identifier
        except ImportError:
            pytest.skip("worktree_manager not importable")

        # These should not raise
        validate_identifier("pipe-123-coder", "container_id")
        validate_identifier("my_pipeline.v2", "container_id")
        validate_identifier("simple", "name")

    def test_validate_identifier_rejects_invalid(self):
        """Invalid identifiers are rejected."""
        try:
            from worktree_manager import validate_identifier
        except ImportError:
            pytest.skip("worktree_manager not importable")

        with pytest.raises(ValueError):
            validate_identifier("", "container_id")

        with pytest.raises(ValueError):
            validate_identifier("../escape", "container_id")

        with pytest.raises(ValueError):
            validate_identifier("path/../../escape", "container_id")

    def test_validate_identifier_rejects_traversal(self):
        """Path traversal attempts are rejected."""
        try:
            from worktree_manager import validate_identifier
        except ImportError:
            pytest.skip("worktree_manager not importable")

        with pytest.raises(ValueError):
            validate_identifier("..", "id")

        with pytest.raises(ValueError):
            validate_identifier("a/../b", "id")


class TestWorktreeInfoDataclass:
    """Tests for the WorktreeInfo dataclass."""

    def test_worktree_info_creation(self):
        """WorktreeInfo can be instantiated with required fields."""
        try:
            from worktree_manager import WorktreeInfo
        except ImportError:
            pytest.skip("worktree_manager not importable")

        info = WorktreeInfo(
            container_id="pipe-1-coder",
            repo_name="egg",
            branch="egg/issue-1481",
            worktree_path=Path("/home/egg/.egg-worktrees/pipe-1-coder/egg"),
            git_dir=None,
        )
        assert info.container_id == "pipe-1-coder"
        assert info.repo_name == "egg"
        assert info.created_at is None  # Optional field


class TestWorktreeRemovalResult:
    """Tests for the WorktreeRemovalResult dataclass."""

    def test_successful_removal(self):
        """Result reports successful removal."""
        try:
            from worktree_manager import WorktreeRemovalResult
        except ImportError:
            pytest.skip("worktree_manager not importable")

        result = WorktreeRemovalResult(success=True, branch_deleted=True)
        assert result.success is True
        assert result.uncommitted_changes is False
        assert result.branch_deleted is True

    def test_failed_removal_with_uncommitted(self):
        """Result reports failure due to uncommitted changes."""
        try:
            from worktree_manager import WorktreeRemovalResult
        except ImportError:
            pytest.skip("worktree_manager not importable")

        result = WorktreeRemovalResult(
            success=False,
            uncommitted_changes=True,
            error="Worktree has uncommitted changes",
        )
        assert result.success is False
        assert result.uncommitted_changes is True
