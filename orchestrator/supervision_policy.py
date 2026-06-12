"""Shared agent-invocation failure-streak supervision policy (#3138 / #3064 slice-3).

Single source of truth for the consecutive *agent-invocation* failure-streak
policy — the backoff/escalation knobs the BRC event machinery applies when an
agent invocation keeps failing. Two code paths consume these values and they
MUST NOT fork:

* ``orchestrator/event_loop.py`` — the orchestrator-side one-shot Job
  supervisor (#3064 slice-3). When a one-shot pod dies *mid-event* (abnormal
  Job termination) the loop respawns the same event key with linear backoff,
  warns at :data:`WARN_STREAK_THRESHOLD`, raises a sticky
  :data:`FAIL_STREAK_ANOMALY` overseer alert at :data:`ALERT_STREAK_THRESHOLD`,
  and — for a producer ``propose`` arm — engages the existing ``AGENT_FAILED``
  path on exhaustion.
* ``orchestrator/consensus_wrapper.py`` — the in-pod event-pump wrapper
  template (#3138). Its ``propose|ack|nack`` arm applies the identical
  backoff + escalation in bash. The wrapper interpolates these constants into
  the template at *composition* time, so the rendered bash carries the same
  literals it always has.

Because the wrapper renders the SAME literal values it carried before this
extraction (``2`` / ``30`` / ``5`` / ``10`` / ``agent-invocation-fail-streak``),
the wrapper's runtime behavior is unchanged — the pod-default golden snapshot
stays byte-identical. The only thing that changes is that both code paths now
read one definition: edit a constant here and the Python supervisor *and* the
rendered wrapper bash move together.
"""

from __future__ import annotations

# Linear backoff: the retry sleep grows by this many seconds per consecutive
# failure (``streak * BACKOFF_FACTOR_SECONDS``). Parity with the wrapper's
# ``confirm`` arm — a one-off transient still retries promptly while a
# deterministic fast-fail loop is throttled.
BACKOFF_FACTOR_SECONDS = 2

# Ceiling on the linear backoff so a persistent fast failure can't hammer the
# orchestrator faster than the idle counter ages, yet the operator still sees
# the idle alert within ~30 min on the default budget.
BACKOFF_CAP_SECONDS = 30

# Sticky warn-level log once the consecutive-failure streak reaches this
# length (a diagnosis line: "likely a permanent failure", not just a count).
WARN_STREAK_THRESHOLD = 5

# Sticky OVERSEER_ALERT once the streak reaches this length — a streak of
# fast-failing invocations is a strong permanent/configuration-class signal
# the operator should hear about directly.
ALERT_STREAK_THRESHOLD = 10

# Anomaly name carried by the streak alert. The wrapper and the orchestrator
# supervisor MUST agree on this string so the operator-facing alert surface
# treats both emitters identically.
FAIL_STREAK_ANOMALY = "agent-invocation-fail-streak"

# A failure that completes in at most this many seconds is classified as a
# *configuration-class* fault (the invocation died before/at SDK init —
# unknown model alias, auth misconfiguration, prompt-rendering crash) rather
# than a transient (API/quota/transport). Whole-second granularity plus
# prompt-composer overhead means a genuine pre-SDK-init crash routinely
# measures 1-2s, so this is ``<= 2``, not ``< 1``.
FAST_FAIL_SECONDS = 2


def backoff_seconds(streak: int) -> int:
    """Return the linear-backoff sleep (seconds) for a consecutive-failure streak.

    ``streak * BACKOFF_FACTOR_SECONDS`` clamped to :data:`BACKOFF_CAP_SECONDS`.
    A non-positive streak yields ``0`` (the first attempt is never delayed).
    This is the Python mirror of the wrapper's
    ``agent_backoff_secs=$(( AGENT_FAIL_STREAK * 2 ))`` + ``-gt 30`` clamp, so
    both paths grow and cap identically.
    """
    if streak <= 0:
        return 0
    return min(streak * BACKOFF_FACTOR_SECONDS, BACKOFF_CAP_SECONDS)
