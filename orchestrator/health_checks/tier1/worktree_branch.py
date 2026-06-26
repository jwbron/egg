"""Worktree / branch + external-PR-state detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``git_state`` field of the snapshot:

* :func:`detect_worktree_corruption` — a corrupt git index or a stale index
  lock that has outlived its grace window.
* :func:`detect_disk_inode_pressure` — disk or inode exhaustion on the worktree
  volume.
* :func:`detect_pr_external_mutation` — the PR head was mutated outside the
  pipeline (its head SHA no longer matches the last commit we pushed).
* :func:`detect_pushed_pr_not_updated` — a local pushed commit is not yet
  reflected in the PR head after the grace window, and the PR was NOT mutated
  externally (that case belongs to :func:`detect_pr_external_mutation`).

A ``branch_divergence`` detector already exists (slice-7) and is intentionally
NOT duplicated here — these are distinct concerns.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class strings. Emitted as plain strings (the detection plane matches a
# detector's output structurally on the raw string, so slice-8 may name classes
# beyond the pinned ``FindingClass`` enum — see health_checks/types.py).
FINDING_WORKTREE_CORRUPTION = "worktree_corruption"
FINDING_DISK_INODE_PRESSURE = "disk_inode_pressure"
FINDING_PR_EXTERNAL_MUTATION = "pr_external_mutation"
FINDING_PUSHED_PR_NOT_UPDATED = "pushed_pr_not_updated"

# Default grace (seconds) before a held index lock is treated as corruption
# rather than a transient lock taken by an in-flight git operation.
_DEFAULT_LOCK_GRACE_S = 300
# Default percentage at/above which disk or inode usage is treated as exhaustion.
_DEFAULT_PRESSURE_PCT = 90
# Default grace (seconds) before a pushed-but-unreflected commit is surfaced,
# giving GitHub time to ingest the push before we cry wolf.
_DEFAULT_PUSH_GRACE_S = 180


def _git_state(snapshot: Any) -> dict[str, Any]:
    state = getattr(snapshot, "git_state", None)
    return state if isinstance(state, dict) else {}


def _raw_section(snapshot: Any, name: str) -> dict[str, Any]:
    """Return the named top-level section of the snapshot (carried in ``raw``)."""
    raw = getattr(snapshot, "raw", {}) or {}
    section = raw.get(name, {}) if isinstance(raw, dict) else {}
    return section if isinstance(section, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_worktree_corruption(
    snapshot: Any,
    *,
    lock_grace_s: int = _DEFAULT_LOCK_GRACE_S,
) -> Finding | None:
    """Fire on git index/lock corruption.

    Corruption is provable when ``git_state.fsck_errors`` is positive, when the
    index lock is held (``index_lock_present``), or when a held lock has outlived
    its grace window (``lock_age_s`` > ``lock_grace_s``) — a transient lock under
    the grace window is normal in-flight git churn and stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    git_state = _git_state(snapshot)

    fsck_errors = _as_float(git_state.get("fsck_errors")) or 0.0
    corrupt = fsck_errors > 0
    index_locked = bool(git_state.get("index_lock_present"))
    lock_age = _as_float(git_state.get("lock_age_s"))
    lock_stale = lock_age is not None and lock_age > lock_grace_s

    if not (corrupt or index_locked or lock_stale):
        return None

    return Finding(
        finding_class=FINDING_WORKTREE_CORRUPTION,
        severity=Severity.HIGH,
        evidence={
            "branch": git_state.get("branch"),
            "fsck_errors": fsck_errors,
            "index_lock_present": index_locked,
            "lock_age_s": lock_age,
            "lock_grace_s": lock_grace_s,
        },
        recommended_action=(
            "The git worktree index is corrupt or holding a stale lock past the "
            "grace window. Clear the stale .git/index.lock (or rebuild the index) "
            "before the next git operation; recreate the worktree if corruption "
            "persists."
        ),
        requires_adjudication=False,
        detector_key="worktree_corruption",
    )


detect_worktree_corruption.detector_key = "worktree_corruption"  # type: ignore[attr-defined]
detect_worktree_corruption.name = "worktree_corruption_detector"  # type: ignore[attr-defined]


