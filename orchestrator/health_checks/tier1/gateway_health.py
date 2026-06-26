"""Gateway-health detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``DetectionPlane.default``).

Detectors here key on the ``gateway_error_counters`` field of the snapshot:

* :func:`detect_gateway_error_spike` — the gateway 5xx error *rate* is over its
  threshold.
* :func:`detect_gateway_repeated_denial` — the same 403 denial repeated (an
  agent stuck retrying a forbidden operation).
* :func:`detect_gateway_token_expiry` — an injected credential/token expired.
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
FINDING_GATEWAY_ERROR_SPIKE = "gateway_error_spike"
FINDING_GATEWAY_REPEATED_DENIAL = "gateway_repeated_denial"
FINDING_GATEWAY_TOKEN_EXPIRY = "gateway_token_expiry"

# Default streak of identical 403 denials before it is treated as a stuck retry.
_DEFAULT_DENIAL_STREAK = 3


def _counters(snapshot: Any) -> dict[str, Any]:
    raw = getattr(snapshot, "gateway_error_counters", None)
    return raw if isinstance(raw, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_gateway_error_spike(snapshot: Any) -> Finding | None:
    """Fire when the gateway 5xx error rate exceeds its threshold.

    Keys on the *rate* (``5xx_rate_per_min``) against the snapshot's
    ``rate_threshold_per_min`` rather than a raw cumulative count, so a long-lived
    pipeline with a few historical 5xx does not trip it. Rate at/under threshold
    (or absent) stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _counters(snapshot)
    rate = _as_float(counters.get("5xx_rate_per_min"))
    threshold = _as_float(counters.get("rate_threshold_per_min"))
    if rate is None or threshold is None or rate <= threshold:
        return None

    return Finding(
        finding_class=FINDING_GATEWAY_ERROR_SPIKE,
        severity=Severity.MEDIUM,
        evidence={
            "5xx_rate_per_min": rate,
            "rate_threshold_per_min": threshold,
        },
        recommended_action=(
            "The gateway 5xx error rate is over its threshold. Inspect gateway "
            "logs and upstream (LiteLLM/Anthropic) health before retrying."
        ),
        requires_adjudication=False,
        detector_key="gateway_error_spike",
    )


detect_gateway_error_spike.detector_key = "gateway_error_spike"  # type: ignore[attr-defined]
detect_gateway_error_spike.name = "gateway_error_spike_detector"  # type: ignore[attr-defined]


def detect_gateway_repeated_denial(
    snapshot: Any,
    *,
    streak_threshold: int = _DEFAULT_DENIAL_STREAK,
) -> Finding | None:
    """Fire when the same 403 denial repeats past the streak threshold.

    A climbing ``identical_403_streak`` means an agent is stuck retrying the same
    forbidden operation — re-scope its task or fix the offending call. A streak
    under the threshold (one-off denial) stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _counters(snapshot)
    streak = _as_float(counters.get("identical_403_streak"))
    if streak is None or streak < streak_threshold:
        return None

    return Finding(
        finding_class=FINDING_GATEWAY_REPEATED_DENIAL,
        severity=Severity.MEDIUM,
        evidence={
            "identical_403_streak": streak,
            "denial_signature": counters.get("denial_signature"),
            "streak_threshold": streak_threshold,
        },
        recommended_action=(
            "The gateway is repeatedly denying the same operation (identical 403 "
            "streak). The agent is retrying a forbidden action — re-scope its "
            "task or fix the offending call."
        ),
        requires_adjudication=False,
        detector_key="gateway_repeated_denial",
    )


detect_gateway_repeated_denial.detector_key = "gateway_repeated_denial"  # type: ignore[attr-defined]
detect_gateway_repeated_denial.name = "gateway_repeated_denial_detector"  # type: ignore[attr-defined]


def detect_gateway_token_expiry(snapshot: Any) -> Finding | None:
    """Fire when an injected gateway credential/token has expired.

    Fires on an explicit ``token_expired`` flag, or on any ``401`` count — both
    indicate the injected credential is no longer valid. Absent/zero stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _counters(snapshot)
    token_expired = bool(counters.get("token_expired"))
    unauthorized = _as_float(counters.get("401")) or 0.0

    if not (token_expired or unauthorized >= 1):
        return None

    return Finding(
        finding_class=FINDING_GATEWAY_TOKEN_EXPIRY,
        severity=Severity.HIGH,
        evidence={
            "token_expired": token_expired,
            "401": unauthorized,
        },
        recommended_action=(
            "A gateway credential/token has expired (token_expired / 401s). "
            "Refresh the injected credential and restart the affected agent."
        ),
        requires_adjudication=False,
        detector_key="gateway_token_expiry",
    )


detect_gateway_token_expiry.detector_key = "gateway_token_expiry"  # type: ignore[attr-defined]
detect_gateway_token_expiry.name = "gateway_token_expiry_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_gateway_error_spike",
    "detect_gateway_repeated_denial",
    "detect_gateway_token_expiry",
]
