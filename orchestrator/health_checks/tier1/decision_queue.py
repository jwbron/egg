"""HITL / decision-queue wedge detectors (#2270 §-core, slice-8, #2219).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``decision_state`` field of the snapshot:

* :func:`detect_auto_advance_wedge` — an auto-advanceable approved decision that
  never advanced the phase.
* :func:`detect_approved_decision_orphaned` — a decision approved/resolved with
  no consumer acting on it.
* :func:`detect_restarted_decision_replay` — a decision replayed after an
  orchestrator restart (a stale escalation cascade).
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
FINDING_AUTO_ADVANCE_WEDGE = "auto_advance_wedge"
FINDING_APPROVED_DECISION_ORPHANED = "approved_decision_orphaned"
FINDING_RESTARTED_DECISION_REPLAY = "restarted_decision_replay"

# The phase-state status that means the orchestrator believes work is in flight.
_RUNNING_STATUS = "RUNNING"

# Decision statuses that mean an operator has cleared the gate.
_RESOLVED_STATUSES = frozenset({"approved", "resolved"})

# Default grace, in seconds, before an approved-but-not-advanced decision is
# treated as a wedge rather than mid-flight bookkeeping.
_DEFAULT_AUTO_ADVANCE_GRACE_S = 180.0
# Default grace, in seconds, before a resolved decision with no consumer is
# treated as orphaned.
_DEFAULT_ORPHANED_GRACE_S = 300.0


def _phase_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "phase_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _decision_state(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "decision_state", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce ``value`` to ``float`` defensively; ``None`` on non-numerics."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_auto_advance_wedge(
    snapshot: Any,
    *,
    grace_s: float = _DEFAULT_AUTO_ADVANCE_GRACE_S,
) -> Finding | None:
    """Fire when an auto-advanceable approved decision did not advance the phase.

    The defect (#2219): a decision is approved and flagged auto-advance-ready,
    but the phase never advanced — the auto-advance path wedged. The signature
    is a RUNNING phase whose ``decision_state.auto_advance_ready`` is truthy and
    whose ``auto_advance_age_s`` has aged past ``grace_s``.

    Deterministic → ``requires_adjudication=False``.
    """
    state = _phase_state(snapshot)
    if str(state.get("status", "")) != _RUNNING_STATUS:
        return None

    decision = _decision_state(snapshot)
    if not decision.get("auto_advance_ready"):
        return None
    age_s = _as_float(decision.get("auto_advance_age_s"))
    if age_s is None or age_s <= grace_s:
        return None

    return Finding(
        finding_class=FINDING_AUTO_ADVANCE_WEDGE,
        severity=Severity.HIGH,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "auto_advance_ready": True,
            "auto_advance_age_s": age_s,
            "grace_s": grace_s,
        },
        recommended_action=(
            "An auto-advanceable approved decision did not advance the phase "
            "past the grace window (#2219). Re-trigger the auto-advance path or "
            "advance the phase manually; the decision consumer is wedged."
        ),
        requires_adjudication=False,
        detector_key="auto_advance_wedge",
    )


detect_auto_advance_wedge.detector_key = "auto_advance_wedge"  # type: ignore[attr-defined]
detect_auto_advance_wedge.name = "auto_advance_wedge_detector"  # type: ignore[attr-defined]


def detect_approved_decision_orphaned(
    snapshot: Any,
    *,
    grace_s: float = _DEFAULT_ORPHANED_GRACE_S,
) -> Finding | None:
    """Fire when an approved/resolved decision has no consumer acting on it.

    The defect (#2219): a decision is approved/resolved by the operator but no
    downstream consumer ever picks it up — it is orphaned. Fires when
    ``decision_state.approved_unconsumed`` is truthy, OR when the last decision's
    status is approved/resolved and its ``orphaned_age_s`` has aged past
    ``grace_s``.

    Deterministic → ``requires_adjudication=False``.
    """
    decision = _decision_state(snapshot)

    approved_unconsumed = bool(decision.get("approved_unconsumed"))

    last_status = str(decision.get("last_decision_status", "") or "")
    orphaned_age_s = _as_float(decision.get("orphaned_age_s"))
    aged_out = (
        last_status in _RESOLVED_STATUSES
        and orphaned_age_s is not None
        and orphaned_age_s > grace_s
    )

    if not (approved_unconsumed or aged_out):
        return None

    return Finding(
        finding_class=FINDING_APPROVED_DECISION_ORPHANED,
        severity=Severity.MEDIUM,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "approved_unconsumed": approved_unconsumed,
            "last_decision_status": last_status or None,
            "orphaned_age_s": orphaned_age_s,
            "grace_s": grace_s,
        },
        recommended_action=(
            "A decision is approved/resolved but no consumer acted on it "
            "(orphaned, #2219). Re-deliver the decision to its consumer or "
            "advance the phase manually."
        ),
        requires_adjudication=False,
        detector_key="approved_decision_orphaned",
    )


detect_approved_decision_orphaned.detector_key = "approved_decision_orphaned"  # type: ignore[attr-defined]
detect_approved_decision_orphaned.name = "approved_decision_orphaned_detector"  # type: ignore[attr-defined]


def detect_restarted_decision_replay(snapshot: Any) -> Finding | None:
    """Fire when a decision is being replayed after an orchestrator restart.

    The defect (#2219): after an orchestrator restart, a stale decision is
    re-escalated — a replay cascade that re-asks an already-answered question.
    Fires when ``decision_state.replay_count`` exceeds 1, OR when
    ``replayed_after_restart`` is truthy.

    Deterministic → ``requires_adjudication=False``.
    """
    decision = _decision_state(snapshot)

    replay_count = _as_float(decision.get("replay_count"))
    replayed_after_restart = bool(decision.get("replayed_after_restart"))

    too_many_replays = replay_count is not None and replay_count > 1
    if not (too_many_replays or replayed_after_restart):
        return None

    return Finding(
        finding_class=FINDING_RESTARTED_DECISION_REPLAY,
        severity=Severity.MEDIUM,
        evidence={
            "phase": getattr(snapshot, "phase", None),
            "replay_count": replay_count,
            "replayed_after_restart": replayed_after_restart,
        },
        recommended_action=(
            "A decision is being replayed after an orchestrator restart (stale "
            "escalation cascade, #2219). De-duplicate against the already "
            "answered decision rather than re-escalating it."
        ),
        requires_adjudication=False,
        detector_key="restarted_decision_replay",
    )


detect_restarted_decision_replay.detector_key = "restarted_decision_replay"  # type: ignore[attr-defined]
detect_restarted_decision_replay.name = "restarted_decision_replay_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_approved_decision_orphaned",
    "detect_auto_advance_wedge",
    "detect_restarted_decision_replay",
]
