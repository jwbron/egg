"""Low-level git execution + path helpers for ``StateStore`` (#3312).

Method bodies extracted verbatim from the pre-split ``state_store.py`` as
module-level functions taking ``self`` explicitly (decomposition-pattern.md
§c). The barrel binds these back onto the ``StateStore`` class, so
``patch.object(StateStore, "_run_git")`` and ``self._run_git(...)`` dispatch
work unchanged.
"""

import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from egg_git.cross_process_lock import bare_repo_lock

from . import logger
from ._errors import GitOperationError, InvalidPipelineIdError, _validate_pipeline_id


@contextmanager
def _git_op(self) -> Generator[None]:
    """Acquire the cross-process lock for git operations.

    Delegates to ``egg_git.cross_process_lock.bare_repo_lock``, which
    provides a reentrant in-process thread lock plus an ``fcntl.flock``
    file lock keyed on the same inode the gateway uses.  Nested calls
    re-enter via the shared depth counter inside ``bare_repo_lock``,
    so compound operations (e.g. ``_commit_state``) can hold the lock
    across several inner ``_run_git`` calls without self-deadlocking.

    Until this delegation, ``StateStore`` maintained a parallel
    implementation of the same flock protocol.  Both kept the same
    invariants and ``flock`` keys on the inode regardless of fd, so
    cross-pod serialisation worked — but two implementations was a
    drift trap, and would self-deadlock if ever co-located in one
    process (each owned its own fd, and ``flock(2)`` treats fds on the
    same file as independent for the calling process).
    """
    with bare_repo_lock(self.repo_path):
        yield


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
