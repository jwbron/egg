"""LLM-substrate detectors (#2270 §5, slice-8; #2769).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``DetectionPlane.default``).

Detectors here key on the ``llm`` section of the snapshot (carried in ``raw``):

* :func:`detect_llm_substrate_unreachable` — the LiteLLM proxy is unreachable
  (#2769).
* :func:`detect_effective_model_drift` — the served model differs from the
  requested model.
* :func:`detect_anthropic_5xx_sustained` — a sustained Anthropic 5xx streak.
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
FINDING_LLM_SUBSTRATE_UNREACHABLE = "llm_substrate_unreachable"
FINDING_EFFECTIVE_MODEL_DRIFT = "effective_model_drift"
FINDING_ANTHROPIC_5XX = "anthropic_5xx"

# Default consecutive LiteLLM failures before "unreachable" is provable.
_DEFAULT_UNREACHABLE_FAILURES = 1
# Default sustained Anthropic 5xx streak before it is worth surfacing.
_DEFAULT_ANTHROPIC_5XX_STREAK = 3


def _llm(snapshot: Any) -> dict[str, Any]:
    """The ``llm`` section of the snapshot (carried in ``raw``)."""
    raw = getattr(snapshot, "raw", {}) or {}
    section = raw.get("llm", {}) if isinstance(raw, dict) else {}
    return section if isinstance(section, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_llm_substrate_unreachable(
    snapshot: Any,
    *,
    failure_threshold: int = _DEFAULT_UNREACHABLE_FAILURES,
) -> Finding | None:
    """Fire when the LiteLLM proxy is unreachable (#2769).

    Fires when ``llm.litellm_reachable`` is explicitly ``False`` (a missing flag
    is NOT evidence of unreachability), or when ``llm.consecutive_failures``
    reaches the threshold.

    Deterministic → ``requires_adjudication=False``.
    """
    llm = _llm(snapshot)
    reachable = llm.get("litellm_reachable")
    failures = _as_float(llm.get("consecutive_failures")) or 0.0

    if not (reachable is False or failures >= failure_threshold):
        return None

    return Finding(
        finding_class=FINDING_LLM_SUBSTRATE_UNREACHABLE,
        severity=Severity.HIGH,
        evidence={
            "litellm_reachable": reachable,
            "consecutive_failures": failures,
        },
        recommended_action=(
            "The LiteLLM proxy is unreachable (#2769). Verify the proxy pod and "
            "upstream routing before respawning agents; do not burn the respawn "
            "budget against a dead substrate."
        ),
        requires_adjudication=False,
        detector_key="llm_substrate_unreachable",
    )


detect_llm_substrate_unreachable.detector_key = "llm_substrate_unreachable"  # type: ignore[attr-defined]
detect_llm_substrate_unreachable.name = "llm_substrate_unreachable_detector"  # type: ignore[attr-defined]


def detect_effective_model_drift(snapshot: Any) -> Finding | None:
    """Fire when the served model differs from the requested model.

    Fires when both ``llm.requested_model`` and ``llm.effective_model`` are
    present and differ. Equal or absent values stay silent.

    Deterministic → ``requires_adjudication=False``.
    """
    llm = _llm(snapshot)
    requested = llm.get("requested_model")
    effective = llm.get("effective_model")
    if not (requested and effective) or requested == effective:
        return None

    return Finding(
        finding_class=FINDING_EFFECTIVE_MODEL_DRIFT,
        severity=Severity.MEDIUM,
        evidence={
            "requested_model": requested,
            "effective_model": effective,
        },
        recommended_action=(
            "The effective served model differs from the requested model. "
            "Inspect LiteLLM routing / fallbacks — agents may be silently "
            "downgraded off their resolved tier."
        ),
        requires_adjudication=False,
        detector_key="effective_model_drift",
    )


detect_effective_model_drift.detector_key = "effective_model_drift"  # type: ignore[attr-defined]
detect_effective_model_drift.name = "effective_model_drift_detector"  # type: ignore[attr-defined]


def detect_anthropic_5xx_sustained(
    snapshot: Any,
    *,
    streak_threshold: int = _DEFAULT_ANTHROPIC_5XX_STREAK,
) -> Finding | None:
    """Fire on a sustained Anthropic 5xx streak.

    Fires when ``llm.anthropic_5xx_streak`` reaches the threshold. A short/absent
    streak stays silent (transient upstream blips are not an outage).

    Deterministic → ``requires_adjudication=False``.
    """
    llm = _llm(snapshot)
    streak = _as_float(llm.get("anthropic_5xx_streak"))
    if streak is None or streak < streak_threshold:
        return None

    return Finding(
        finding_class=FINDING_ANTHROPIC_5XX,
        severity=Severity.HIGH,
        evidence={
            "anthropic_5xx_streak": streak,
            "window_s": llm.get("window_s"),
            "streak_threshold": streak_threshold,
        },
        recommended_action=(
            "Sustained Anthropic 5xx errors. Back off and retry; surface to the "
            "operator if the upstream outage persists."
        ),
        requires_adjudication=False,
        detector_key="anthropic_5xx",
    )


detect_anthropic_5xx_sustained.detector_key = "anthropic_5xx"  # type: ignore[attr-defined]
detect_anthropic_5xx_sustained.name = "anthropic_5xx_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_anthropic_5xx_sustained",
    "detect_effective_model_drift",
    "detect_llm_substrate_unreachable",
]
