"""Orchestrator-owned contract live store.

Fixes #1781: previously the gateway's contract API forwarded writes
into each caller's per-agent worktree, so producers and reviewers
saw different copies of the same contract and NACK'd each other on
phantom divergence.

Under this design, there is exactly one live contract file per
pipeline — inside the *shared* pipeline worktree at
``/home/egg/.egg-worktrees/<pipeline_id>/<repo>/.egg-state/contracts/``.
Agents no longer talk to their own worktree for contract state:
every ``egg-contract`` command hits the gateway, which proxies to the
orchestrator's ``/api/v1/contracts/…`` endpoints, which in turn read
and write the shared-worktree file.  Per-agent worktrees are purely
code-isolation again.

Serialization to the feature branch keeps happening via
``_commit_statefiles_to_worktree`` at pipeline checkpoint events
(phase completion, pre-PR).  Because the live file already lives in
the shared worktree, those commits pick it up naturally — no
separate "serialize then commit" dance is needed.

In-process concurrency is serialized via per-identifier RLocks to
protect concurrent mutations arriving from multiple Flask threads.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

# Shared packages live under ../shared relative to this file.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

if TYPE_CHECKING:
    from egg_contracts import Contract

logger = logging.getLogger("orchestrator.contract_store")


# Must match gateway/contract_api.py WORKTREE_BASE_DIR and the
# docker-compose volume mounts.  Pipeline worktrees live at
# ``<base>/<pipeline_id>/<repo>/``.
_WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Path components must be simple names — no slashes, dots-only, or other
# characters that could escape the worktree base directory.
_SAFE_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(identifier: int | str) -> threading.RLock:
    """Return the per-identifier lock used to serialize mutations."""
    key = str(identifier)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def resolve_pipeline_worktree(
    pipeline_id: str,
    repo_hint: str | None = None,
) -> Path | None:
    """Locate the shared pipeline worktree for *pipeline_id*.

    Returns the path when the worktree exists, or ``None`` if the
    pipeline is not currently set up on this host (e.g. the worktree
    has already been cleaned up).

    The worktree is a git checkout, so this also verifies ``.git`` is
    present before returning a candidate.

    Args:
        pipeline_id: Pipeline identifier (e.g. ``issue-1781`` or
            ``pipeline-abc12345``).
        repo_hint: Optional repo name (last component of ``owner/repo``)
            to disambiguate when the host runs multiple repos.
    """
    if not pipeline_id or not _SAFE_COMPONENT_RE.match(pipeline_id):
        return None
    if repo_hint and not _SAFE_COMPONENT_RE.match(repo_hint):
        return None

    base = _WORKTREE_BASE_DIR / pipeline_id
    if not base.is_dir():
        return None

    candidates: list[Path] = []
    if repo_hint:
        candidates.append(base / repo_hint)

    # Fall back to any subdirectory that looks like a git checkout.
    try:
        for entry in base.iterdir():
            if entry.is_dir() and entry not in candidates:
                candidates.append(entry)
    except OSError:
        return None

    for candidate in candidates:
        if (candidate / ".git").exists():
            if repo_hint and candidate.name != repo_hint:
                logger.warning(
                    "Worktree repo name mismatch, using fallback",
                    extra={
                        "expected_repo": repo_hint,
                        "actual_repo": candidate.name,
                        "pipeline_id": pipeline_id,
                    },
                )
            return candidate

    return None


def load_contract_from_branch(
    identifier: int | str,
    repo_path: Path,
    branch: str,
) -> Contract | None:
    """Read the committed contract for *identifier* from *branch*.

    Enables post-completion reads (PR review, contract audits) once
    the shared pipeline worktree has been pruned. The on-branch file at
    ``.egg-state/contracts/<pipeline_id>.json`` is authoritative after
    the pipeline's final commit.

    Tries ``origin/<branch>`` first — the remote ref survives local
    cleanup and reflects the last pushed state — then falls back to
    ``<branch>`` in case the main repo has a local ref but no origin
    copy (e.g. pre-push crashes).

    Returns ``None`` when neither ref yields the contract file.
    Propagates ``ContractValidationError`` — a malformed contract is a
    real failure, not a miss. ``repo_path`` should be the main repo
    checkout (``StateStore.repo_path``), not a worktree path.
    """
    # Lazy import — egg_contracts sits in shared/ and is wired into
    # sys.path above, but importing at module load would force every
    # caller of contract_store to pull it in.
    from egg_contracts import ContractNotFoundError
    from egg_contracts import load_contract_from_branch as _load_from_branch

    refs = []
    if not branch.startswith("origin/"):
        refs.append(f"origin/{branch}")
    refs.append(branch)

    last_error: ContractNotFoundError | None = None
    for ref in refs:
        try:
            return _load_from_branch(identifier, repo_path, ref)
        except ContractNotFoundError as exc:
            last_error = exc
            continue

    logger.debug(
        "Branch-read fallback failed",
        extra={
            "identifier": str(identifier),
            "branch": branch,
            "repo_path": str(repo_path),
            "error": str(last_error) if last_error else None,
        },
    )
    return None
