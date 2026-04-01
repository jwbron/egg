"""Tests for worktree HITL recovery and cleanup (#1481).

Covers:
- ContainerSpawner.detect_uncommitted_changes()
- WorktreeManager.cleanup_clean_worktree()
- WorktreeManager.cleanup_stale_pipeline_worktrees()
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure gateway is importable for WorktreeManager tests
_gateway_path = str(Path(__file__).parent.parent.parent / "gateway")
if _gateway_path not in sys.path:
    sys.path.insert(0, _gateway_path)


class TestDetectUncommittedChanges:
    """Tests for ContainerSpawner.detect_uncommitted_changes()."""

    def _make_spawner(self):
        """Create a ContainerSpawner with mocked dependencies."""
        from container_spawner import ContainerSpawner

        return ContainerSpawner(
            docker_client=MagicMock(),
            gateway_client=MagicMock(),
        )

    def test_returns_dict_with_changes(self, tmp_path):
        """Should return a dict when worktree has uncommitted changes."""
        spawner = self._make_spawner()

        # Create directory structure matching the expected layout
        repo_dir = tmp_path / "issue-99-coder" / "myrepo"
        repo_dir.mkdir(parents=True)

        mock_result = MagicMock(
            returncode=0,
            stdout=" M changed.py\n?? new_file.txt\n",
        )

        # Patch the base path used inside the method and subprocess.run
        with patch("subprocess.run", return_value=mock_result), \
             patch("container_spawner.Path") as mock_path_cls:
            # Make Path("/home/egg/.egg-worktrees") return tmp_path
            mock_path_cls.return_value = tmp_path

            result = spawner.detect_uncommitted_changes(
                pipeline_id="issue-99",
                agent_role="coder",
            )

        assert result is not None
        assert result["pipeline_id"] == "issue-99"
        assert result["agent_role"] == "coder"
        assert result["worktree_id"] == "issue-99-coder"
        assert result["file_count"] == 2
        assert "changed.py" in result["changed_files"]
        assert "new_file.txt" in result["changed_files"]

    def test_clean_worktree_returns_none(self, tmp_path):
        """Should return None when worktree has no uncommitted changes."""
        spawner = self._make_spawner()

        repo_dir = tmp_path / "issue-99-coder" / "myrepo"
        repo_dir.mkdir(parents=True)

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", return_value=mock_result), \
             patch("container_spawner.Path") as mock_path_cls:
            mock_path_cls.return_value = tmp_path

            result = spawner.detect_uncommitted_changes(
                pipeline_id="issue-99",
                agent_role="coder",
            )

        assert result is None

    def test_nonexistent_worktree_returns_none(self, tmp_path):
        """Should return None when worktree directory doesn't exist."""
        spawner = self._make_spawner()

        # tmp_path exists but does NOT contain "nonexistent-pipeline-coder"
        with patch("container_spawner.Path") as mock_path_cls:
            mock_path_cls.return_value = tmp_path

            result = spawner.detect_uncommitted_changes(
                pipeline_id="nonexistent-pipeline",
                agent_role="coder",
            )

        assert result is None

    def test_subprocess_error_returns_none(self, tmp_path):
        """Should return None when git status fails."""
        spawner = self._make_spawner()

        repo_dir = tmp_path / "issue-99-coder" / "myrepo"
        repo_dir.mkdir(parents=True)

        mock_result = MagicMock(returncode=128, stdout="", stderr="fatal: error")

        with patch("subprocess.run", return_value=mock_result), \
             patch("container_spawner.Path") as mock_path_cls:
            mock_path_cls.return_value = tmp_path

            result = spawner.detect_uncommitted_changes(
                pipeline_id="issue-99",
                agent_role="coder",
            )

        assert result is None


