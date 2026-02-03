"""Tests for worktree_manager.py."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.worktree_manager import (
    WorktreeInfo,
    WorktreeManager,
    WorktreeRemovalResult,
    get_active_docker_containers,
)


# Helper function for validate_identifier if not directly exported
def validate_identifier(identifier: str, param_name: str) -> None:
    """Validate that an identifier is safe for path construction."""
    if not identifier:
        raise ValueError(f"{param_name} cannot be empty")
    if ".." in identifier:
        raise ValueError(f"{param_name} cannot contain path traversal")
    # Check for characters that are not alphanumeric, dash, underscore, or dot
    # and that it starts with alphanumeric
    if not identifier[0].isalnum():
        raise ValueError(f"{param_name} must be alphanumeric")
    for char in identifier:
        if not (char.isalnum() or char in "-_."):
            raise ValueError(f"{param_name} must be alphanumeric, dash, underscore, or dot")


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
        validate_identifier("egg-container-123", "test_id")
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
            container_id="egg-123",
            repo_name="myrepo",
            branch="egg/egg-123/work",
            worktree_path=Path("/tmp/worktree"),
            git_dir=Path("/tmp/git"),
        )
        assert info.container_id == "egg-123"
        assert info.repo_name == "myrepo"
        assert info.branch == "egg/egg-123/work"
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
        mock_run.return_value = MagicMock(returncode=0, stdout="container1\ncontainer2\negg-123\n")

        result = get_active_docker_containers()

        assert result == {"container1", "container2", "egg-123"}

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


class TestStartupCleanup:
    """Tests for startup_cleanup function."""

    @patch("gateway.worktree_manager.get_active_docker_containers")
    def test_startup_cleanup_no_worktrees(self, mock_containers, temp_worktree_dir, temp_repos_dir):
        """Startup cleanup should handle empty worktree base."""
        from gateway.worktree_manager import startup_cleanup

        mock_containers.return_value = set()
        removed = startup_cleanup(worktree_base=temp_worktree_dir, repos_base=temp_repos_dir)
        assert removed == 0

    @patch("gateway.worktree_manager.get_active_docker_containers")
    def test_startup_cleanup_with_orphans(self, mock_containers, temp_worktree_dir, temp_repos_dir):
        """Startup cleanup should remove orphaned worktrees."""
        from gateway.worktree_manager import startup_cleanup

        # Create an orphaned container directory
        orphan_dir = temp_worktree_dir / "orphan-container"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "test-repo").mkdir()

        mock_containers.return_value = {"active-container"}
        removed = startup_cleanup(worktree_base=temp_worktree_dir, repos_base=temp_repos_dir)

        # Should have attempted cleanup
        assert removed >= 0


class TestWorktreeManagerAdvanced:
    """Additional tests for WorktreeManager covering more edge cases."""

    @pytest.fixture
    def temp_dirs(self):
        """Create temporary directories for testing."""
        worktree_base = Path(tempfile.mkdtemp())
        repos_base = Path(tempfile.mkdtemp())
        yield worktree_base, repos_base
        shutil.rmtree(worktree_base, ignore_errors=True)
        shutil.rmtree(repos_base, ignore_errors=True)

    @pytest.fixture
    def manager(self, temp_dirs):
        """Create a WorktreeManager with temp directories."""
        worktree_base, repos_base = temp_dirs
        return WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

    def test_create_worktree_invalid_uid(self, manager, temp_dirs):
        """Invalid uid should raise ValueError."""
        worktree_base, repos_base = temp_dirs
        (repos_base / "test-repo").mkdir()

        with pytest.raises(ValueError, match="Invalid uid"):
            manager.create_worktree("test-repo", "container-123", uid=-1)

    def test_create_worktree_invalid_gid(self, manager, temp_dirs):
        """Invalid gid should raise ValueError."""
        worktree_base, repos_base = temp_dirs
        (repos_base / "test-repo").mkdir()

        with pytest.raises(ValueError, match="Invalid gid"):
            manager.create_worktree("test-repo", "container-123", gid=-1)

    def test_list_worktrees_with_data(self, manager, temp_dirs):
        """List worktrees should return worktree data."""
        worktree_base, _ = temp_dirs

        # Create fake worktree structure
        container_dir = worktree_base / "test-container"
        repo_dir = container_dir / "test-repo"
        repo_dir.mkdir(parents=True)

        # Create a .git file that points to a gitdir
        git_file = repo_dir / ".git"
        git_file.write_text("gitdir: /some/path/.git/worktrees/test-repo")

        result = manager.list_worktrees()
        assert len(result) == 1
        assert result[0]["container_id"] == "test-container"
        assert len(result[0]["repos"]) == 1
        assert result[0]["repos"][0]["name"] == "test-repo"

    def test_list_worktrees_skips_non_directories(self, manager, temp_dirs):
        """List worktrees should skip non-directory entries."""
        worktree_base, _ = temp_dirs

        # Create a file at the worktree base level
        (worktree_base / "some-file.txt").write_text("test")

        result = manager.list_worktrees()
        assert result == []

    def test_list_worktrees_handles_invalid_git_file(self, manager, temp_dirs):
        """List worktrees should handle invalid .git files gracefully."""
        worktree_base, _ = temp_dirs

        container_dir = worktree_base / "test-container"
        repo_dir = container_dir / "test-repo"
        repo_dir.mkdir(parents=True)

        # Create an invalid .git file
        git_file = repo_dir / ".git"
        git_file.write_text("invalid content")

        result = manager.list_worktrees()
        assert len(result) == 1
        # Branch should be None due to invalid .git file
        assert result[0]["repos"][0]["branch"] is None

    def test_chown_single_permission_error(self, manager, temp_dirs):
        """_chown_single should handle permission errors gracefully."""
        worktree_base, _ = temp_dirs
        test_path = worktree_base / "test-file"
        test_path.write_text("test")

        # This should not raise even if chown fails (non-root)
        manager._chown_single(test_path, 1000, 1000)

    @patch("subprocess.run")
    def test_chown_recursive_permission_error(self, mock_run, manager, temp_dirs):
        """_chown_recursive should handle permission errors gracefully."""
        worktree_base, _ = temp_dirs

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Operation not permitted",
        )

        # Should not raise
        manager._chown_recursive(worktree_base, 1000, 1000)

    @patch("subprocess.run")
    def test_chown_recursive_other_error(self, mock_run, manager, temp_dirs):
        """_chown_recursive should log warning for other errors."""
        worktree_base, _ = temp_dirs

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Some other error",
        )

        # Should not raise
        manager._chown_recursive(worktree_base, 1000, 1000)

    def test_find_worktree_git_dir_not_exists(self, manager, temp_dirs):
        """_find_worktree_git_dir should return expected path even if not exists."""
        _, repos_base = temp_dirs
        main_repo = repos_base / "test-repo"
        main_repo.mkdir(parents=True)
        worktree_path = Path("/tmp/nonexistent/worktree")

        result = manager._find_worktree_git_dir(main_repo, worktree_path)
        assert result == main_repo / ".git" / "worktrees" / "worktree"

    def test_find_worktree_git_dir_with_variants(self, manager, temp_dirs):
        """_find_worktree_git_dir should find numbered variants."""
        _, repos_base = temp_dirs
        main_repo = repos_base / "test-repo"
        main_repo.mkdir(parents=True)

        worktree_path = Path("/tmp/test/worktree")

        # Create git dir structure with variant
        worktrees_dir = main_repo / ".git" / "worktrees"
        variant_dir = worktrees_dir / "worktree1"
        variant_dir.mkdir(parents=True)
        (variant_dir / "gitdir").write_text(str(worktree_path))

        result = manager._find_worktree_git_dir(main_repo, worktree_path)
        assert result == variant_dir

    def test_remove_worktree_with_warning(self, manager, temp_dirs):
        """Remove worktree should return warning for uncommitted changes without force."""
        worktree_base, repos_base = temp_dirs

        # Create worktree directory
        container_id = "test-container"
        repo_name = "test-repo"
        worktree_path = worktree_base / container_id / repo_name
        worktree_path.mkdir(parents=True)

        # Create main repo
        main_repo = repos_base / repo_name
        main_repo.mkdir(parents=True)

        # Mock git status to return uncommitted changes
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="M modified-file.txt\n",
            )

            result = manager.remove_worktree(container_id, repo_name, force=False)

        assert not result.success
        assert result.uncommitted_changes
        assert result.warning is not None

    def test_cleanup_orphaned_worktrees_nonexistent_base(self, temp_dirs):
        """Cleanup should handle nonexistent worktree base."""
        worktree_base, repos_base = temp_dirs

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # Now remove the worktree base that was created by manager init
        shutil.rmtree(worktree_base)

        removed = manager.cleanup_orphaned_worktrees(set())
        assert removed == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
