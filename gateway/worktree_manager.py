"""
Worktree Manager - Manages git worktrees for container isolation.

Provides:
- Worktree lifecycle management (create, delete, list)
- Orphaned worktree cleanup on gateway startup
- Container-to-worktree mapping
- Integration with gateway API endpoints

The gateway creates worktrees before containers start, allowing containers
to mount only the working directory (with .git shadowed by tmpfs). All git
operations then route through the gateway API.
"""

import contextlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.egg_logging import get_logger

from .git_client import git_cmd

logger = get_logger("gateway.worktree-manager")

# Default paths - can be configured via constructor
DEFAULT_WORKTREE_BASE_DIR = Path.home() / ".egg-worktrees"
DEFAULT_REPOS_BASE_DIR = Path.home() / "repos"


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    container_id: str
    repo_name: str
    branch: str
    worktree_path: Path
    git_dir: Path  # Path to worktree admin directory in .git/worktrees/
    created_at: str | None = None


@dataclass
class WorktreeRemovalResult:
    """Result of worktree removal operation."""

    success: bool
    uncommitted_changes: bool = False
    branch_deleted: bool = False
    warning: str | None = None
    error: str | None = None


def validate_identifier(value: str, name: str) -> None:
    """Ensure identifier contains only safe characters.

    Prevents path traversal attacks via container_id or repo_name containing '../'.

    Raises:
        ValueError: If identifier contains unsafe characters
    """
    if not value:
        raise ValueError(f"Invalid {name}: cannot be empty")
    # Check path traversal first for specific error message
    if ".." in value:
        raise ValueError(f"Invalid {name}: path traversal not allowed")
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", value):
        raise ValueError(f"Invalid {name}: must be alphanumeric with ._- allowed")