class TestCleanupCleanWorktree:
    """Tests for WorktreeManager.cleanup_clean_worktree()."""

    def _make_manager(self, tmp_path):
        from worktree_manager import WorktreeManager

        repos_base = tmp_path / "repos"
        repos_base.mkdir(parents=True, exist_ok=True)
        return WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=repos_base,
        )

    def test_removes_clean_worktree(self, tmp_path):
        """Should remove worktree that has no uncommitted changes."""
        manager = self._make_manager(tmp_path)

        wt_path = manager.worktree_base / "container-1" / "myrepo"
        wt_path.mkdir(parents=True)

        clean_result = MagicMock(returncode=0, stdout="", stderr="")
        removal_result = MagicMock(success=True)

        with patch("worktree_manager.subprocess.run", return_value=clean_result), \
             patch.object(manager, "remove_worktree", return_value=removal_result) as mock_remove:
            result = manager.cleanup_clean_worktree("container-1", "myrepo")

        assert result is True
        mock_remove.assert_called_once_with(
            "container-1", "myrepo", force=True, delete_branch=True
        )

    def test_preserves_dirty_worktree(self, tmp_path):
        """Should NOT remove worktree that has uncommitted changes."""
        manager = self._make_manager(tmp_path)

        wt_path = manager.worktree_base / "container-1" / "myrepo"
        wt_path.mkdir(parents=True)

        dirty_result = MagicMock(returncode=0, stdout=" M dirty.py\n", stderr="")

        with patch("worktree_manager.subprocess.run", return_value=dirty_result), \
             patch.object(manager, "remove_worktree") as mock_remove:
            result = manager.cleanup_clean_worktree("container-1", "myrepo")

        assert result is False
        mock_remove.assert_not_called()

    def test_already_cleaned_returns_true(self, tmp_path):
        """Non-existent worktree should return True (already clean)."""
        manager = self._make_manager(tmp_path)
        result = manager.cleanup_clean_worktree("nonexistent", "myrepo")
        assert result is True

    def test_status_check_failure_returns_false(self, tmp_path):
        """Should return False when git status check fails."""
        manager = self._make_manager(tmp_path)

        wt_path = manager.worktree_base / "container-1" / "myrepo"
        wt_path.mkdir(parents=True)

        with patch("worktree_manager.subprocess.run", side_effect=OSError("git not found")):
            result = manager.cleanup_clean_worktree("container-1", "myrepo")

        assert result is False


class TestCleanupStalePipelineWorktrees:
    """Tests for WorktreeManager.cleanup_stale_pipeline_worktrees()."""

    def _make_manager(self, tmp_path):
        from worktree_manager import WorktreeManager

        repos_base = tmp_path / "repos"
        repos_base.mkdir(parents=True, exist_ok=True)
        return WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=repos_base,
        )

    def test_removes_old_worktrees(self, tmp_path):
        """Should remove worktrees older than max_age_hours."""
        manager = self._make_manager(tmp_path)

        container_dir = manager.worktree_base / "old-container"
        repo_dir = container_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        # Set mtime to 72 hours ago
        old_time = time.time() - (72 * 3600)
        os.utime(str(container_dir), (old_time, old_time))

        removal_result = MagicMock(success=True)
        with patch.object(manager, "remove_worktree", return_value=removal_result) as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(max_age_hours=48)

        assert removed == 1
        mock_remove.assert_called_once_with(
            "old-container", "myrepo", force=True, delete_branch=True
        )

    def test_preserves_recent_worktrees(self, tmp_path):
        """Should NOT remove worktrees newer than max_age_hours."""
        manager = self._make_manager(tmp_path)

        container_dir = manager.worktree_base / "recent-container"
        repo_dir = container_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        with patch.object(manager, "remove_worktree") as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(max_age_hours=48)

        assert removed == 0
        mock_remove.assert_not_called()

    def test_empty_base_returns_zero(self, tmp_path):
        """Empty worktree base should return 0."""
        manager = self._make_manager(tmp_path)
        removed = manager.cleanup_stale_pipeline_worktrees()
        assert removed == 0
