"""
Git-backed state persistence for pipeline state.

All pipeline state lives on a dedicated ``egg/pipeline-state`` orphan branch,
accessed via a persistent git worktree.  The main checkout is never modified.

Read/write operations go directly to the worktree directory on disk.  Commits
are made in-place inside the worktree and stay on the state branch.

Note: The state branch is **local-only** and is not pushed to the remote.
State persistence relies on the Docker state volume (``/home/egg/.egg-state``).
This differs from checkpoints which are pushed to remote for cross-container
access.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from models import Pipeline, PipelineStatus
from pydantic import ValidationError

logger = logging.getLogger("orchestrator.state_store")

# Valid pipeline ID format: issue-{number} or local-{8 hex chars}
PIPELINE_ID_PATTERN = re.compile(r"^(issue-[0-9]+|local-[0-9a-f]{8})$")

# Dedicated branch for pipeline state (orphan, never merged into main)
STATE_BRANCH = "egg/pipeline-state"

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
        self._git_lock = threading.Lock()  # serialize git operations

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
            # Quick validity check
            result = self._run_git("rev-parse", "--is-inside-work-tree", cwd=wt, check=False)
            if result.returncode == 0:
                return wt
            # Stale/broken — remove and recreate
            shutil.rmtree(wt, ignore_errors=True)
            self._remove_stale_admin_dir()

        wt.parent.mkdir(parents=True, exist_ok=True)

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

        Serializes access with a threading lock and retries on index.lock
        contention (up to 3 attempts with exponential backoff).  Stale lock
        files older than 60 seconds are removed between retries.

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

        with self._git_lock:
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
        pipeline.updated_at = datetime.utcnow()
        pipeline.version = (pipeline.version or 0) + 1

        # Write state to the worktree
        with path.open("w") as f:
            f.write(pipeline.model_dump_json(indent=2))

        # For local pipelines, only commit if force_commit is True (phase boundaries)
        # For issue pipelines, always commit when commit=True
        is_local = getattr(pipeline, "mode", "issue") == "local"
        should_commit = commit and (not is_local or force_commit)
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
        self._run_git("add", rel_path, cwd=wt)

        result = self._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
        if result.returncode == 0:
            # No changes staged - return current HEAD or empty string for unborn branch
            head_result = self._run_git("rev-parse", "HEAD", cwd=wt, check=False)
            return head_result.stdout.strip() if head_result.returncode == 0 else ""

        self._run_git("commit", "--no-verify", "-m", message, cwd=wt)
        return self._run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    def _get_current_commit(self) -> str:
        """Get the current HEAD commit SHA."""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def _generate_commit_message(self, pipeline: Pipeline) -> str:
        """Generate a commit message for pipeline state update."""
        return f"Update pipeline state: {pipeline.id} ({pipeline.status.value})"

    # -- pipeline lifecycle ------------------------------------------------

    def create_pipeline(
        self,
        issue_number: int | None = None,
        repo: str | None = None,
        branch: str | None = None,
        config: dict[str, Any] | None = None,
        mode: str = "issue",
        prompt: str | None = None,
        pipeline_id: str | None = None,
        network_mode: str | None = None,
    ) -> Pipeline:
        """Create a new pipeline.

        Args:
            issue_number: GitHub issue number (required for issue mode)
            repo: Repository in owner/name format (required for issue mode)
            branch: Work branch name (required for issue mode)
            config: Optional pipeline configuration
            mode: Pipeline mode - "issue" or "local"
            prompt: User prompt (required for local mode)
            pipeline_id: Explicit pipeline ID (auto-generated if not provided)
            network_mode: Network mode for spawned containers ("public", "private", or None)

        Returns:
            Created pipeline

        Raises:
            StateStoreError: If pipeline already exists
        """
        if mode == "local":
            if not pipeline_id:
                pipeline_id = f"local-{os.urandom(4).hex()}"
        else:
            if not issue_number:
                raise StateStoreError("issue_number is required for issue-mode pipelines")
            pipeline_id = pipeline_id or f"issue-{issue_number}"

        if self.pipeline_exists(pipeline_id):
            raise StateStoreError(f"Pipeline {pipeline_id} already exists")

        pipeline = Pipeline(
            id=pipeline_id,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            mode=mode,
            prompt=prompt,
            network_mode=network_mode,
            # Contract is created separately — mark as unsynced until verified
            contract_synced=False,
        )

        if config:
            from models import PipelineConfig

            pipeline.config = PipelineConfig.model_validate(config)

        commit_msg = (
            f"Create local pipeline {pipeline_id}"
            if mode == "local"
            else f"Create pipeline for issue #{issue_number}"
        )
        self.save_pipeline(pipeline, message=commit_msg)
        return pipeline

    def delete_pipeline(
        self, pipeline_id: str, commit: bool = True, force_commit: bool = False
    ) -> None:
        """Delete a pipeline.

        Args:
            pipeline_id: Pipeline ID to delete
            commit: Whether to commit the deletion (ignored for local unless force_commit)
            force_commit: If True, commit deletion even for local pipelines

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
        """
        path = self._get_pipeline_path(pipeline_id)
        if not path.exists():
            raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

        rel_path = str(path.relative_to(self.worktree))
        path.unlink()

        # For local pipelines, only commit if force_commit is True
        # For issue pipelines, always commit when commit=True
        is_local = pipeline_id.startswith("local-")
        should_commit = commit and (not is_local or force_commit)
        if should_commit:
            wt = self.worktree
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


def get_state_store(repo_path: Path | str) -> StateStore:
    """Get a state store for a repository.

    Args:
        repo_path: Path to the repository

    Returns:
        StateStore instance
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)
    return StateStore(repo_path)
