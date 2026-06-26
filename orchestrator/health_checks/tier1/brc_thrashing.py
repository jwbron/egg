"""BRC consensus-thrash detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``consensus`` field of the snapshot:

* :func:`detect_brc_thrashing` — a reviewer/producer NACK->propose->NACK thrash.
* :func:`detect_late_confirm_renack` — a CONFIRMED edge followed by a re-NACK.
* :func:`detect_incomplete_consensus_deferral` — unbounded incomplete-consensus
  deferral.
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
FINDING_BRC_THRASHING = "brc_thrashing"
FINDING_LATE_CONFIRM_RENACK = "late_confirm_renack"
FINDING_INCOMPLETE_CONSENSUS_DEFERRAL = "incomplete_consensus_deferral"

# Default number of NACK->propose->NACK cycles before a consensus edge is
# considered thrashing rather than ordinary back-and-forth review.
_DEFAULT_THRASH_THRESHOLD = 3
# Default cap on incomplete-consensus deferrals before the unbounded-deferral
# loop is itself worth surfacing.
_DEFAULT_DEFERRAL_CAP = 5


def _consensus(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "consensus", None)
    return raw if isinstance(raw, dict) else {}


def _as_int(value: Any) -> int | None:
    """Best-effort numeric coercion; ``None`` for non-numeric / missing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def detect_brc_thrashing(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_THRASH_THRESHOLD,
) -> Finding | None:
    """Fire on a reviewer/producer NACK->propose->NACK consensus thrash.

    A consensus edge that cycles NACK -> re-propose -> NACK repeatedly is not
    making progress. Fire when either the recorded ``thrash_count`` or the
    ``nack_cycles`` count reaches ``threshold``. A small amount of normal
    back-and-forth (below ``threshold``) stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    consensus = _consensus(snapshot)

    thrash_count = _as_int(consensus.get("thrash_count"))
    nack_cycles = _as_int(consensus.get("nack_cycles"))

    thrash_hit = thrash_count is not None and thrash_count >= threshold
    cycles_hit = nack_cycles is not None and nack_cycles >= threshold
    if not (thrash_hit or cycles_hit):
        return None

    return Finding(
        finding_class=FINDING_BRC_THRASHING,
        severity=Severity.MEDIUM,
        evidence={
            "protocol": consensus.get("protocol"),
            "blocking_agents": consensus.get("blocking_agents"),
            "thrash_count": thrash_count,
            "nack_cycles": nack_cycles,
            "threshold": threshold,
        },
        recommended_action=(
            "A BRC consensus edge is thrashing (NACK->propose->NACK past the "
            "threshold) without converging. Surface the disagreement for "
            "adjudication or an operator HITL rather than letting the producer "
            "re-propose indefinitely."
        ),
        requires_adjudication=False,
        detector_key="brc_thrashing",
    )


detect_brc_thrashing.detector_key = "brc_thrashing"  # type: ignore[attr-defined]
detect_brc_thrashing.name = "brc_thrashing_detector"  # type: ignore[attr-defined]


def detect_late_confirm_renack(snapshot: Any) -> Finding | None:
    """Fire when a CONFIRMED consensus edge is followed by a re-NACK.

    Once consensus is CONFIRMED the edge should be settled; a subsequent NACK
    means the confirmation was premature or has been reopened. Fire when the
    snapshot records ``confirmed_then_renacked`` as truthy, or a non-zero
    ``post_confirm_nack_count``.

    Deterministic → ``requires_adjudication=False``.
    """
    consensus = _consensus(snapshot)

    confirmed_then_renacked = bool(consensus.get("confirmed_then_renacked"))
    post_confirm_nack = _as_int(consensus.get("post_confirm_nack_count"))
    post_confirm_hit = post_confirm_nack is not None and post_confirm_nack >= 1

    if not (confirmed_then_renacked or post_confirm_hit):
        return None

    return Finding(
        finding_class=FINDING_LATE_CONFIRM_RENACK,
        severity=Severity.MEDIUM,
        evidence={
            "protocol": consensus.get("protocol"),
            "blocking_agents": consensus.get("blocking_agents"),
            "confirmed_then_renacked": confirmed_then_renacked,
            "post_confirm_nack_count": post_confirm_nack,
        },
        recommended_action=(
            "A consensus edge was re-NACKed after it had already been CONFIRMED "
            "(#2270 §5). Treat the confirmation as reopened and re-run the BRC "
            "cycle for the edge rather than merging on the stale CONFIRMED."
        ),
        requires_adjudication=False,
        detector_key="late_confirm_renack",
    )


detect_late_confirm_renack.detector_key = "late_confirm_renack"  # type: ignore[attr-defined]
detect_late_confirm_renack.name = "late_confirm_renack_detector"  # type: ignore[attr-defined]


def detect_incomplete_consensus_deferral(
    snapshot: Any,
    *,
    cap: int = _DEFAULT_DEFERRAL_CAP,
) -> Finding | None:
    """Fire on unbounded incomplete-consensus deferral.

    An incomplete consensus may be deferred a bounded number of times while it
    converges; deferring past ``cap`` is an unbounded-deferral loop. Fire when
    the recorded ``deferral_count`` exceeds ``cap`` (deferrals at or below the
    cap stay silent).

    Deterministic → ``requires_adjudication=False``.
    """
    consensus = _consensus(snapshot)

    deferral_count = _as_int(consensus.get("deferral_count"))
    if deferral_count is None or deferral_count <= cap:
        return None

    return Finding(
        finding_class=FINDING_INCOMPLETE_CONSENSUS_DEFERRAL,
        severity=Severity.MEDIUM,
        evidence={
            "protocol": consensus.get("protocol"),
            "blocking_agents": consensus.get("blocking_agents"),
            "deferral_count": deferral_count,
            "cap": cap,
        },
        recommended_action=(
            "An incomplete consensus has been deferred past its cap (#2270 §5), "
            "an unbounded-deferral loop. Force resolution of the edge or escalate "
            "to an operator HITL rather than deferring again."
        ),
        requires_adjudication=False,
        detector_key="incomplete_consensus_deferral",
    )


detect_incomplete_consensus_deferral.detector_key = "incomplete_consensus_deferral"  # type: ignore[attr-defined]
detect_incomplete_consensus_deferral.name = "incomplete_consensus_deferral_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_brc_thrashing",
    "detect_incomplete_consensus_deferral",
    "detect_late_confirm_renack",
]
