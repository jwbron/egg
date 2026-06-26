"""BRC consensus-thrash detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline).

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``DetectionPlane.default``).

Detectors here key on the ``consensus`` section of the snapshot:

* :func:`detect_brc_thrash` — reviewer/producer NACK→propose→NACK thrash, or a
  late CONFIRMED that was then re-NACKed. A thrash needs human/LLM judgement to
  break (adjudicate), so it escalates.
* :func:`detect_incomplete_consensus_deferral` — unbounded incomplete-consensus
  deferral past its cap.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class strings (matched structurally on the raw string by the plane).
FINDING_BRC_THRASH = "brc_thrash"
FINDING_INCOMPLETE_CONSENSUS_DEFERRAL = "incomplete_consensus_deferral"

# Default NACK-cycle count before a review edge is treated as thrashing.
_DEFAULT_NACK_CYCLE_THRESHOLD = 3
# Default cap fallback when the snapshot does not carry an explicit deferral cap.
_DEFAULT_DEFERRAL_CAP = 20


def _consensus(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "consensus", None)
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_brc_thrash(
    snapshot: Any,
    *,
    nack_cycle_threshold: int = _DEFAULT_NACK_CYCLE_THRESHOLD,
) -> Finding | None:
    """Fire on BRC review thrash or a late CONFIRMED-then-re-NACK.

    Fires when ``consensus.nack_cycles`` reaches ``nack_cycle_threshold`` (a
    review edge cycling NACK→propose→NACK without converging), OR when
    ``consensus.late_confirmed_then_renack`` is set (a CONFIRMED edge that was
    then re-NACKed). Both are genuine disagreements that need adjudication to
    break, so the verdict escalates → ``requires_adjudication=True``.
    """
    consensus = _consensus(snapshot)
    nack_cycles = _as_float(consensus.get("nack_cycles")) or 0.0
    late_renack = bool(consensus.get("late_confirmed_then_renack"))

    if not (nack_cycles >= nack_cycle_threshold or late_renack):
        return None

    return Finding(
        finding_class=FINDING_BRC_THRASH,
        severity=Severity.MEDIUM,
        evidence={
            "nack_cycles": nack_cycles,
            "late_confirmed_then_renack": late_renack,
            "nack_cycle_threshold": nack_cycle_threshold,
        },
        recommended_action=(
            "BRC consensus is thrashing (repeated NACK cycles, or a CONFIRMED "
            "edge re-NACKed). Adjudicate the disagreement or open an operator "
            "HITL to break the loop rather than letting it cycle."
        ),
        requires_adjudication=True,
        detector_key="brc_thrash",
    )


detect_brc_thrash.detector_key = "brc_thrash"  # type: ignore[attr-defined]
detect_brc_thrash.name = "brc_thrash_detector"  # type: ignore[attr-defined]


def detect_incomplete_consensus_deferral(snapshot: Any) -> Finding | None:
    """Fire when incomplete-consensus deferral exceeds its cap.

    Fires when ``consensus.incomplete_consensus_deferrals`` exceeds the snapshot's
    ``consensus.deferral_cap`` (falling back to a default). Caps the unbounded
    "defer again" loop so it cannot run forever.

    Deterministic → ``requires_adjudication=False``.
    """
    consensus = _consensus(snapshot)
    deferrals = _as_float(consensus.get("incomplete_consensus_deferrals"))
    if deferrals is None:
        return None
    cap = _as_float(consensus.get("deferral_cap"))
    if cap is None:
        cap = float(_DEFAULT_DEFERRAL_CAP)
    if deferrals <= cap:
        return None

    return Finding(
        finding_class=FINDING_INCOMPLETE_CONSENSUS_DEFERRAL,
        severity=Severity.MEDIUM,
        evidence={
            "incomplete_consensus_deferrals": deferrals,
            "deferral_cap": cap,
        },
        recommended_action=(
            "Incomplete-consensus deferral exceeded its cap. Stop deferring and "
            "escalate the blocked consensus to an operator HITL."
        ),
        requires_adjudication=False,
        detector_key="incomplete_consensus_deferral",
    )


detect_incomplete_consensus_deferral.detector_key = "incomplete_consensus_deferral"  # type: ignore[attr-defined]
detect_incomplete_consensus_deferral.name = "incomplete_consensus_deferral_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_brc_thrash",
    "detect_incomplete_consensus_deferral",
]
