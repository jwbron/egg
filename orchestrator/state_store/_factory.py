"""Repo discovery + ``StateStore`` factory (#3312).

Extracted verbatim from the pre-split ``state_store.py``. ``get_state_store``
resolves ``StateStore`` and ``discover_repo_paths`` through the package barrel
(``_pkg``) so the pre-split module-global patch seams
(``patch("state_store.discover_repo_paths")``, ``patch("state_store.StateStore")``)
keep taking effect inside the factory.
"""

import os
from pathlib import Path

import state_store as _pkg

from ._errors import StateStoreError


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


def get_state_store(repo_path: Path | str) -> _pkg.StateStore:
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
        elif len(_pkg.discover_repo_paths(env_resolved)) == 1:
            # EGG_REPO_PATH is a parent dir with a single child repo —
            # use the default worktree path for backward compatibility.
            worktree_dir = None
        else:
            # Multi-repo: derive a unique worktree path per repo.
            state_dir = Path(os.environ.get("EGG_STATE_DIR", "/home/egg/.egg-state"))
            worktree_dir = state_dir / f"pipeline-worktree-{repo_path.name}"
    else:
        worktree_dir = None

    return _pkg.StateStore(repo_path, worktree_dir=worktree_dir)