class WorktreeManager:
    """Manages git worktrees for container isolation.

    Each container gets its own worktree(s), providing:
    - Isolated working directory
    - Separate staging area (index)
    - Container-specific branch (egg/{container_id}/work)

    All worktrees share the git object store for efficient storage.
    """

    def __init__(
        self,
        worktree_base: Path | None = None,
        repos_base: Path | None = None,
        branch_prefix: str = "egg",
    ):
        """Initialize the worktree manager.

        Args:
            worktree_base: Base directory for worktrees
            repos_base: Base directory for main repos
            branch_prefix: Prefix for worktree branches (default: "egg")
        """
        self.worktree_base = worktree_base or DEFAULT_WORKTREE_BASE_DIR
        self.repos_base = repos_base or DEFAULT_REPOS_BASE_DIR
        self.branch_prefix = branch_prefix
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Track active worktrees in memory
        self._active_worktrees: dict[str, list[WorktreeInfo]] = {}

    def create_worktree(
        self,
        repo_name: str,
        container_id: str,
        base_branch: str = "HEAD",
        uid: int | None = None,
        gid: int | None = None,
    ) -> WorktreeInfo:
        """Create an isolated worktree for a container.

        Args:
            repo_name: Name of the repository
            container_id: Container identifier
            base_branch: Branch or ref to base the worktree on (default: HEAD)
            uid: User ID to set ownership to (default: 1000)
            gid: Group ID to set ownership to (default: 1000)

        Returns:
            WorktreeInfo with paths and branch information

        Raises:
            ValueError: If inputs are invalid or repo not found
            RuntimeError: If worktree creation fails
        """
        # Default to user (1000:1000) if not specified
        if uid is None:
            uid = 1000
        if gid is None:
            gid = 1000

        # Validate uid/gid are positive integers
        if not isinstance(uid, int) or uid < 0:
            raise ValueError(f"Invalid uid: must be a non-negative integer, got {uid!r}")
        if not isinstance(gid, int) or gid < 0:
            raise ValueError(f"Invalid gid: must be a non-negative integer, got {gid!r}")

        # Validate inputs to prevent path traversal
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")

        # Find main repo
        main_repo = self.repos_base / repo_name
        if not main_repo.exists():
            raise ValueError(f"Repository not found: {repo_name}")

        # Determine paths
        worktree_path = self.worktree_base / container_id / repo_name
        branch_name = f"{self.branch_prefix}/{container_id}/work"

        # Create container directory and set ownership immediately
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._chown_single(worktree_path.parent, uid, gid)

        # Check if worktree already exists AND is valid
        git_file = worktree_path / ".git"
        worktree_is_valid = (
            worktree_path.exists()
            and git_file.exists()
            and git_file.is_file()
            and git_file.read_text().strip().startswith("gitdir:")
        )

        if worktree_is_valid:
            logger.info(
                "Worktree already exists",
                container_id=container_id,
                repo=repo_name,
                path=str(worktree_path),
            )
            # Ensure ownership is correct
            self._chown_recursive(worktree_path, uid, gid)
            self._chown_single(worktree_path.parent, uid, gid)
            return WorktreeInfo(
                container_id=container_id,
                repo_name=repo_name,
                branch=branch_name,
                worktree_path=worktree_path,
                git_dir=self._find_worktree_git_dir(main_repo, worktree_path),
            )

        # If directory exists but is not a valid worktree, remove it first
        if worktree_path.exists():
            logger.warning(
                "Removing invalid/empty worktree directory",
                container_id=container_id,
                repo=repo_name,
                path=str(worktree_path),
            )
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Check if branch already exists (from crashed session)
        branch_exists = (
            subprocess.run(
                git_cmd("rev-parse", "--verify", branch_name),
                cwd=main_repo,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

        if branch_exists:
            # Use existing branch instead of creating new one
            logger.info(
                "Reusing existing branch for worktree",
                branch=branch_name,
                container_id=container_id,
            )
            result = subprocess.run(
                git_cmd("worktree", "add", str(worktree_path), branch_name),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            # Create new branch from base
            result = subprocess.run(
                git_cmd(
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    base_branch,
                ),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        # Set ownership so the container user can write to the worktree
        self._chown_recursive(worktree_path, uid, gid)
        self._chown_single(worktree_path.parent, uid, gid)

        # Find the actual git dir
        git_dir = self._find_worktree_git_dir(main_repo, worktree_path)

        info = WorktreeInfo(
            container_id=container_id,
            repo_name=repo_name,
            branch=branch_name,
            worktree_path=worktree_path,
            git_dir=git_dir,
        )

        # Track in memory
        if container_id not in self._active_worktrees:
            self._active_worktrees[container_id] = []
        self._active_worktrees[container_id].append(info)

        logger.info(
            "Worktree created",
            container_id=container_id,
            repo=repo_name,
            path=str(worktree_path),
            branch=branch_name,
        )

        return info

    def _chown_single(self, path: Path, uid: int, gid: int) -> None:
        """Change ownership of a single file or directory (non-recursive)."""
        try:
            os.chown(path, uid, gid)
        except PermissionError:
            logger.debug(
                "Skipping chown (running as non-root)",
                path=str(path),
                target_uid=uid,
                target_gid=gid,
            )
        except OSError as e:
            logger.warning(
                "Failed to chown",
                path=str(path),
                target_uid=uid,
                target_gid=gid,
                error=str(e),
            )

    def _chown_recursive(self, path: Path, uid: int, gid: int) -> None:
        """Recursively change ownership of a directory."""
        try:
            result = subprocess.run(
                ["chown", "-R", f"{uid}:{gid}", str(path)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                error_msg = result.stderr.decode() if result.stderr else "unknown error"
                if "Operation not permitted" in error_msg or "Permission denied" in error_msg:
                    logger.debug(
                        "Skipping recursive chown (running as non-root)",
                        path=str(path),
                        target_uid=uid,
                        target_gid=gid,
                    )
                else:
                    logger.warning(
                        "Failed to chown recursively",
                        path=str(path),
                        target_uid=uid,
                        target_gid=gid,
                        error=error_msg,
                    )
        except subprocess.SubprocessError as e:
            logger.warning(
                "Failed to run chown command",
                path=str(path),
                target_uid=uid,
                target_gid=gid,
                error=str(e),
            )

    def _find_worktree_git_dir(self, main_repo: Path, worktree_path: Path) -> Path:
        """Find the git worktree admin directory."""
        basename = worktree_path.name
        git_dir = main_repo / ".git" / "worktrees" / basename

        if git_dir.exists():
            return git_dir

        # Check for numbered variants
        worktrees_dir = main_repo / ".git" / "worktrees"
        if worktrees_dir.exists():
            for entry in worktrees_dir.iterdir():
                if entry.name.startswith(basename):
                    gitdir_file = entry / "gitdir"
                    if gitdir_file.exists():
                        gitdir_content = gitdir_file.read_text().strip()
                        if str(worktree_path) in gitdir_content:
                            return entry

        return git_dir

    def remove_worktree(
        self,
        container_id: str,
        repo_name: str,
        force: bool = False,
        delete_branch: bool = True,
    ) -> WorktreeRemovalResult:
        """Remove a container's worktree."""
        result = WorktreeRemovalResult(success=False)

        try:
            validate_identifier(container_id, "container_id")
            validate_identifier(repo_name, "repo_name")
        except ValueError as e:
            result.error = str(e)
            return result

        worktree_path = self.worktree_base / container_id / repo_name
        main_repo = self.repos_base / repo_name
        branch_name = f"{self.branch_prefix}/{container_id}/work"

        if not worktree_path.exists():
            result.success = True
            return result

        # Check for uncommitted changes
        if main_repo.exists():
            status = subprocess.run(
                git_cmd("status", "--porcelain"),
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
            )
            has_changes = bool(status.stdout.strip())

            if has_changes and not force:
                result.uncommitted_changes = True
                result.warning = (
                    "Worktree has uncommitted changes. "
                    "Use force=True to remove anyway, or commit/stash changes first."
                )
                return result

            if has_changes:
                logger.warning(
                    "Removing worktree with uncommitted changes",
                    container_id=container_id,
                    repo=repo_name,
                )
                result.warning = "Worktree removed with uncommitted changes"

        # Remove the worktree
        if main_repo.exists():
            remove_result = subprocess.run(
                git_cmd("worktree", "remove", str(worktree_path), "--force"),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )

            if remove_result.returncode != 0:
                logger.warning(
                    "Git worktree remove failed, using shutil",
                    container_id=container_id,
                    repo=repo_name,
                    stderr=remove_result.stderr,
                )
                shutil.rmtree(worktree_path, ignore_errors=True)

            # Prune worktree references
            subprocess.run(
                git_cmd("worktree", "prune"),
                cwd=main_repo,
                capture_output=True,
                check=False,
            )

            # Delete the branch if requested
            if delete_branch:
                result.branch_deleted = self._delete_worktree_branch(main_repo, branch_name, force)
                if not result.branch_deleted and not force:
                    result.warning = (
                        (result.warning or "")
                        + f" Branch {branch_name} has unmerged commits and was not deleted."
                    ).strip()
        else:
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Clean up container directory if empty
        container_dir = self.worktree_base / container_id
        if container_dir.exists() and not any(container_dir.iterdir()):
            with contextlib.suppress(OSError):
                container_dir.rmdir()

        # Remove from memory tracking
        if container_id in self._active_worktrees:
            self._active_worktrees[container_id] = [
                wt for wt in self._active_worktrees[container_id] if wt.repo_name != repo_name
            ]
            if not self._active_worktrees[container_id]:
                del self._active_worktrees[container_id]

        logger.info(
            "Worktree removed",
            container_id=container_id,
            repo=repo_name,
            force=force,
            branch_deleted=result.branch_deleted,
        )

        result.success = True
        return result

    def _delete_worktree_branch(self, main_repo: Path, branch_name: str, force: bool) -> bool:
        """Delete a worktree branch if it's safe to do so."""
        merge_check = subprocess.run(
            git_cmd("branch", "--merged", "HEAD", "--list", branch_name),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        is_merged = branch_name in merge_check.stdout

        if is_merged or force:
            delete_result = subprocess.run(
                git_cmd("branch", "-D" if force else "-d", branch_name),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            return delete_result.returncode == 0

        return False

    def list_worktrees(self) -> list[dict[str, Any]]:
        """List all active worktrees."""
        worktrees: list[dict[str, Any]] = []

        if not self.worktree_base.exists():
            return worktrees

        for container_dir in self.worktree_base.iterdir():
            if not container_dir.is_dir():
                continue

            container_id = container_dir.name
            repos = []

            for repo_dir in container_dir.iterdir():
                if repo_dir.is_dir():
                    branch = None
                    git_file = repo_dir / ".git"
                    if git_file.exists():
                        try:
                            gitdir_content = git_file.read_text().strip()
                            if gitdir_content.startswith("gitdir: "):
                                gitdir_path = Path(gitdir_content[8:])
                                head_file = gitdir_path / "HEAD"
                                if head_file.exists():
                                    head_content = head_file.read_text().strip()
                                    if head_content.startswith("ref: refs/heads/"):
                                        branch = head_content[16:]
                        except Exception:
                            pass

                    repos.append(
                        {
                            "name": repo_dir.name,
                            "path": str(repo_dir),
                            "branch": branch,
                        }
                    )

            if repos:
                worktrees.append(
                    {
                        "container_id": container_id,
                        "repos": repos,
                    }
                )

        return worktrees

    def cleanup_orphaned_worktrees(self, active_containers: set[str]) -> int:
        """Remove worktrees for containers that no longer exist."""
        removed = 0

        if not self.worktree_base.exists():
            return removed

        for container_dir in list(self.worktree_base.iterdir()):
            if not container_dir.is_dir():
                continue

            container_id = container_dir.name

            if container_id in active_containers:
                continue

            logger.info(
                "Cleaning up orphaned worktrees",
                container_id=container_id,
            )

            for worktree in list(container_dir.iterdir()):
                if worktree.is_dir():
                    result = self.remove_worktree(container_id, worktree.name, force=True)
                    if result.success:
                        removed += 1
                    else:
                        logger.warning(
                            "Failed to remove orphaned worktree",
                            container_id=container_id,
                            repo=worktree.name,
                            error=result.error,
                        )

            try:
                if container_dir.exists():
                    shutil.rmtree(container_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(
                    "Failed to remove container worktree dir",
                    container_id=container_id,
                    error=str(e),
                )

        return removed

    def get_worktree_paths(self, container_id: str, repo_name: str) -> tuple[Path, Path]:
        """Get worktree paths for path mapping."""
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")

        worktree_path = self.worktree_base / container_id / repo_name
        main_repo = self.repos_base / repo_name

        return worktree_path, main_repo


def get_active_docker_containers() -> set[str]:
    """Get set of currently running Docker container names."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return set(result.stdout.strip().split("\n")) - {""}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return set()


def startup_cleanup(worktree_base: Path | None = None, repos_base: Path | None = None) -> int:
    """Clean up orphaned worktrees on gateway startup."""
    manager = WorktreeManager(worktree_base=worktree_base, repos_base=repos_base)
    active_containers = get_active_docker_containers()

    logger.info(
        "Running startup worktree cleanup",
        active_containers=len(active_containers),
    )

    removed = manager.cleanup_orphaned_worktrees(active_containers)

    if removed > 0:
        logger.info(f"Cleaned up {removed} orphaned worktree(s)")

    return removed
