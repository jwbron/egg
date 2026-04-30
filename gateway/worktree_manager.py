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
from collections.abc import Generator

from egg_git.cross_process_lock import bare_repo_lock
from egg_logging import get_logger

# Import git_cmd helper
try:
    from .git_client import git_cmd
except ImportError:
    from git_client import git_cmd  # type: ignore[no-redef, import-untyped]


logger = get_logger("gateway.worktree-manager")

# Default paths - hardcoded to /home/egg to match container mounts
# The gateway container runs as root but mounts are at /home/egg/*
# (see docker-compose.yml volumes and git_client.py ALLOWED_REPO_PATHS)
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")
REPOS_BASE_DIR = Path("/home/egg/repos")


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    size: float = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    container_id: str
    repo_name: str
    branch: str
    worktree_path: Path
    git_dir: (
        Path | None
    )  # Path to worktree admin directory in .git/worktrees/, or None if not found
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


def validate_branch_ref(value: str, name: str = "base_branch") -> None:
    """
    Ensure a git branch/ref name contains only safe characters.

    Similar to validate_identifier but also allows '/' for branch names
    like 'egg/issue-1495' or 'origin/main'.

    Args:
        value: The branch ref to validate
        name: Name of the parameter (for error messages)

    Raises:
        ValueError: If the ref contains unsafe characters
    """
    if not value:
        raise ValueError(f"Invalid {name}: cannot be empty")
    if "\x00" in value:
        raise ValueError(f"Invalid {name}: null bytes not allowed")
    if ".." in value:
        raise ValueError(f"Invalid {name}: '..' not allowed")
    if "//" in value:
        raise ValueError(f"Invalid {name}: consecutive slashes not allowed")
    if value.endswith("/") or value.endswith("."):
        raise ValueError(f"Invalid {name}: cannot end with '/' or '.'")
    if "/." in value:
        raise ValueError(f"Invalid {name}: component cannot start with '.'")
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]*$", value):
        raise ValueError(f"Invalid {name}: must be alphanumeric with ._-/ allowed")


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

    def resolve_default_branch(self, repo_name: str) -> str:
        """
        Resolve the remote's default branch for a repository.

        Tries in order:
        1. origin/HEAD symbolic ref (most reliable when configured)
        2. origin/main
        3. origin/master
        4. HEAD (fallback)

        Args:
            repo_name: Name of the repository

        Returns:
            The resolved branch reference (e.g., "origin/main")
        """
        main_repo = self.repos_base / repo_name
        if not main_repo.exists():
            return "HEAD"

        # Try origin/HEAD first (configured by git clone or git remote set-head)
        result = subprocess.run(
            git_cmd("symbolic-ref", "refs/remotes/origin/HEAD", "--short"),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Try origin/main
        result = subprocess.run(
            git_cmd("rev-parse", "--verify", "origin/main"),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return "origin/main"

        # Try origin/master
        result = subprocess.run(
            git_cmd("rev-parse", "--verify", "origin/master"),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return "origin/master"

        # Fallback to HEAD — this may re-introduce the push rejection from #860
        # for pipeline sessions, so log at error level.
        logger.error(
            "Could not resolve remote default branch, falling back to HEAD",
            repo=repo_name,
        )
        return "HEAD"

    def create_worktree(
        self,
        repo_name: str,
        container_id: str,
        base_branch: str = "HEAD",
        uid: int | None = None,
        gid: int | None = None,
        assigned_branch: str | None = None,
    ) -> WorktreeInfo:
        """
        Create an isolated worktree for a container.

        Args:
            repo_name: Name of the repository
            container_id: Container identifier (e.g., 'egg-xxx-yyy')
            base_branch: Branch or ref to base the worktree on (default: HEAD)
            uid: User ID to set ownership to (default: 1000)
            gid: Group ID to set ownership to (default: 1000)
            assigned_branch: Remote branch this worktree's pushes should
                target.  When set, configures ``branch.<local>.merge`` so
                the sandbox's push client builds a refspec that targets the
                assigned branch instead of the per-worktree local branch
                (which the gateway would reject as push_denied_wrong_branch).
                See #1809.

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
        validate_branch_ref(base_branch, "base_branch")
        if assigned_branch is not None:
            validate_branch_ref(assigned_branch, "assigned_branch")

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
                assigned_branch=assigned_branch,
            )
            # Ensure ownership is correct (may have been created with different uid/gid)
            self._chown_recursive(worktree_path, uid, gid)
            self._chown_single(worktree_path.parent, uid, gid)
            # Re-apply push upstream config and reset to a safe ref under
            # the per-repo lock — both write to ``.git/config`` (and the
            # latter to ``.git/index``), and concurrent callers without
            # the lock race on ``.git/config.lock`` (#2311).
            with self._get_repo_lock(repo_name):
                self._configure_push_upstream(main_repo, branch_name, assigned_branch)
                # Reset HEAD to a known-good ref so we don't inherit a stale
                # left-over HEAD from a prior pipeline that collided on
                # ``container_id`` (deterministic pipeline_id can collide when
                # the same issue is resubmitted — see #2222).  Without this,
                # the new pipeline's first push hits non-fast-forward and
                # the reconcile path can absorb upstream main commits onto
                # the pipeline branch.
                self._reset_reused_worktree_to_safe_ref(
                    worktree_path=worktree_path,
                    main_repo=main_repo,
                    container_id=container_id,
                    assigned_branch=assigned_branch,
                    base_branch=base_branch,
                )
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
            # Clean up stale git admin dir (.git/worktrees/<name>) left by a
            # previous worktree that was not properly removed (e.g. broken btrfs
            # mount after restart_phase).  Without this, `git worktree add` fails
            # with "already registered" even though the worktree itself is
            # invalid.  Must be inside the repo lock to avoid TOCTOU race with
            # concurrent create_worktree / remove_worktree calls.  (#1723)
            admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)
            if admin_dir is not None and admin_dir.exists():
                logger.warning(
                    "Removing stale worktree admin dir before recreation",
                    admin_dir=str(admin_dir),
                    container_id=container_id,
                    repo=repo_name,
                )
                shutil.rmtree(admin_dir, ignore_errors=True)

            # Check if branch already exists (from crashed session)
            branch_exists = (
                subprocess.run(
                    git_cmd("rev-parse", "--verify", branch_name),
                    cwd=main_repo,
                    capture_output=True,
                    text=True,
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
                # Resolve the base ref: if base_branch is not available
                # locally (e.g. a pipeline branch that only exists on the
                # remote), fetch it first and use origin/<base_branch>.
                effective_base = base_branch
                if base_branch != "HEAD":
                    local_ref_exists = (
                        subprocess.run(
                            git_cmd("rev-parse", "--verify", base_branch),
                            cwd=main_repo,
                            capture_output=True,
                            text=True,
                            check=False,
                        ).returncode
                        == 0
                    )
                    if not local_ref_exists:
                        logger.info(
                            "Base branch not found locally, fetching from remote",
                            base_branch=base_branch,
                            container_id=container_id,
                        )
                        try:
                            fetch_result = subprocess.run(
                                git_cmd("fetch", "origin", base_branch),
                                cwd=main_repo,
                                capture_output=True,
                                text=True,
                                check=False,
                                timeout=120,
                            )
                        except subprocess.TimeoutExpired as e:
                            raise RuntimeError(
                                f"Timed out fetching base branch '{base_branch}' from remote"
                            ) from e
                        if fetch_result.returncode == 0:
                            effective_base = f"origin/{base_branch}"
                        else:
                            raise RuntimeError(
                                f"Failed to fetch base branch '{base_branch}' from remote: "
                                f"{fetch_result.stderr.strip()}"
                            )

                # Create new branch from base
                result = self._run_git_worktree_add(
                    git_cmd(
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(worktree_path),
                        effective_base,
                    ),
                    cwd=main_repo,
                    main_repo=main_repo,
                    worktree_path=worktree_path,
                )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to create worktree: {result.stderr}")

            # Lock the worktree so git worktree prune never removes its admin
            # dir while the container is alive.  Without this, git gc --auto
            # (triggered e.g. by git fetch) can run git worktree prune and
            # delete the admin dir if the worktree path is momentarily
            # inaccessible, breaking all subsequent git operations in the
            # container.  Removal uses --force --force to override the lock
            # (a single --force only handles dirty worktrees, not locked ones).
            lock_result = subprocess.run(
                git_cmd("worktree", "lock", str(worktree_path)),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if lock_result.returncode != 0:
                logger.warning(
                    "Failed to lock worktree",
                    container_id=container_id,
                    repo=repo_name,
                    stderr=lock_result.stderr.strip(),
                )

        # Set ownership so the container user can write to the worktree
        self._chown_recursive(worktree_path, uid, gid)
        # Also ensure the container directory itself is writable (non-recursive)
        self._chown_single(worktree_path.parent, uid, gid)

        # Point the per-worktree local branch at the assigned remote branch
        # so `git push` resolves to a refspec the gateway will accept
        # (#1809).  Must happen before returning so the sandbox's push
        # client sees the config on its first push.  Held under the
        # per-repo lock so the ``.git/config`` write does not race the
        # state-store's commits in the orchestrator pod (#2311).
        with self._get_repo_lock(repo_name):
            self._configure_push_upstream(main_repo, branch_name, assigned_branch)

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

    def lookup_worktree(
        self,
        repo_name: str,
        container_id: str,
    ) -> WorktreeInfo:
        """Return info for an existing worktree without creating one.

        Used when the caller already created the worktree in a prior step
        and just needs its paths (e.g. session_create reusing a worktree
        that create_worktrees already made — see #1857).  Creating a second
        worktree for the same agent races on ``.git/config.lock`` in the
        bare repo and intermittently fails concurrent spawns.

        Args:
            repo_name: Name of the repository.
            container_id: Container id under which the worktree was created.

        Returns:
            WorktreeInfo describing the existing worktree.

        Raises:
            ValueError: If inputs are invalid, the repo doesn't exist, or
                no valid worktree is present at the expected path.

        Note:
            Unlike ``create_worktree``'s reuse path, this method deliberately
            skips ``_chown_recursive`` and ``_configure_push_upstream``.  In
            the current flow both ``create_worktrees`` and ``register_session``
            run with the same ``host_uid``/``host_gid``, and push upstream was
            already configured by the original ``create_worktree`` call.
        """
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")

        main_repo = self.repos_base / repo_name
        if not main_repo.exists():
            raise ValueError(f"Repository not found: {repo_name}")

        worktree_path = self.worktree_base / container_id / repo_name
        git_file = worktree_path / ".git"
        if not (
            worktree_path.exists()
            and git_file.is_file()
            and git_file.read_text().strip().startswith("gitdir:")
        ):
            raise ValueError(
                f"Worktree not found for container_id={container_id} "
                f"repo={repo_name} at {worktree_path}"
            )

        return WorktreeInfo(
            container_id=container_id,
            repo_name=repo_name,
            branch=f"egg/{container_id}/work",
            worktree_path=worktree_path,
            git_dir=self._find_worktree_git_dir(main_repo, worktree_path),
        )

    @contextlib.contextmanager
    def _get_repo_lock(self, repo_name: str) -> Generator[None]:
        """Hold the per-repo lock for serializing git operations.

        Combines two layers:

        * A per-repo ``threading.Lock`` for in-process serialization (the
          original #1857 / #1863 fix).
        * ``bare_repo_lock`` for cross-process serialization against the
          orchestrator's state-store, which runs git from a different pod
          but shares the same hostPath-mounted bare repo (#2311).  Without
          this, parallel ``git worktree add`` calls race the state-store's
          commits on ``.git/config.lock`` and fail with ``could not lock
          config file .git/config: File exists``.
        """
        with self._repo_locks_guard:
            if repo_name not in self._repo_locks:
                self._repo_locks[repo_name] = threading.Lock()
            thread_lock = self._repo_locks[repo_name]
        with thread_lock, bare_repo_lock(self.repos_base / repo_name):
            yield

    def _configure_push_upstream(
        self,
        main_repo: Path,
        branch_name: str,
        assigned_branch: str | None,
    ) -> None:
        """Configure the per-worktree branch to push to the assigned branch.

        Without this, the sandbox's push client (``sandbox/egg_lib/orch_cli.py``)
        reads ``branch.<local>.merge`` and — finding it unset — sends the
        local branch name as the push destination.  The gateway's
        ``push_denied_wrong_branch`` policy then rejects the push because
        ``egg/{container_id}/work`` differs from the pipeline's assigned
        branch.  Agents sometimes "recover" from that rejection with
        ``git reset --hard``, destroying their own committed work
        (#1809).

        Best-effort: logs and returns on failure rather than aborting the
        worktree creation, since the old behaviour (no upstream) is still
        workable for non-pipeline sessions.
        """
        if not assigned_branch or assigned_branch == branch_name:
            return

        for key, value in (
            (f"branch.{branch_name}.remote", "origin"),
            (f"branch.{branch_name}.merge", f"refs/heads/{assigned_branch}"),
        ):
            result = subprocess.run(
                git_cmd("config", key, value),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to configure push upstream for worktree branch",
                    branch=branch_name,
                    assigned_branch=assigned_branch,
                    key=key,
                    stderr=result.stderr.strip(),
                )
                return

    def _reset_reused_worktree_to_safe_ref(
        self,
        worktree_path: Path,
        main_repo: Path,
        container_id: str,
        assigned_branch: str | None,
        base_branch: str,
    ) -> None:
        """Hard-reset a reused worktree to a known-good remote ref.

        Picks the ref in this order:

        1. ``origin/{assigned_branch}`` if ``assigned_branch`` is set and
           resolvable.  This is the pipeline's own branch tip — by the
           time we reach this code the orchestrator's create-pipeline
           stale-branch check (#2222 Phase 3a) has already refused
           re-submits where ``origin/{assigned_branch}`` carries
           prior-pipeline commits, so a reset to it discards only
           container-local state.
        2. ``origin/{base_branch}`` if ``base_branch != "HEAD"`` and
           resolvable.  Used when the assigned branch hasn't been
           pushed yet (fresh pipeline, first agent) so there is no
           remote tip to reset to.
        3. No-op if neither resolves — preserves prior behaviour rather
           than risk leaving the worktree in an undefined state.

        Best-effort: any git failure is logged and swallowed so a
        transient hiccup doesn't break worktree reuse.  The downstream
        orchestrator-side ``_sync_worktree_with_remote`` and
        ``_rebase_pipeline_branch_onto_base`` provide a second line of
        defence.
        """
        # Best-effort fetch so the remote-tracking refs are current; if
        # this fails we still attempt the reset against whatever local
        # state we have.  This runs inside the cross-process lock (the
        # caller holds it across this whole method) so the timeout
        # caps how long every other state-store commit / worktree
        # create on this repo can be blocked by a slow remote — keep
        # it tight.
        try:
            subprocess.run(
                git_cmd("fetch", "origin"),
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "Fetch before worktree-reuse reset failed (continuing)",
                container_id=container_id,
                worktree_path=str(worktree_path),
                error=str(exc),
            )

        target_ref: str | None = None
        candidates: list[str] = []
        if assigned_branch:
            candidates.append(f"origin/{assigned_branch}")
        if base_branch and base_branch != "HEAD":
            candidates.append(f"origin/{base_branch}")
        for candidate in candidates:
            verify = subprocess.run(
                git_cmd("rev-parse", "--verify", candidate),
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if verify.returncode == 0:
                target_ref = candidate
                break

        if target_ref is None:
            logger.info(
                "Worktree reuse: no remote ref to reset to (preserving HEAD)",
                container_id=container_id,
                worktree_path=str(worktree_path),
                assigned_branch=assigned_branch,
                base_branch=base_branch,
            )
            return

        reset = subprocess.run(
            git_cmd("reset", "--hard", target_ref),
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if reset.returncode != 0:
            logger.warning(
                "Worktree reuse: reset to safe ref failed (continuing)",
                container_id=container_id,
                worktree_path=str(worktree_path),
                target_ref=target_ref,
                stderr=reset.stderr.strip(),
            )
            return
        logger.info(
            "Worktree reuse: reset HEAD to safe remote ref",
            container_id=container_id,
            worktree_path=str(worktree_path),
            target_ref=target_ref,
        )

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

        Attempts up to 5 times with exponential backoff (0.5 s, 1.0 s,
        2.0 s, 4.0 s).
        """
        max_attempts = 5
        backoff = 0.5

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

    def _find_worktree_git_dir(self, main_repo: Path, worktree_path: Path) -> Path | None:
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
            Path to worktree admin directory, or None if no matching admin dir
            was found.  Callers MUST handle None to avoid deleting an admin dir
            that belongs to a different container.
        """
        worktrees_dir = main_repo / ".git" / "worktrees"
        basename = worktree_path.name

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

        # No admin dir matched this worktree.  Do NOT fall back to the
        # default path — it may belong to a different container sharing the
        # same basename.  See: https://github.com/jwbron/egg/issues/1245
        return None

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
        sub-branches have been merged.

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
            # Clean all phase worktrees for this container by scanning.
            # Match "{container_id}-{phase}" where {phase} has the shape
            # of a sanitized phase ID ([a-zA-Z0-9-]+).  A naive
            # `startswith(prefix)` could collide if another container_id
            # extends this one (same bug class as #1865).  In practice
            # the risk is low because container_id already includes the
            # agent role suffix (e.g. "issue-1758-coder"), but
            # constraining the suffix shape adds a layer of safety.
            worktree_dir = self.worktree_base / repo_name
            if worktree_dir.exists():
                phase_pattern = re.compile(rf"{re.escape(container_id)}-[a-zA-Z0-9-]+")
                for entry in worktree_dir.iterdir():
                    if (
                        entry.is_dir()
                        and entry.name != container_id
                        and phase_pattern.fullmatch(entry.name)
                    ):
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
            # Directory already gone (e.g., Docker cleanup), but git may still
            # have a stale worktree registration in .git/worktrees/.  Clean it
            # up so `git worktree list` doesn't show the dead entry and branch
            # checkout isn't blocked.  See: https://github.com/jwbron/egg/issues/929
            if main_repo.exists():
                with self._get_repo_lock(repo_name):
                    admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)
                    if admin_dir is not None and admin_dir.exists():
                        shutil.rmtree(admin_dir, ignore_errors=True)
                        logger.info(
                            "Removed stale worktree admin dir (directory already gone)",
                            admin_dir=str(admin_dir),
                            container_id=container_id,
                            repo=repo_name,
                        )
                    elif admin_dir is None:
                        logger.info(
                            "No matching admin dir found for stale worktree, skipping cleanup",
                            container_id=container_id,
                            repo=repo_name,
                        )

                    if delete_branch:
                        result.branch_deleted = self._delete_worktree_branch(
                            main_repo, branch_name, force=True
                        )

            # Clean up container directory if empty
            container_dir = self.worktree_base / container_id
            if container_dir.exists() and not any(container_dir.iterdir()):
                with contextlib.suppress(OSError):
                    container_dir.rmdir()

            # Remove from memory tracking
            with self._lock:
                if container_id in self._active_worktrees:
                    self._active_worktrees[container_id] = [
                        wt
                        for wt in self._active_worktrees[container_id]
                        if wt.repo_name != repo_name
                    ]
                    if not self._active_worktrees[container_id]:
                        del self._active_worktrees[container_id]

            logger.info(
                "Stale worktree cleaned up (directory already removed)",
                container_id=container_id,
                repo=repo_name,
                branch_deleted=result.branch_deleted,
            )
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
                    git_cmd("worktree", "remove", str(worktree_path), "--force", "--force"),
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

                    # Remove the specific admin dir for this worktree.
                    # admin_dir is None when no admin dir matched — skip
                    # deletion to avoid destroying another container's state.
                    # See: https://github.com/jwbron/egg/issues/1245
                    if admin_dir is not None and admin_dir.exists():
                        shutil.rmtree(admin_dir, ignore_errors=True)
                        logger.info(
                            "Removed worktree admin dir",
                            admin_dir=str(admin_dir),
                            container_id=container_id,
                            repo=repo_name,
                        )
                    elif admin_dir is None:
                        logger.warning(
                            "No matching admin dir found during manual cleanup, "
                            "skipping admin dir deletion to avoid cross-container damage",
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
                                if not gitdir_path.is_absolute():
                                    gitdir_path = (repo_dir / gitdir_path).resolve()
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

    def list_worktrees_for_pipeline(self, pipeline_id: str) -> list[WorktreeInfo]:
        """List all worktrees for a pipeline (all agent roles).

        Per-agent worktrees (#1481) use IDs of the form
        '{pipeline_id}-{role}', so we scan for all container directories
        matching this prefix as well as the base pipeline worktree.

        Args:
            pipeline_id: Pipeline identifier to scan for.

        Returns:
            List of WorktreeInfo for every repo worktree belonging to
            this pipeline (including both the shared orchestrator
            worktree and per-agent worktrees).
        """
        results: list[WorktreeInfo] = []
        if not self.worktree_base.exists():
            return results

        # Only match the pipeline-level worktree or "{pipeline_id}-{role}"
        # where {role} is shaped like an AgentRole value (lower-case
        # letters and underscores, no hyphens).  A naive `startswith`
        # match collides when one pipeline ID is a prefix of another —
        # e.g. `issue-1758` would spuriously match
        # `issue-1758-worktree-fix-tester` (#1865).
        #
        # Assumes AgentRole values match [a-z_]+ — update if the enum
        # gains values with digits or other characters.
        per_agent = re.compile(rf"{re.escape(pipeline_id)}-[a-z_]+")
        for entry in self.worktree_base.iterdir():
            if not entry.is_dir():
                continue
            if entry.name != pipeline_id and not per_agent.fullmatch(entry.name):
                continue
            for repo_dir in entry.iterdir():
                if not repo_dir.is_dir():
                    continue
                # Try to read branch from .git file
                branch = ""
                git_file = repo_dir / ".git"
                if git_file.exists() and git_file.is_file():
                    try:
                        gitdir_content = git_file.read_text().strip()
                        if gitdir_content.startswith("gitdir: "):
                            gitdir_path = Path(gitdir_content[8:])
                            if not gitdir_path.is_absolute():
                                gitdir_path = (repo_dir / gitdir_path).resolve()
                            head_file = gitdir_path / "HEAD"
                            if head_file.exists():
                                head_content = head_file.read_text().strip()
                                if head_content.startswith("ref: refs/heads/"):
                                    branch = head_content[16:]
                    except OSError:
                        pass

                results.append(
                    WorktreeInfo(
                        container_id=entry.name,
                        repo_name=repo_dir.name,
                        branch=branch,
                        worktree_path=repo_dir,
                        git_dir=None,
                    )
                )

        return results

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

            # Skip worktrees that this process just created.  create_worktree
            # populates ``_active_worktrees[container_id]`` before returning,
            # so any per-agent worktree made during this gateway's lifetime
            # is protected even if its session was not yet registered when
            # ``active_containers`` was captured — closing the spawn-vs-prune
            # race that motivated #1874.  Re-checked per iteration so a
            # worktree created mid-sweep is still shielded.
            with self._lock:
                if container_id in self._active_worktrees:
                    continue

            logger.info(
                "Cleaning up orphaned worktrees",
                container_id=container_id,
            )

            # Capture session-end checkpoint for crashed container.
            # Wait for checkpoint storage to complete before removing
            # worktrees — the checkpoint thread uses the repo dir as cwd.
            if session_manager is not None:
                try:
                    session = session_manager.get_session_by_container(container_id)
                    if session:
                        from checkpoint_handler import SESSION_END_CAPTURE_TIMEOUT  # type: ignore[import-untyped]  # noqa: I001
                        from session_manager import _capture_and_cleanup_session  # type: ignore[import-untyped]  # noqa: I001

                        checkpoint_event = _capture_and_cleanup_session(session, "failed")
                        if checkpoint_event is not None:
                            checkpoint_event.wait(timeout=SESSION_END_CAPTURE_TIMEOUT)
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

    def prune_stale_worktrees(self) -> int:
        """
        Run ``git worktree prune`` on every repo to remove registrations
        whose working directories no longer exist.

        This is a defense-in-depth measure intended to be called at gateway
        startup (after orphan cleanup), when the set of active containers is
        known and Docker mount races are not a concern.  It catches any stale
        entries that slipped past ``remove_worktree`` — for example if the
        gateway crashed before cleanup could run.

        Note: ``git worktree prune`` respects locks.  Because
        ``create_worktree`` locks every worktree to protect it from
        ``git gc --auto``, a stale locked admin dir (e.g., gateway crash +
        Docker cleanup) will **not** be pruned by this method.  The
        ``cleanup_orphaned_worktrees`` → ``remove_worktree`` path handles
        this case via its manual ``shutil.rmtree`` fallback.

        Returns:
            Number of repos where ``git worktree prune`` ran successfully.
        """
        repos_checked = 0
        if not self.repos_base.exists():
            return repos_checked

        for repo_dir in self.repos_base.iterdir():
            if not repo_dir.is_dir():
                continue
            git_dir = repo_dir / ".git"
            # Only prune actual repos (with a .git directory, not a .git file
            # which would indicate a worktree itself)
            if not git_dir.is_dir():
                continue

            repo_name = repo_dir.name
            with self._get_repo_lock(repo_name):
                try:
                    result = subprocess.run(
                        git_cmd("worktree", "prune"),
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "git worktree prune timed out",
                        repo=repo_name,
                    )
                    continue
                if result.returncode == 0:
                    repos_checked += 1
                else:
                    logger.warning(
                        "git worktree prune failed",
                        repo=repo_name,
                        stderr=result.stderr.strip(),
                    )

        if repos_checked > 0:
            logger.info(
                "Ran git worktree prune on repos",
                repos_checked=repos_checked,
            )

        return repos_checked

    def git_worktree_prune_all(self) -> dict[str, list[str]]:
        """Run ``git worktree prune -v`` on each repo, returning pruned paths.

        Unlike :meth:`prune_stale_worktrees` (which returns the *count* of
        repos it ran against), this variant captures the porcelain output
        so callers can display the specific paths git reported as stale.
        Intended for operator-facing tooling where the set of removed
        registrations is interesting on its own.

        Returns:
            Dict keyed by repo name, each value a list of paths git
            reported as pruned (may be empty). Repos git skipped due to
            locks are not present.
        """
        result: dict[str, list[str]] = {}
        if not self.repos_base.exists():
            return result

        for repo_dir in self.repos_base.iterdir():
            if not repo_dir.is_dir():
                continue
            git_dir = repo_dir / ".git"
            if not git_dir.is_dir():
                continue
            repo_name = repo_dir.name
            with self._get_repo_lock(repo_name):
                try:
                    proc = subprocess.run(
                        git_cmd("worktree", "prune", "--verbose"),
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "git worktree prune timed out",
                        repo=repo_name,
                    )
                    continue
                if proc.returncode != 0:
                    logger.warning(
                        "git worktree prune failed",
                        repo=repo_name,
                        stderr=proc.stderr.strip(),
                    )
                    continue
                # -v output format: "Removing worktrees/<name>: <reason>"
                paths: list[str] = []
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.lower().startswith("removing "):
                        tail = line[len("Removing ") :]
                        name = tail.split(":", 1)[0].strip()
                        if name:
                            paths.append(name)
                result[repo_name] = paths
        return result

    def list_orphan_worktree_dirs(self, active_containers: set[str]) -> list[str]:
        """Return absolute paths of container dirs considered orphaned.

        A container dir under ``worktree_base`` is considered orphaned
        when its name is not in *active_containers*.  Each returned path
        is first validated via :func:`Path.resolve` +
        ``is_relative_to(self.worktree_base)`` to protect against
        symlink-based traversal.
        """
        orphans: list[str] = []
        if not self.worktree_base.exists():
            return orphans

        base_resolved = self.worktree_base.resolve()
        for child in self.worktree_base.iterdir():
            if not child.is_dir():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            # Path-traversal guard: every candidate must live under the
            # worktree base even after symlink resolution.
            try:
                if not resolved.is_relative_to(base_resolved):
                    logger.warning(
                        "worktree dir resolves outside base; skipping",
                        child=str(child),
                        resolved=str(resolved),
                    )
                    continue
            except AttributeError:
                # Python <3.9 fallback (should never hit on our runtime)
                if not str(resolved).startswith(str(base_resolved) + os.sep):
                    continue
            if child.name in active_containers:
                continue
            # Mirror the _active_worktrees guard from cleanup_orphaned_worktrees
            # so dry-run output accurately reflects what cleanup would skip.
            with self._lock:
                if child.name in self._active_worktrees:
                    continue
            orphans.append(str(child))
        return orphans

    def cleanup_orphaned_pack_files(
        self,
        repo_name: str | None = None,
        max_age_seconds: float | None = None,
    ) -> tuple[int, int]:
        """
        Remove orphaned temporary pack files from git repositories.

        Git creates ``tmp_pack_*``, ``tmp_obj_*``, and ``tmp_idx_*`` files in
        ``.git/objects/pack/`` during fetch/clone/repack operations.  When the
        operation completes, git renames them to their final names.  When the
        operation is **interrupted** (SIGKILL, OOM, container stop, timeout),
        the temp files are left behind as orphans — git never garbage-collects
        them automatically, especially with ``gc.auto=0``.

        These files are always safe to delete when no git operation is actively
        writing them.  Use ``max_age_seconds`` to avoid racing with concurrent
        operations at runtime.  At startup (no concurrent ops), pass
        ``max_age_seconds=None`` to clean everything.

        Args:
            repo_name: If set, only clean this specific repo.  Otherwise scan
                all repos in ``repos_base``.
            max_age_seconds: If set, only remove files whose mtime is older
                than this many seconds ago.  None means remove all.

        Returns:
            Tuple of (files_removed, bytes_reclaimed).
        """
        files_removed = 0
        bytes_reclaimed = 0

        if not self.repos_base.exists():
            return files_removed, bytes_reclaimed

        if repo_name is not None:
            repo_dirs = [self.repos_base / repo_name]
        else:
            try:
                repo_dirs = [d for d in self.repos_base.iterdir() if d.is_dir()]
            except OSError as e:
                logger.warning("Failed to list repos_base", error=str(e))
                return files_removed, bytes_reclaimed

        now = time.time()

        for repo_dir in repo_dirs:
            git_dir = repo_dir / ".git"
            # Only process actual repos (with a .git directory, not a .git file
            # which would indicate a worktree itself)
            if not git_dir.is_dir():
                continue

            pack_dir = git_dir / "objects" / "pack"
            if not pack_dir.is_dir():
                continue

            safe_name = repo_dir.name
            with self._get_repo_lock(safe_name):
                try:
                    for entry in pack_dir.iterdir():
                        name = entry.name
                        if not (
                            name.startswith("tmp_pack_")
                            or name.startswith("tmp_obj_")
                            or name.startswith("tmp_idx_")
                        ):
                            continue

                        if not entry.is_file():
                            continue

                        try:
                            st = entry.stat()
                        except OSError:
                            continue

                        if max_age_seconds is not None:
                            if (now - st.st_mtime) < max_age_seconds:
                                continue

                        try:
                            entry.unlink()
                            files_removed += 1
                            bytes_reclaimed += st.st_size
                        except OSError as e:
                            logger.warning(
                                "Failed to remove orphaned pack file",
                                file=str(entry),
                                error=str(e),
                            )
                except OSError as e:
                    logger.warning(
                        "Failed to scan pack directory",
                        repo=safe_name,
                        pack_dir=str(pack_dir),
                        error=str(e),
                    )

        if files_removed > 0:
            logger.info(
                "Cleaned up orphaned pack files",
                files_removed=files_removed,
                bytes_reclaimed=bytes_reclaimed,
                bytes_reclaimed_human=_format_bytes(bytes_reclaimed),
            )

        return files_removed, bytes_reclaimed

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

    def cleanup_clean_worktree(self, container_id: str, repo_name: str) -> bool:
        """Remove a worktree that has no uncommitted changes.

        Called after container exit for worktrees without uncommitted work.

        Returns:
            True if cleaned up, False if has uncommitted changes or error.
        """
        worktree_path = self.worktree_base / container_id / repo_name
        if not worktree_path.exists():
            return True  # Already cleaned

        # Check for uncommitted changes
        try:
            result = subprocess.run(
                git_cmd("status", "--porcelain"),
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info(
                    "Worktree has uncommitted changes, preserving for HITL",
                    container_id=container_id,
                    repo_name=repo_name,
                )
                return False
        except Exception as e:
            logger.warning(
                "Failed to check worktree status for cleanup",
                container_id=container_id,
                error=str(e),
            )
            return False

        # Clean worktree -- remove it.  Use force=False as a TOCTOU safety net:
        # if changes were made between the check and removal, git will refuse
        # rather than silently discarding.  (#1494 review)
        removal = self.remove_worktree(container_id, repo_name, force=False, delete_branch=True)
        return removal.success

    def cleanup_stale_pipeline_worktrees(
        self, max_age_hours: int = 48, active_containers: set[str] | None = None
    ) -> int:
        """Remove worktrees older than max_age_hours regardless of state.

        Periodic cleanup to prevent disk space exhaustion from abandoned
        worktrees.

        TODO: Wire this into the orchestrator's maintenance loop. Currently
        only called from tests — not yet connected to production scheduling.

        Args:
            max_age_hours: Worktrees inactive for longer than this are removed.
            active_containers: Set of running container IDs. Worktrees with
                active containers are never deleted. If None, fetched via
                ``get_active_docker_containers()``.

        Returns:
            Number of worktrees removed.
        """
        removed = 0
        if not self.worktree_base.exists():
            return removed

        if active_containers is None:
            active_containers = get_active_docker_containers()

        cutoff = time.time() - (max_age_hours * 3600)

        for entry in self.worktree_base.iterdir():
            if not entry.is_dir():
                continue
            # Skip worktrees whose containers are still running.
            if entry.name in active_containers:
                continue
            try:
                # Use .git/index (updated on every commit/checkout) as the
                # staleness signal instead of walking the entire tree, which
                # would cause an I/O storm on large worktrees containing
                # .git/objects, node_modules, build artifacts, etc.
                newest_mtime = entry.stat().st_mtime
                for repo_subdir in entry.iterdir():
                    if not repo_subdir.is_dir():
                        continue
                    dot_git = repo_subdir / ".git"
                    try:
                        # Worktrees have a .git *file* (not directory)
                        # containing "gitdir: /path/to/admin/dir".
                        # Resolve to the actual git admin dir to check
                        # index/HEAD mtime.
                        if dot_git.is_file():
                            gitdir_line = dot_git.read_text().strip()
                            if gitdir_line.startswith("gitdir:"):
                                git_admin = Path(gitdir_line.split(":", 1)[1].strip())
                                if not git_admin.is_absolute():
                                    git_admin = (repo_subdir / git_admin).resolve()
                                for git_file in ("index", "HEAD"):
                                    try:
                                        fmt = os.stat(git_admin / git_file).st_mtime
                                        if fmt > newest_mtime:
                                            newest_mtime = fmt
                                    except OSError:
                                        continue
                        elif dot_git.is_dir():
                            # Full clone (shouldn't happen but handle it)
                            for git_file in ("index", "HEAD"):
                                try:
                                    fmt = os.stat(dot_git / git_file).st_mtime
                                    if fmt > newest_mtime:
                                        newest_mtime = fmt
                                except OSError:
                                    continue
                    except OSError:
                        continue
                if newest_mtime < cutoff:
                    for repo_dir in entry.iterdir():
                        if repo_dir.is_dir():
                            removal_result = self.remove_worktree(
                                entry.name, repo_dir.name, force=True, delete_branch=True
                            )
                            if removal_result.success:
                                removed += 1
                            else:
                                logger.warning(
                                    "Failed to remove stale worktree",
                                    container_id=entry.name,
                                    repo=repo_dir.name,
                                    error=removal_result.error,
                                )
                    # Clean up empty container directory
                    if entry.exists() and not any(entry.iterdir()):
                        entry.rmdir()
            except Exception as e:
                logger.warning(
                    "Error during stale worktree cleanup",
                    container_id=entry.name,
                    error=str(e),
                )

        logger.info(
            "Stale worktree cleanup complete",
            removed=removed,
            max_age_hours=max_age_hours,
        )
        return removed


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
    except subprocess.TimeoutExpired, FileNotFoundError:
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

    # Defense-in-depth: prune any remaining stale git worktree registrations
    # whose working directories no longer exist.  Safe at startup because the
    # set of active containers is known and no Docker mount races can occur.
    try:
        manager.prune_stale_worktrees()
    except Exception as e:
        logger.warning("git worktree prune failed during startup", error=str(e))

    # Clean up orphaned temporary pack files left by interrupted git operations
    # (fetch/clone/repack).  Safe at startup because no git operations are
    # running yet, so all tmp_pack_*/tmp_obj_*/tmp_idx_* files are orphans.
    try:
        manager.cleanup_orphaned_pack_files()
    except Exception as e:
        logger.warning("Pack file cleanup failed during startup", error=str(e))

    return removed
