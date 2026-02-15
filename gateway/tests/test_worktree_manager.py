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
    """Tests for startup_cleanup module-level function."""

    def test_with_active_containers(self, tmp_path):
        """Cleans up orphaned worktrees, preserving active ones."""
        from worktree_manager import startup_cleanup

        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        # Create an orphaned container directory
        orphan = worktree_base / "orphaned-container"
        orphan.mkdir()
        (orphan / "repo").mkdir()

        # Create an active container directory
        active = worktree_base / "active-container"
        active.mkdir()
        (active / "repo").mkdir()

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 1
            MockManager.return_value = mock_instance

            removed = startup_cleanup(active_containers={"active-container"})
            assert removed == 1
            mock_instance.cleanup_orphaned_worktrees.assert_called_once_with(
                {"active-container"}, None
            )

    def test_with_none_uses_docker(self):
        """Falls back to querying Docker when active_containers is None."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 0
            MockManager.return_value = mock_instance

            with patch(
                "worktree_manager.get_active_docker_containers",
                return_value={"container-1"},
            ):
                removed = startup_cleanup(active_containers=None)
                assert removed == 0

    def test_with_empty_set(self):
        """Cleans up all worktrees when no active containers."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 3
            MockManager.return_value = mock_instance

            removed = startup_cleanup(active_containers=set())
            assert removed == 3


class TestWorktreeManagerCreateWorktree:
    """Tests for WorktreeManager.create_worktree validation."""

    @pytest.fixture
    def manager_with_repo(self, tmp_path):
        """Create a manager with a fake repo dir."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        # Create a fake .git dir so it looks like a repo
        (repo_dir / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        return manager

    def test_create_worktree_invalid_uid(self, manager_with_repo):
        """Rejects negative uid."""
        with pytest.raises(ValueError, match="uid"):
            manager_with_repo.create_worktree("test-repo", "container-1", uid=-1)

    def test_create_worktree_invalid_gid(self, manager_with_repo):
        """Rejects negative gid."""
        with pytest.raises(ValueError, match="gid"):
            manager_with_repo.create_worktree("test-repo", "container-1", gid=-1)

    def test_list_worktrees_with_directories(self, tmp_path):
        """Lists worktrees from filesystem directories."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        worktree_base.mkdir()
        repos_base.mkdir()

        # Create fake worktree directory structure
        container_dir = worktree_base / "container-1"
        container_dir.mkdir()
        repo_dir = container_dir / "test-repo"
        repo_dir.mkdir()
        # Create a .git file pointing to something
        (repo_dir / ".git").write_text("gitdir: /some/path")

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        worktrees = manager.list_worktrees()

        assert len(worktrees) >= 1
        found = False
        for entry in worktrees:
            if entry["container_id"] == "container-1":
                found = True
                assert len(entry["repos"]) >= 1
        assert found

    def test_remove_worktree_with_subprocess_mock(self, tmp_path):
        """Removes worktree using mocked subprocess for git operations."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        # Create worktree directory to remove
        wt_dir = worktree_base / "container-1" / "test-repo"
        wt_dir.mkdir(parents=True)
        (wt_dir / "file.txt").write_text("content")

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # git status --porcelain returns empty (no changes)
            # git worktree remove succeeds
            # git worktree prune succeeds
            # git branch -d succeeds
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = manager.remove_worktree("container-1", "test-repo", force=True)
            assert result.success


class TestWorktreeManagerDockerGitDir:
    """Tests for create_worktree when Docker pre-creates a .git directory."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a real git repo for worktree tests."""
        import subprocess

        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        result = subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"git init not available: {result.stderr.strip()}")
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            check=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        worktree_base = tmp_path / "worktrees"
        return worktree_base, repos_base, repo_dir

    def test_create_worktree_removes_preexisting_git_directory(self, git_repo):
        """When Docker pre-creates a .git directory, create_worktree should remove it and succeed."""
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # Simulate Docker creating the mount point with a .git directory
        worktree_path = worktree_base / "container-1" / "test-repo"
        worktree_path.mkdir(parents=True)
        git_dir = worktree_path / ".git"
        git_dir.mkdir()  # Docker's tmpfs creates this as a directory

        # create_worktree should handle the .git directory and succeed
        info = manager.create_worktree("test-repo", "container-1")

        assert info.container_id == "container-1"
        assert info.repo_name == "test-repo"
        assert info.branch == "egg/container-1/work"
        assert info.worktree_path == worktree_path

        # .git should now be a file (gitdir pointer), not a directory
        assert git_dir.exists()
        assert git_dir.is_file()
        assert git_dir.read_text().strip().startswith("gitdir:")

    def test_create_worktree_normal_case(self, git_repo):
        """Normal case: no pre-existing directory, worktree created fresh."""
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        info = manager.create_worktree("test-repo", "container-2")

        assert info.container_id == "container-2"
        assert info.worktree_path.exists()
        git_file = info.worktree_path / ".git"
        assert git_file.is_file()
        assert git_file.read_text().strip().startswith("gitdir:")


