"""Shared leaf helpers for the worktree_manager sub-package (#3312 slice-12).

Constants, dataclasses, identifier/branch validation, and the module logger.
Has no intra-package dependencies so cluster submodules and the barrel can all
import from it without an import cycle. Extracted verbatim from the pre-split
``gateway/worktree_manager.py`` — pure refactor.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Best-effort shared/ bootstrap (mirrors the pre-split module; the computed
# path does not exist for the repo layout, so this is a no-op there and
# egg_logging resolves via PYTHONPATH — preserved for behavioural fidelity).
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

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


def _tracking_refspec(branch: str) -> str:
    """Forced fetch refspec that pins ``refs/remotes/origin/<branch>``.

    ``git fetch origin <branch>`` only updates the remote-tracking ref
    opportunistically when the repo's *configured* fetch refspec covers
    that branch.  Mirrors of large monorepos are commonly configured with
    a narrow single-branch refspec (e.g.
    ``+refs/heads/master:refs/remotes/origin/master``), under which a
    bare-name fetch writes only ``FETCH_HEAD`` — the subsequent
    ``origin/<branch>`` lookup then silently resolves a stale (or
    missing) tracking ref and the worktree is created behind the real
    remote tip (#3068).  Fetching with an explicit refspec updates the
    tracking ref regardless of the repo's configuration.  The leading
    ``+`` permits non-fast-forward tracking-ref moves (rebased or
    force-pushed base branches).

    ``branch`` must be a bare branch name (no ``origin/`` prefix);
    callers strip the prefix before building the refspec.
    """
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
