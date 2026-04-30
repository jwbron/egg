"""Tests for worktree_manager.py."""

import shutil
import subprocess
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
    validate_branch_ref,
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


class TestValidateBranchRef:
    """Tests for branch ref validation."""

    @pytest.mark.parametrize(
        "value",
        ["main", "egg/issue-1495", "origin/egg/issue-1495", "HEAD", "v1.0.0", "my-branch"],
    )
    def test_valid_refs_accepted(self, value):
        validate_branch_ref(value, "base_branch")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_branch_ref("", "base_branch")

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="'\\.\\.' not allowed"):
            validate_branch_ref("../etc/passwd", "base_branch")

    def test_null_bytes_rejected(self):
        with pytest.raises(ValueError, match="null bytes not allowed"):
            validate_branch_ref("foo\x00bar", "base_branch")

    def test_consecutive_slashes_rejected(self):
        with pytest.raises(ValueError, match="consecutive slashes"):
            validate_branch_ref("a//b", "base_branch")

    def test_trailing_slash_rejected(self):
        with pytest.raises(ValueError, match="cannot end with"):
            validate_branch_ref("egg/branch/", "base_branch")

    def test_trailing_dot_rejected(self):
        with pytest.raises(ValueError, match="cannot end with"):
            validate_branch_ref("egg/branch.", "base_branch")

    def test_component_starting_with_dot_rejected(self):
        with pytest.raises(ValueError, match="component cannot start with"):
            validate_branch_ref("egg/.hidden", "base_branch")

    @pytest.mark.parametrize("value", ["with space", "semi;colon", "back`tick", "$(cmd)"])
    def test_special_chars_rejected(self, value):
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_branch_ref(value, "base_branch")

    def test_leading_special_char_rejected(self):
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_branch_ref(".hidden", "base_branch")
        with pytest.raises(ValueError, match="must be alphanumeric"):
            validate_branch_ref("-dashed", "base_branch")


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

    def test_cleanup_skips_in_flight_worktrees(self, manager, temp_dirs):
        """Worktrees tracked in ``_active_worktrees`` must survive cleanup.

        Regression for #1874: a worktree just created by this gateway but
        whose session has not yet been registered (so its container_id is
        absent from ``active_containers``) was being wiped when startup or
        prune cleanup raced with spawn.  ``_active_worktrees`` now shields
        in-process worktrees from the sweep.
        """
        worktree_base, _ = temp_dirs

        container_dir = worktree_base / "issue-1758-again-coder"
        container_dir.mkdir(parents=True)
        (container_dir / "webapp").mkdir()

        info = WorktreeInfo(
            container_id="issue-1758-again-coder",
            repo_name="webapp",
            branch="egg/issue-1758-again-coder/work",
            worktree_path=container_dir / "webapp",
            git_dir=None,
        )
        manager._active_worktrees["issue-1758-again-coder"] = [info]

        # Session not yet registered — only the unrelated container is
        # in the active set.  Worktree must still survive.
        removed = manager.cleanup_orphaned_worktrees({"egg-agent-overseer"})

        assert removed == 0
        assert container_dir.exists()


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

    def test_create_worktree_locks_worktree(self, git_repo):
        """Worktree should be locked after creation to prevent gc prune."""
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        info = manager.create_worktree("test-repo", "container-lock")

        # Admin dir should have a 'locked' file
        assert (info.git_dir / "locked").exists()

    def test_remove_locked_worktree(self, git_repo):
        """Removing a locked worktree should succeed (--force --force)."""
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        info = manager.create_worktree("test-repo", "container-rm")
        assert info.worktree_path.exists()
        assert (info.git_dir / "locked").exists()

        result = manager.remove_worktree("container-rm", "test-repo", force=True)

        assert result.success
        assert not info.worktree_path.exists()
        assert not info.git_dir.exists()

    def test_create_worktree_cleans_stale_admin_dir(self, git_repo):
        """When a stale git admin dir exists from a broken previous worktree,
        create_worktree should clean it up before recreating.

        Regression test for #1723: after restart_phase, a stale btrfs mount
        leaves an invalid worktree directory AND a stale admin dir in
        .git/worktrees/.  Without admin dir cleanup, ``git worktree add``
        fails with "already registered".
        """
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # First, create a valid worktree to populate the admin dir
        info1 = manager.create_worktree("test-repo", "stale-container")
        assert info1.worktree_path.exists()
        admin_dir = info1.git_dir
        assert admin_dir.exists()

        # Simulate a broken state: remove the worktree directory but leave the
        # git admin dir (as happens when a btrfs mount is removed externally
        # but git state is not cleaned up).
        shutil.rmtree(info1.worktree_path)
        # Re-create the worktree path as an empty directory (simulating a
        # broken mount point that exists but has no valid .git file).
        info1.worktree_path.mkdir(parents=True)

        # Admin dir still exists (stale)
        assert admin_dir.exists()

        # create_worktree should clean up the stale admin dir and succeed
        info2 = manager.create_worktree("test-repo", "stale-container")

        assert info2.worktree_path.exists()
        assert info2.git_dir.exists()
        git_file = info2.worktree_path / ".git"
        assert git_file.is_file()
        assert git_file.read_text().strip().startswith("gitdir:")

    def test_create_worktree_configures_push_upstream(self, git_repo):
        """When assigned_branch is set, branch.<local>.merge should point at it.

        Regression test for #1809.  Without this config, the sandbox's push
        client falls back to pushing the local branch name, which the
        gateway rejects as push_denied_wrong_branch.  Agents sometimes
        "recover" from that rejection with ``git reset --hard`` and
        destroy their committed work.
        """
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        info = manager.create_worktree(
            "test-repo",
            "issue-42-coder",
            assigned_branch="egg/issue-42",
        )
        assert info.branch == "egg/issue-42-coder/work"

        # Config is keyed by the per-worktree local branch name; values
        # make the sandbox's push client build ``<local>:egg/issue-42``.
        remote = subprocess.run(
            ["git", "-C", str(repo_dir), "config", "branch.egg/issue-42-coder/work.remote"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert remote.stdout.strip() == "origin"
        merge = subprocess.run(
            ["git", "-C", str(repo_dir), "config", "branch.egg/issue-42-coder/work.merge"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert merge.stdout.strip() == "refs/heads/egg/issue-42"

    def test_create_worktree_leaves_upstream_alone_when_assigned_branch_absent(self, git_repo):
        """Without assigned_branch, the gateway does not touch branch.<local>.merge.

        Git's own ``worktree add -b`` may auto-set tracking against HEAD,
        and non-pipeline callers rely on that default.  The fix should
        only act when the caller explicitly passes assigned_branch.
        """
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        manager.create_worktree("test-repo", "nonpipe-container")

        result = subprocess.run(
            ["git", "-C", str(repo_dir), "config", "branch.egg/nonpipe-container/work.merge"],
            capture_output=True,
            text=True,
            check=False,
        )
        # If git set tracking on its own (branching from master/main), that
        # value must not be overwritten with our pipeline refspec.
        if result.returncode == 0:
            assert not result.stdout.strip().startswith("refs/heads/egg/")

    def test_worktree_reuse_resets_to_safe_remote_ref(self, tmp_path):
        """When a valid worktree is reused, the helper resets HEAD to a
        known-good remote ref so a stale local HEAD from a prior pipeline
        run on the same container_id (deterministic ID collision, see
        #2222) doesn't get inherited.

        Reproduction of the pre-fix shape:
        - First create_worktree creates the worktree at origin/main.
        - Test simulates yesterday's pipeline by adding a local-only
          commit on top of the worktree HEAD.
        - Second create_worktree (same container_id, with
          assigned_branch=egg/issue-99 which exists on origin) must
          reset HEAD to origin/egg/issue-99 — discarding the stale local
          commit.
        """
        import subprocess as sp

        env = {
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        # Bare origin remote with main + a feature branch.
        origin = tmp_path / "origin.git"
        sp.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)

        seed = tmp_path / "seed"
        seed.mkdir()
        sp.run(["git", "init", "-b", "main"], cwd=seed, check=True)
        sp.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
        sp.run(["git", "config", "user.name", "test"], cwd=seed, check=True)
        (seed / "README.md").write_text("init\n")
        sp.run(["git", "add", "."], cwd=seed, check=True)
        sp.run(["git", "commit", "-m", "init"], cwd=seed, check=True, env=env)
        sp.run(["git", "push", "origin", "main"], cwd=seed, check=True)
        sp.run(["git", "checkout", "-b", "egg/issue-99"], cwd=seed, check=True)
        (seed / "feature.md").write_text("feature\n")
        sp.run(["git", "add", "."], cwd=seed, check=True)
        sp.run(["git", "commit", "-m", "feature"], cwd=seed, check=True, env=env)
        sp.run(["git", "push", "origin", "egg/issue-99"], cwd=seed, check=True)
        feature_sha = sp.run(
            ["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True, text=True, check=True
        ).stdout.strip()

        # repos_base hosts a clone of the bare origin so create_worktree
        # has somewhere to ``git worktree add`` from.
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        sp.run(["git", "clone", str(origin), str(repo_dir)], check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
        sp.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
        sp.run(["git", "fetch", "origin", "egg/issue-99"], cwd=repo_dir, check=True)

        worktree_base = tmp_path / "worktrees"
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # First creation — bind the worktree to origin/egg/issue-99.
        info1 = manager.create_worktree(
            "test-repo",
            "issue-99-coder",
            assigned_branch="egg/issue-99",
        )
        wt_path = info1.worktree_path
        sp.run(
            ["git", "-C", str(wt_path), "reset", "--hard", "origin/egg/issue-99"],
            check=True,
        )

        # Simulate yesterday's pipeline leaving a local-only commit on top
        # of origin/egg/issue-99.  Without the reset on reuse, the new
        # pipeline would inherit this HEAD.
        (wt_path / "stale_local.md").write_text("yesterday's leftover\n")
        sp.run(["git", "-C", str(wt_path), "add", "."], check=True)
        sp.run(
            ["git", "-C", str(wt_path), "commit", "-m", "stale local-only"],
            check=True,
            env=env,
        )
        stale_head = sp.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert stale_head != feature_sha, "precondition: stale HEAD must differ from origin tip"

        # Reuse: second create_worktree with the same container_id.
        info2 = manager.create_worktree(
            "test-repo",
            "issue-99-coder",
            assigned_branch="egg/issue-99",
        )
        assert info2.worktree_path == wt_path

        # After reuse, HEAD must be back at origin/egg/issue-99 — the
        # stale local commit has been discarded.
        head_after = sp.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == feature_sha, (
            f"reuse should reset HEAD to origin/egg/issue-99 ({feature_sha}); "
            f"got {head_after} (still on stale local commit)"
        )

    def test_create_worktree_reapplies_upstream_on_reuse(self, git_repo):
        """When a valid worktree is reused, upstream config is re-applied.

        Restart paths (e.g. restart_agent_job) call create_worktree against
        an existing per-agent worktree.  If the assigned branch changes or
        the config was never set (pre-fix worktree), reuse must still end
        with the correct upstream wired up.
        """
        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # Initial creation without assigned_branch — no upstream written.
        info1 = manager.create_worktree("test-repo", "reuse-container")
        assert info1.worktree_path.exists()

        # Second call with assigned_branch — early-return path must set config.
        info2 = manager.create_worktree(
            "test-repo",
            "reuse-container",
            assigned_branch="egg/issue-99",
        )
        assert info2.worktree_path == info1.worktree_path

        merge = subprocess.run(
            ["git", "-C", str(repo_dir), "config", "branch.egg/reuse-container/work.merge"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert merge.stdout.strip() == "refs/heads/egg/issue-99"


class TestLookupWorktree:
    """Tests for WorktreeManager.lookup_worktree (#1857).

    lookup_worktree exists so that session_create can reuse a worktree
    made by a prior /api/v1/worktrees/create call instead of racing to
    create its own on the same bare repo's ``.git/config.lock``.
    """

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Same real-repo fixture as TestWorktreeManagerDockerGitDir."""
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

    def test_lookup_returns_info_for_existing_worktree(self, git_repo):
        """Lookup returns WorktreeInfo with the same paths the creator saw."""
        worktree_base, repos_base, _ = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        created = manager.create_worktree("test-repo", "pipe-1-coder")
        looked_up = manager.lookup_worktree("test-repo", "pipe-1-coder")

        assert looked_up.container_id == "pipe-1-coder"
        assert looked_up.repo_name == "test-repo"
        assert looked_up.worktree_path == created.worktree_path
        assert looked_up.branch == created.branch
        assert looked_up.git_dir == created.git_dir

    def test_lookup_raises_when_worktree_missing(self, git_repo):
        """Lookup must fail loudly rather than silently return a fake path —
        otherwise a misconfigured caller would get a session pointing at
        a non-existent worktree."""
        worktree_base, repos_base, _ = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with pytest.raises(ValueError, match="Worktree not found"):
            manager.lookup_worktree("test-repo", "never-created")

    def test_lookup_raises_when_directory_exists_but_not_a_worktree(self, git_repo):
        """An empty directory at the expected path shouldn't be treated as
        a valid worktree — the .git file gating in create_worktree is the
        same invariant we rely on here."""
        worktree_base, repos_base, _ = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        bogus = worktree_base / "broken" / "test-repo"
        bogus.mkdir(parents=True)

        with pytest.raises(ValueError, match="Worktree not found"):
            manager.lookup_worktree("test-repo", "broken")

    def test_lookup_rejects_path_traversal(self, tmp_path):
        """Identifier validation applies to lookup the same way it applies
        to create_worktree."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with pytest.raises(ValueError, match="container_id"):
            manager.lookup_worktree("test-repo", "../evil")
        with pytest.raises(ValueError, match="repo_name"):
            manager.lookup_worktree("../evil", "container-1")

    def test_lookup_raises_when_repo_missing(self, tmp_path):
        """Looking up a worktree for a repo that doesn't exist fails before
        any filesystem check on the worktree path itself."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with pytest.raises(ValueError, match="Repository not found"):
            manager.lookup_worktree("missing-repo", "container-1")


class TestWorktreeManagerRemoteBranchFetch:
    """Tests for create_worktree fetching remote branches that don't exist locally."""

    @pytest.fixture
    def manager_with_repo(self, tmp_path):
        """Create manager with a fake repo that has a .git directory."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        return manager, repos_base, repo_dir, worktree_base

    def test_fetches_remote_when_base_branch_not_local(self, manager_with_repo):
        """When base_branch doesn't exist locally, fetch from origin and use origin/<branch>."""
        manager, repos_base, repo_dir, worktree_base = manager_with_repo

        call_log = []

        def mock_run(args, **kwargs):
            call_log.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""

            if "rev-parse" in args and "--verify" in args:
                # Neither branch_name nor base_branch exist locally
                result.returncode = 1
            elif "fetch" in args and "origin" in args:
                result.returncode = 0
            elif "worktree" in args and "add" in args:
                # Simulate successful worktree add
                wt_path = None
                for i, a in enumerate(args):
                    if a == "-b" and i + 2 < len(args):
                        wt_path = Path(args[i + 2])
                        break
                if wt_path:
                    wt_path.mkdir(parents=True, exist_ok=True)
                    git_file = wt_path / ".git"
                    git_file.write_text("gitdir: /fake/git/dir")
                result.returncode = 0
            elif "worktree" in args and "lock" in args:
                result.returncode = 0

            return result

        with patch("subprocess.run", side_effect=mock_run):
            with patch.object(
                manager, "_find_worktree_git_dir", return_value=Path("/fake/git/dir")
            ):
                with patch.object(manager, "_chown_recursive"):
                    with patch.object(manager, "_chown_single"):
                        info = manager.create_worktree(
                            "test-repo", "issue-1495-coder", base_branch="egg/issue-1495"
                        )

        assert info.container_id == "issue-1495-coder"
        # Verify fetch was called
        fetch_calls = [c for c in call_log if "fetch" in c and "origin" in c]
        assert len(fetch_calls) == 1
        assert "egg/issue-1495" in fetch_calls[0]
        # Verify worktree add used origin/<branch> as effective base
        wt_add_calls = [c for c in call_log if "worktree" in c and "add" in c and "-b" in c]
        assert len(wt_add_calls) == 1
        assert "origin/egg/issue-1495" in wt_add_calls[0]

    def test_skips_fetch_when_base_branch_exists_locally(self, manager_with_repo):
        """When base_branch exists locally, no fetch needed."""
        manager, repos_base, repo_dir, worktree_base = manager_with_repo

        call_log = []

        def mock_run(args, **kwargs):
            call_log.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""

            if "rev-parse" in args:
                if args[-1] == "egg/my-branch":
                    result.returncode = 0  # base_branch exists locally
                elif args[-1] == "egg/my-container/work":
                    result.returncode = 1  # branch_name doesn't exist yet
            elif "worktree" in args and "add" in args:
                wt_path = None
                for i, a in enumerate(args):
                    if a == "-b" and i + 2 < len(args):
                        wt_path = Path(args[i + 2])
                        break
                if wt_path:
                    wt_path.mkdir(parents=True, exist_ok=True)
                    (wt_path / ".git").write_text("gitdir: /fake/git/dir")
            elif "worktree" in args and "lock" in args:
                pass

            return result

        with patch("subprocess.run", side_effect=mock_run):
            with patch.object(
                manager, "_find_worktree_git_dir", return_value=Path("/fake/git/dir")
            ):
                with patch.object(manager, "_chown_recursive"):
                    with patch.object(manager, "_chown_single"):
                        manager.create_worktree(
                            "test-repo", "my-container", base_branch="egg/my-branch"
                        )

        # No fetch should have been called
        fetch_calls = [c for c in call_log if "fetch" in c]
        assert len(fetch_calls) == 0
        # worktree add should use the original base_branch directly
        wt_add_calls = [c for c in call_log if "worktree" in c and "add" in c and "-b" in c]
        assert len(wt_add_calls) == 1
        assert "egg/my-branch" in wt_add_calls[0]
        assert "origin/egg/my-branch" not in wt_add_calls[0]

    def test_skips_fetch_for_head(self, manager_with_repo):
        """HEAD should never trigger a fetch."""
        manager, repos_base, repo_dir, worktree_base = manager_with_repo

        call_log = []

        def mock_run(args, **kwargs):
            call_log.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""

            if "rev-parse" in args and args[-1] == "egg/head-container/work":
                result.returncode = 1  # branch_name doesn't exist
            elif "worktree" in args and "add" in args:
                wt_path = None
                for i, a in enumerate(args):
                    if a == "-b" and i + 2 < len(args):
                        wt_path = Path(args[i + 2])
                        break
                if wt_path:
                    wt_path.mkdir(parents=True, exist_ok=True)
                    (wt_path / ".git").write_text("gitdir: /fake/git/dir")
            elif "worktree" in args and "lock" in args:
                pass

            return result

        with patch("subprocess.run", side_effect=mock_run):
            with patch.object(
                manager, "_find_worktree_git_dir", return_value=Path("/fake/git/dir")
            ):
                with patch.object(manager, "_chown_recursive"):
                    with patch.object(manager, "_chown_single"):
                        manager.create_worktree("test-repo", "head-container", base_branch="HEAD")

        fetch_calls = [c for c in call_log if "fetch" in c]
        assert len(fetch_calls) == 0

    def test_raises_when_fetch_fails_and_ref_missing(self, manager_with_repo):
        """When base_branch doesn't exist locally and fetch fails, raise immediately."""
        manager, repos_base, repo_dir, worktree_base = manager_with_repo

        call_log = []

        def mock_run(args, **kwargs):
            call_log.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""

            if "rev-parse" in args and "--verify" in args:
                result.returncode = 1
            elif "fetch" in args and "origin" in args:
                result.returncode = 128
                result.stderr = "fatal: couldn't find remote ref egg/nonexistent"

            return result

        with patch("subprocess.run", side_effect=mock_run):
            with patch.object(
                manager, "_find_worktree_git_dir", return_value=Path("/fake/git/dir")
            ):
                with patch.object(manager, "_chown_recursive"):
                    with patch.object(manager, "_chown_single"):
                        with pytest.raises(
                            RuntimeError,
                            match="Failed to fetch base branch 'egg/nonexistent' from remote",
                        ):
                            manager.create_worktree(
                                "test-repo", "fail-container", base_branch="egg/nonexistent"
                            )

        # Verify fetch was attempted
        fetch_calls = [c for c in call_log if "fetch" in c and "origin" in c]
        assert len(fetch_calls) == 1
        # Verify worktree add was NOT attempted (we raise before reaching it)
        wt_add_calls = [c for c in call_log if "worktree" in c and "add" in c]
        assert len(wt_add_calls) == 0


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

    def test_returns_none_when_no_worktrees_dir(self, tmp_path):
        """Should return None when .git/worktrees/ doesn't exist."""
        main_repo = tmp_path / "repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()

        worktree_path = tmp_path / "worktrees" / "container" / "egg"

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        result = manager._find_worktree_git_dir(main_repo, worktree_path)
        assert result is None

    def test_returns_none_when_no_matching_admin_dir(self, tmp_path):
        """Should return None when worktrees dir exists but no admin dir matches.

        This is the core fix for #1245: when the target container's admin dir
        has already been cleaned up, the function must NOT fall back to a
        default path that may belong to a different container.
        """
        main_repo = tmp_path / "repo"
        worktrees_dir = main_repo / ".git" / "worktrees"

        # Admin dir "egg" belongs to a DIFFERENT container
        other_wt = tmp_path / "worktrees" / "other-container" / "egg"
        admin_egg = worktrees_dir / "egg"
        admin_egg.mkdir(parents=True)
        (admin_egg / "gitdir").write_text(str(other_wt / ".git") + "\n")

        # We're looking for THIS container's worktree — no admin dir matches
        target_wt = tmp_path / "worktrees" / "target-container" / "egg"

        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path,
        )

        result = manager._find_worktree_git_dir(main_repo, target_wt)
        assert result is None, (
            f"Expected None when no admin dir matches, got '{result}'. "
            "Returning a default would risk deleting another container's admin dir."
        )

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

    def test_concurrent_create_worktree_with_sibling_state_writes(self, git_repo):
        """Concurrent worktree creates do not lose ``.git/config`` lock races to
        a sibling process running state-store-style git operations on the
        same bare repo (#2311).

        The pre-#2311 gateway only serialised worktree creates inside its
        own process; a separate process (the orchestrator's state-store)
        could write to ``.git/config`` between the gateway's claim and
        commit of ``.git/config.lock``, and ``git worktree add`` would
        fail with ``could not lock config file .git/config: File exists``.
        With the cross-process flock, both sides cooperatively serialise
        on a shared sentinel inside ``.git/`` — ``git worktree add`` no
        longer races.
        """
        import os as _os
        import subprocess as _sp
        import sys as _sys
        import textwrap
        import threading

        worktree_base, repos_base, repo_dir = git_repo
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        # Sibling subprocess: bursts of ``git config`` writes guarded by
        # ``bare_repo_lock`` — i.e. the same lock the gateway acquires.
        # Each write goes through ``.git/config.lock``, the same file that
        # ``git worktree add`` claims when it sets ``branch.<x>.remote``.
        ready_sentinel = repo_dir / ".contender-ready"
        contender_script = textwrap.dedent(
            f"""
            import subprocess, sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent.parent / "shared")!r})
            from egg_git.cross_process_lock import bare_repo_lock

            repo = {str(repo_dir)!r}
            open({str(ready_sentinel)!r}, "w").close()
            deadline = time.monotonic() + 8
            i = 0
            while time.monotonic() < deadline:
                # Reuse a single key — same race surface on
                # ``.git/config.lock`` without bloating ``.git/config``
                # with thousands of distinct keys over the test window.
                with bare_repo_lock(repo):
                    subprocess.run(
                        ["git", "-C", repo, "config", "egg.statetest", str(i)],
                        check=False, capture_output=True,
                    )
                i += 1
            """
        )
        contender = _sp.Popen(
            [_sys.executable, "-c", contender_script],
            env={**_os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
        )

        try:
            # Wait for the contender to start running before launching
            # threads so the race window actually overlaps.
            import time as _time

            deadline = _time.monotonic() + 5
            while not ready_sentinel.exists() and _time.monotonic() < deadline:
                if contender.poll() is not None:
                    out, err = contender.communicate(timeout=1)
                    pytest.fail(
                        f"contender exited early rc={contender.returncode}: "
                        f"stdout={out!r} stderr={err!r}"
                    )
                _time.sleep(0.01)
            assert ready_sentinel.exists(), "contender did not signal readiness"

            results: dict[str, object] = {}
            container_ids = [f"container-{i}" for i in range(6)]
            barrier = threading.Barrier(len(container_ids))

            def create(cid: str) -> None:
                try:
                    barrier.wait(timeout=10)
                    # ``assigned_branch`` triggers ``_configure_push_upstream``,
                    # which writes ``branch.<x>.remote`` / ``branch.<x>.merge``
                    # into ``.git/config``.  That is the same write that races
                    # ``.git/config.lock`` in #2311.
                    results[cid] = manager.create_worktree(
                        "test-repo",
                        cid,
                        assigned_branch="egg/issue-2311-target",
                    )
                except Exception as exc:
                    results[cid] = exc

            threads = [threading.Thread(target=create, args=(cid,)) for cid in container_ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        finally:
            contender.terminate()
            try:
                contender.wait(timeout=5)
            except _sp.TimeoutExpired:
                contender.kill()
                contender.wait(timeout=2)

        for cid in container_ids:
            assert cid in results, f"Thread for {cid} did not finish"
            info = results[cid]
            if isinstance(info, Exception):
                # Surface the exact error message — the regression
                # signature is ``could not lock config file .git/config``.
                pytest.fail(f"{cid} failed: {info!r}")
            assert isinstance(info, WorktreeInfo)
            assert info.worktree_path.exists()


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

    def test_retry_exhausts_all_attempts(self, tmp_path):
        """All 5 retry attempts should be tried before giving up."""
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
        # 5 attempts total (initial + 4 retries)
        assert mock_run.call_count == 5
        # 4 sleeps between attempts with exponential backoff from 0.5s
        assert mock_sleep.call_count == 4
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_args == [0.5, 1.0, 2.0, 4.0]

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


class TestResolveDefaultBranch:
    """Tests for WorktreeManager.resolve_default_branch."""

    @pytest.fixture
    def manager_with_repo(self, tmp_path):
        """Create a manager with a real git repo."""
        import subprocess

        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        result = subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"git init not available: {result.stderr.strip()}")
        git_env = {
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            capture_output=True,
            check=True,
            env=git_env,
        )

        worktree_base = tmp_path / "worktrees"
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        return manager, repo_dir, git_env

    def test_nonexistent_repo_returns_head(self, tmp_path):
        """Returns HEAD when repo doesn't exist."""
        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "repos",
        )
        assert manager.resolve_default_branch("nonexistent") == "HEAD"

    def test_repo_without_remote_returns_head(self, manager_with_repo):
        """Returns HEAD when no remote is configured (no origin/main or origin/master)."""
        manager, repo_dir, _ = manager_with_repo
        # Local repo with no remote — origin/main and origin/master don't exist
        result = manager.resolve_default_branch("test-repo")
        assert result == "HEAD"

    def test_repo_with_origin_main(self, manager_with_repo):
        """Returns origin/main when the remote has a main branch."""
        import subprocess

        manager, repo_dir, git_env = manager_with_repo

        # Create a bare remote with main branch
        bare_dir = repo_dir.parent / "bare-remote.git"
        subprocess.run(
            ["git", "clone", "--bare", str(repo_dir), str(bare_dir)],
            capture_output=True,
            check=True,
            env=git_env,
        )
        # Add the bare repo as origin remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare_dir)],
            cwd=repo_dir,
            capture_output=True,
            check=True,
            env=git_env,
        )
        # Fetch so origin/main (or origin/master) exists locally
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_dir,
            capture_output=True,
            check=True,
            env=git_env,
        )

        result = manager.resolve_default_branch("test-repo")
        # Should return origin/main or origin/master depending on git defaults
        assert result in ("origin/main", "origin/master")

    def test_uses_origin_head_when_configured(self, tmp_path):
        """Returns origin/HEAD target when symbolic-ref is configured."""
        import subprocess

        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="origin/main\n", stderr=""
        )
        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "repos",
        )
        (tmp_path / "repos").mkdir()
        (tmp_path / "repos" / "test-repo").mkdir()

        with patch("worktree_manager.subprocess.run", return_value=completed):
            result = manager.resolve_default_branch("test-repo")
        assert result == "origin/main"


class TestRemoveWorktreeStaleCleanup:
    """Tests for remove_worktree cleaning up stale git registrations when the directory is gone.

    Regression tests for https://github.com/jwbron/egg/issues/929
    """

    def test_removes_admin_dir_when_worktree_directory_gone(self, tmp_path):
        """When worktree directory is already removed, should still clean up .git/worktrees/ admin dir."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        # Create a main repo with a stale admin dir
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        worktrees_dir = repo_dir / ".git" / "worktrees"

        # Simulate stale admin dir for container-1's worktree
        worktree_path = worktree_base / "container-1" / "test-repo"
        admin_dir = worktrees_dir / "test-repo"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(str(worktree_path / ".git") + "\n")
        (admin_dir / "HEAD").write_text("ref: refs/heads/egg/container-1/work\n")

        # Do NOT create worktree_path — it's already gone (the bug scenario)
        assert not worktree_path.exists()
        assert admin_dir.exists()

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # Mock branch deletion (git branch -D)
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = manager.remove_worktree("container-1", "test-repo", force=True)

        assert result.success
        # Admin dir should be removed
        assert not admin_dir.exists()

    def test_deletes_branch_when_worktree_directory_gone(self, tmp_path):
        """When worktree directory is already removed, should still delete the branch."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        worktrees_dir = repo_dir / ".git" / "worktrees"

        worktree_path = worktree_base / "container-1" / "test-repo"
        admin_dir = worktrees_dir / "test-repo"
        admin_dir.mkdir(parents=True)
        (admin_dir / "gitdir").write_text(str(worktree_path / ".git") + "\n")
        (admin_dir / "HEAD").write_text("ref: refs/heads/egg/container-1/work\n")

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # git branch --merged returns empty (not merged), git branch -D succeeds
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = manager.remove_worktree(
                "container-1", "test-repo", force=True, delete_branch=True
            )

        assert result.success
        assert result.branch_deleted
        # Verify git branch -D was called
        branch_calls = [c for c in mock_run.call_args_list if "branch" in str(c) and "-D" in str(c)]
        assert len(branch_calls) > 0

    def test_no_admin_dir_still_succeeds(self, tmp_path):
        """When worktree directory and admin dir are both gone, should succeed cleanly."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        result = manager.remove_worktree("container-1", "test-repo")

        assert result.success

    def test_cleans_up_empty_container_dir(self, tmp_path):
        """Should remove empty container directory when worktree dir is gone."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        (repos_base / "test-repo").mkdir()

        # Create empty container dir (worktree subdir already removed)
        container_dir = worktree_base / "container-1"
        container_dir.mkdir(parents=True)
        assert container_dir.exists()

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        result = manager.remove_worktree("container-1", "test-repo")

        assert result.success
        assert not container_dir.exists()

    def test_removes_from_memory_tracking(self, tmp_path):
        """Should remove entry from in-memory tracking when directory is gone."""
        worktree_base = tmp_path / "worktrees"
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        (repos_base / "test-repo").mkdir()
        (repos_base / "test-repo" / ".git").mkdir()

        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        # Add to in-memory tracking
        manager._active_worktrees["container-1"] = [
            WorktreeInfo(
                container_id="container-1",
                repo_name="test-repo",
                branch="egg/container-1/work",
                worktree_path=worktree_base / "container-1" / "test-repo",
                git_dir=repos_base / "test-repo" / ".git" / "worktrees" / "test-repo",
            )
        ]

        result = manager.remove_worktree("container-1", "test-repo")

        assert result.success
        assert "container-1" not in manager._active_worktrees


class TestPruneStaleWorktrees:
    """Tests for prune_stale_worktrees defense-in-depth method."""

    def test_prunes_repos_with_git_directory(self, tmp_path):
        """Should run git worktree prune on repos that have a .git directory."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        # Create two repos
        repo1 = repos_base / "repo1"
        repo1.mkdir()
        (repo1 / ".git").mkdir()

        repo2 = repos_base / "repo2"
        repo2.mkdir()
        (repo2 / ".git").mkdir()

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # Each repo gets a list call (no locks) then a prune call
            no_locked = MagicMock(
                returncode=0,
                stdout="worktree /main\nHEAD abc\nbranch refs/heads/main\n\n",
                stderr="",
            )
            prune_ok = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [no_locked, prune_ok, no_locked, prune_ok]
            pruned = manager.prune_stale_worktrees()

        assert pruned == 2
        # Verify git worktree prune was called for each repo
        prune_calls = [c for c in mock_run.call_args_list if "prune" in c[0][0]]
        assert len(prune_calls) == 2

    def test_skips_worktree_repos(self, tmp_path):
        """Should skip repos where .git is a file (worktree pointers)."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        # Real repo with .git directory
        real_repo = repos_base / "real-repo"
        real_repo.mkdir()
        (real_repo / ".git").mkdir()

        # Worktree-mounted repo with .git file
        wt_repo = repos_base / "wt-repo"
        wt_repo.mkdir()
        (wt_repo / ".git").write_text("gitdir: /some/path/.git/worktrees/wt-repo")

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            no_locked = MagicMock(
                returncode=0,
                stdout="worktree /main\nHEAD abc\nbranch refs/heads/main\n\n",
                stderr="",
            )
            prune_ok = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [no_locked, prune_ok]
            pruned = manager.prune_stale_worktrees()

        # Only the real repo should be pruned
        assert pruned == 1

    def test_handles_missing_repos_base(self, tmp_path):
        """Should return 0 when repos_base doesn't exist."""
        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "nonexistent-repos",
        )
        assert manager.prune_stale_worktrees() == 0

    def test_handles_prune_failure(self, tmp_path):
        """Should log warning and continue when prune fails for a repo."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo = repos_base / "test-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            no_locked = MagicMock(
                returncode=0,
                stdout="worktree /main\nHEAD abc\nbranch refs/heads/main\n\n",
                stderr="",
            )
            prune_fail = MagicMock(returncode=1, stdout="", stderr="error: prune failed")
            mock_run.side_effect = [no_locked, prune_fail]
            pruned = manager.prune_stale_worktrees()

        # Should not count failed pruning
        assert pruned == 0

    def test_handles_prune_timeout(self, tmp_path):
        """Should log warning and continue to next repo when prune times out."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo1 = repos_base / "repo1"
        repo1.mkdir()
        (repo1 / ".git").mkdir()

        repo2 = repos_base / "repo2"
        repo2.mkdir()
        (repo2 / ".git").mkdir()

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        no_locked = MagicMock(
            returncode=0, stdout="worktree /main\nHEAD abc\nbranch refs/heads/main\n\n", stderr=""
        )
        prune_ok = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run") as mock_run:
            # First repo: list ok, prune times out. Second repo: list ok, prune ok.
            mock_run.side_effect = [
                no_locked,
                subprocess.TimeoutExpired(cmd="git worktree prune", timeout=30),
                no_locked,
                prune_ok,
            ]
            pruned = manager.prune_stale_worktrees()

        # Only the second repo should be counted
        assert pruned == 1

    def test_skips_prune_when_locked_worktrees_found(self, tmp_path):
        """Should skip pruning when git worktree list shows locked worktrees."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo = repos_base / "test-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # git worktree list --porcelain returns output with "locked" keyword
            list_result = MagicMock(
                returncode=0,
                stdout="worktree /path/to/worktree\nHEAD abc123\nbranch refs/heads/egg/work\nlocked\n\n",
                stderr="",
            )
            mock_run.return_value = list_result
            pruned = manager.prune_stale_worktrees()

        # Should not prune because locked worktrees exist
        assert pruned == 0
        # Only the list call should have been made, not the prune call
        prune_calls = [c for c in mock_run.call_args_list if "prune" in c[0][0]]
        assert len(prune_calls) == 0

    def test_prunes_when_no_locked_worktrees(self, tmp_path):
        """Should proceed with pruning when no locked worktrees found."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()

        repo = repos_base / "test-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)

        with patch("subprocess.run") as mock_run:
            # First call: git worktree list (no locked)
            list_result = MagicMock(
                returncode=0,
                stdout="worktree /path/to/main\nHEAD abc123\nbranch refs/heads/main\n\n",
                stderr="",
            )
            # Second call: git worktree prune succeeds
            prune_result = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [list_result, prune_result]
            pruned = manager.prune_stale_worktrees()

        assert pruned == 1


class TestCleanupOrphanedPackFiles:
    """Tests for cleanup_orphaned_pack_files method."""

    def _create_repo_with_pack_dir(self, repos_base, repo_name="test-repo"):
        """Helper to create a fake repo with .git/objects/pack/ directory."""
        repo_dir = repos_base / repo_name
        repo_dir.mkdir(parents=True, exist_ok=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir(exist_ok=True)
        pack_dir = git_dir / "objects" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        return repo_dir, pack_dir

    def test_removes_tmp_pack_files(self, tmp_path):
        """Should remove tmp_pack_*, tmp_obj_*, and tmp_idx_* files."""
        repos_base = tmp_path / "repos"
        _, pack_dir = self._create_repo_with_pack_dir(repos_base)

        # Create orphaned tmp files
        (pack_dir / "tmp_pack_abc123").write_bytes(b"x" * 100)
        (pack_dir / "tmp_obj_def456").write_bytes(b"x" * 200)
        (pack_dir / "tmp_idx_ghi789").write_bytes(b"x" * 50)

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 3
        assert bytes_reclaimed == 350
        assert not (pack_dir / "tmp_pack_abc123").exists()
        assert not (pack_dir / "tmp_obj_def456").exists()
        assert not (pack_dir / "tmp_idx_ghi789").exists()

    def test_preserves_legitimate_pack_files(self, tmp_path):
        """Should NOT remove legitimate pack-*.pack and pack-*.idx files."""
        repos_base = tmp_path / "repos"
        _, pack_dir = self._create_repo_with_pack_dir(repos_base)

        # Create legitimate pack files
        (pack_dir / "pack-abc123def456.pack").write_bytes(b"x" * 500)
        (pack_dir / "pack-abc123def456.idx").write_bytes(b"x" * 100)
        (pack_dir / "pack-abc123def456.keep").write_bytes(b"")
        # And one orphan
        (pack_dir / "tmp_pack_orphan").write_bytes(b"x" * 50)

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 1
        assert bytes_reclaimed == 50
        # Legitimate files untouched
        assert (pack_dir / "pack-abc123def456.pack").exists()
        assert (pack_dir / "pack-abc123def456.idx").exists()
        assert (pack_dir / "pack-abc123def456.keep").exists()

    def test_respects_max_age_filter(self, tmp_path):
        """Should only remove files older than max_age_seconds."""
        import os
        import time

        repos_base = tmp_path / "repos"
        _, pack_dir = self._create_repo_with_pack_dir(repos_base)

        old_file = pack_dir / "tmp_pack_old"
        old_file.write_bytes(b"x" * 100)
        # Set mtime to 10 minutes ago
        old_mtime = time.time() - 600
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = pack_dir / "tmp_pack_recent"
        recent_file.write_bytes(b"x" * 200)
        # Leave mtime as now (just created)

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files(max_age_seconds=300)

        assert files == 1
        assert bytes_reclaimed == 100
        assert not old_file.exists()
        assert recent_file.exists()

    def test_handles_missing_pack_dir(self, tmp_path):
        """Should handle repos with .git but no objects/pack/ directory."""
        repos_base = tmp_path / "repos"
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        # No objects/pack/ directory

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 0
        assert bytes_reclaimed == 0

    def test_handles_missing_repos_base(self, tmp_path):
        """Should return (0, 0) when repos_base doesn't exist."""
        manager = WorktreeManager(
            worktree_base=tmp_path / "worktrees",
            repos_base=tmp_path / "nonexistent-repos",
        )
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 0
        assert bytes_reclaimed == 0

    def test_skips_worktree_git_files(self, tmp_path):
        """Should skip entries where .git is a file (worktree pointer), not a directory."""
        repos_base = tmp_path / "repos"
        repo_dir = repos_base / "worktree-repo"
        repo_dir.mkdir(parents=True)
        # .git as a file (worktree pointer), not a directory
        (repo_dir / ".git").write_text("gitdir: /some/other/path/.git/worktrees/wt1")

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 0
        assert bytes_reclaimed == 0

    def test_single_repo_mode(self, tmp_path):
        """When repo_name is specified, only clean that repo."""
        repos_base = tmp_path / "repos"
        _, pack_dir1 = self._create_repo_with_pack_dir(repos_base, "repo1")
        _, pack_dir2 = self._create_repo_with_pack_dir(repos_base, "repo2")

        (pack_dir1 / "tmp_pack_a").write_bytes(b"x" * 100)
        (pack_dir2 / "tmp_pack_b").write_bytes(b"x" * 200)

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files(repo_name="repo1")

        assert files == 1
        assert bytes_reclaimed == 100
        assert not (pack_dir1 / "tmp_pack_a").exists()
        # repo2's file untouched
        assert (pack_dir2 / "tmp_pack_b").exists()

    def test_multiple_repos(self, tmp_path):
        """Should clean across all repos when no repo_name specified."""
        repos_base = tmp_path / "repos"
        _, pack_dir1 = self._create_repo_with_pack_dir(repos_base, "repo1")
        _, pack_dir2 = self._create_repo_with_pack_dir(repos_base, "repo2")

        (pack_dir1 / "tmp_pack_a").write_bytes(b"x" * 100)
        (pack_dir2 / "tmp_pack_b").write_bytes(b"x" * 200)

        manager = WorktreeManager(worktree_base=tmp_path / "worktrees", repos_base=repos_base)
        files, bytes_reclaimed = manager.cleanup_orphaned_pack_files()

        assert files == 2
        assert bytes_reclaimed == 300


class TestStartupCleanupWithPrune:
    """Tests for startup_cleanup calling prune_stale_worktrees."""

    def test_calls_prune_after_orphan_cleanup(self):
        """startup_cleanup should call prune_stale_worktrees after orphan cleanup."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 0
            mock_instance.prune_stale_worktrees.return_value = 1
            MockManager.return_value = mock_instance

            startup_cleanup(active_containers=set())

            mock_instance.cleanup_orphaned_worktrees.assert_called_once()
            mock_instance.prune_stale_worktrees.assert_called_once()

    def test_prune_failure_does_not_prevent_cleanup(self):
        """If prune raises an exception, startup_cleanup should still succeed."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 2
            mock_instance.prune_stale_worktrees.side_effect = RuntimeError("prune failed")
            MockManager.return_value = mock_instance

            # Should not raise
            removed = startup_cleanup(active_containers=set())
            assert removed == 2

    def test_calls_pack_cleanup_after_prune(self):
        """startup_cleanup should call cleanup_orphaned_pack_files after prune."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 0
            mock_instance.prune_stale_worktrees.return_value = 1
            mock_instance.cleanup_orphaned_pack_files.return_value = (5, 1024000)
            MockManager.return_value = mock_instance

            startup_cleanup(active_containers=set())

            mock_instance.cleanup_orphaned_worktrees.assert_called_once()
            mock_instance.prune_stale_worktrees.assert_called_once()
            mock_instance.cleanup_orphaned_pack_files.assert_called_once()

    def test_pack_cleanup_failure_does_not_prevent_startup(self):
        """If pack cleanup raises, startup_cleanup should still succeed."""
        from worktree_manager import startup_cleanup

        with patch("worktree_manager.WorktreeManager") as MockManager:
            mock_instance = MagicMock()
            mock_instance.cleanup_orphaned_worktrees.return_value = 1
            mock_instance.prune_stale_worktrees.return_value = 0
            mock_instance.cleanup_orphaned_pack_files.side_effect = RuntimeError("disk error")
            MockManager.return_value = mock_instance

            removed = startup_cleanup(active_containers=set())
            assert removed == 1


class TestCreateWorktreeFetchTimeout:
    """Tests for fetch timeout handling in create_worktree."""

    @pytest.fixture
    def manager_with_repo(self, tmp_path):
        """Create manager with a fake repo that has a .git directory."""
        repos_base = tmp_path / "repos"
        repos_base.mkdir()
        repo_dir = repos_base / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
        return manager, repos_base, repo_dir, worktree_base

    def test_raises_when_fetch_times_out(self, manager_with_repo):
        """When fetch times out, raise RuntimeError."""
        manager, repos_base, repo_dir, worktree_base = manager_with_repo

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""

            if "rev-parse" in args and "--verify" in args:
                result.returncode = 1  # branch not found locally
            elif "fetch" in args and "origin" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=120)
            return result

        with patch("subprocess.run", side_effect=mock_run):
            with patch.object(
                manager, "_find_worktree_git_dir", return_value=Path("/fake/git/dir")
            ):
                with patch.object(manager, "_chown_recursive"):
                    with patch.object(manager, "_chown_single"):
                        with pytest.raises(RuntimeError, match="Timed out fetching base branch"):
                            manager.create_worktree(
                                "test-repo",
                                "timeout-container",
                                base_branch="egg/slow-branch",
                            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