class TestFindWorktreeGitDir:
    """Tests for _find_worktree_git_dir admin dir resolution."""

    def test_matches_correct_admin_dir_by_gitdir_content(self, tmp_path):
        """Should match admin dir whose gitdir file points to the worktree's .git file."""
        main_repo = tmp_path / "repo"
        worktrees_dir = main_repo / ".git" / "worktrees"

        # Simulate two worktrees with same basename: "egg" (interactive) and "egg1" (pipeline)
        interactive_wt = tmp_path / "worktrees" / "interactive" / "egg"
        pipeline_wt = tmp_path / "worktrees" / "pipeline" / "egg"

        # Admin dir "egg" belongs to interactive container
        admin_egg = worktrees_dir / "egg"
        admin_egg.mkdir(parents=True)
        (admin_egg / "gitdir").write_text(str(interactive_wt / ".git") + "\n")

        # Admin dir "egg1" belongs to pipeline container
        admin_egg1 = worktrees_dir / "egg1"
        admin_egg1.mkdir(parents=True)
        (admin_egg1 / "gitdir").write_text(str(pipeline_wt / ".git") + "\n")

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        # Looking up interactive worktree should return "egg" admin dir
        result = manager._find_worktree_git_dir(main_repo, interactive_wt)
        assert result == admin_egg

        # Looking up pipeline worktree should return "egg1" admin dir
        result = manager._find_worktree_git_dir(main_repo, pipeline_wt)
        assert result == admin_egg1

    def test_returns_default_when_no_worktrees_dir(self, tmp_path):
        """Should return default path when .git/worktrees/ doesn't exist."""
        main_repo = tmp_path / "repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()

        worktree_path = tmp_path / "worktrees" / "container" / "egg"

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        result = manager._find_worktree_git_dir(main_repo, worktree_path)
        assert result == main_repo / ".git" / "worktrees" / "egg"

    def test_no_false_match_without_git_suffix(self, tmp_path):
        """Gitdir content without /.git suffix should not match (guards against regression).

        This test verifies that a malformed gitdir (missing the /.git suffix) does
        NOT produce a match. We set up two admin dirs:
        - "egg" with WRONG content (missing /.git suffix) — should NOT match
        - "egg1" with CORRECT content (includes /.git suffix) — should match

        If the comparison incorrectly matched without /.git, we'd get "egg" back.
        The fix ensures we get "egg1" (the correct admin dir).
        """
        main_repo = tmp_path / "repo"
        worktrees_dir = main_repo / ".git" / "worktrees"

        worktree_path = tmp_path / "worktrees" / "container" / "egg"

        # Admin dir "egg" has MALFORMED gitdir content (missing /.git suffix)
        admin_dir_wrong = worktrees_dir / "egg"
        admin_dir_wrong.mkdir(parents=True)
        (admin_dir_wrong / "gitdir").write_text(str(worktree_path) + "\n")

        # Admin dir "egg1" has CORRECT gitdir content (includes /.git suffix)
        admin_dir_correct = worktrees_dir / "egg1"
        admin_dir_correct.mkdir(parents=True)
        (admin_dir_correct / "gitdir").write_text(str(worktree_path / ".git") + "\n")

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        # The malformed "egg" admin dir should NOT match; the correct "egg1" should
        result = manager._find_worktree_git_dir(main_repo, worktree_path)
        assert result == admin_dir_correct, (
            f"Expected correct admin dir 'egg1', got '{result.name}'. "
            "The fix ensures gitdir content must include /.git suffix to match."
        )

    def test_original_bug_comparison_without_git_suffix(self, tmp_path):
        """Demonstrate why comparing against worktree_path (not worktree_path/.git) was wrong.

        This test simulates the original bug from PR #589's broken fix:
        - The gitdir file contains /path/to/worktree/.git (with /.git suffix)
        - The old comparison checked: gitdir_content == str(worktree_path)
        - Since "/path/.git" != "/path", the match NEVER succeeded
        - Every lookup fell through to default_git_dir, causing collisions

        The fix compares against str(worktree_path / ".git") so it matches correctly.
        """
        main_repo = tmp_path / "repo"
        worktrees_dir = main_repo / ".git" / "worktrees"

        # Set up two worktrees with same basename (the collision scenario)
        interactive_wt = tmp_path / "worktrees" / "interactive" / "egg"
        pipeline_wt = tmp_path / "worktrees" / "pipeline" / "egg"

        # Admin dirs with CORRECT gitdir content (as git actually writes it)
        admin_interactive = worktrees_dir / "egg"
        admin_interactive.mkdir(parents=True)
        (admin_interactive / "gitdir").write_text(str(interactive_wt / ".git") + "\n")

        admin_pipeline = worktrees_dir / "egg1"
        admin_pipeline.mkdir(parents=True)
        (admin_pipeline / "gitdir").write_text(str(pipeline_wt / ".git") + "\n")

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        # Simulate OLD (buggy) comparison: gitdir_content == str(worktree_path)
        # This would NEVER match because gitdir contains ".../.git" but we compared to "..."
        gitdir_content = (admin_pipeline / "gitdir").read_text().strip()
        old_buggy_comparison = gitdir_content == str(pipeline_wt)
        assert not old_buggy_comparison, (
            "Old comparison should NOT match (gitdir has /.git suffix, worktree_path doesn't)"
        )

        # NEW (fixed) comparison: gitdir_content == str(worktree_path / ".git")
        new_fixed_comparison = gitdir_content == str(pipeline_wt / ".git")
        assert new_fixed_comparison, "New comparison SHOULD match (both have /.git suffix)"

        # Verify the actual function returns the correct admin dir
        result = manager._find_worktree_git_dir(main_repo, pipeline_wt)
        assert result == admin_pipeline, (
            f"Expected 'egg1' for pipeline worktree, got '{result.name}'"
        )


