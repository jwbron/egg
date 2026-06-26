"""LLM-substrate detectors (#2270 §5, slice-8; #2769).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``raw`` and ``gateway_error_counters`` fields of the
snapshot:

* :func:`detect_litellm_unreachable` — the LiteLLM proxy is unreachable.
* :func:`detect_effective_model_drift` — requested model != served model.
* :func:`detect_anthropic_5xx_sustained` — sustained Anthropic 5xx responses.
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
FINDING_LITELLM_UNREACHABLE = "litellm_unreachable"
FINDING_EFFECTIVE_MODEL_DRIFT = "effective_model_drift"
FINDING_ANTHROPIC_5XX_SUSTAINED = "anthropic_5xx_sustained"

# Default number of Anthropic 5xx responses before the error rate is considered
# sustained rather than a transient blip.
_DEFAULT_ANTHROPIC_5XX_THRESHOLD = 3


def _raw(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _gateway_error_counters(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "gateway_error_counters", None)
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


def detect_litellm_unreachable(snapshot: Any) -> Finding | None:
    """Fire when the LiteLLM proxy is unreachable (#2769).

    Fire when ``raw.litellm_reachable`` is *explicitly* ``False`` (a missing
    key is not a signal — the substrate may simply not be probed), or when the
    recorded ``litellm_unreachable_count`` is at least 1.

    Deterministic → ``requires_adjudication=False``.
    """
    raw = _raw(snapshot)
    counters = _gateway_error_counters(snapshot)

    reachable = raw.get("litellm_reachable")
    explicit_unreachable = reachable is False

    unreachable_count = _as_int(counters.get("litellm_unreachable_count"))
    count_hit = unreachable_count is not None and unreachable_count >= 1

    if not (explicit_unreachable or count_hit):
        return None

    return Finding(
        finding_class=FINDING_LITELLM_UNREACHABLE,
        severity=Severity.HIGH,
        evidence={
            "litellm_reachable": reachable,
            "litellm_unreachable_count": unreachable_count,
        },
        recommended_action=(
            "The LiteLLM proxy is unreachable (#2769). Agents cannot route LLM "
            "calls — check the egg-litellm service health and connectivity "
            "before respawning cohorts that will only fail the same way."
        ),
        requires_adjudication=False,
        detector_key="litellm_unreachable",
    )


detect_litellm_unreachable.detector_key = "litellm_unreachable"  # type: ignore[attr-defined]
detect_litellm_unreachable.name = "litellm_unreachable_detector"  # type: ignore[attr-defined]


def detect_effective_model_drift(snapshot: Any) -> Finding | None:
    """Fire when the requested model differs from the served model.

    Fire when both ``raw.requested_model`` and ``raw.effective_model`` are
    present (non-empty) and differ — the substrate silently served a different
    model than asked for. If either is missing the comparison is not provable
    and the detector stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    raw = _raw(snapshot)

    requested = raw.get("requested_model")
    effective = raw.get("effective_model")

    if not requested or not effective:
        return None
    if requested == effective:
        return None

    return Finding(
        finding_class=FINDING_EFFECTIVE_MODEL_DRIFT,
        severity=Severity.MEDIUM,
        evidence={
            "requested_model": requested,
            "effective_model": effective,
        },
        recommended_action=(
            "The substrate served a different model than requested (#2769 "
            "effective-model drift). Confirm the LiteLLM route mapping and "
            "fallbacks; a silent downgrade changes cost and capability."
        ),
        requires_adjudication=False,
        detector_key="effective_model_drift",
    )


detect_effective_model_drift.detector_key = "effective_model_drift"  # type: ignore[attr-defined]
detect_effective_model_drift.name = "effective_model_drift_detector"  # type: ignore[attr-defined]


def detect_anthropic_5xx_sustained(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_ANTHROPIC_5XX_THRESHOLD,
) -> Finding | None:
    """Fire on sustained Anthropic 5xx responses.

    Fire when the recorded ``anthropic_5xx`` error count reaches ``threshold``.
    A small number of transient 5xx responses (below ``threshold``) stays
    silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _gateway_error_counters(snapshot)

    anthropic_5xx = _as_int(counters.get("anthropic_5xx"))
    if anthropic_5xx is None or anthropic_5xx < threshold:
        return None

    return Finding(
        finding_class=FINDING_ANTHROPIC_5XX_SUSTAINED,
        severity=Severity.HIGH,
        evidence={
            "anthropic_5xx": anthropic_5xx,
            "threshold": threshold,
        },
        recommended_action=(
            "Anthropic is returning sustained 5xx responses (#2769). This is an "
            "upstream substrate outage, not an agent defect — back off and "
            "retry with jitter rather than churning cohorts against the error."
        ),
        requires_adjudication=False,
        detector_key="anthropic_5xx_sustained",
    )


detect_anthropic_5xx_sustained.detector_key = "anthropic_5xx_sustained"  # type: ignore[attr-defined]
detect_anthropic_5xx_sustained.name = "anthropic_5xx_sustained_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_anthropic_5xx_sustained",
    "detect_effective_model_drift",
    "detect_litellm_unreachable",
]
