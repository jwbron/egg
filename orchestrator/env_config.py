"""Single home for orchestrator env-var reads (issue #1897).

Centralises the parsing of environment knobs so that the orchestrator
has ONE place to look when reasoning about what an env var does. This
avoids the drift that comes from multiple modules re-parsing the same
env var with slightly different fallbacks.

All helpers return typed values and never raise on parse failure —
they fall back to the documented default and log a warning when a
raw value looks intentional but malformed.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings

logger = logging.getLogger("orchestrator.env_config")

# -----------------------------------------------------------------
# EGG_MESSAGE_POLL_MAX_WAIT — long-poll cap for GET /messages and
# GET /messages/wait.  Raising this above the gateway Squid timeout
# will make long polls return 504 (see the startup warning in
# ``log_message_poll_max_wait_startup``).
# -----------------------------------------------------------------

DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS = 60
MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS = 90


def get_message_poll_max_wait() -> int:
    """Return the effective ``?wait=`` cap in seconds.

    Reads ``EGG_MESSAGE_POLL_MAX_WAIT`` (default 60s).  Malformed
    values fall back to the default; ``<= 0`` also falls back.

    Coupled with the gateway image's Squid ``read_timeout`` and
    ``request_timeout`` directives (baked into ``gateway/squid.conf``;
    raising the orchestrator cap REQUIRES a gateway image rebuild
    with the matching directive values, NOT a ConfigMap edit).
    See docs/reference/agent-wait-patterns.md §6.
    """
    raw = os.environ.get("EGG_MESSAGE_POLL_MAX_WAIT", "").strip()
    if not raw:
        return DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS
    try:
        val = int(raw)
    except TypeError, ValueError:
        logger.warning(
            "EGG_MESSAGE_POLL_MAX_WAIT=%r is not an integer; falling back to %ds",
            raw,
            DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS,
        )
        return DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS
    if val <= 0:
        return DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS
    return val


def log_message_poll_max_wait_startup() -> None:
    """Emit the startup log line naming the effective cap.

    If the cap exceeds the safe threshold
    (``MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS``) we escalate to
    WARNING + ``warnings.warn`` naming the gateway Squid coupling so
    the operator knows a gateway image rebuild is required.
    """
    cap = get_message_poll_max_wait()
    if cap > MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS:
        msg = (
            f"EGG_MESSAGE_POLL_MAX_WAIT={cap}s exceeds the safe "
            f"threshold ({MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS}s); "
            "ensure the gateway image's Squid ``read_timeout`` and "
            "``request_timeout`` directives (baked into "
            "``gateway/squid.conf`` — requires an image rebuild, NOT "
            "a ConfigMap edit) are raised in lockstep or long polls "
            "will return 504.  See "
            "docs/reference/agent-wait-patterns.md."
        )
        logger.warning(msg)
        warnings.warn(msg, stacklevel=2)
    else:
        logger.info(
            "EGG_MESSAGE_POLL_MAX_WAIT effective cap: %ds",
            cap,
        )


# -----------------------------------------------------------------
# EGG_ORCH_WAITRESS_THREADS — worker thread count for the waitress
# production server.  Raised from 16 → 24 in issue #1932 so the pool
# can absorb host-side ``/status/wait`` load (now driven by the
# ``egg-orch pipeline wait-status`` Bash CLI per #2211) on top of the
# existing sandbox-side ``egg-orch message wait-loop`` long-poll
# volume.  Each host-side wait costs two threads for up to the wait
# duration (one Waitress worker blocked on ``queue.get``, one daemon
# thread blocked inside ``message_store.get_messages``) — see
# docs/reference/agent-wait-patterns.md §7 "Host-Side Waits" for the
# budget rationale.
#
# Refuse-to-boot semantics: if the operator sets a value below 4 the
# server MUST ``sys.exit(78)`` (``EX_CONFIG``) so k8s restarts and
# the operator sees a loud error rather than a silently-saturated
# pool.  See plan TASK-4-1 and docs/reference/agent-wait-patterns.md
# §7.
# -----------------------------------------------------------------

DEFAULT_WAITRESS_THREADS = 24
WAITRESS_THREADS_MIN = 4
WAITRESS_REFUSE_EXIT_CODE = 78  # EX_CONFIG per sysexits.h


def get_waitress_threads() -> int:
    """Return the effective waitress ``threads=`` value.

    ``sys.exit(EX_CONFIG)`` if the operator set a value below
    ``WAITRESS_THREADS_MIN``.  Malformed values fall back to the
    default.
    """
    raw = os.environ.get("EGG_ORCH_WAITRESS_THREADS", "").strip()
    if not raw:
        return DEFAULT_WAITRESS_THREADS
    try:
        val = int(raw)
    except TypeError, ValueError:
        logger.warning(
            "EGG_ORCH_WAITRESS_THREADS=%r is not an integer; falling back to %d",
            raw,
            DEFAULT_WAITRESS_THREADS,
        )
        return DEFAULT_WAITRESS_THREADS
    if val < WAITRESS_THREADS_MIN:
        logger.error(
            "EGG_ORCH_WAITRESS_THREADS=%d is below the minimum "
            "safe value of %d; refusing to boot so the operator "
            "notices (see #1897).",
            val,
            WAITRESS_THREADS_MIN,
        )
        sys.exit(WAITRESS_REFUSE_EXIT_CODE)
    return val


# -----------------------------------------------------------------
# EGG_HEARTBEAT_RATE_LIMIT — per (pipeline_id, role) HEARTBEAT rate
# cap.  Exceeding the cap produces HTTP 429 with a ``retry_after``
# body field.  See plan TASK-3-4 and
# docs/reference/agent-wait-patterns.md §5.
# -----------------------------------------------------------------

DEFAULT_HEARTBEAT_RATE_LIMIT_PER_MIN = 20


def get_heartbeat_rate_limit() -> int:
    """Return heartbeats-per-minute-per-role cap (default 20)."""
    raw = os.environ.get("EGG_HEARTBEAT_RATE_LIMIT", "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_RATE_LIMIT_PER_MIN
    try:
        val = int(raw)
    except TypeError, ValueError:
        logger.warning(
            "EGG_HEARTBEAT_RATE_LIMIT=%r not an integer; falling back to %d/min",
            raw,
            DEFAULT_HEARTBEAT_RATE_LIMIT_PER_MIN,
        )
        return DEFAULT_HEARTBEAT_RATE_LIMIT_PER_MIN
    if val <= 0:
        return DEFAULT_HEARTBEAT_RATE_LIMIT_PER_MIN
    return val


# -----------------------------------------------------------------
# #2137 — slice-scheduler configuration knobs.
#
# EGG_ORCH_MAX_PARALLEL_SLICES — soft concurrency cap on slice spawns
#   per wave. Default 5 (refine-phase decision-5 + Q1: typical 3–7
#   slices, worst-case 10–15; trust container limits and gateway
#   throttling but give the operator a knob to dial back when
#   confidence is low).
#
# EGG_ORCH_SLICE_LOCAL_MAX_CYCLES — per-slice BRC re-proposal ceiling
#   before HITL escalation (refine-phase decision-9 opt-3 two-tier
#   model). Default 3.
#
# EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES — pipeline-wide cap on summed
#   slice cycles. Default 10. Either trip escalates HITL.
#
# EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS — grace window between a slice
#   failure and the orchestrator marking the downstream subtree
#   ``BLOCKED_ON_FAILED_DEPENDENCY``. Default 60 (refine-phase
#   decision-10 opt-3 hybrid). Allows HITL resolution before the
#   cascade fires.
#
# EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS — period of the
#   stacked-PR reconciler that catches child PRs whose base branch
#   was deleted out from under them. Default 30 (refine-phase
#   decision-16 opt-3 hybrid).
# -----------------------------------------------------------------

DEFAULT_MAX_PARALLEL_SLICES = 5
DEFAULT_SLICE_LOCAL_MAX_CYCLES = 3
DEFAULT_SLICE_GLOBAL_MAX_CYCLES = 10
DEFAULT_SLICE_FAILURE_GRACE_SECONDS = 60.0
DEFAULT_STACKED_PR_RECONCILER_INTERVAL_SECONDS = 30.0


def _coerce_positive_int(env_name: str, default: int) -> int:
    """Read a positive-int env var with a default; warn on bad input."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except TypeError, ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %d",
            env_name,
            raw,
            default,
        )
        return default
    if val <= 0:
        logger.warning(
            "%s=%d must be > 0; falling back to %d",
            env_name,
            val,
            default,
        )
        return default
    return val


