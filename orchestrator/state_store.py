"""
Git-backed state persistence for pipeline state.

All pipeline state lives on a dedicated ``egg/pipeline-state`` orphan branch,
accessed via a persistent git worktree.  The main checkout is never modified.

Read/write operations go directly to the worktree directory on disk.  Commits
are made in-place inside the worktree and stay on the state branch.

The state branch is synced to the remote after every commit (best-effort,
async push via a daemon thread).  On startup, if the local branch does not
exist, it is restored from the remote — mirroring the ``egg/checkpoints/v2``
pattern for cross-host recovery.
"""

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from models import Pipeline, PipelineMode, PipelineStatus
from pydantic import ValidationError

logger = logging.getLogger("orchestrator.state_store")

# Valid pipeline ID formats:
#   issue-{number}[-qualifier[-...]]  — GitHub issue-driven
#   {LETTERS}-{digits}[-qualifier[-...]] — JIRA ticket-driven (e.g. KORE-1234, KORE-1234-v2-hotfix)
#   local-{8 hex chars}         — local dev
#   pipeline-{8 hex chars}      — auto-generated
#   pr-{number}                 — babysit mode
PIPELINE_ID_PATTERN = re.compile(
    r"^("
    r"issue-[0-9]+(-[a-z0-9]+)*"
    r"|[A-Z][A-Z0-9]+-[0-9]+(-[a-z0-9]+)*"
    r"|local-[0-9a-f]{8}"
    r"|pipeline-[0-9a-f]{8}"
    r"|pr-[0-9]+"
    r")$"
)

# Dedicated branch for pipeline state (orphan, never merged into main).
# Shared constant — also referenced by gateway.py's INFRASTRUCTURE_BRANCHES.
from egg_config.constants import PIPELINE_STATE_BRANCH as STATE_BRANCH

# Relative to the Docker state volume (/home/egg/.egg-state)
_DEFAULT_WORKTREE_DIR = (
    Path(os.environ.get("EGG_STATE_DIR", "/home/egg/.egg-state")) / "pipeline-worktree"
)


class StateStoreError(Exception):
    """Base exception for state store errors."""

    pass


class PipelineNotFoundError(StateStoreError):
    """Pipeline state not found."""

    pass


class StateValidationError(StateStoreError):
    """Pipeline state validation failed."""

    pass


class GitOperationError(StateStoreError):
    """Git operation failed."""

    pass


class InvalidPipelineIdError(StateStoreError):
    """Invalid pipeline ID format."""

    pass


class VersionConflictError(StateStoreError):
    """Optimistic locking version conflict."""

    pass


def _validate_pipeline_id(pipeline_id: str) -> None:
    """Validate pipeline ID format to prevent path traversal attacks.

    Args:
        pipeline_id: Pipeline ID to validate

    Raises:
        InvalidPipelineIdError: If pipeline ID format is invalid
    """
    if not pipeline_id or not PIPELINE_ID_PATTERN.match(pipeline_id):
        raise InvalidPipelineIdError(f"Invalid pipeline ID format: {pipeline_id}")


