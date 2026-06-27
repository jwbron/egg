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

Sub-package barrel (#3312 slice-12). ``worktree_manager.py`` (2,507 lines) was split
into underscore-prefixed cluster submodules following the canonical decomposition
pattern (docs/guides/decomposition-pattern.md, method-modules-on-class). This barrel
is the stable public API: external importers and ``unittest.mock.patch`` targets
(gateway modules import top-level, e.g. ``import worktree_manager`` /
``patch("worktree_manager.get_token_for_repo")``) resolve through it. The
WorktreeManager method bodies live in the ``_create`` / ``_fsutil`` / ``_query`` /
``_remove`` / ``_cleanup`` submodules and are bound onto the class below; leaf helpers
(constants, dataclasses, validators, logger) live in ``_common``.
"""

import subprocess
import threading
import time  # noqa: F401  -- re-exported attribute seam: patch("worktree_manager.time.sleep")
from pathlib import Path
from typing import Any

from ._common import (
    REPOS_BASE_DIR,
    WORKTREE_BASE_DIR,
    WorktreeInfo,
    WorktreeRemovalResult,
    _format_bytes,  # noqa: F401  -- re-exported for external/patch consumers
    _tracking_refspec,  # noqa: F401  -- re-exported for external/patch consumers
    logger,
    validate_branch_ref,
    validate_identifier,
)

# Credential helpers are imported here (not in the submodule) so the
# ``_create`` cluster can read them off this barrel via ``_barrel()`` and
# ``patch("worktree_manager.get_token_for_repo")`` resolves at call time.
try:
    from ..git_client import (
        cleanup_credential_helper,  # noqa: F401  -- barrel seam for _create._barrel()
        create_credential_helper,  # noqa: F401  -- barrel seam for _create._barrel()
        get_token_for_repo,  # noqa: F401  -- patch seam: worktree_manager.get_token_for_repo
    )
except ImportError:  # pragma: no cover - flat (container) import path
    from git_client import (  # type: ignore[no-redef, import-untyped]
        cleanup_credential_helper,  # noqa: F401
        create_credential_helper,  # noqa: F401
        get_token_for_repo,  # noqa: F401
    )

from . import (
    _cleanup,
    _create,
    _fsutil,
    _query,
    _remove,
)


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

    # -- bodies in _create.py --
    resolve_default_branch = _create.resolve_default_branch
    create_worktree = _create.create_worktree
    _configure_push_upstream = _create._configure_push_upstream
    _git_credential_env = _create._git_credential_env
    _resolve_assigned_fork_point = _create._resolve_assigned_fork_point
    _reset_reused_worktree_to_safe_ref = _create._reset_reused_worktree_to_safe_ref
    _run_git_worktree_add = _create._run_git_worktree_add
    create_phase_worktree = _create.create_phase_worktree

    # -- bodies in _fsutil.py --
    _get_repo_lock = _fsutil._get_repo_lock
    _chown_single = _fsutil._chown_single
    _chown_recursive = _fsutil._chown_recursive
    _find_worktree_git_dir = _fsutil._find_worktree_git_dir
    get_worktree_paths = _fsutil.get_worktree_paths

    # -- bodies in _query.py --
    lookup_worktree = _query.lookup_worktree
    list_worktrees = _query.list_worktrees
    list_worktrees_for_pipeline = _query.list_worktrees_for_pipeline

    # -- bodies in _remove.py --
    cleanup_phase_worktrees = _remove.cleanup_phase_worktrees
    remove_worktree = _remove.remove_worktree
    _delete_worktree_branch = _remove._delete_worktree_branch
    cleanup_clean_worktree = _remove.cleanup_clean_worktree

    # -- bodies in _cleanup.py --
    cleanup_orphaned_worktrees = _cleanup.cleanup_orphaned_worktrees
    prune_stale_worktrees = _cleanup.prune_stale_worktrees
    git_worktree_prune_all = _cleanup.git_worktree_prune_all
    list_orphan_worktree_dirs = _cleanup.list_orphan_worktree_dirs
    cleanup_orphaned_pack_files = _cleanup.cleanup_orphaned_pack_files
    cleanup_stale_pipeline_worktrees = _cleanup.cleanup_stale_pipeline_worktrees

    # -- staticmethod body in _cleanup.py --
    _is_pipeline_anchored = staticmethod(_cleanup._is_pipeline_anchored)


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
    active_pipeline_ids: set[str] | None = None,
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
        session_manager: Optional SessionManager for session auto-commit on cleanup
        active_pipeline_ids: IDs of non-terminal pipelines per the
            orchestrator (``orchestrator_pipelines.fetch_active_pipeline_ids``).
            ``None`` means pipeline liveness could not be verified — the
            orphan sweep is SKIPPED entirely rather than run blind, because
            container liveness alone cannot distinguish a crashed leftover
            from a pipeline parked at a HITL gate (#3070; a redeploy swept
            every worktree, contract included, with ``active_containers=0``).
            Pass an empty set only when the orchestrator confirmed nothing
            is active.

    Returns:
        Number of orphaned worktrees removed
    """
    manager = WorktreeManager()
    if active_containers is None:
        active_containers = get_active_docker_containers()

    if active_pipeline_ids is None:
        logger.error(
            "Skipping orphaned-worktree sweep: pipeline liveness unknown "
            "(orchestrator unreachable?); stale worktrees will accumulate "
            "until the next startup or an operator-run prune",
            active_containers=len(active_containers),
        )
        removed = 0
    else:
        logger.info(
            "Running startup worktree cleanup",
            active_containers=len(active_containers),
            active_pipelines=len(active_pipeline_ids),
        )
        removed = manager.cleanup_orphaned_worktrees(
            active_containers,
            session_manager,
            active_pipeline_ids=active_pipeline_ids,
        )

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


__all__ = [
    "WORKTREE_BASE_DIR",
    "REPOS_BASE_DIR",
    "WorktreeInfo",
    "WorktreeRemovalResult",
    "WorktreeManager",
    "validate_identifier",
    "validate_branch_ref",
    "get_active_docker_containers",
    "startup_cleanup",
    "get_token_for_repo",
    "create_credential_helper",
    "cleanup_credential_helper",
    "_format_bytes",
    "_tracking_refspec",
]
