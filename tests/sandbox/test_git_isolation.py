"""
Tests for git worktree isolation implementation.

Tests actual git worktree behavior to verify the implementation works end-to-end.
These tests can run on the host machine without Docker.

Note: Tests for the old container-side worktree setup have been removed as
PR #595 moved to gateway-managed worktrees where git isolation is handled
by the gateway sidecar rather than the container entrypoint.
"""

import shutil
import subprocess

import pytest


class TestHostWorktreeIntegration:
    """Integration tests that verify host worktree operations work correctly.

    These tests create actual git repositories and worktrees to verify
    the implementation works end-to-end.
    """

    @pytest.fixture
    def real_git_repo(self, tmp_path):
        """Create a real git repository with a worktree."""
        # Create main repo
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()

        # Initialize git repo (may be blocked by gateway sidecar in container)
        result = subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"git init not available: {result.stderr.strip()}")
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=main_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=main_repo,
            check=True,
            capture_output=True,
        )

        # Create initial commit
        (main_repo / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "README.md"], cwd=main_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=main_repo,
            check=True,
            capture_output=True,
        )

        # Create worktree
        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "test-branch", str(worktree_path)],
            cwd=main_repo,
            check=True,
            capture_output=True,
        )

        return {
            "main_repo": main_repo,
            "worktree_path": worktree_path,
            "git_dir": main_repo / ".git",
        }

    def test_worktree_has_correct_structure(self, real_git_repo):
        """Test that git worktree has expected structure."""
        worktree_path = real_git_repo["worktree_path"]
        git_dir = real_git_repo["git_dir"]

        # .git file should exist in worktree
        assert (worktree_path / ".git").is_file()

        # Worktree admin dir should exist
        worktree_name = worktree_path.name
        admin_dir = git_dir / "worktrees" / worktree_name
        assert admin_dir.is_dir()

        # Admin dir should have required files
        assert (admin_dir / "HEAD").exists()
        assert (admin_dir / "gitdir").exists()
        assert (admin_dir / "commondir").exists()

    def test_gitdir_backup_restore_cycle(self, real_git_repo):
        """Test complete backup/restore cycle for gitdir."""
        git_dir = real_git_repo["git_dir"]
        worktree_name = real_git_repo["worktree_path"].name
        admin_dir = git_dir / "worktrees" / worktree_name

        # Save original gitdir content
        original_gitdir = (admin_dir / "gitdir").read_text()

        # Simulate container startup: backup and rewrite
        backup_path = admin_dir / "gitdir.host-backup"
        shutil.copy2(admin_dir / "gitdir", backup_path)
        (admin_dir / "gitdir").write_text("/home/egg/repos/worktree\n")

        # Verify git operations still work in main repo
        result = subprocess.run(
            ["git", "status"],
            cwd=real_git_repo["main_repo"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

        # Simulate container exit: restore
        shutil.copy2(backup_path, admin_dir / "gitdir")
        backup_path.unlink()

        # Verify gitdir is restored
        assert (admin_dir / "gitdir").read_text() == original_gitdir
        assert not backup_path.exists()

        # Verify worktree operations work
        result = subprocess.run(
            ["git", "status"],
            cwd=real_git_repo["worktree_path"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

    def test_commondir_modification_doesnt_break_main_repo(self, real_git_repo):
        """Test that modifying commondir doesn't break main repo operations."""
        git_dir = real_git_repo["git_dir"]
        worktree_name = real_git_repo["worktree_path"].name
        admin_dir = git_dir / "worktrees" / worktree_name

        # Save original commondir
        original_commondir = (admin_dir / "commondir").read_text()

        # Simulate container: change commondir to container path
        (admin_dir / "commondir").write_text("/home/egg/.git-common/test-repo\n")

        # Main repo should still work (commondir only affects worktree)
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=real_git_repo["main_repo"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

        # Restore commondir
        (admin_dir / "commondir").write_text(original_commondir)

        # Worktree should work again
        result = subprocess.run(
            ["git", "status"],
            cwd=real_git_repo["worktree_path"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

    def test_worktree_fails_without_commondir_restore(self, real_git_repo):
        """Test that worktree FAILS if commondir points to non-existent container path.

        This demonstrates WHY we need backup/restore for commondir:
        - Container writes /home/egg/.git-common/repo to commondir
        - After container exit, this path doesn't exist on host
        - Git operations on worktree fail with "unable to read commondir"

        The backup/restore mechanism in entrypoint.py prevents this.
        """
        git_dir = real_git_repo["git_dir"]
        worktree_name = real_git_repo["worktree_path"].name
        admin_dir = git_dir / "worktrees" / worktree_name

        # Save original commondir for cleanup
        original_commondir = (admin_dir / "commondir").read_text()

        try:
            # Simulate container exit WITHOUT restore (crash scenario)
            (admin_dir / "commondir").write_text("/home/egg/.git-common/test-repo\n")

            # Worktree operations should FAIL because commondir path doesn't exist
            result = subprocess.run(
                ["git", "status"],
                cwd=real_git_repo["worktree_path"],
                capture_output=True,
                text=True,
                check=False,
            )

            # This SHOULD fail - that's the bug we're protecting against
            assert result.returncode != 0, (
                "Expected git status to fail with invalid commondir, "
                "but it succeeded. This test verifies the need for backup/restore."
            )
            assert "commondir" in result.stderr.lower() or "fatal" in result.stderr.lower()

        finally:
            # Always restore for test cleanup
            (admin_dir / "commondir").write_text(original_commondir)

    def test_commondir_backup_restore_cycle(self, real_git_repo):
        """Test complete backup/restore cycle for commondir (same as gitdir test)."""
        git_dir = real_git_repo["git_dir"]
        worktree_name = real_git_repo["worktree_path"].name
        admin_dir = git_dir / "worktrees" / worktree_name

        # Save original commondir content
        original_commondir = (admin_dir / "commondir").read_text()

        # Simulate container startup: backup and rewrite
        backup_path = admin_dir / "commondir.host-backup"
        shutil.copy2(admin_dir / "commondir", backup_path)
        (admin_dir / "commondir").write_text("/home/egg/.git-common/test-repo\n")

        # Main repo should still work
        result = subprocess.run(
            ["git", "status"],
            cwd=real_git_repo["main_repo"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

        # Simulate container exit: restore
        shutil.copy2(backup_path, admin_dir / "commondir")
        backup_path.unlink()

        # Verify commondir is restored
        assert (admin_dir / "commondir").read_text() == original_commondir
        assert not backup_path.exists()

        # Verify worktree operations work after restore
        result = subprocess.run(
            ["git", "status"],
            cwd=real_git_repo["worktree_path"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