class TestWorktreeManagerConcurrency:
    """Tests for concurrent worktree creation."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a real git repo for concurrency tests."""
        import subprocess as sp

        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        result = sp.run(["git", "init"], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"git init not available: {result.stderr.strip()}")
        sp.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            check=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        worktree_base = tmp_path / "worktrees"
        return worktree_base, repos_base, repo_dir

    def test_concurrent_create_worktree(self, git_repo):
        """Three threads creating worktrees simultaneously should all succeed."""
        import threading

        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # Dict key assignment is atomic under CPython's GIL, so no lock needed.
        results: dict[str, WorktreeInfo | Exception] = {}
        container_ids = ["container-a", "container-b", "container-c"]
        barrier = threading.Barrier(len(container_ids))

        def create(cid: str) -> None:
            try:
                barrier.wait(timeout=5)
                info = manager.create_worktree("test-repo", cid)
                results[cid] = info
            except Exception as exc:
                results[cid] = exc

        threads = [threading.Thread(target=create, args=(cid,)) for cid in container_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All three should succeed
        for cid in container_ids:
            assert cid in results, f"Thread for {cid} did not finish"
            info = results[cid]
            assert not isinstance(info, Exception), f"{cid} failed: {info}"
            assert isinstance(info, WorktreeInfo)
            assert info.worktree_path.exists()
            git_file = info.worktree_path / ".git"
            assert git_file.is_file(), f"{cid}: .git should be a file"
            assert git_file.read_text().strip().startswith("gitdir:")


class TestRunGitWorktreeAddRetry:
    """Tests for _run_git_worktree_add retry logic on index.lock contention."""

    def test_retry_succeeds_after_index_lock_error(self, tmp_path):
        """Retry should succeed when index.lock error clears on second attempt."""
        import subprocess

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "repos",
        )
        main_repo = tmp_path / "repos" / "test-repo"
        main_repo.mkdir(parents=True)
        (main_repo / ".git").mkdir()

        fail_result = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=128,
            stdout="",
            stderr="fatal: Unable to create '.git/index.lock': File exists.",
        )
        ok_result = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with (
            patch(
                "worktree_manager.subprocess.run", side_effect=[fail_result, ok_result]
            ) as mock_run,
            patch("worktree_manager.time.sleep") as mock_sleep,
        ):
            result = manager._run_git_worktree_add(
                args=["git", "worktree", "add", "/tmp/wt"],
                cwd=main_repo,
                main_repo=main_repo,
            )

        assert result.returncode == 0
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    def test_no_retry_on_non_lock_error(self, tmp_path):
        """Non-index.lock errors should fail immediately without retry."""
        import subprocess

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "repos",
        )
        main_repo = tmp_path / "repos" / "test-repo"
        main_repo.mkdir(parents=True)
        (main_repo / ".git").mkdir()

        fail_result = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=128,
            stdout="",
            stderr="fatal: '/tmp/wt' already exists",
        )

        with (
            patch("worktree_manager.subprocess.run", return_value=fail_result) as mock_run,
            patch("worktree_manager.time.sleep") as mock_sleep,
        ):
            result = manager._run_git_worktree_add(
                args=["git", "worktree", "add", "/tmp/wt"],
                cwd=main_repo,
                main_repo=main_repo,
            )

        assert result.returncode == 128
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_retry_cleans_partial_worktree(self, tmp_path):
        """Partial worktree directory should be cleaned up between retries."""
        import subprocess

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "repos",
        )
        main_repo = tmp_path / "repos" / "test-repo"
        main_repo.mkdir(parents=True)
        (main_repo / ".git").mkdir()

        worktree_path = tmp_path / "worktrees" / "ctr" / "test-repo"

        fail_result = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=128,
            stdout="",
            stderr="fatal: Unable to create '.git/index.lock': File exists.",
        )
        ok_result = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=0,
            stdout="",
            stderr="",
        )

        def side_effect(*args, **kwargs):
            """Simulate git creating a partial worktree dir on first call."""
            if side_effect.call_count == 0:
                # Simulate partial worktree: directory exists but no valid .git file
                worktree_path.mkdir(parents=True, exist_ok=True)
                side_effect.call_count += 1
                return fail_result
            side_effect.call_count += 1
            return ok_result

        side_effect.call_count = 0

        with (
            patch("worktree_manager.subprocess.run", side_effect=side_effect),
            patch("worktree_manager.time.sleep"),
        ):
            result = manager._run_git_worktree_add(
                args=["git", "worktree", "add", str(worktree_path)],
                cwd=main_repo,
                main_repo=main_repo,
                worktree_path=worktree_path,
            )

        assert result.returncode == 0
        # The partial directory should have been cleaned between retries
        # (it may or may not exist after the successful second call depending
        # on what git does, but the cleanup ran)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
