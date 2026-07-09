"""Supervision policy constants shared by the orchestrator event loop and
the consensus wrapper (#3138, #3064 slice-3).

The extract here prevents a fork between the loop (orchestrator side)
and the pod wrapper (bash template), so the values are always in sync
and a single-line edit here changes both::
    orchestrator.event_loop    — reads constants for supervision,
                                 backoff, streak tracking
    orchestrator.consensus_wrapper — embeds the same constants in the bash
                                 event-pump template
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Agent-invocation failure supervision (#3138)
# ---------------------------------------------------------------------------
# Linear backoff: ``streak * _BACKOFF_FACTOR`` seconds,
# capped at ``_BACKOFF_CAP_SECONDS``.
SUPERVISION_BACKOFF_FACTOR = 2
SUPERVISION_BACKOFF_CAP_SECONDS = 30

# Warn-level log / sticky message when the per-dupe-key streak
# reaches this threshold.
SUPERVISION_FAILURE_STREAK_WARN = 5

# Sticky alert (anomaly ``agent-invocation-fail-streak``) when the
# per-dupe-key streak reaches this threshold.
SUPERVISION_FAILURE_STREAK_ALERT = 10

# ---------------------------------------------------------------------------
# Successful-no-op park (#3425)
# ---------------------------------------------------------------------------
# A one-shot invocation that exits cleanly WITHOUT changing BRC state is a
# "successful no-op": the loop re-derives the identical dedupe key next poll
# and would re-spawn indefinitely (observed as ~50 pod spawns against a slice
# wedged on an unresolved operator HITL decision). After this many consecutive
# clean completions of the SAME dedupe key the arm is parked. A productive
# success changes BRC state — its key is never re-derived — so it can never
# accumulate a streak past 1.
SUPERVISION_NOOP_STREAK_PARK = 3

# While parked, a probe spawn is still allowed this often. This is the
# liveness backstop for a wedge whose unblock the orchestrator cannot observe
# through the contract-decision fingerprint; it bounds the burn to ~2 pods/h
# instead of deadlocking the arm.
SUPERVISION_NOOP_PARK_RETRY_SECONDS = 1800

# ---------------------------------------------------------------------------
# Transient rate-limit / cap-wall paced retry (#3364 PR C)
# ---------------------------------------------------------------------------
# A transient throttle (bare HTTP 429 / "rate limit" / "overloaded" — the
# signatures ``egg_agent.auth_errors`` deliberately keeps OFF the
# ``EX_AUTH_FATAL`` path) is neither a credential failure nor an ordinary
# crash: it self-heals once the rolling cap window lifts. It must therefore be
# PACED across that window (a persistent weekly/subscription cap can stay shut
# for hours-to-days) rather than hammered on the 30s abnormal backoff, and it
# must NEVER feed the ``agent-invocation-fail-streak`` halt.
#
# These constants are deliberately SEPARATE from ``SUPERVISION_BACKOFF_*`` (the
# abnormal path) so the two policies can never bleed into each other — the
# 30s abnormal cap and streak-to-10 halt above are left byte-for-byte
# unchanged (AC-C6).

# Bounded paced-retry backoff used when the throttle error carries NO parseable
# reset time: linear ``retry_count * FACTOR`` growth capped WELL above the 30s
# abnormal cap so a persistent cap wall is retried on a minutes-scale cadence
# (not every ~5s poll) without hammering the API. Distinct from
# ``SUPERVISION_BACKOFF_CAP_SECONDS`` (30).
SUPERVISION_RATE_LIMIT_BACKOFF_FACTOR = 30
SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS = 900  # 15 min bounded backoff

# Upper bound applied to a reset-time-DERIVED wait so a malformed / absurd reset
# hint ("resets in 9999h") cannot park an arm effectively forever; the paced
# retry keeps probing on at most this cadence. Hours-scale.
SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS = 3600  # 1 h

# cq-1 (resolved, binding): NO hard wall-clock ceiling on the paced retry — a
# cap wall (incl. a multi-day weekly cap) self-heals with no operator action —
# BUT emit an ``OVERSEER_ALERT`` once the CUMULATIVE paced wait for a key
# crosses this threshold, so an attended operator is informed while
# auto-recovery continues. Orthogonal to the deterministic-loop-guard
# escalation below.
SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS = 1800  # 30 min cumulative wait

# Deterministic-loop guard: how many consecutive reproductions of the IDENTICAL
# failure fingerprint are tolerated before the guard escalates — but ONLY once
# the failure has been positively classified as a NON-throttle (deterministic)
# error. This threshold NEVER halts a genuine transient throttle: a steady cap
# wall carries a throttle-classified (or absent) signature, so it fails the
# non-throttle gate no matter how many times its fingerprint reproduces, and
# paces indefinitely per binding cq-1 (no hard ceiling). The threshold applies
# only to a failure whose signature has CHANGED to a non-throttle error — a
# deterministic failure that would otherwise loop forever. (v1 open-NACK fix:
# the guard must not infer "deterministic" from a frozen BRC progression, which
# a genuine account-wide cap wall freezes identically.)
SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS = 5


# Reset-time hints an agent's throttle error text may carry. Best-effort — most
# production throttles reaching the orchestrator arrive as a bare exit code with
# no text, so the paced retry falls back to the bounded backoff; these patterns
# pace precisely WHEN a reset hint is present (e.g. surfaced via ``exit_detail``
# or in a unit-tested error string).
_RATE_LIMIT_RESET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Retry-After: 120" / "retry after 120s" / "retry_after=120"
    re.compile(r"retry[\s_-]*after[=:\s]+(\d+)\s*(?:s|sec|secs|second|seconds)?\b", re.IGNORECASE),
    # "try again in 90 seconds" / "retry in 90s"
    re.compile(
        r"(?:try again|retry)\s+in\s+(\d+)\s*(?:s|sec|secs|second|seconds)\b", re.IGNORECASE
    ),
    # "resets in 45 minutes" → captured as minutes (converted below)
    re.compile(r"reset[s]?\s+in\s+(\d+)\s*(?:m|min|mins|minute|minutes)\b", re.IGNORECASE),
)
# Which of the patterns above capture MINUTES (vs seconds) so the group can be
# scaled to seconds. Index-aligned with ``_RATE_LIMIT_RESET_PATTERNS``.
_RATE_LIMIT_RESET_UNIT_SECONDS: tuple[int, ...] = (1, 1, 60)


def parse_rate_limit_reset_seconds(text: str | None) -> float | None:
    """Best-effort seconds-to-wait until the cap window lifts, from *text*.

    Returns ``None`` when no reset hint is parseable (the common production
    case — a throttle usually reaches the orchestrator as a bare exit code
    with no text — so the caller falls back to the bounded backoff). A parsed
    value is clamped to ``[0, SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS]`` so a
    malformed / absurd hint can never park an arm effectively forever.
    """
    if not text:
        return None
    for pattern, unit in zip(
        _RATE_LIMIT_RESET_PATTERNS, _RATE_LIMIT_RESET_UNIT_SECONDS, strict=True
    ):
        match = pattern.search(text)
        if match:
            # ``match.group(1)`` is a ``\d+`` capture, so ``float`` on it never
            # raises — no exception guard needed (and none that would drag in
            # parenthesis-free ``except A, B`` syntax).
            seconds = float(match.group(1)) * unit
            return min(seconds, float(SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS))
    return None


def rate_limit_backoff_seconds(retry_count: int, reset_seconds: float | None = None) -> float:
    """Paced backoff for a transient rate limit (#3364 PR C).

    Prefer the error's own reset-time hint when present (pace precisely to the
    cap window); otherwise a BOUNDED linear backoff — ``retry_count * FACTOR``
    capped at ``SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS`` — so a persistent
    cap wall is retried on a minutes-scale cadence without hammering the API.
    Deliberately hours-scale-capable and entirely distinct from the 30s
    abnormal ``backoff_seconds`` (AC-C2 / AC-C6).
    """
    if reset_seconds is not None and reset_seconds > 0:
        return min(float(reset_seconds), float(SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS))
    count = max(retry_count, 1)
    return float(
        min(
            count * SUPERVISION_RATE_LIMIT_BACKOFF_FACTOR,
            SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS,
        )
    )


@dataclass(frozen=True)
class RateLimitFingerprint:
    """Identity of a rate-limit failure for the deterministic-loop guard (#3364).

    ``signature`` — the failure signature (the error text / exit detail): WHAT
    failed. ``progression`` — an opaque marker of how far the pipeline got when
    it failed (in production the consensus-state fingerprint): WHERE it failed.

    Frozen, so ``__eq__`` / ``__hash__`` are structural: two fingerprints are
    equal iff BOTH fields match. The guard uses equality only to count IDENTICAL
    reproductions and to reset that count when the progression advances (AC-C4
    "continue when state advances"). Equality (frozen progression) is NOT by
    itself evidence of a deterministic loop — a genuine account-wide cap wall
    freezes the progression identically to a deterministic failure, so the guard
    escalates only when the ``signature`` is additionally classified as a
    NON-throttle error (see ``JobSupervisor.record_rate_limited``). A steady
    transient throttle therefore reproduces an identical fingerprint forever
    without ever escalating, which is what binding cq-1 (no hard ceiling)
    requires.
    """

    signature: str
    progression: str