def _coerce_positive_float(env_name: str, default: float) -> float:
    """Read a positive-float env var with a default; warn on bad input."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except TypeError, ValueError:
        logger.warning(
            "%s=%r is not a number; falling back to %.1f",
            env_name,
            raw,
            default,
        )
        return default
    if val <= 0:
        logger.warning(
            "%s=%.1f must be > 0; falling back to %.1f",
            env_name,
            val,
            default,
        )
        return default
    return val


def get_max_parallel_slices() -> int:
    """Return the per-pipeline parallel-slice spawn cap (default 5)."""
    return _coerce_positive_int("EGG_ORCH_MAX_PARALLEL_SLICES", DEFAULT_MAX_PARALLEL_SLICES)


def get_slice_local_max_cycles() -> int:
    """Return the per-slice BRC cycle ceiling (default 3)."""
    return _coerce_positive_int("EGG_ORCH_SLICE_LOCAL_MAX_CYCLES", DEFAULT_SLICE_LOCAL_MAX_CYCLES)


def get_slice_global_max_cycles() -> int:
    """Return the pipeline-wide BRC cycle ceiling (default 10)."""
    return _coerce_positive_int("EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES", DEFAULT_SLICE_GLOBAL_MAX_CYCLES)


def get_slice_failure_grace_seconds() -> float:
    """Return the failure-cascade grace window in seconds (default 60)."""
    return _coerce_positive_float(
        "EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS",
        DEFAULT_SLICE_FAILURE_GRACE_SECONDS,
    )


def get_stacked_pr_reconciler_interval_seconds() -> float:
    """Return the stacked-PR reconciler cadence in seconds (default 30)."""
    return _coerce_positive_float(
        "EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS",
        DEFAULT_STACKED_PR_RECONCILER_INTERVAL_SECONDS,
    )


# -----------------------------------------------------------------
# EGG_ORCH_STATE_STORE_PROBE_INTERVAL — cadence (in seconds) of the
# background state-store self-heal probe (#2191). Lowering this
# tightens the wedge-detection window at the cost of more frequent
# ``git`` calls; raising it does the inverse. The staleness watchdog
# in :mod:`state_store_probe` flips ``/api/v1/ready`` to 503 when the
# cache age exceeds ``interval * 2``, so operators tuning this knob
# also widen/narrow the readiness flap window proportionally. Note the
# boot first-probe window also scales with this value: until the BG
# thread completes one iteration, ``/api/v1/ready`` returns 503, so
# raising the interval above ~30s can exceed the readinessProbe's
# ``initialDelaySeconds (5) + periodSeconds (10) * failureThreshold (3)
# = 35s`` boot tolerance.
# -----------------------------------------------------------------

DEFAULT_STATE_STORE_PROBE_INTERVAL_SECONDS = 15.0


def get_state_store_probe_interval() -> float:
    """Return the BG state-store probe cadence in seconds (default 15)."""
    raw = os.environ.get("EGG_ORCH_STATE_STORE_PROBE_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_STATE_STORE_PROBE_INTERVAL_SECONDS
    try:
        val = float(raw)
    except TypeError, ValueError:
        logger.warning(
            "EGG_ORCH_STATE_STORE_PROBE_INTERVAL=%r is not a number; falling back to %.1fs",
            raw,
            DEFAULT_STATE_STORE_PROBE_INTERVAL_SECONDS,
        )
        return DEFAULT_STATE_STORE_PROBE_INTERVAL_SECONDS
    if val <= 0:
        return DEFAULT_STATE_STORE_PROBE_INTERVAL_SECONDS
    return val
