"""Cost / budget detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``cost_counters`` field of the snapshot:

* :func:`detect_cost_per_hour_breach` — hourly LLM cost over budget.
* :func:`detect_token_cost_anomaly` — an anomalous cost-per-token spike.
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
FINDING_COST_PER_HOUR_BREACH = "cost_per_hour_breach"
FINDING_TOKEN_COST_ANOMALY = "token_cost_anomaly"

# Default per-hour USD budget over which the hourly LLM spend is a breach.
_DEFAULT_MAX_PER_HOUR_USD = 5.0
# Default USD-per-token ceiling above which the cost-per-token is anomalous.
_DEFAULT_PER_TOKEN_THRESHOLD = 0.0005


def _cost_counters(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "cost_counters", None)
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` for non-numeric / missing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_cost_per_hour_breach(
    snapshot: Any,
    *,
    max_per_hour: float = _DEFAULT_MAX_PER_HOUR_USD,
) -> Finding | None:
    """Fire when the hourly LLM cost is over budget.

    Fire when the recorded ``cost_per_hour_usd`` strictly exceeds
    ``max_per_hour``. Spend at or below the budget stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _cost_counters(snapshot)

    cost_per_hour = _as_float(counters.get("cost_per_hour_usd"))
    if cost_per_hour is None or cost_per_hour <= max_per_hour:
        return None

    return Finding(
        finding_class=FINDING_COST_PER_HOUR_BREACH,
        severity=Severity.HIGH,
        evidence={
            "cost_per_hour_usd": cost_per_hour,
            "max_per_hour_usd": max_per_hour,
            "tokens": counters.get("tokens"),
            "cost_usd": counters.get("cost_usd"),
        },
        recommended_action=(
            "Hourly LLM spend is over budget (#2270 §5). Throttle or pause the "
            "offending cohort and inspect for a runaway loop before the spend "
            "compounds; escalate to an operator HITL if it persists."
        ),
        requires_adjudication=False,
        detector_key="cost_per_hour_breach",
    )


detect_cost_per_hour_breach.detector_key = "cost_per_hour_breach"  # type: ignore[attr-defined]
detect_cost_per_hour_breach.name = "cost_per_hour_breach_detector"  # type: ignore[attr-defined]


def detect_token_cost_anomaly(
    snapshot: Any,
    *,
    per_token_threshold: float = _DEFAULT_PER_TOKEN_THRESHOLD,
) -> Finding | None:
    """Fire on an anomalous cost-per-token spike.

    Fire when there is a positive ``tokens`` count and a present ``cost_usd``
    and the derived USD-per-token (``cost_usd / tokens``) exceeds
    ``per_token_threshold`` — or when the snapshot explicitly flags ``anomaly``.
    Division by zero is guarded by requiring ``tokens > 0`` first.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _cost_counters(snapshot)

    tokens = _as_float(counters.get("tokens"))
    cost_usd = _as_float(counters.get("cost_usd"))

    per_token = None
    threshold_hit = False
    if tokens is not None and tokens > 0 and cost_usd is not None:
        per_token = cost_usd / tokens
        threshold_hit = per_token > per_token_threshold

    anomaly_flag = bool(counters.get("anomaly"))

    if not (threshold_hit or anomaly_flag):
        return None

    return Finding(
        finding_class=FINDING_TOKEN_COST_ANOMALY,
        severity=Severity.MEDIUM,
        evidence={
            "tokens": tokens,
            "cost_usd": cost_usd,
            "cost_per_token_usd": per_token,
            "per_token_threshold_usd": per_token_threshold,
            "anomaly": anomaly_flag,
        },
        recommended_action=(
            "Cost-per-token has spiked above the expected ceiling (#2270 §5), "
            "indicating an anomalous route or a pathological prompt. Inspect the "
            "effective model/route for the cohort before continuing."
        ),
        requires_adjudication=False,
        detector_key="token_cost_anomaly",
    )


detect_token_cost_anomaly.detector_key = "token_cost_anomaly"  # type: ignore[attr-defined]
detect_token_cost_anomaly.name = "token_cost_anomaly_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_cost_per_hour_breach",
    "detect_token_cost_anomaly",
]
