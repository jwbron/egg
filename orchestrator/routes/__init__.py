"""
REST API routes for egg-orchestrator.

Each module provides a Flask Blueprint for a logical group of endpoints.
"""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from flask import abort, request

if TYPE_CHECKING:
    from models import Pipeline
    from state_store import StateStore

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.routes")


def get_repo_path() -> Path:
    """Get the repository path from environment or request.

    When EGG_REPO_PATH is a parent directory containing multiple repos
    (e.g. /home/egg/repos), this resolves to the specific repo
    subdirectory using the ``repo`` field from the request body
    (e.g. ``owner/name`` -> ``/home/egg/repos/name``).

    Returns:
        Path to the repository
    """
    # Check request args first
    repo_path = request.args.get("repo_path")
    if repo_path:
        return Path(repo_path)

    # Check JSON body
    data = request.get_json(silent=True) or {}
    if data.get("repo_path"):
        return Path(data["repo_path"])

    # Check environment
    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path:
        base = Path(env_path)
        # EGG_REPO_PATH may be a parent dir containing repo subdirectories.
        # If it's not itself a git repo, resolve using the repo name from
        # the request body (e.g. "owner/name" -> base / "name").
        if not (base / ".git").exists():
            repo = data.get("repo", "") or request.args.get("repo", "")
            if repo:
                repo_name = repo.split("/")[-1]
                candidate = base / repo_name
                if (candidate / ".git").exists():
                    return candidate
                logger.warning(
                    "Repo not found in multi-repo base directory",
                    repo_name=repo_name,
                    base_path=str(base),
                )
                abort(
                    400,
                    description=f"Repo '{repo_name}' not found",
                )
        return base

    # Default to current working directory
    return Path.cwd()


def resolve_worktree_repo_path(base_path: Path, repo_name: str) -> Path:
    """Resolve a worktree repo path from an env-style base + a repo name.

    Used by code paths that already know the repo name (from
    ``pipeline.repo``) but only have ``EGG_REPO_PATH`` as a starting
    point.  Prefers the named subdirectory; falls back to ``base_path``
    only when ``base_path`` itself is the git repo.

    Replaces the historical ``base_path / repo_name if not (base_path /
    ".git").exists() else base_path`` toggle, which silently flipped
    meaning when a stray ``.git`` appeared at ``base_path`` (see #1749).

    Raises:
        RuntimeError: if neither ``base_path / repo_name`` nor
            ``base_path`` contains a ``.git`` directory.  Failing fast
            with a specific error beats silently resolving to the
            wrong path.
    """
    candidate = base_path / repo_name if repo_name else None
    if candidate is not None and (candidate / ".git").exists():
        return candidate
    if (base_path / ".git").exists():
        return base_path
    raise RuntimeError(
        f"EGG_REPO_PATH={base_path} contains neither a git repo nor a "
        f"'{repo_name}' subdirectory with one"
    )


def resolve_repo_path_for_pipeline(pipeline_id: str, base_path: Path) -> Path:
    """Resolve the correct repo subdirectory for a pipeline.

    Signal requests don't include a ``repo`` field in their body, so
    :func:`get_repo_path` falls through to the bare parent directory
    (e.g. ``/home/egg/repos/`` instead of ``/home/egg/repos/egg/``).

    This helper loads the pipeline from the state store to read its
    ``repo`` field and resolves the correct subdirectory.  The state
    store can load pipelines even with the wrong ``repo_path`` because
    it uses a separate persistent worktree for pipeline state.

    Args:
        pipeline_id: Pipeline ID to look up
        base_path: Base repo path (may be a parent directory)

    Returns:
        Resolved path to the specific repository directory
    """
    # If base_path is already a git repo, no resolution needed
    if (base_path / ".git").exists():
        return base_path

    try:
        # Import here to avoid circular imports
        from state_store import discover_repo_paths, get_state_store

        for repo_path in discover_repo_paths(base_path):
            try:
                store = get_state_store(repo_path)
                pipeline = store.load_pipeline(pipeline_id)
                if pipeline.repo:
                    repo_name = pipeline.repo.split("/")[-1]
                    candidate = base_path / repo_name
                    if candidate.exists() and (candidate / ".git").exists():
                        return candidate
                # Pipeline found in this repo — return it even if repo field
                # is missing.
                return repo_path
            except Exception:
                continue
    except Exception as e:
        logger.warning(
            "Failed to resolve repo path for pipeline",
            pipeline_id=pipeline_id,
            base_path=str(base_path),
            error=str(e),
        )

    return base_path


