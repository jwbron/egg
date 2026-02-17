"""Tests for phase-level worktree lifecycle (Tier 3).

Covers:
- create_phase_worktree argument construction and delegation
- cleanup_phase_worktrees with explicit phase_ids
- cleanup_phase_worktrees scanning for all phase worktrees
- Phase ID sanitization in path/branch names
- validate_identifier for phase container IDs
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from worktree_manager import WorktreeInfo, WorktreeManager, WorktreeRemovalResult


class TestCreatePhaseWorktree:
    """Tests for WorktreeManager.create_phase_worktree()."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create a WorktreeManager with temp dirs."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        worktree_base.mkdir()
        repos_base.mkdir()
        return WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

    def test_delegates_to_create_worktree(self, manager: WorktreeManager):
        """create_phase_worktree delegates to create_worktree with composite container_id."""
        mock_info = WorktreeInfo(
            container_id="ctr-abc-phase-1",
            repo_name="myrepo",
            branch="egg/ctr-abc-phase-1/work",
            worktree_path=Path("/tmp/wt"),
            git_dir=Path("/tmp/git"),
        )
        with patch.object(manager, "create_worktree", return_value=mock_info) as mock_create:
            result = manager.create_phase_worktree(
                repo_name="myrepo",
                container_id="ctr-abc",
                phase_id="phase-1",
                base_branch="egg/issue-732",
            )

            mock_create.assert_called_once_with(
                repo_name="myrepo",
                container_id="ctr-abc-phase-1",
                base_branch="egg/issue-732",
                uid=None,
                gid=None,
            )
            assert result is mock_info

    def test_sanitizes_phase_id(self, manager: WorktreeManager):
        """Special characters in phase_id are replaced with hyphens."""
        mock_info = MagicMock()
        with patch.object(manager, "create_worktree", return_value=mock_info) as mock_create:
            manager.create_phase_worktree(
                repo_name="myrepo",
                container_id="ctr-abc",
                phase_id="phase/1.special",
                base_branch="HEAD",
            )

            # phase/1.special -> phase-1-special
            called_container_id = mock_create.call_args[1]["container_id"]
            assert "/" not in called_container_id
            assert "." not in called_container_id
            assert "phase-1-special" in called_container_id

    def test_passes_uid_gid(self, manager: WorktreeManager):
        """uid and gid are passed through to create_worktree."""
        mock_info = MagicMock()
        with patch.object(manager, "create_worktree", return_value=mock_info) as mock_create:
            manager.create_phase_worktree(
                repo_name="myrepo",
                container_id="ctr-abc",
                phase_id="phase-2",
                base_branch="HEAD",
                uid=1001,
                gid=1001,
            )

            mock_create.assert_called_once_with(
                repo_name="myrepo",
                container_id="ctr-abc-phase-2",
                base_branch="HEAD",
                uid=1001,
                gid=1001,
            )

    def test_invalid_container_id_raises(self, manager: WorktreeManager):
        """Invalid container_id raises ValueError."""
        with pytest.raises(ValueError, match="container_id"):
            manager.create_phase_worktree(
                repo_name="myrepo",
                container_id="../escape",
                phase_id="phase-1",
            )

    def test_invalid_repo_name_raises(self, manager: WorktreeManager):
        """Invalid repo_name raises ValueError."""
        with pytest.raises(ValueError, match="repo_name"):
            manager.create_phase_worktree(
                repo_name="../escape",
                container_id="ctr-abc",
                phase_id="phase-1",
            )


class TestCleanupPhaseWorktrees:
    """Tests for WorktreeManager.cleanup_phase_worktrees()."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create a WorktreeManager with temp dirs."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        worktree_base.mkdir()
        repos_base.mkdir()
        return WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

    def test_cleanup_specific_phases(self, manager: WorktreeManager):
        """cleanup_phase_worktrees removes specific phase worktrees."""
        success_result = WorktreeRemovalResult(success=True)
        with patch.object(manager, "remove_worktree", return_value=success_result) as mock_remove:
            results = manager.cleanup_phase_worktrees(
                container_id="ctr-abc",
                repo_name="myrepo",
                phase_ids=["phase-1", "phase-2"],
            )

            assert len(results) == 2
            assert all(r.success for r in results)
            mock_remove.assert_any_call(
                container_id="ctr-abc-phase-1",
                repo_name="myrepo",
                force=True,
                delete_branch=True,
            )
            mock_remove.assert_any_call(
                container_id="ctr-abc-phase-2",
                repo_name="myrepo",
                force=True,
                delete_branch=True,
            )

    def test_cleanup_all_scans_directory(self, manager: WorktreeManager):
        """cleanup_phase_worktrees without phase_ids scans for phase dirs."""
        # Create directory structure that matches the scanning pattern
        repo_dir = manager.worktree_base / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "ctr-abc-phase-1").mkdir()
        (repo_dir / "ctr-abc-phase-2").mkdir()
        (repo_dir / "other-container").mkdir()  # Should not be cleaned

        success_result = WorktreeRemovalResult(success=True)
        with patch.object(manager, "remove_worktree", return_value=success_result) as mock_remove:
            results = manager.cleanup_phase_worktrees(
                container_id="ctr-abc",
                repo_name="myrepo",
            )

            assert len(results) == 2
            # Should have called remove for both phase dirs but not other-container
            removed_ids = [c[1]["container_id"] for c in mock_remove.call_args_list]
            assert "ctr-abc-phase-1" in removed_ids
            assert "ctr-abc-phase-2" in removed_ids
            assert "other-container" not in removed_ids

    def test_cleanup_empty_phases_returns_empty(self, manager: WorktreeManager):
        """cleanup_phase_worktrees with empty phase_ids list returns empty."""
        results = manager.cleanup_phase_worktrees(
            container_id="ctr-abc",
            repo_name="myrepo",
            phase_ids=[],
        )
        assert results == []

    def test_cleanup_nonexistent_dir_returns_empty(self, manager: WorktreeManager):
        """cleanup_phase_worktrees with no directory to scan returns empty."""
        results = manager.cleanup_phase_worktrees(
            container_id="ctr-abc",
            repo_name="myrepo",
        )
        assert results == []

    def test_cleanup_sanitizes_phase_ids(self, manager: WorktreeManager):
        """cleanup_phase_worktrees sanitizes phase_ids for container_id construction."""
        success_result = WorktreeRemovalResult(success=True)
        with patch.object(manager, "remove_worktree", return_value=success_result) as mock_remove:
            manager.cleanup_phase_worktrees(
                container_id="ctr-abc",
                repo_name="myrepo",
                phase_ids=["phase/1"],
            )

            called_container_id = mock_remove.call_args[1]["container_id"]
            assert "/" not in called_container_id