class StateStore:
    """Git-backed state store for pipeline state.

    All state files live in a persistent git worktree on the
    ``egg/pipeline-state`` orphan branch.  The main repo checkout
    is never modified.
    """

    PIPELINES_DIR = ".egg-state/pipelines"

    # -- cross-process git serialization ------------------------------------
    # RLock allows compound operations (_commit_state, delete_pipeline) to
    # hold the lock while inner _run_git calls re-enter without deadlocking.
    # fcntl.flock provides cross-process serialization via the shared
    # filesystem — threading locks only protect within a single process.
    _thread_lock: ClassVar[threading.RLock] = threading.RLock()
    _flock_fds: ClassVar[dict[str, int]] = {}
    _flock_depth: ClassVar[int] = 0  # nesting depth, protected by _thread_lock

    # -- remote sync state -------------------------------------------------
    _push_in_flight: ClassVar[bool] = False
    _push_pending: ClassVar[bool] = False
    _push_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        repo_path: Path,
        worktree_dir: Path | None = None,
    ):
        """Initialize state store for a repository.

        Args:
            repo_path: Path to the main git repository
            worktree_dir: Override the persistent worktree location
                (default: ``/home/egg/.egg-state/pipeline-worktree``)
        """
        self.repo_path = repo_path
        self._worktree_dir = worktree_dir or _DEFAULT_WORKTREE_DIR
        self._worktree: Path | None = None  # lazily initialised

    # -- cross-process locking ---------------------------------------------

    @property
    def _lock_path(self) -> Path:
        """Lock file for cross-process git serialization."""
        return self._worktree_dir.parent / ".git-ops.lock"

    @classmethod
    def _get_flock_fd(cls, lock_path: Path) -> int:
        """Get or create a file descriptor for cross-process flock."""
        key = str(lock_path)
        if key not in cls._flock_fds:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            cls._flock_fds[key] = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        return cls._flock_fds[key]

    @contextmanager
    def _git_op(self) -> Generator[None]:
        """Acquire thread + process locks for git operations.

        Combines a reentrant threading lock (for in-process thread
        serialization) with an ``fcntl.flock`` file lock (for cross-process
        serialization via shared filesystem).

        Reentrant: safe to nest.  Compound operations (e.g. ``_commit_state``)
        hold the lock for their entire duration while inner ``_run_git`` calls
        re-enter without releasing.
        """
        self._thread_lock.acquire()
        try:
            fd = self._get_flock_fd(self._lock_path)
            if StateStore._flock_depth == 0:
                fcntl.flock(fd, fcntl.LOCK_EX)
            StateStore._flock_depth += 1
            try:
                yield
            finally:
                StateStore._flock_depth -= 1
                if StateStore._flock_depth == 0:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            self._thread_lock.release()

    # -- worktree lifecycle ------------------------------------------------

    @property
    def worktree(self) -> Path:
        """Path to the persistent state worktree (created lazily)."""
        if self._worktree is None:
            self._worktree = self._ensure_worktree()
        return self._worktree

    @property
    def pipelines_dir(self) -> Path:
        return self.worktree / self.PIPELINES_DIR

    def _ensure_worktree(self) -> Path:
        """Create or validate the persistent state worktree."""
        # Clean up stale admin dir for THIS worktree only (e.g., from crashes).
        # IMPORTANT: Do NOT use `git worktree prune` — the orchestrator cannot
        # see the gateway's worktree paths (different bind mounts), so prune
        # would incorrectly remove admin dirs for active gateway worktrees,
        # breaking all container git operations.
        self._remove_stale_admin_dir()

        wt = self._worktree_dir

        if wt.exists() and (wt / ".git").exists():
            # Quick validity check with one retry for transient git
            # contention (e.g., concurrent _commit_statefiles_to_worktree
            # holding a lock on the shared .git directory).  See #1396.
            for _attempt in range(2):
                result = self._run_git("rev-parse", "--is-inside-work-tree", cwd=wt, check=False)
                if result.returncode == 0:
                    return wt
                if _attempt == 0:
                    time.sleep(0.1)
            # Stale/broken — remove and recreate
            shutil.rmtree(wt, ignore_errors=True)
            self._remove_stale_admin_dir()

        wt.parent.mkdir(parents=True, exist_ok=True)

        # Try to restore from remote if the local branch doesn't exist yet.
        # This enables cross-host recovery when the local state volume is lost.
        if not self._state_branch_exists():
            self._restore_from_remote()

        if self._state_branch_exists():
            self._run_git("worktree", "add", str(wt), STATE_BRANCH)
        else:
            # First run: create orphan branch
            # Wrap in try/except to clean up on partial failure
            try:
                self._run_git("worktree", "add", "--detach", str(wt))
                self._run_git("checkout", "--orphan", STATE_BRANCH, cwd=wt)
                self._run_git("rm", "-rf", "--cached", ".", cwd=wt, check=False)
                # Remove inherited files from working directory
                for item in wt.iterdir():
                    if item.name == ".git":
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            except (GitOperationError, OSError):
                # Clean up partial worktree on failure to avoid broken state.
                # Catch both GitOperationError (git command failures) and OSError
                # (filesystem errors during file cleanup like permission denied).
                shutil.rmtree(wt, ignore_errors=True)
                self._remove_stale_admin_dir()
                raise

        return wt

    def _remove_stale_admin_dir(self) -> None:
        """Remove the git admin dir for the state worktree if it's stale.

        When the state worktree directory is gone but its admin dir still
        exists under ``{repo}/.git/worktrees/``, ``git worktree add`` will
        refuse to recreate it.  This method finds and removes only the
        admin dir that belongs to this state worktree — without touching
        admin dirs for other worktrees (e.g., gateway-managed container
        worktrees).
        """
        worktrees_dir = self.repo_path / ".git" / "worktrees"
        if not worktrees_dir.exists():
            return

        wt = self._worktree_dir
        expected_gitdir = str(wt / ".git")

        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            gitdir_file = entry / "gitdir"
            if not gitdir_file.exists():
                continue
            try:
                gitdir_content = gitdir_file.read_text().strip()
                if gitdir_content.rstrip("/") == expected_gitdir.rstrip("/"):
                    # This admin dir belongs to our state worktree
                    if not wt.exists():
                        # Worktree dir is gone — admin dir is stale
                        shutil.rmtree(entry, ignore_errors=True)
                    return
            except OSError:
                continue

    def _state_branch_exists(self) -> bool:
        """Check if the state branch exists locally."""
        result = self._run_git(
            "rev-parse",
            "--verify",
            f"refs/heads/{STATE_BRANCH}",
            check=False,
        )
        return result.returncode == 0

    # -- low-level helpers -------------------------------------------------

    def _get_pipeline_path(self, pipeline_id: str) -> Path:
        """Get the file path for a pipeline's state.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            Path to the pipeline state file

        Raises:
            InvalidPipelineIdError: If pipeline ID format is invalid
        """
        _validate_pipeline_id(pipeline_id)
        path = self.pipelines_dir / f"{pipeline_id}.json"
        # Additional safety: ensure the resolved path stays within pipelines_dir
        resolved = path.resolve()
        if not resolved.is_relative_to(self.pipelines_dir.resolve()):
            raise InvalidPipelineIdError(f"Path traversal detected in pipeline ID: {pipeline_id}")
        return path

    def _ensure_dir(self) -> None:
        """Ensure the pipelines directory exists."""
        self.pipelines_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the repository.

        Acquires a cross-process file lock (``fcntl.flock``) and an in-process
        reentrant thread lock before executing.  Retries on ``index.lock``
        contention up to 3 times with exponential backoff.  Stale lock files
        older than 60 seconds are removed between retries.

        Args:
            args: Git command arguments
            check: Whether to check return code
            cwd: Working directory (default: self.repo_path)

        Returns:
            CompletedProcess result

        Raises:
            GitOperationError: If command fails and check=True
        """
        # SECURITY: Disable all git hooks. The orchestrator runs git commands internally
        # for state management. Hooks from repos must not execute in the orchestrator's
        # trusted environment. See issue #58 for context on hook-based attacks.
        work_dir = str(cwd) if cwd else str(self.repo_path)
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "-C", work_dir] + list(args)

        max_attempts = 3
        backoff = 0.1

        with self._git_op():
            for attempt in range(1, max_attempts + 1):
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=check,
                    )
                    return result
                except subprocess.CalledProcessError as e:
                    if "index.lock" in (e.stderr or "") and attempt < max_attempts:
                        logger.warning(
                            "index.lock contention on attempt %d/%d, retrying: %s",
                            attempt,
                            max_attempts,
                            e.stderr.strip(),
                        )
                        self._cleanup_stale_locks()
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    raise GitOperationError(f"Git command failed: {e.stderr}") from e

        # Unreachable, but keeps type checkers happy
        raise GitOperationError("_run_git exited retry loop unexpectedly")

    def _cleanup_stale_locks(self) -> None:
        """Remove stale index.lock files older than 60 seconds."""
        lock_candidates = [self.repo_path / ".git" / "index.lock"]
        worktrees_dir = self.repo_path / ".git" / "worktrees"
        if worktrees_dir.exists():
            lock_candidates.extend(worktrees_dir.glob("*/index.lock"))

        for lock_file in lock_candidates:
            if lock_file.exists():
                try:
                    age = time.time() - lock_file.stat().st_mtime
                    if age > 60:
                        lock_file.unlink(missing_ok=True)
                        logger.info(
                            "Removed stale lock file: %s (age: %.1fs)",
                            lock_file,
                            age,
                        )
                except OSError:
                    pass

    # -- CRUD operations ---------------------------------------------------

    def pipeline_exists(self, pipeline_id: str) -> bool:
        """Check if a pipeline exists.

        Args:
            pipeline_id: Pipeline ID to check

        Returns:
            True if pipeline exists
        """
        return self._get_pipeline_path(pipeline_id).exists()

    def load_pipeline(self, pipeline_id: str) -> Pipeline:
        """Load pipeline state from disk.

        Args:
            pipeline_id: Pipeline ID to load

        Returns:
            Pipeline state

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
            StateValidationError: If state is invalid
        """
        path = self._get_pipeline_path(pipeline_id)
        if not path.exists():
            raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

        try:
            with path.open() as f:
                data = json.load(f)
            return Pipeline.model_validate(data)
        except json.JSONDecodeError as e:
            raise StateValidationError(f"Invalid JSON in pipeline state: {e}") from e
        except ValidationError as e:
            raise StateValidationError(f"Pipeline state validation failed: {e}") from e

    def save_pipeline(
        self,
        pipeline: Pipeline,
        commit: bool = True,
        message: str | None = None,
        expected_version: int | None = None,
        force_commit: bool = False,
    ) -> Path:
        """Save pipeline state to disk with optimistic locking.

        Args:
            pipeline: Pipeline state to save
            commit: Whether to commit the change (ignored for local pipelines unless force_commit=True)
            message: Commit message (auto-generated if not provided)
            expected_version: If provided, checks that current version matches
                              before saving (optimistic locking)
            force_commit: If True, commit even for local pipelines. Use at phase
                          boundaries to ensure state is persisted to git.

        Returns:
            Path to saved file

        Raises:
            GitOperationError: If git operations fail
            VersionConflictError: If expected_version doesn't match current version
        """
        self._ensure_dir()
        path = self._get_pipeline_path(pipeline.id)

        # Optimistic locking check
        if expected_version is not None and path.exists():
            try:
                current = self.load_pipeline(pipeline.id)
                if current.version != expected_version:
                    raise VersionConflictError(
                        f"Version conflict for pipeline {pipeline.id}: "
                        f"expected version {expected_version}, but current is {current.version}"
                    )
            except PipelineNotFoundError:
                pass  # New pipeline, no conflict possible

        # Update timestamp and increment version
        pipeline.updated_at = datetime.now(UTC)
        pipeline.version = (pipeline.version or 0) + 1

        # Write state to the worktree
        with path.open("w") as f:
            f.write(pipeline.model_dump_json(indent=2))

        # For prompt-driven pipelines (no issue_number), only commit if force_commit
        # is True (phase boundaries). For issue pipelines, always commit when commit=True.
        is_prompt_driven = pipeline.issue_number is None
        should_commit = commit and (not is_prompt_driven or force_commit)
        if should_commit:
            self._commit_state(pipeline, message)

        return path

    # -- git commit helpers ------------------------------------------------

    def _commit_state(self, pipeline: Pipeline, message: str | None = None) -> str:
        """Commit pipeline state to the state branch.

        Commits directly in the persistent worktree.

        Args:
            pipeline: Pipeline being saved
            message: Optional commit message

        Returns:
            Commit SHA (on the state branch)

        Raises:
            GitOperationError: If commit fails
        """
        if not message:
            message = self._generate_commit_message(pipeline)

        path = self._get_pipeline_path(pipeline.id)
        rel_path = str(path.relative_to(self.worktree))

        wt = self.worktree
        # Hold lock for entire add→diff→commit sequence so concurrent
        # operations cannot interleave and stage into the wrong commit.
        with self._git_op():
            self._run_git("add", rel_path, cwd=wt)

            result = self._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
            if result.returncode == 0:
                # No changes staged - return current HEAD or empty string for unborn branch
                head_result = self._run_git("rev-parse", "HEAD", cwd=wt, check=False)
                return head_result.stdout.strip() if head_result.returncode == 0 else ""

            self._run_git("commit", "--no-verify", "-m", message, cwd=wt)
            sha = self._run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()

            # Best-effort async push to remote after every commit
            self._sync_to_remote_async()

            return sha

    def _get_current_commit(self) -> str:
        """Get the current HEAD commit SHA."""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def _generate_commit_message(self, pipeline: Pipeline) -> str:
        """Generate a commit message for pipeline state update."""
        return f"Update pipeline state: {pipeline.id} ({pipeline.status.value})"

    # -- remote sync -------------------------------------------------------

    def _detect_gateway_mode(self) -> Literal["public", "private"]:
        """Auto-detect gateway session mode from repository visibility.

        Result is cached for the lifetime of this StateStore instance since
        repo visibility does not change during a process run.
        """
        if hasattr(self, "_cached_gateway_mode"):
            return self._cached_gateway_mode

        mode = "public"
        try:
            from gateway_client import get_gateway_client

            client = get_gateway_client()
            # Extract owner/repo from git remote
            result = self._run_git("remote", "get-url", "origin", cwd=self.repo_path, check=False)
            if result.returncode == 0:
                url = result.stdout.strip()
                # Normalize SSH colon syntax: git@github.com:owner/repo → git@github.com/owner/repo
                if ":" in url and not url.startswith(("http://", "https://", "ssh://", "git://")):
                    url = url.replace(":", "/", 1)
                # Parse "https://github.com/owner/repo.git" or "owner/repo"
                parts = url.rstrip("/").removesuffix(".git").rsplit("/", 2)
                if len(parts) >= 2:
                    repo = f"{parts[-2]}/{parts[-1]}"
                    vis = client.get_repo_visibility(repo)
                    if vis in ("private", "internal"):
                        mode = "private"
        except Exception:
            pass

        self._cached_gateway_mode = mode
        return mode

    def sync_to_remote(self) -> bool:
        """Push the state branch to remote (best-effort).

        Uses the gateway client to push via a temporary session.

        Returns:
            True on success, False on failure (logged, never raises)
        """
        try:
            from gateway_client import get_gateway_client

            client = get_gateway_client()
            return client.push_worktree_branch(
                pipeline_id="state-sync",
                repo_path=str(self.worktree),
                branch=STATE_BRANCH,
                mode=self._detect_gateway_mode(),
            )
        except Exception as e:
            logger.warning(
                "Failed to sync state branch to remote: %s",
                e,
            )
            return False

    _MAX_PUSH_RETRIES: ClassVar[int] = 3

    def _sync_to_remote_async(self, _retry_depth: int = 0) -> None:
        """Push the state branch to remote in a daemon thread.

        Debounces: if a push is already in flight, marks a pending flag so
        the in-flight thread re-pushes after completing.  This ensures the
        latest committed state always reaches the remote.

        Retries are capped at ``_MAX_PUSH_RETRIES`` to prevent unbounded
        recursion if commits arrive faster than pushes complete.
        """
        with StateStore._push_lock:
            if StateStore._push_in_flight:
                StateStore._push_pending = True
                logger.debug("Push already in flight — marked pending for retry")
                return
            StateStore._push_in_flight = True

        def _push() -> None:
            try:
                self.sync_to_remote()
            finally:
                retry = False
                with StateStore._push_lock:
                    StateStore._push_in_flight = False
                    if StateStore._push_pending:
                        StateStore._push_pending = False
                        retry = True
                if retry:
                    next_depth = _retry_depth + 1
                    if next_depth >= StateStore._MAX_PUSH_RETRIES:
                        logger.warning(
                            "Max push retries (%d) reached — skipping retry",
                            StateStore._MAX_PUSH_RETRIES,
                        )
                    else:
                        self._sync_to_remote_async(_retry_depth=next_depth)

        t = threading.Thread(target=_push, daemon=True)
        t.start()

    def _restore_from_remote(self) -> bool:
        """Restore the state branch from remote if it exists.

        Called during worktree initialization when the local branch
        doesn't exist. Checks remote via ls-remote, then fetches.

        Returns:
            True if the local branch was restored from remote, False otherwise
        """
        try:
            from gateway_client import get_gateway_client

            client = get_gateway_client()
            mode = self._detect_gateway_mode()

            # Check if remote branch exists
            if not client.ls_remote_branch(
                pipeline_id="state-restore",
                repo_path=str(self.repo_path),
                ref=f"refs/heads/{STATE_BRANCH}",
                mode=mode,
            ):
                logger.debug("No remote state branch found — will create fresh")
                return False

            # Fetch the remote branch to create the local tracking ref
            if not client.fetch_branch(
                pipeline_id="state-restore",
                repo_path=str(self.repo_path),
                args=[f"+refs/heads/{STATE_BRANCH}:refs/heads/{STATE_BRANCH}"],
                mode=mode,
            ):
                logger.warning("Failed to fetch state branch from remote")
                return False

            logger.info("Restored state branch from remote")
            return True
        except Exception as e:
            logger.debug(
                "Could not restore state branch from remote (will create fresh): %s",
                e,
            )
            return False

    # -- pipeline lifecycle ------------------------------------------------

    def create_pipeline(
        self,
        issue_number: int | None = None,
        repo: str | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
        config: dict[str, Any] | None = None,
        prompt: str | None = None,
        pipeline_id: str | None = None,
        network_mode: str | None = None,
        mode: PipelineMode | None = None,
        pr_number: int | None = None,
        analysis: str | None = None,
        plan: str | None = None,
    ) -> Pipeline:
        """Create a new pipeline.

        Args:
            issue_number: GitHub issue number (optional)
            repo: Repository in owner/name format
            branch: Work branch name
            base_branch: Base branch for PR creation (optional, auto-detected if not set)
            config: Optional pipeline configuration
            prompt: User prompt (for prompt-driven pipelines)
            pipeline_id: Explicit pipeline ID (auto-generated if not provided)
            network_mode: Network mode for spawned containers ("public", "private", or None)
            mode: Pipeline mode (ISSUE or BABYSIT). Defaults to ISSUE if not set.
            pr_number: PR number for babysit-mode pipelines (optional).
            analysis: Pre-generated analysis markdown for short flow pipelines (optional).
            plan: Pre-generated plan markdown with yaml-tasks appendix (optional).

        Returns:
            Created pipeline

        Raises:
            StateStoreError: If an active pipeline with the same ID already exists
        """
        if not pipeline_id:
            if issue_number:
                pipeline_id = f"issue-{issue_number}"
            else:
                pipeline_id = f"pipeline-{os.urandom(4).hex()}"

        with get_pipeline_state_lock(pipeline_id):
            if self.pipeline_exists(pipeline_id):
                existing = self.load_pipeline(pipeline_id)
                terminal = {
                    PipelineStatus.CANCELLED,
                    PipelineStatus.FAILED,
                    PipelineStatus.COMPLETE,
                }
                if existing.status in terminal:
                    logger.info(
                        "Replacing terminal pipeline %s (status=%s)",
                        pipeline_id,
                        existing.status.value,
                    )
                    self.delete_pipeline(pipeline_id, commit=True, cleanup_lock=False)
                else:
                    raise StateStoreError(f"Pipeline {pipeline_id} already exists")

            pipeline_kwargs: dict[str, Any] = {
                "id": pipeline_id,
                "issue_number": issue_number,
                "repo": repo,
                "branch": branch,
                "base_branch": base_branch,
                "prompt": prompt,
                "network_mode": network_mode,
                # Contract is created separately — mark as unsynced until verified
                "contract_synced": False,
                "analysis": analysis,
                "plan": plan,
            }
            if mode is not None:
                pipeline_kwargs["mode"] = mode
            if pr_number is not None:
                pipeline_kwargs["pr_number"] = pr_number
            pipeline = Pipeline(**pipeline_kwargs)

            if config:
                from models import PipelineConfig

                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except json.JSONDecodeError as e:
                        raise StateStoreError(f"Invalid config JSON: {e}") from e
                pipeline.config = PipelineConfig.model_validate(config)

            commit_msg = f"Create pipeline {pipeline_id}"
            self.save_pipeline(pipeline, message=commit_msg)
            return pipeline

    def delete_pipeline(
        self,
        pipeline_id: str,
        commit: bool = True,
        force_commit: bool = False,
        cleanup_lock: bool = True,
    ) -> None:
        """Delete a pipeline.

        Args:
            pipeline_id: Pipeline ID to delete
            commit: Whether to commit the deletion (ignored for local unless force_commit)
            force_commit: If True, commit deletion even for local pipelines
            cleanup_lock: Whether to release the per-pipeline state lock.
                Set to False when called from within a lock (e.g. create_pipeline
                replacing a terminal pipeline) to avoid removing the lock while
                the caller still holds it.

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
        """
        path = self._get_pipeline_path(pipeline_id)
        if not path.exists():
            raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

        rel_path = str(path.relative_to(self.worktree))
        path.unlink()

        # Clean up the per-pipeline state lock to prevent unbounded growth
        if cleanup_lock:
            release_pipeline_state_lock(pipeline_id)

        # For local pipelines, only commit if force_commit is True
        # For issue pipelines, always commit when commit=True
        is_local = pipeline_id.startswith("local-")
        should_commit = commit and (not is_local or force_commit)
        if should_commit:
            wt = self.worktree
            with self._git_op():
                self._run_git("add", rel_path, cwd=wt)

                result = self._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
                if result.returncode != 0:
                    self._run_git(
                        "commit",
                        "--no-verify",
                        "-m",
                        f"Delete pipeline: {pipeline_id}",
                        cwd=wt,
                    )
                    # Sync deletion to remote (mirrors _commit_state behavior)
                    self._sync_to_remote_async()

    def list_pipelines(self) -> list[str]:
        """List all pipeline IDs.

        Returns:
            List of pipeline IDs
        """
        if not self.pipelines_dir.exists():
            return []

        return [p.stem for p in self.pipelines_dir.glob("*.json") if p.is_file()]

    def get_active_pipelines(self) -> list[Pipeline]:
        """Get all active (non-terminal) pipelines.

        Returns:
            List of active pipelines
        """
        terminal_statuses = {
            PipelineStatus.COMPLETE,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        }

        pipelines = []
        for pipeline_id in self.list_pipelines():
            try:
                pipeline = self.load_pipeline(pipeline_id)
                if pipeline.status not in terminal_statuses:
                    pipelines.append(pipeline)
            except StateStoreError:
                # Skip invalid pipelines
                continue

        return pipelines

    def update_pipeline(
        self,
        pipeline_id: str,
        updates: dict[str, Any],
        commit: bool = True,
    ) -> Pipeline:
        """Update pipeline state with partial updates.

        Uses a per-pipeline lock to make the load-modify-save cycle atomic,
        preventing concurrent writers (e.g. DecisionQueue.resolve_decision)
        from having their changes silently overwritten.

        Args:
            pipeline_id: Pipeline ID to update
            updates: Dictionary of field updates
            commit: Whether to commit the change

        Returns:
            Updated pipeline

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
            StateValidationError: If updates are invalid
        """
        with get_pipeline_state_lock(pipeline_id):
            pipeline = self.load_pipeline(pipeline_id)

            # Apply updates
            data = pipeline.model_dump()
            for key, value in updates.items():
                if "." in key:
                    # Nested update
                    parts = key.split(".")
                    target = data
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = value
                else:
                    data[key] = value

            # Validate and save
            try:
                pipeline = Pipeline.model_validate(data)
            except ValidationError as e:
                raise StateValidationError(f"Update validation failed: {e}") from e

            self.save_pipeline(pipeline, commit=commit)
            return pipeline


# Per-pipeline state locks for atomic load-modify-save cycles.
# Prevents race conditions where concurrent writers (e.g. update_pipeline
# and DecisionQueue.resolve_decision) can clobber each other's changes.
_pipeline_state_locks: dict[str, threading.RLock] = {}
_state_locks_lock = threading.Lock()


def get_pipeline_state_lock(pipeline_id: str) -> threading.RLock:
    """Get a per-pipeline lock for coordinating state access.

    All code that does a load-modify-save cycle on pipeline state
    should acquire this lock to prevent concurrent writes from
    overwriting each other.  The lock is reentrant (RLock) so
    nested acquisitions within the same thread are safe.

    Args:
        pipeline_id: Pipeline ID

    Returns:
        RLock for the given pipeline
    """
    with _state_locks_lock:
        if pipeline_id not in _pipeline_state_locks:
            _pipeline_state_locks[pipeline_id] = threading.RLock()
        return _pipeline_state_locks[pipeline_id]


def release_pipeline_state_lock(pipeline_id: str) -> None:
    """Remove the per-pipeline lock when a pipeline is deleted.

    Call this after deleting a pipeline to prevent unbounded growth
    of ``_pipeline_state_locks``.  Safe to call even if no lock exists
    for the given pipeline ID.

    Precondition: the lock must not be currently held by any thread.
    If it is, the lock is left in place to avoid breaking mutual
    exclusion for threads still referencing the old lock object.

    Args:
        pipeline_id: Pipeline ID whose lock should be discarded
    """
    with _state_locks_lock:
        lock = _pipeline_state_locks.get(pipeline_id)
        if lock is None:
            return
        # Only remove if the lock is not currently held.  A held lock
        # means another thread is mid-operation; removing it would cause
        # new callers to get a fresh lock, breaking mutual exclusion.
        # RLock has no .locked() method, so we try a non-blocking acquire.
        if lock.acquire(blocking=False):
            lock.release()
            _pipeline_state_locks.pop(pipeline_id, None)


def discover_repo_paths(base_path: Path | str) -> list[Path]:
    """Discover git repositories under a base path.

    If *base_path* is itself a git repo, returns ``[base_path]``.
    Otherwise, scans immediate children for directories containing ``.git``.

    Args:
        base_path: A git repo or a parent directory containing repos.

    Returns:
        List of paths to git repositories (may be empty).
    """
    if isinstance(base_path, str):
        base_path = Path(base_path)
    if (base_path / ".git").exists():
        return [base_path]
    if base_path.is_dir():
        return [
            child
            for child in sorted(base_path.iterdir())
            if child.is_dir() and (child / ".git").exists()
        ]
    return []


def get_state_store(repo_path: Path | str) -> StateStore:
    """Get a state store for a repository.

    *repo_path* must be a git repository (contains ``.git``).  If it is a
    parent directory containing multiple repos, use
    :func:`discover_repo_paths` first.

    For multi-repo setups each repo gets a unique worktree path derived
    from its directory name (e.g. ``pipeline-worktree-egg``).

    Args:
        repo_path: Path to the git repository

    Returns:
        StateStore instance

    Raises:
        StateStoreError: If *repo_path* is not a git repository.
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)

    if not (repo_path / ".git").exists():
        raise StateStoreError(
            f"Cannot create StateStore for non-git directory: {repo_path}. "
            f"Use discover_repo_paths() to find repos first."
        )

    # Determine whether we need a per-repo worktree path.
    env_path = os.environ.get("EGG_REPO_PATH", "")
    if env_path:
        env_resolved = Path(env_path).resolve()
        repo_resolved = repo_path.resolve()
        if env_resolved == repo_resolved:
            # Single-repo: EGG_REPO_PATH points directly to this repo.
            worktree_dir = None
        elif len(discover_repo_paths(env_resolved)) == 1:
            # EGG_REPO_PATH is a parent dir with a single child repo —
            # use the default worktree path for backward compatibility.
            worktree_dir = None
        else:
            # Multi-repo: derive a unique worktree path per repo.
            state_dir = Path(os.environ.get("EGG_STATE_DIR", "/home/egg/.egg-state"))
            worktree_dir = state_dir / f"pipeline-worktree-{repo_path.name}"
    else:
        worktree_dir = None

    return StateStore(repo_path, worktree_dir=worktree_dir)
