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

import os
import re
import shutil
import subprocess

# Add shared directory to path for egg_logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
import contextlib

from egg_logging import get_logger

# Import git_cmd helper
try:
    from .git_client import git_cmd
except ImportError:
    from git_client import git_cmd  # type: ignore[no-redef, import-not-found]


logger = get_logger("gateway.worktree-manager")

# Default paths - hardcoded to /home/egg to match container mounts
# The gateway container runs as root but mounts are at /home/egg/*
# (see start-gateway.sh CONTAINER_HOME and git_client.py ALLOWED_REPO_PATHS)
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")
REPOS_BASE_DIR = Path("/home/egg/repos")


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
    """
    Ensure identifier contains only safe characters.

    Prevents path traversal attacks via container_id or repo_name containing '../'.

    Args:
        value: The identifier value to validate
        name: Name of the identifier (for error messages)

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
    """
    Manages git worktrees for container isolation.

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
    ):
        """
        Initialize the worktree manager.

        Args:
            worktree_base: Base directory for worktrees (default: ~/.egg-worktrees)
            repos_base: Base directory for main repos (default: ~/repos)
        """
        self.worktree_base = worktree_base or WORKTREE_BASE_DIR
        self.repos_base = repos_base or REPOS_BASE_DIR
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Track active worktrees in memory.
        # NOTE: Currently write-only; list_worktrees() scans the filesystem.
        # Kept for future use as an in-memory cache to avoid filesystem scans.
        self._active_worktrees: dict[str, list[WorktreeInfo]] = {}

        # Concurrency control
        self._lock = threading.Lock()  # protects _active_worktrees
        self._repo_locks: dict[str, threading.Lock] = {}  # per-repo locks for git ops
        self._repo_locks_guard = threading.Lock()  # protects _repo_locks dict

    def create_worktree(
        self,
        repo_name: str,
        container_id: str,
        base_branch: str = "HEAD",
        uid: int | None = None,
        gid: int | None = None,
    ) -> WorktreeInfo:
        """
        Create an isolated worktree for a container.

        Args:
            repo_name: Name of the repository
            container_id: Container identifier (e.g., 'egg-xxx-yyy')
            base_branch: Branch or ref to base the worktree on (default: HEAD)
            uid: User ID to set ownership to (default: 1000)
            gid: Group ID to set ownership to (default: 1000)

        Returns:
            WorktreeInfo with paths and branch information

        Raises:
            ValueError: If inputs are invalid or repo not found
            RuntimeError: If worktree creation fails
        """
        # Default to egg user (1000:1000) if not specified
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
        branch_name = f"egg/{container_id}/work"

        # Create container directory and set ownership immediately
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._chown_single(worktree_path.parent, uid, gid)

        # Check if worktree already exists AND is valid
        # A valid worktree has a .git file (not directory) containing "gitdir: ..."
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
            # Ensure ownership is correct (may have been created with different uid/gid)
            self._chown_recursive(worktree_path, uid, gid)
            self._chown_single(worktree_path.parent, uid, gid)
            # Return info about existing worktree
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

        # If rmtree couldn't fully remove (e.g., Docker bind mount), at minimum
        # remove the .git directory so git worktree add can create its .git FILE.
        if worktree_path.exists():
            git_path = worktree_path / ".git"
            if git_path.exists() and git_path.is_dir():
                shutil.rmtree(git_path, ignore_errors=True)

        # Serialize git operations against this repo to prevent index.lock contention
        with self._get_repo_lock(repo_name):
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
                result = self._run_git_worktree_add(
                    git_cmd("worktree", "add", str(worktree_path), branch_name),
                    cwd=main_repo,
                    main_repo=main_repo,
                    worktree_path=worktree_path,
                )
            else:
                # Create new branch from base
                result = self._run_git_worktree_add(
                    git_cmd(
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(worktree_path),
                        base_branch,
                    ),
                    cwd=main_repo,
                    main_repo=main_repo,
                    worktree_path=worktree_path,
                )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        # Set ownership so the container user can write to the worktree
        self._chown_recursive(worktree_path, uid, gid)
        # Also ensure the container directory itself is writable (non-recursive)
        self._chown_single(worktree_path.parent, uid, gid)

        # Find the actual git dir (git names it based on worktree basename)
        git_dir = self._find_worktree_git_dir(main_repo, worktree_path)

        info = WorktreeInfo(
            container_id=container_id,
            repo_name=repo_name,
            branch=branch_name,
            worktree_path=worktree_path,
            git_dir=git_dir,
        )

        # Track in memory
        with self._lock:
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

    def _get_repo_lock(self, repo_name: str) -> threading.Lock:
        """Get or create a per-repo lock for serializing git operations."""
        with self._repo_locks_guard:
            if repo_name not in self._repo_locks:
                self._repo_locks[repo_name] = threading.Lock()
            return self._repo_locks[repo_name]

    def _run_git_worktree_add(
        self,
        args: list[str],
        cwd: Path,
        main_repo: Path,
        worktree_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run ``git worktree add`` with retry on index.lock contention.

        The primary benefit of retrying is **waiting for a short-lived
        external lock to be released** (e.g., a concurrent ``git fetch``
        started outside the WorktreeManager).  The stale-lock cleanup
        (files older than 60 s) is a secondary safeguard for the rare
        case where a previous process crashed and left a lock behind.

        Between retries the helper also removes any partial worktree
        directory that ``git worktree add`` may have created before
        hitting the lock error, preventing the next attempt from
        failing with "already exists".

        Attempts up to 3 times with exponential backoff (0.1 s, 0.2 s,
        0.4 s).
        """
        max_attempts = 3
        backoff = 0.1

        for attempt in range(1, max_attempts + 1):
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                return result

            if "index.lock" not in result.stderr or attempt == max_attempts:
                return result

            # index.lock contention — try to clean stale lock and retry
            logger.warning(
                "index.lock contention, retrying",
                attempt=attempt,
                max_attempts=max_attempts,
                stderr=result.stderr.strip(),
            )

            # Look for stale lock files in the main repo and worktrees dir
            lock_candidates = [main_repo / ".git" / "index.lock"]
            worktrees_dir = main_repo / ".git" / "worktrees"
            if worktrees_dir.exists():
                lock_candidates.extend(worktrees_dir.glob("*/index.lock"))

            for lock_candidate in lock_candidates:
                if lock_candidate.exists():
                    try:
                        age = time.time() - lock_candidate.stat().st_mtime
                        if age > 60:
                            lock_candidate.unlink(missing_ok=True)
                            logger.info(
                                "Removed stale lock file",
                                path=str(lock_candidate),
                                age_seconds=round(age, 1),
                            )
                    except OSError:
                        pass

            # Clean up partial worktree state so the next attempt doesn't
            # fail with "already exists" or "already checked out".
            if worktree_path is not None and worktree_path.exists():
                git_file = worktree_path / ".git"
                worktree_is_valid = (
                    git_file.exists()
                    and git_file.is_file()
                    and git_file.read_text().strip().startswith("gitdir:")
                )
                if not worktree_is_valid:
                    logger.info(
                        "Removing partial worktree directory before retry",
                        path=str(worktree_path),
                        attempt=attempt,
                    )
                    shutil.rmtree(worktree_path, ignore_errors=True)

            time.sleep(backoff)
            backoff *= 2

        return result  # unreachable, but keeps type checkers happy

    def _chown_single(self, path: Path, uid: int, gid: int) -> None:
        """
        Change ownership of a single file or directory (non-recursive).

        When running as non-root (e.g., gateway with --user flag), chown will fail
        if trying to change to a different user. This is OK because files will
        already be owned by the current user.

        Args:
            path: Path to change ownership of
            uid: User ID to set
            gid: Group ID to set
        """
        try:
            os.chown(path, uid, gid)
        except PermissionError:
            # Running as non-root, can't chown - files already owned by current user
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
        """
        Recursively change ownership of a directory.

        When running as non-root (e.g., gateway with --user flag), chown will fail
        if trying to change to a different user. This is OK because files will
        already be owned by the current user.

        Args:
            path: Path to change ownership of
            uid: User ID to set
            gid: Group ID to set
        """
        try:
            # Use chown -R for efficiency on large directories
            result = subprocess.run(
                ["chown", "-R", f"{uid}:{gid}", str(path)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                error_msg = result.stderr.decode() if result.stderr else "unknown error"
                # Check if this is a permission error (running as non-root)
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
        """
        Find the git worktree admin directory.

        Git names worktree admin directories based on the basename of the worktree path.
        For /path/to/worktrees/{id}/{repo}, git creates .git/worktrees/{repo}.
        If multiple worktrees have the same basename, git appends a number.

        IMPORTANT: Always verifies the admin dir's gitdir file points to the
        correct worktree path.  Multiple worktrees can share the same basename
        (e.g., interactive container and pipeline both have ``egg``), so we
        must check the gitdir content — not just the directory name.

        Args:
            main_repo: Path to main repository
            worktree_path: Path to worktree working directory

        Returns:
            Path to worktree admin directory
        """
        worktrees_dir = main_repo / ".git" / "worktrees"
        basename = worktree_path.name
        default_git_dir = worktrees_dir / basename

        # Scan all admin dirs and verify via gitdir file content.
        # Multiple worktrees can share the same basename (e.g., "egg",
        # "egg1", "egg2"), so we must match by the full worktree path
        # recorded in the gitdir file — not just by directory name.
        if worktrees_dir.exists():
            for entry in worktrees_dir.iterdir():
                if not entry.name.startswith(basename):
                    continue
                gitdir_file = entry / "gitdir"
                if gitdir_file.exists():
                    try:
                        gitdir_content = gitdir_file.read_text().strip()
                        # The gitdir file contains the path to the worktree's
                        # .git *file* (e.g., /path/to/worktree/.git), so we
                        # must compare against worktree_path/.git — not the
                        # bare worktree directory.
                        expected = str(worktree_path / ".git")
                        if gitdir_content.rstrip("/") == expected.rstrip("/"):
                            return entry
                    except OSError:
                        continue

        return default_git_dir  # Return expected path even if not found

    def create_phase_worktree(
        self,
        repo_name: str,
        container_id: str,
        phase_id: str,
        base_branch: str = "HEAD",
        uid: int | None = None,
        gid: int | None = None,
    ) -> WorktreeInfo:
        """Create a sub-worktree for a specific plan phase (Tier 3 parallel dispatch).

        Creates a worktree branched from the pipeline worktree for isolated
        phase-level implementation. Branch naming: egg/<feature>/phase-N.

        Args:
            repo_name: Name of the repository
            container_id: Container identifier
            phase_id: Plan phase ID (e.g., 'phase-1')
            base_branch: Branch or ref to base the worktree on
            uid: User ID for ownership
            gid: Group ID for ownership

        Returns:
            WorktreeInfo for the phase worktree
        """
        # Sanitize phase_id for use in paths
        safe_phase_id = re.sub(r"[^a-zA-Z0-9-]", "-", phase_id)
        phase_container_id = f"{container_id}-{safe_phase_id}"

        # Validate
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")

        # Derive branch name: append phase to existing branch
        # e.g., egg/issue-732 -> egg/issue-732/phase-1
        if base_branch.startswith("egg/"):
            phase_branch = f"{base_branch}/{safe_phase_id}"
        else:
            phase_branch = f"egg/{container_id}/{safe_phase_id}"

        # Create worktree using existing infrastructure
        return self.create_worktree(
            repo_name=repo_name,
            container_id=phase_container_id,
            base_branch=base_branch,
            uid=uid,
            gid=gid,
        )

    def cleanup_phase_worktrees(
        self,
        container_id: str,
        repo_name: str,
        phase_ids: list[str] | None = None,
    ) -> list[WorktreeRemovalResult]:
        """Remove phase worktrees after integration.

        Cleans up sub-worktrees created by create_phase_worktree() after
        the integrator has merged sub-branches.

        Args:
            container_id: Container identifier
            repo_name: Repository name
            phase_ids: Specific phase IDs to clean up. If None, cleans all
                phase worktrees for this container.

        Returns:
            List of WorktreeRemovalResult for each cleaned worktree
        """
        results: list[WorktreeRemovalResult] = []

        if phase_ids:
            for phase_id in phase_ids:
                safe_phase_id = re.sub(r"[^a-zA-Z0-9-]", "-", phase_id)
                phase_container_id = f"{container_id}-{safe_phase_id}"
                result = self.remove_worktree(
                    container_id=phase_container_id,
                    repo_name=repo_name,
                    force=True,
                    delete_branch=True,
                )
                results.append(result)
        else:
            # Clean all phase worktrees for this container by scanning
            worktree_dir = self.worktree_base / repo_name
            if worktree_dir.exists():
                prefix = f"{container_id}-phase-"
                for entry in worktree_dir.iterdir():
                    if entry.name.startswith(prefix) and entry.is_dir():
                        # Extract the phase container ID from dir name
                        phase_container_id = entry.name
                        result = self.remove_worktree(
                            container_id=phase_container_id,
                            repo_name=repo_name,
                            force=True,
                            delete_branch=True,
                        )
                        results.append(result)

        return results

    def remove_worktree(
        self,
        container_id: str,
        repo_name: str,
        force: bool = False,
        delete_branch: bool = True,
    ) -> WorktreeRemovalResult:
        """
        Remove a container's worktree.

        Args:
            container_id: Container identifier
            repo_name: Repository name
            force: If True, remove even with uncommitted changes
            delete_branch: If True, delete the egg/{container_id}/work branch

        Returns:
            WorktreeRemovalResult with operation status
        """
        result = WorktreeRemovalResult(success=False)

        try:
            validate_identifier(container_id, "container_id")
            validate_identifier(repo_name, "repo_name")
        except ValueError as e:
            result.error = str(e)
            return result

        worktree_path = self.worktree_base / container_id / repo_name
        main_repo = self.repos_base / repo_name
        branch_name = f"egg/{container_id}/work"

        if not worktree_path.exists():
            result.success = True
            return result

        # Check for uncommitted changes and remove worktree under per-repo lock
        if main_repo.exists():
            with self._get_repo_lock(repo_name):
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

                # Find the admin dir BEFORE removal so we can clean it up
                # manually if `git worktree remove` fails.
                admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)

                remove_result = subprocess.run(
                    git_cmd("worktree", "remove", str(worktree_path), "--force"),
                    cwd=main_repo,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                if remove_result.returncode != 0:
                    # `git worktree remove` failed — clean up manually.
                    # Remove the worktree directory first, then surgically
                    # remove only this worktree's admin dir.  Avoids calling
                    # `git worktree prune` which can accidentally remove
                    # admin dirs for OTHER containers' worktrees if their
                    # paths are temporarily inaccessible (e.g., Docker mount
                    # race conditions during container lifecycle changes).
                    logger.info(
                        "Git worktree remove failed, cleaning up manually",
                        container_id=container_id,
                        repo=repo_name,
                        stderr=remove_result.stderr,
                    )
                    shutil.rmtree(worktree_path, ignore_errors=True)

                    # Remove the specific admin dir for this worktree
                    if admin_dir.exists():
                        shutil.rmtree(admin_dir, ignore_errors=True)
                        logger.info(
                            "Removed worktree admin dir",
                            admin_dir=str(admin_dir),
                            container_id=container_id,
                            repo=repo_name,
                        )

                # Delete the branch if requested
                if delete_branch:
                    result.branch_deleted = self._delete_worktree_branch(
                        main_repo, branch_name, force
                    )
                    if not result.branch_deleted and not force:
                        result.warning = (
                            (result.warning or "")
                            + f" Branch {branch_name} has unmerged commits and was not deleted."
                        ).strip()
        else:
            # Main repo not found, just remove the directory
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Clean up container directory if empty
        container_dir = self.worktree_base / container_id
        if container_dir.exists() and not any(container_dir.iterdir()):
            with contextlib.suppress(OSError):
                container_dir.rmdir()

        # Remove from memory tracking
        with self._lock:
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
        """
        Delete a worktree branch if it's safe to do so.

        Args:
            main_repo: Path to main repository
            branch_name: Name of branch to delete
            force: If True, force delete even if unmerged

        Returns:
            True if branch was deleted, False otherwise
        """
        # Check if branch is fully merged
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
        """
        List all active worktrees.

        Returns:
            List of worktree information dictionaries
        """
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
                    # Get branch info if possible
                    branch = None
                    git_file = repo_dir / ".git"
                    if git_file.exists():
                        try:
                            # Read gitdir from .git file
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

    def cleanup_orphaned_worktrees(
        self,
        active_containers: set[str],
        session_manager: Any | None = None,
    ) -> int:
        """
        Remove worktrees for containers that no longer exist.

        Called on gateway startup and periodically to clean up orphaned worktrees
        from crashed containers.

        For orphaned containers with active sessions, captures a session-end
        checkpoint with FAILED status before cleaning up transcript buffers.

        Args:
            active_containers: Set of currently active container IDs
            session_manager: Optional SessionManager for session-end checkpoints

        Returns:
            Number of worktrees removed
        """
        removed = 0

        if not self.worktree_base.exists():
            return removed

        for container_dir in list(self.worktree_base.iterdir()):
            if not container_dir.is_dir():
                continue

            container_id = container_dir.name

            # Skip active containers
            if container_id in active_containers:
                continue

            logger.info(
                "Cleaning up orphaned worktrees",
                container_id=container_id,
            )

            # Capture session-end checkpoint for crashed container
            if session_manager is not None:
                try:
                    session = session_manager.get_session_by_container(container_id)
                    if session:
                        from session_manager import _capture_and_cleanup_session  # type: ignore[import-not-found]  # noqa: I001

                        _capture_and_cleanup_session(session, "failed")
                except Exception as e:
                    logger.warning(
                        "Failed to capture checkpoint for orphaned container",
                        container_id=container_id,
                        error=str(e),
                    )

            # Remove each worktree
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

            # Remove container directory
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
        """
        Get worktree paths for path mapping.

        Used by the gateway to map container paths to worktree paths.

        Args:
            container_id: Container identifier
            repo_name: Repository name

        Returns:
            Tuple of (worktree_path, main_repo_path)

        Raises:
            ValueError: If inputs are invalid
        """
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")

        worktree_path = self.worktree_base / container_id / repo_name
        main_repo = self.repos_base / repo_name

        return worktree_path, main_repo


def get_active_docker_containers() -> set[str]:
    """
    Get set of currently running Docker container names.

    Returns:
        Set of container names that are currently running
    """
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


def startup_cleanup(
    active_containers: set[str] | None = None,
    session_manager: Any | None = None,
) -> int:
    """
    Clean up orphaned worktrees on gateway startup.

    Should be called when the gateway starts to clean up worktrees
    from containers that may have crashed.

    Args:
        active_containers: Set of active container IDs whose worktrees are
            preserved. Pass an empty set when no containers are active. When
            None, falls back to querying Docker (which may not be available
            inside the gateway container).
        session_manager: Optional SessionManager for session-end checkpoints

    Returns:
        Number of orphaned worktrees removed
    """
    manager = WorktreeManager()
    if active_containers is None:
        active_containers = get_active_docker_containers()

    logger.info(
        "Running startup worktree cleanup",
        active_containers=len(active_containers),
    )

    removed = manager.cleanup_orphaned_worktrees(active_containers, session_manager)

    if removed > 0:
        logger.info(f"Cleaned up {removed} orphaned worktree(s)")

    return removed
