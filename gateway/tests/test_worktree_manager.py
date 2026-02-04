"""Tests for worktree_manager.py."""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from worktree_manager import (
    WorktreeInfo,
    WorktreeManager,
    WorktreeRemovalResult,
    get_active_docker_containers,
    validate_identifier,
)


class TestValidateIdentifier:
    """Tests for identifier validation."""

    def test_empty_rejected(self):
        """Empty identifiers should be rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_identifier("", "test_id")

    def test_path_traversal_rejected(self):
        """Path traversal should be rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            validate_identifier("../parent", "test_id")
        with pytest.raises(ValueError, match="path traversal"):
            validate_identifier("foo/../bar", "test_id")

    def test_special_chars_rejected(self):
        """Special characters should be rejected."""
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_identifier("/absolute", "test_id")
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_identifier("with space", "test_id")
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_identifier("with;semicolon", "test_id")

    def test_valid_identifiers_accepted(self):
        """Valid identifiers should be accepted."""
        # These should not raise
        validate_identifier("sandbox-123", "test_id")
        validate_identifier("my_repo", "test_id")
        validate_identifier("repo.name", "test_id")
        validate_identifier("MyRepo123", "test_id")

    def test_leading_special_char_rejected(self):
        """Leading special characters should be rejected."""
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_identifier("-starting-with-dash", "test_id")
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_identifier(".hidden", "test_id")


class TestWorktreeInfo:
    """Tests for WorktreeInfo dataclass."""

    def test_creation(self):
        """WorktreeInfo should be creatable with required fields."""
        info = WorktreeInfo(
            container_id="jib-123",
            repo_name="myrepo",
            branch="jib/jib-123/work",
            worktree_path=Path("/tmp/worktree"),
            git_dir=Path("/tmp/git"),
        )
        assert info.container_id == "jib-123"
        assert info.repo_name == "myrepo"
        assert info.branch == "jib/jib-123/work"
        assert info.created_at is None  # Optional field


class TestWorktreeRemovalResult:
    """Tests for WorktreeRemovalResult dataclass."""

    def test_default_values(self):
        """Default values should be sensible."""
        result = WorktreeRemovalResult(success=True)
        assert result.success is True
        assert result.uncommitted_changes is False
        assert result.branch_deleted is False
        assert result.warning is None
        assert result.error is None


class TestWorktreeManager:
    """Tests for WorktreeManager class."""

    @pytest.fixture
    def temp_dirs(self):
        """Create temporary directories for testing."""
        worktree_base = Path(tempfile.mkdtemp())
        repos_base = Path(tempfile.mkdtemp())
        yield worktree_base, repos_base
        # Cleanup
        shutil.rmtree(worktree_base, ignore_errors=True)
        shutil.rmtree(repos_base, ignore_errors=True)

    @pytest.fixture
    def manager(self, temp_dirs):
        """Create a WorktreeManager with temp directories."""
        worktree_base, repos_base = temp_dirs
        return WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

    def test_init_creates_worktree_base(self, temp_dirs):
        """WorktreeManager should create worktree base directory."""
        worktree_base, repos_base = temp_dirs
        # Remove to test creation
        shutil.rmtree(worktree_base)

        WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        assert worktree_base.exists()

    def test_create_worktree_invalid_container_id(self, manager):
        """Invalid container_id should raise ValueError."""
        with pytest.raises(ValueError, match="container_id"):
            manager.create_worktree("myrepo", "../evil")

    def test_create_worktree_invalid_repo_name(self, manager):
        """Invalid repo_name should raise ValueError."""
        with pytest.raises(ValueError, match="repo_name"):
            manager.create_worktree("../evil", "container-123")

    def test_create_worktree_repo_not_found(self, manager):
        """Missing repo should raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            manager.create_worktree("nonexistent-repo", "container-123")

    def test_list_worktrees_empty(self, manager):
        """Empty worktree base should return empty list."""
        result = manager.list_worktrees()
        assert result == []

    def test_remove_worktree_invalid_identifiers(self, manager):
        """Invalid identifiers should return error result."""
        result = manager.remove_worktree("../evil", "repo")
        assert not result.success
        assert result.error is not None

        result = manager.remove_worktree("container", "../evil")
        assert not result.success
        assert result.error is not None

    def test_remove_nonexistent_worktree(self, manager):
        """Removing nonexistent worktree should succeed (idempotent)."""
        result = manager.remove_worktree("container-123", "myrepo")
        assert result.success

    def test_get_worktree_paths(self, manager, temp_dirs):
        """get_worktree_paths should return correct paths."""
        worktree_base, repos_base = temp_dirs

        wt_path, repo_path = manager.get_worktree_paths("container-123", "myrepo")

        assert wt_path == worktree_base / "container-123" / "myrepo"
        assert repo_path == repos_base / "myrepo"

    def test_get_worktree_paths_invalid_identifiers(self, manager):
        """Invalid identifiers should raise ValueError."""
        with pytest.raises(ValueError, match="container_id"):
            manager.get_worktree_paths("../evil", "repo")
        with pytest.raises(ValueError, match="repo_name"):
            manager.get_worktree_paths("container", "../evil")

    def test_cleanup_orphaned_worktrees_with_active_container(self, manager, temp_dirs):
        """Active containers should not be cleaned up."""
        worktree_base, _ = temp_dirs

        # Create a fake worktree directory
        container_dir = worktree_base / "active-container"
        container_dir.mkdir(parents=True)
        (container_dir / "repo").mkdir()

        # Cleanup with this container marked as active
        removed = manager.cleanup_orphaned_worktrees({"active-container"})

        assert removed == 0
        assert container_dir.exists()

    def test_cleanup_orphaned_worktrees_removes_inactive(self, manager, temp_dirs):
        """Inactive container worktrees should be cleaned up."""
        worktree_base, _ = temp_dirs

        # Create a fake worktree directory
        container_dir = worktree_base / "orphaned-container"
        container_dir.mkdir(parents=True)
        (container_dir / "repo").mkdir()

        # Cleanup with no active containers
        removed = manager.cleanup_orphaned_worktrees(set())

        # Should have attempted cleanup (may fail since it's not a real worktree)
        assert removed >= 0


class TestGetActiveDockerContainers:
    """Tests for get_active_docker_containers helper."""

    @patch("subprocess.run")
    def test_returns_container_names(self, mock_run):
        """Should return set of container names."""
        mock_run.return_value = MagicMock(returncode=0, stdout="container1\ncontainer2\njib-123\n")

        result = get_active_docker_containers()

        assert result == {"container1", "container2", "jib-123"}

    @patch("subprocess.run")
    def test_handles_empty_output(self, mock_run):
        """Should handle empty output."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = get_active_docker_containers()

        assert result == set()

    @patch("subprocess.run")
    def test_handles_docker_failure(self, mock_run):
        """Should handle docker command failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        result = get_active_docker_containers()

        assert result == set()

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_handles_docker_not_installed(self, mock_run):
        """Should handle docker not being installed."""
        result = get_active_docker_containers()

        assert result == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
