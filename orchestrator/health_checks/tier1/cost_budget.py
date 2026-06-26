"""Cost / budget detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False``.

Registered into the slice-1 calibration corpus by ``detector_key`` and into the
production :class:`DetectionPlane` (see ``DetectionPlane.default``).

Detectors here key on the ``cost_counters`` section of the snapshot:

* :func:`detect_cost_anomaly` — the hourly LLM cost has breached the configured
  ``max_llm_cost_per_hour`` budget envelope.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class string (matched structurally on the raw string by the plane).
FINDING_COST_ANOMALY = "cost_anomaly"

# Default hourly-cost ceiling when the snapshot omits an explicit budget.
_DEFAULT_MAX_COST_PER_HOUR = 5.0


def _cost_counters(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "cost_counters", None)
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_cost_anomaly(snapshot: Any) -> Finding | None:
    """Fire when hourly LLM cost breaches the configured budget envelope.

    Fires when ``cost_counters.cost_per_hour_usd`` exceeds the snapshot's
    ``cost_counters.max_llm_cost_per_hour`` (falling back to a default). Cost at
    or under budget (or absent) stays silent — the ``max_llm_cost_per_hour``
    envelope is the single budget control (#2270 §5).

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _cost_counters(snapshot)
    cost_per_hour = _as_float(counters.get("cost_per_hour_usd"))
    if cost_per_hour is None:
        return None
    budget = _as_float(counters.get("max_llm_cost_per_hour"))
    if budget is None:
        budget = _DEFAULT_MAX_COST_PER_HOUR
    if cost_per_hour <= budget:
        return None

    return Finding(
        finding_class=FINDING_COST_ANOMALY,
        severity=Severity.HIGH,
        evidence={
            "cost_per_hour_usd": cost_per_hour,
            "max_llm_cost_per_hour": budget,
            "tokens": counters.get("tokens"),
            "cost_usd": counters.get("cost_usd"),
        },
        recommended_action=(
            "Hourly LLM cost has breached the max_llm_cost_per_hour budget "
            "envelope. Throttle or pause the offending agent tier; check for a "
            "runaway prompt or a model-tier misroute."
        ),
        requires_adjudication=False,
        detector_key="cost_anomaly",
    )


detect_cost_anomaly.detector_key = "cost_anomaly"  # type: ignore[attr-defined]
detect_cost_anomaly.name = "cost_anomaly_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_cost_anomaly",
]
