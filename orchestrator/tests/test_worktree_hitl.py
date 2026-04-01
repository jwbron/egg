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

        # Patch the WORKTREE_BASE_DIR constant and subprocess.run
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("container_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
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

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("container_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
            result = spawner.detect_uncommitted_changes(
                pipeline_id="issue-99",
                agent_role="coder",
            )

        assert result is None

    def test_nonexistent_worktree_returns_none(self, tmp_path):
        """Should return None when worktree directory doesn't exist."""
        spawner = self._make_spawner()

        # tmp_path exists but does NOT contain "nonexistent-pipeline-coder"
        with patch("container_spawner.WORKTREE_BASE_DIR", tmp_path):
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

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("container_spawner.WORKTREE_BASE_DIR", tmp_path),
        ):
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

        with (
            patch("worktree_manager.subprocess.run", return_value=clean_result),
            patch.object(manager, "remove_worktree", return_value=removal_result) as mock_remove,
        ):
            result = manager.cleanup_clean_worktree("container-1", "myrepo")

        assert result is True
        mock_remove.assert_called_once_with(
            "container-1", "myrepo", force=False, delete_branch=True
        )

    def test_preserves_dirty_worktree(self, tmp_path):
        """Should NOT remove worktree that has uncommitted changes."""
        manager = self._make_manager(tmp_path)

        wt_path = manager.worktree_base / "container-1" / "myrepo"
        wt_path.mkdir(parents=True)

        dirty_result = MagicMock(returncode=0, stdout=" M dirty.py\n", stderr="")

        with (
            patch("worktree_manager.subprocess.run", return_value=dirty_result),
            patch.object(manager, "remove_worktree") as mock_remove,
        ):
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

        # Create a realistic .git file (worktrees use a file, not a directory)
        # pointing to a git admin dir with old index/HEAD files.
        git_admin_dir = tmp_path / "git-admin" / "worktrees" / "old-container"
        git_admin_dir.mkdir(parents=True)
        (repo_dir / ".git").write_text(f"gitdir: {git_admin_dir}\n")
        (git_admin_dir / "index").touch()
        (git_admin_dir / "HEAD").write_text("ref: refs/heads/egg/old-container/work\n")

        # Set mtime to 72 hours ago on all relevant files
        old_time = time.time() - (72 * 3600)
        os.utime(str(container_dir), (old_time, old_time))
        os.utime(str(git_admin_dir / "index"), (old_time, old_time))
        os.utime(str(git_admin_dir / "HEAD"), (old_time, old_time))

        removal_result = MagicMock(success=True)
        with patch.object(manager, "remove_worktree", return_value=removal_result) as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(
                max_age_hours=48, active_containers=set()
            )

        assert removed == 1
        mock_remove.assert_called_once_with(
            "old-container", "myrepo", force=True, delete_branch=True
        )

    def test_preserves_recent_worktrees(self, tmp_path):
        """Should NOT remove worktrees newer than max_age_hours.

        Uses a .git file pointing to a git admin dir with recent index/HEAD,
        verifying the mtime detection follows the gitdir indirection.
        """
        manager = self._make_manager(tmp_path)

        container_dir = manager.worktree_base / "recent-container"
        repo_dir = container_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        # Create realistic .git file pointing to git admin dir
        git_admin_dir = tmp_path / "git-admin" / "worktrees" / "recent-container"
        git_admin_dir.mkdir(parents=True)
        (repo_dir / ".git").write_text(f"gitdir: {git_admin_dir}\n")
        (git_admin_dir / "index").touch()
        (git_admin_dir / "HEAD").write_text("ref: refs/heads/egg/recent-container/work\n")

        # Parent dir old, but git admin files are recent — should be preserved
        old_time = time.time() - (72 * 3600)
        os.utime(str(container_dir), (old_time, old_time))

        with patch.object(manager, "remove_worktree") as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(
                max_age_hours=48, active_containers=set()
            )

        assert removed == 0
        mock_remove.assert_not_called()

    def test_skips_active_container_worktrees(self, tmp_path):
        """Should NOT remove worktrees whose containers are still running."""
        manager = self._make_manager(tmp_path)

        container_dir = manager.worktree_base / "active-container"
        repo_dir = container_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        # Set mtime to 72 hours ago — would be stale, but container is active
        old_time = time.time() - (72 * 3600)
        os.utime(str(container_dir), (old_time, old_time))

        with patch.object(manager, "remove_worktree") as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(
                max_age_hours=48, active_containers={"active-container"}
            )

        assert removed == 0
        mock_remove.assert_not_called()

    def test_preserves_recent_worktrees_relative_gitdir(self, tmp_path):
        """Should resolve relative gitdir paths in .git files for mtime checks."""
        manager = self._make_manager(tmp_path)

        container_dir = manager.worktree_base / "rel-gitdir-container"
        repo_dir = container_dir / "myrepo"
        repo_dir.mkdir(parents=True)

        # Place git admin dir relative to the repo dir
        git_admin_dir = tmp_path / "git-admin" / "worktrees" / "rel-gitdir-container"
        git_admin_dir.mkdir(parents=True)
        # Write a *relative* gitdir path (from repo_dir to git_admin_dir)
        rel_path = os.path.relpath(git_admin_dir, repo_dir)
        (repo_dir / ".git").write_text(f"gitdir: {rel_path}\n")
        (git_admin_dir / "index").touch()
        (git_admin_dir / "HEAD").write_text("ref: refs/heads/egg/rel-gitdir/work\n")

        # Parent dir old, but git admin files are recent — should be preserved
        old_time = time.time() - (72 * 3600)
        os.utime(str(container_dir), (old_time, old_time))

        with patch.object(manager, "remove_worktree") as mock_remove:
            removed = manager.cleanup_stale_pipeline_worktrees(
                max_age_hours=48, active_containers=set()
            )

        assert removed == 0
        mock_remove.assert_not_called()

    def test_empty_base_returns_zero(self, tmp_path):
        """Empty worktree base should return 0."""
        manager = self._make_manager(tmp_path)
        removed = manager.cleanup_stale_pipeline_worktrees(active_containers=set())
        assert removed == 0