def detect_disk_inode_pressure(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_PRESSURE_PCT,
) -> Finding | None:
    """Fire on disk or inode exhaustion on the worktree volume.

    Fires when either ``resources.disk_used_pct`` or ``resources.inode_used_pct``
    is at/above the snapshot's ``resources.disk_threshold_pct`` (falling back to
    ``threshold``). Usage below the threshold (or absent) stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    resources = _raw_section(snapshot, "resources")

    disk_pct = _as_float(resources.get("disk_used_pct"))
    inode_pct = _as_float(resources.get("inode_used_pct"))
    limit = _as_float(resources.get("disk_threshold_pct"))
    if limit is None:
        limit = float(threshold)

    disk_pressure = disk_pct is not None and disk_pct >= limit
    inode_pressure = inode_pct is not None and inode_pct >= limit

    if not (disk_pressure or inode_pressure):
        return None

    return Finding(
        finding_class=FINDING_DISK_INODE_PRESSURE,
        severity=Severity.MEDIUM,
        evidence={
            "disk_used_pct": disk_pct,
            "inode_used_pct": inode_pct,
            "threshold": limit,
        },
        recommended_action=(
            "Disk or inode usage on the worktree volume is at/above the "
            "exhaustion threshold. Reclaim space (prune worktrees/build "
            "artifacts/logs) before the next write; an exhausted volume will "
            "fail git operations."
        ),
        requires_adjudication=False,
        detector_key="disk_inode_pressure",
    )


detect_disk_inode_pressure.detector_key = "disk_inode_pressure"  # type: ignore[attr-defined]
detect_disk_inode_pressure.name = "disk_inode_pressure_detector"  # type: ignore[attr-defined]


def detect_pr_external_mutation(snapshot: Any) -> Finding | None:
    """Fire when the PR head was mutated outside the pipeline.

    Fires when ``pr_state.external_mutation`` is set, or when both the PR head
    SHA and the pushed SHA are present and differ — meaning the PR head is no
    longer the commit we pushed. Equal or absent SHAs stay silent (a missing SHA
    is not provable divergence; an equal pair means the PR reflects our push).

    Deterministic → ``requires_adjudication=False``.
    """
    pr_state = _raw_section(snapshot, "pr_state")

    pr_head = pr_state.get("pr_head_sha")
    pushed = pr_state.get("pushed_sha")
    sha_divergence = (
        pr_head is not None
        and pushed is not None
        and pr_head != pushed
    )

    if not (pr_state.get("external_mutation") or sha_divergence):
        return None

    return Finding(
        finding_class=FINDING_PR_EXTERNAL_MUTATION,
        severity=Severity.MEDIUM,
        evidence={
            "pr_head_sha": pr_head,
            "pushed_sha": pushed,
            "external_mutation": bool(pr_state.get("external_mutation")),
        },
        recommended_action=(
            "The PR head was mutated outside the pipeline (its head SHA no longer "
            "matches the commit we last pushed). Reconcile against the external "
            "change before pushing again to avoid clobbering it."
        ),
        requires_adjudication=False,
        detector_key="pr_external_mutation",
    )


detect_pr_external_mutation.detector_key = "pr_external_mutation"  # type: ignore[attr-defined]
detect_pr_external_mutation.name = "pr_external_mutation_detector"  # type: ignore[attr-defined]


def detect_pushed_pr_not_updated(
    snapshot: Any,
    *,
    push_grace_s: int = _DEFAULT_PUSH_GRACE_S,
) -> Finding | None:
    """Fire when a local pushed commit is not reflected in the PR head.

    Fires when the last-pushed SHA and PR head SHA are present and differ, the
    push is older than ``push_grace_s`` (so GitHub had time to ingest it), and the
    PR was NOT mutated externally — that latter case belongs to
    :func:`detect_pr_external_mutation`, so the two detectors never double-fire on
    the same condition.

    Deterministic → ``requires_adjudication=False``.
    """
    git_state = _git_state(snapshot)

    pr_head = git_state.get("pr_head_sha")
    last_pushed = git_state.get("last_pushed_sha")
    if pr_head is None or last_pushed is None or pr_head == last_pushed:
        return None

    pushed_age = _as_float(git_state.get("pushed_age_s"))
    if pushed_age is None or pushed_age <= push_grace_s:
        return None

    if git_state.get("pr_externally_mutated"):
        return None

    return Finding(
        finding_class=FINDING_PUSHED_PR_NOT_UPDATED,
        severity=Severity.MEDIUM,
        evidence={
            "pr_head_sha": pr_head,
            "last_pushed_sha": last_pushed,
            "pushed_age_s": pushed_age,
            "push_grace_s": push_grace_s,
        },
        recommended_action=(
            "A local commit was pushed but the PR head still does not reflect it "
            "past the grace window. Confirm the push reached the remote and the "
            "PR points at the right branch; re-push if the remote is behind."
        ),
        requires_adjudication=False,
        detector_key="pushed_pr_not_updated",
    )


detect_pushed_pr_not_updated.detector_key = "pushed_pr_not_updated"  # type: ignore[attr-defined]
detect_pushed_pr_not_updated.name = "pushed_pr_not_updated_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_disk_inode_pressure",
    "detect_pr_external_mutation",
    "detect_pushed_pr_not_updated",
    "detect_worktree_corruption",
]
