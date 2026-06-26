"""Gateway-health detectors (#2270 §5, slice-8).

Deterministic detection-plane detectors over an :class:`EventStreamSnapshot`.
Each is a pure function ``snapshot -> Finding | None``: it never raises, never
calls an LLM, and fires only on a condition it can *prove* from the snapshot
(the §2 "stop crying wolf" discipline). Routine findings carry
``requires_adjudication=False`` so the bounded corrective vocabulary (slice-6)
handles them without an LLM.

These detectors are registered into the slice-1 calibration corpus by
``detector_key`` (so each gets a strict corpus row) and into the production
:class:`DetectionPlane` (see ``routes/pipelines.register_coverage_gap_detectors``).

Detectors here key on the ``gateway_error_counters`` field of the snapshot:

* :func:`detect_gateway_error_spike` — a spike of gateway 5xx responses.
* :func:`detect_repeated_identical_denials` — the same 403 denial repeated, a
  signature of an agent retrying a structurally-blocked operation.
* :func:`detect_gateway_token_expiry` — credential/token expiry surfaced by the
  gateway.
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
FINDING_GATEWAY_ERROR_SPIKE = "gateway_error_spike"
FINDING_REPEATED_IDENTICAL_DENIALS = "repeated_identical_denials"
FINDING_GATEWAY_TOKEN_EXPIRY = "gateway_token_expiry"

# Default count of 5xx responses that constitutes a spike worth surfacing.
_DEFAULT_5XX_THRESHOLD = 5
# Default count of identical 403 denials that constitutes a stuck retry loop.
_DEFAULT_DENIAL_THRESHOLD = 3


def _error_counters(snapshot: Any) -> dict[str, Any]:
    counters = getattr(snapshot, "gateway_error_counters", None)
    return counters if isinstance(counters, dict) else {}


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_gateway_error_spike(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_5XX_THRESHOLD,
) -> Finding | None:
    """Fire on a spike of gateway 5xx responses.

    Fires when ``gateway_error_counters["5xx"]`` is at/above ``threshold``. A
    count below the threshold (or absent) stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _error_counters(snapshot)

    count_5xx = _as_float(counters.get("5xx"))
    if count_5xx is None or count_5xx < threshold:
        return None

    return Finding(
        finding_class=FINDING_GATEWAY_ERROR_SPIKE,
        severity=Severity.HIGH,
        evidence={
            "5xx": count_5xx,
            "threshold": threshold,
        },
        recommended_action=(
            "The gateway is returning a spike of 5xx errors. Inspect gateway "
            "health/logs before retrying; sustained 5xx will fail agent git/gh "
            "operations across the cohort."
        ),
        requires_adjudication=False,
        detector_key="gateway_error_spike",
    )


detect_gateway_error_spike.detector_key = "gateway_error_spike"  # type: ignore[attr-defined]
detect_gateway_error_spike.name = "gateway_error_spike_detector"  # type: ignore[attr-defined]


def detect_repeated_identical_denials(
    snapshot: Any,
    *,
    threshold: int = _DEFAULT_DENIAL_THRESHOLD,
) -> Finding | None:
    """Fire when the same 403 denial repeats.

    Fires when ``gateway_error_counters["repeated_denial_max"]`` is at/above
    ``threshold`` (the gateway already collapsed identical denials into a max
    repeat count), or when the raw ``403`` count is at/above ``threshold`` AND the
    gateway flagged the denials as identical (``denials_identical`` truthy). A
    handful of distinct denials stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _error_counters(snapshot)

    repeated_max = _as_float(counters.get("repeated_denial_max"))
    count_403 = _as_float(counters.get("403"))

    by_repeat_max = repeated_max is not None and repeated_max >= threshold
    by_403_identical = (
        count_403 is not None and count_403 >= threshold and bool(counters.get("denials_identical"))
    )

    if not (by_repeat_max or by_403_identical):
        return None

    return Finding(
        finding_class=FINDING_REPEATED_IDENTICAL_DENIALS,
        severity=Severity.MEDIUM,
        evidence={
            "repeated_denial_max": repeated_max,
            "403": count_403,
            "denials_identical": bool(counters.get("denials_identical")),
            "threshold": threshold,
        },
        recommended_action=(
            "An agent is repeatedly hitting the same 403 gateway denial — a "
            "structurally-blocked operation being retried, not a transient error. "
            "Hand the write off to the owning role or defer it; retrying will not "
            "clear the denial."
        ),
        requires_adjudication=False,
        detector_key="repeated_identical_denials",
    )


detect_repeated_identical_denials.detector_key = "repeated_identical_denials"  # type: ignore[attr-defined]
detect_repeated_identical_denials.name = "repeated_identical_denials_detector"  # type: ignore[attr-defined]


def detect_gateway_token_expiry(snapshot: Any) -> Finding | None:
    """Fire on credential/token expiry surfaced by the gateway.

    Fires when ``gateway_error_counters["token_expired"]`` is truthy or when
    ``gateway_error_counters["token_expiry_count"]`` is at/above 1. Otherwise
    stays silent.

    Deterministic → ``requires_adjudication=False``.
    """
    counters = _error_counters(snapshot)

    expiry_count = _as_float(counters.get("token_expiry_count"))
    has_expiry = expiry_count is not None and expiry_count >= 1

    if not (counters.get("token_expired") or has_expiry):
        return None

    return Finding(
        finding_class=FINDING_GATEWAY_TOKEN_EXPIRY,
        severity=Severity.HIGH,
        evidence={
            "token_expired": bool(counters.get("token_expired")),
            "token_expiry_count": expiry_count,
        },
        recommended_action=(
            "The gateway is reporting an expired credential/token. Refresh the "
            "gateway-injected credential before retrying; an expired token will "
            "fail every authenticated git/gh operation until rotated."
        ),
        requires_adjudication=False,
        detector_key="gateway_token_expiry",
    )


detect_gateway_token_expiry.detector_key = "gateway_token_expiry"  # type: ignore[attr-defined]
detect_gateway_token_expiry.name = "gateway_token_expiry_detector"  # type: ignore[attr-defined]


__all__ = [
    "detect_gateway_error_spike",
    "detect_gateway_token_expiry",
    "detect_repeated_identical_denials",
]
