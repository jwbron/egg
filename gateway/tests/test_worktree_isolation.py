"""
Tests for per-agent worktree isolation (#1481).

Tests the list_worktrees_for_pipeline() method and verifies that
per-agent worktrees are correctly identified by pipeline prefix.
"""

import sys
import tempfile
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from worktree_manager import WorktreeInfo, WorktreeManager


@pytest.fixture
def temp_dirs():
    """Create temporary directories for worktree and repos base."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_base = Path(tmpdir) / "worktrees"
        repos_base = Path(tmpdir) / "repos"
        worktree_base.mkdir()
        repos_base.mkdir()
        yield worktree_base, repos_base


@pytest.fixture
def manager(temp_dirs):
    """Create a WorktreeManager with temp directories."""
    worktree_base, repos_base = temp_dirs
    return WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)


class TestListWorktreesForPipeline:
    """Tests for list_worktrees_for_pipeline()."""

    def test_empty_base_returns_empty(self, manager):
        """Empty worktree base should return empty list."""
        result = manager.list_worktrees_for_pipeline("issue-123")
        assert result == []

    def test_finds_pipeline_level_worktree(self, manager, temp_dirs):
        """Should find the pipeline-level worktree (worktree_id == pipeline_id)."""
        worktree_base, _ = temp_dirs
        # Create pipeline-level worktree directory
        (worktree_base / "issue-123" / "egg").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 1
        assert result[0].container_id == "issue-123"
        assert result[0].repo_name == "egg"

    def test_finds_per_agent_worktrees(self, manager, temp_dirs):
        """Should find per-agent worktrees with '{pipeline_id}-{role}' IDs."""
        worktree_base, _ = temp_dirs
        # Create per-agent worktree directories
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)
        (worktree_base / "issue-123-tester" / "egg").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 2

        container_ids = {wt.container_id for wt in result}
        assert "issue-123-coder" in container_ids
        assert "issue-123-tester" in container_ids

    def test_finds_all_worktrees_for_pipeline(self, manager, temp_dirs):
        """Should find both pipeline-level and per-agent worktrees."""
        worktree_base, _ = temp_dirs
        (worktree_base / "issue-123" / "egg").mkdir(parents=True)
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)
        (worktree_base / "issue-123-tester" / "egg").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 3

        container_ids = {wt.container_id for wt in result}
        assert "issue-123" in container_ids
        assert "issue-123-coder" in container_ids
        assert "issue-123-tester" in container_ids

    def test_excludes_other_pipelines(self, manager, temp_dirs):
        """Should not include worktrees from other pipelines."""
        worktree_base, _ = temp_dirs
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)
        (worktree_base / "issue-456-coder" / "egg").mkdir(parents=True)
        (worktree_base / "issue-456" / "egg").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 1
        assert result[0].container_id == "issue-123-coder"

    def test_multi_repo_worktrees(self, manager, temp_dirs):
        """Should find worktrees for multiple repos within same agent."""
        worktree_base, _ = temp_dirs
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)
        (worktree_base / "issue-123-coder" / "frontend").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 2

        repos = {wt.repo_name for wt in result}
        assert "egg" in repos
        assert "frontend" in repos

    def test_returns_worktree_info_type(self, manager, temp_dirs):
        """Returned items should be WorktreeInfo instances."""
        worktree_base, _ = temp_dirs
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 1
        assert isinstance(result[0], WorktreeInfo)
        assert result[0].worktree_path == worktree_base / "issue-123-coder" / "egg"
        assert result[0].git_dir is None

    def test_skips_non_directory_entries(self, manager, temp_dirs):
        """Should skip files in the worktree base directory."""
        worktree_base, _ = temp_dirs
        (worktree_base / "issue-123-coder" / "egg").mkdir(parents=True)
        # Create a file that matches the prefix
        (worktree_base / "issue-123-stale.lock").write_text("lock")

        result = manager.list_worktrees_for_pipeline("issue-123")
        assert len(result) == 1
        assert result[0].container_id == "issue-123-coder"

    def test_empty_worktree_base_returns_empty(self, temp_dirs):
        """Worktree base with no matching dirs should return empty list."""
        worktree_base, repos_base = temp_dirs
        # Create a dir for a different pipeline only
        (worktree_base / "issue-999-coder" / "egg").mkdir(parents=True)

        mgr = WorktreeManager(
            worktree_base=worktree_base,
            repos_base=repos_base,
        )
        result = mgr.list_worktrees_for_pipeline("issue-123")
        assert result == []
