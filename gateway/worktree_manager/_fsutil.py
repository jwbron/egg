"""WorktreeManager filesystem / lock helper method bodies (#3312 slice-12).

Per-repo lock acquisition, chown (single/recursive), worktree git-dir discovery, and
container→worktree path mapping. Extracted verbatim and bound onto ``WorktreeManager``
in the barrel; they take ``self`` explicitly.
"""

import contextlib
import os
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path

from egg_git.cross_process_lock import bare_repo_lock

from ._common import (
    logger,
    validate_identifier,
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