def get_state_store_for_pipeline(pipeline_id: str) -> tuple["StateStore", "Pipeline"]:  # noqa: UP037
    """Get state store and pipeline, resolving multi-repo paths automatically.

    This is the preferred way for routes to load a pipeline.  It handles
    the multi-repo case where ``get_repo_path()`` returns a parent
    directory (e.g. ``/home/egg/repos``) by searching all discovered
    repos for the pipeline.

    Args:
        pipeline_id: Pipeline ID to look up

    Returns:
        (StateStore, Pipeline) tuple

    Raises:
        PipelineNotFoundError: if the pipeline cannot be found
        InvalidPipelineIdError: if the ID format is invalid
    """
    from state_store import (
        PipelineNotFoundError,
        _validate_pipeline_id,
        discover_repo_paths,
        get_state_store,
    )

    # Validate format before any repo lookup so InvalidPipelineIdError is
    # always raised for malformed IDs even when no repos are discovered
    # (e.g. in k8s where EGG_REPO_PATH may point to an empty directory).
    _validate_pipeline_id(pipeline_id)

    base_path = get_repo_path()

    # Fast path: base_path is itself a git repo
    if (base_path / ".git").exists():
        store = get_state_store(base_path)
        pipeline = store.load_pipeline(pipeline_id)
        return store, pipeline

    # Multi-repo: search all discovered repos.
    # Only catch PipelineNotFoundError here — InvalidPipelineIdError and
    # other StateStoreErrors should propagate immediately since an invalid
    # ID or infrastructure failure won't resolve in a different repo.
    for repo_path in discover_repo_paths(base_path):
        try:
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            return store, pipeline
        except PipelineNotFoundError:
            continue

    raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found") from None


# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
_WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")


def resolve_worktree_path(pipeline_id: str, repo_path: Path) -> Path:
    """Resolve the worktree repo path for a pipeline.

    Contracts and other container-written files live in per-pipeline
    worktrees at ``/home/egg/.egg-worktrees/<pipeline_id>/<repo>/``.
    This helper checks for a worktree and returns it when present,
    falling back to ``repo_path`` otherwise (e.g. when worktrees have
    already been cleaned up or were never created).

    Args:
        pipeline_id: Pipeline ID (e.g. ``issue-546``)
        repo_path: Main repo path (e.g. ``/home/egg/repos/egg``)

    Returns:
        Worktree path if it exists, otherwise ``repo_path``
    """
    wt_pipeline_dir = _WORKTREE_BASE_DIR / pipeline_id
    if not wt_pipeline_dir.is_dir():
        return repo_path

    # Match by repo directory name (last component of repo_path)
    repo_name = repo_path.name
    candidate = wt_pipeline_dir / repo_name
    if candidate.is_dir():
        return candidate

    # Fallback: take the first existing subdirectory.
    # iterdir() order is non-deterministic; log a warning so operators
    # can detect when the heuristic fires (e.g. after a repo rename).
    try:
        for entry in wt_pipeline_dir.iterdir():
            if entry.is_dir():
                logger.warning(
                    "Worktree repo name mismatch, using fallback",
                    pipeline_id=pipeline_id,
                    expected_repo=repo_name,
                    fallback_path=str(entry),
                )
                return entry
    except OSError:
        pass

    return repo_path
