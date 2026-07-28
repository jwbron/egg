"""JobSupervisor method bodies for the event_loop sub-package (#3447).

Method bodies extracted verbatim from the pre-split ``event_loop.py`` as
module-level functions taking ``self`` explicitly (decomposition-pattern.md
§c). The barrel binds these back onto the ``JobSupervisor`` class, so
``patch.object(JobSupervisor, ...)`` and ``self.<method>(...)`` dispatch work
unchanged. JobSupervisor touches only ``self`` + the SUPERVISION_* constants +
``logger``; it calls no monkeypatched module-global function, so the constants
are value-imported from the barrel with no ``_pkg`` indirection.
"""

from __future__ import annotations

from collections.abc import Iterable  # noqa: F401 — used in method annotations
from datetime import UTC, datetime
from typing import Any

from egg_agent.auth_errors import is_transient_rate_limit_error

from . import (
    SUPERVISION_BACKOFF_CAP_SECONDS,
    SUPERVISION_BACKOFF_FACTOR,
    SUPERVISION_EXIT_HISTORY_MAX,
    SUPERVISION_FAILURE_STREAK_ALERT,
    SUPERVISION_FAILURE_STREAK_WARN,
    SUPERVISION_NOOP_PARK_RETRY_SECONDS,
    SUPERVISION_NOOP_STREAK_PARK,
    SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS,
    SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS,
    SUPERVISION_SESSION_TIMEOUT_BUDGET,
    RateLimitFingerprint,
    logger,
    parse_rate_limit_reset_seconds,
    rate_limit_backoff_seconds,
)


def record_success(self, dedupe_key: str, *, action: str = "", role: str = "") -> None:
    """Reset the failure streak and latches for a given dedupe key.

    Called when a finished Job with ``dedupe_key`` returns success
    (rc=0, agent completed the event cleanly).

    #3425: a clean exit is not necessarily progress. A success that moved
    the BRC state is never re-derived under the same key, so its no-op
    counter dies at 1; a successful *no-op* (the agent ran, discovered it
    was blocked — typically on an unresolved operator HITL ``cq-N`` — and
    exited without a bus message) is re-spawned under an identical key and
    climbs the counter. At ``SUPERVISION_NOOP_STREAK_PARK`` the arm parks
    and a sticky alert fires once (see :meth:`noop_parked` for the
    release conditions). The #3138 failure-streak park cannot catch this
    case — these invocations *succeed*.
    """
    self._streaks.pop(dedupe_key, None)
    self._alerted_warn.pop(dedupe_key, None)
    self._alerted_10.pop(dedupe_key, None)
    self._exhausted.discard(dedupe_key)
    self._exit_history.pop(dedupe_key, None)
    # #3364 PR C: a clean completion means the throttle lifted — reset the
    # paced-retry state so a later cap wall on the same key starts fresh
    # (count, backoff window, cumulative-wait threshold latch, loop-guard
    # fingerprint). No effect on the abnormal streak semantics above.
    self._clear_rate_limit_state(dedupe_key)
    # #3658: a clean completion means this arm is finishing inside its budget —
    # drop the session-boundary counter so a later expiry starts from a full
    # budget instead of inheriting an old arm's spent one.
    self._session_timeout_count.pop(dedupe_key, None)
    streak = self._noop_streaks.get(dedupe_key, 0) + 1
    self._noop_streaks[dedupe_key] = streak
    logger.debug(
        "JobSupervisor: success for key=%s — failure streak reset (clean completions=%d)",
        dedupe_key,
        streak,
    )
    if streak >= SUPERVISION_NOOP_STREAK_PARK:
        # Record the arm labels for the parked key. ``_last_action`` is
        # otherwise written only by ``record_abort``, so a key that parked
        # without ever aborting would surface as an anonymous ``/`` row in
        # ``noop_park_report()`` (#3548).
        if action or role:
            self._last_action[dedupe_key] = (action, role)
    if streak >= SUPERVISION_NOOP_STREAK_PARK and not self._alerted_noop.get(dedupe_key, False):
        self._alerted_noop[dedupe_key] = True
        fingerprint = self._probe_hitl_fingerprint()
        self._noop_fingerprint[dedupe_key] = fingerprint
        self._noop_brc_fingerprint[dedupe_key] = self._probe_brc_fingerprint()
        self._noop_last_probe[dedupe_key] = self.clock()
        logger.warning(
            "JobSupervisor: %d consecutive successful no-op invocations for key=%s "
            "(action=%s, role=%s) — parking the arm; spawning at an unchanged BRC "
            "state cannot resolve an operator-bound wedge",
            streak,
            dedupe_key,
            action,
            role,
        )
        # #3520: with no gating contract decision visible, the role's own
        # WAITING_ON_ROLE self-report decides the alert's severity — a
        # dependent role no-oping while its live upstream producer works
        # toward its first proposal is choreography, not a wedge. A visible
        # cq-N keeps the original wedge hypothesis, so the probe is skipped.
        waiting = None if fingerprint else self._probe_waiting_on(role)
        self._emit_noop_alert(dedupe_key, streak, action, role, fingerprint, waiting)


def retire(self, dedupe_key: str) -> None:
    """Forget ALL supervision state for a key that will never be re-derived.

    The "this key is done" primitive for superseded same-role siblings
    (#3337): the event's tracker state is stale, so its streaks, latches,
    exhaustion, and no-op park state must not linger for the process
    lifetime. Distinct from :meth:`record_success`, which counts the
    completion toward the #3425 no-op streak.
    """
    self._streaks.pop(dedupe_key, None)
    self._last_abort_time.pop(dedupe_key, None)
    self._last_action.pop(dedupe_key, None)
    self._alerted_warn.pop(dedupe_key, None)
    self._alerted_10.pop(dedupe_key, None)
    self._exhausted.discard(dedupe_key)
    self._exit_history.pop(dedupe_key, None)
    self._noop_streaks.pop(dedupe_key, None)
    self._noop_fingerprint.pop(dedupe_key, None)
    self._noop_brc_fingerprint.pop(dedupe_key, None)
    self._noop_last_probe.pop(dedupe_key, None)
    self._noop_release_context.pop(dedupe_key, None)
    self._alerted_noop.pop(dedupe_key, None)
    self._clear_rate_limit_state(dedupe_key)  # #3364 PR C
    self._session_timeout_count.pop(dedupe_key, None)  # #3658
    logger.debug("JobSupervisor: retired key=%s — all supervision state dropped", dedupe_key)


def record_legitimate_outcome(self, dedupe_key: str, outcome: str) -> None:
    """Called when a Job finishes with a legitimate BRC outcome.

    ``outcome`` is one of: ``confirmed`` (confirm voted), ``nack``
    (proposed or review nack — legitimate). This does NOT change the
    current-dedupe-key budget; a subsequent abnormal termination of the
    same key continues incrementing the streak.

    The wrapper equivalent is the wrapper arm's own logic —
    ``egg-orch consensus confirmed`` → exit 0, no counter increment.
    """
    logger.debug(
        "JobSupervisor: legitimate outcome (%s) for key=%s — streak untouched",
        outcome,
        dedupe_key,
    )


def record_abort(
    self, dedupe_key: str, action: str, role: str, *, exit_detail: str | None = None
) -> None:
    """Called when a Job terminates abnormally (non-zero, non-BRC-legitimate).

    Increments the per-key streak and (the caller applies backoff if the
    key is not exhausted) for scheduling the respawn. Proposes sending
    ``sticky OVERSEER_ALERT`` when crossing thresholds.

    ``exit_detail`` (#3496) is an optional short operator-facing string
    describing the termination (e.g. the pod's exit code) recorded into
    the per-key history that :meth:`exhausted_report` surfaces.
    """
    streak = self._streaks.get(dedupe_key, 0) + 1
    self._streaks[dedupe_key] = streak
    self._last_abort_time[dedupe_key] = self.clock()
    self._last_action[dedupe_key] = (action, role)
    self._record_exit(dedupe_key, "abnormal", exit_detail)
    # Silent retries below the warn threshold (#3138): a one-off transient
    # is expected to recover on the next respawn, so it stays at debug
    # rather than spamming warn-level logs on every streak increment.
    logger.debug(
        "JobSupervisor: abnormal terminate for key=%s (action=%s, role=%s) — streak=%d",
        dedupe_key,
        action,
        role,
        streak,
    )
    # Threshold guards — each fires exactly once per key-lifetime.
    # Sticky warn at the WARN threshold: a streak this long is likely a
    # permanent failure (unknown model alias, auth misconfig, prompt
    # crash) rather than a transient — mirror the wrapper's warn line.
    if streak >= SUPERVISION_FAILURE_STREAK_WARN and not self._alerted_warn.get(dedupe_key, False):
        self._alerted_warn[dedupe_key] = True
        logger.warning(
            "JobSupervisor: agent-invocation failure streak crossed %d for key=%s "
            "(action=%s, role=%s) — likely a permanent failure, not a transient",
            SUPERVISION_FAILURE_STREAK_WARN,
            dedupe_key,
            action,
            role,
        )
    # Sticky alert + exhaustion at the ALERT threshold.
    if streak >= SUPERVISION_FAILURE_STREAK_ALERT and not self._alerted_10.get(dedupe_key, False):
        self._alerted_10[dedupe_key] = True
        self._exhausted.add(dedupe_key)
        # #3496: name the underlying termination categories at the
        # exhaustion transition — without this the cause (crash vs quota
        # vs image regression) is unrecoverable from the streak alert.
        logger.warning(
            "JobSupervisor: key=%s exhausted (action=%s, role=%s, streak=%d) — "
            "recent terminations: %s",
            dedupe_key,
            action,
            role,
            streak,
            self._format_exit_history(dedupe_key),
        )
        self._emit_alert(dedupe_key, streak, action, role)
        # #3064 slice-4: release the role's reused orchestrator-mode gateway
        # session — the exhausted arm spawns no further events, so the
        # long-lived session is torn down at this transition (any later
        # spawn for the role simply re-registers on a cache miss). Fires for
        # every action (a stuck reviewer arm is just as dead as a producer);
        # best-effort so a teardown failure never wedges supervision.
        if self._on_exhausted is not None:
            try:
                self._on_exhausted(role=role, action=action, dedupe_key=dedupe_key)
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.warning(
                    "JobSupervisor: on_exhausted teardown failed for key=%s (action=%s, role=%s)",
                    dedupe_key,
                    action,
                    role,
                )
        # #2806 relocated for orchestrator mode: a *producer* propose arm
        # that exhausts its budget engages the existing AGENT_FAILED path.
        # Reviewer arms (ack/nack) are not producer failures.
        if action == "propose" and self._agent_failed is not None:
            self._agent_failed(
                role=role,
                action=action,
                dedupe_key=dedupe_key,
                streak=streak,
            )


def record_fatal(
    self, dedupe_key: str, action: str, role: str, *, exit_detail: str | None = None
) -> None:
    """Called when a Job terminates with a non-retryable credential failure.

    The agent exited with ``egg_agent.auth_errors.EX_AUTH_FATAL`` (#3373):
    its Claude credential is unusable (subscription weekly/usage limit,
    expired/invalid token, 401, exhausted credit balance). Retrying only
    re-uses the same rejected credential, so — unlike :meth:`record_abort`,
    which increments a streak toward the ALERT threshold — this exhausts the
    key on the *first* occurrence and emits a named, actionable alert that
    identifies the cause and the remediation. Idempotent per key (a
    re-read before the Job is reaped does not re-emit).
    """
    if dedupe_key in self._exhausted:
        return
    self._exhausted.add(dedupe_key)
    self._last_action[dedupe_key] = (action, role)
    self._record_exit(dedupe_key, "fatal", exit_detail)
    # Latch the streak alerts so a later abort on the same key cannot
    # re-fire the generic streak alert after this fatal one.
    self._alerted_warn[dedupe_key] = True
    self._alerted_10[dedupe_key] = True
    logger.warning(
        "JobSupervisor: fatal credential failure for key=%s (action=%s, role=%s) "
        "— exhausting immediately, no retry (agent credential rejected)",
        dedupe_key,
        action,
        role,
    )
    self._emit_fatal_alert(dedupe_key, action, role)
    # Mirror the streak-exhaustion transition: release the role's reused
    # gateway session (the arm spawns no further events) — best-effort.
    if self._on_exhausted is not None:
        try:
            self._on_exhausted(role=role, action=action, dedupe_key=dedupe_key)
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.warning(
                "JobSupervisor: on_exhausted teardown failed for fatal key=%s (action=%s, role=%s)",
                dedupe_key,
                action,
                role,
            )
    # A producer's propose arm that fails fatally is just as stuck as one
    # that exhausts its streak — route it through the same AGENT_FAILED /
    # HITL path so the failure reaches the operator's decision queue. Pass
    # ``fatal=True`` (and the honest ``streak=1`` — a fatal exhausts on its
    # first failure, not after the streak-to-10 budget) so the handler
    # renders the HITL entry as a named credential failure with its
    # remediation, rather than the generic "exhausted after 10 consecutive
    # agent-invocation failures" message this work set out to replace.
    if action == "propose" and self._agent_failed is not None:
        self._agent_failed(
            role=role,
            action=action,
            dedupe_key=dedupe_key,
            streak=1,
            fatal=True,
        )


def record_rate_limited(
    self, dedupe_key: str, action: str, role: str, *, exit_detail: str | None = None
) -> None:
    """Record a TRANSIENT rate-limit / cap-wall outcome (#3364 PR C).

    The agent exited ``egg_agent.auth_errors.EX_RATE_LIMITED``: a bare HTTP
    429 / "rate limit" / "overloaded" throttle that self-heals once the
    rolling cap window lifts. Unlike :meth:`record_abort` this leaves the
    abnormal ``_streaks`` / ``_last_abort_time`` / ``_exhausted`` state
    ENTIRELY untouched (AC-C1 / AC-C6) — mirroring
    :meth:`record_legitimate_outcome`'s "streak untouched" contract — so a cap
    wall can never trip the ``agent-invocation-fail-streak`` halt. Instead it
    PACES the respawn across the cap window (:meth:`ready_to_respawn` honours
    the per-key rate-limit backoff) and captures the failure fingerprint the
    deterministic-loop guard consumes.

    Recording ONLY — it does not itself halt or escalate. It reports two
    transitions to the wired ``rate_limited_notifier`` (the executor owns the
    action, TASK-2-7), each latched once per key:

    * ``threshold_crossed`` — the CUMULATIVE paced wait first crossed
      ``SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS`` (the cq-1
      OVERSEER_ALERT: an attended operator is informed while auto-recovery
      continues — there is NO hard wall-clock ceiling).
    * ``deterministic_loop`` — the SAME fingerprint at the SAME progression
      point reproduced ``SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS`` times in a
      row (no state advance): a deterministic failure masquerading as a
      throttle, which the executor escalates + halts instead of looping.

    The two triggers are orthogonal: a threshold crossing does not imply a
    deterministic loop (a genuine cap wall advances its progression the moment
    it lifts), and vice-versa.
    """
    now = self.clock()
    count = self._rate_limit_count.get(dedupe_key, 0) + 1
    self._rate_limit_count[dedupe_key] = count
    self._last_action[dedupe_key] = (action, role)
    self._record_exit(dedupe_key, "rate_limited", exit_detail)

    # Pace: reset-time-paced when the error text carries a hint (rare on the
    # bare-exit-code production path — the pod exits with just EX_RATE_LIMITED),
    # else the bounded rate-limit backoff. Either way hours-scale-capable and
    # entirely separate from the 30s abnormal cap.
    reset_seconds = parse_rate_limit_reset_seconds(exit_detail)
    backoff = rate_limit_backoff_seconds(count, reset_seconds)
    self._rate_limit_backoff[dedupe_key] = backoff
    self._rate_limit_last_time[dedupe_key] = now
    total = self._rate_limit_wait_total.get(dedupe_key, 0.0) + backoff
    self._rate_limit_wait_total[dedupe_key] = total

    logger.debug(
        "JobSupervisor: rate-limited outcome for key=%s (action=%s, role=%s) — "
        "pacing respawn %.0fs (retry #%d, cumulative wait %.0fs); abnormal "
        "streak untouched",
        dedupe_key,
        action,
        role,
        backoff,
        count,
        total,
    )

    # Deterministic-loop guard (AC-C4) — CORRECTED after the v1 open-NACK
    # barrier (reviewer_contract / reviewer_concurrency / reviewer_code /
    # reviewer_code_holistic all NACKed the original frozen-progression guard).
    #
    # THE FIX: a genuine account-wide cap wall — the headline scenario — freezes
    # the BRC progression (``consensus_state_fingerprint``) EXACTLY like a
    # deterministic failure would, because every producer exits EX_RATE_LIMITED
    # before doing any work, so the bus never moves; and on the bare-exit-code
    # production path the throttle signature is the invariant "rate_limited".
    # So "identical fingerprint / frozen progression" is NOT evidence of a
    # deterministic loop — it is the NORMAL signature of the cap wall this
    # feature exists to ride out. Binding cq-1 forbids a hard ceiling, so a
    # steady throttle MUST pace indefinitely and NEVER halt.
    #
    # The guard therefore escalates ONLY on POSITIVE evidence that the failure
    # is no longer a fresh transient throttle: an exit signature that is present
    # AND does NOT classify as a transient rate limit (the failure CHANGED to a
    # non-throttle error). A genuine throttle — no signature on the orchestrator
    # path (``_observe_jobs`` deliberately passes no ``exit_detail``: the pod
    # exit code carries no classifiable error text), or throttle-classified text
    # — never satisfies this, so it paces forever with ONLY the cq-1 threshold
    # alert. The progression fingerprint is retained so an advancing progression
    # (real cross-arm progress) still resets the repeat counter (AC-C4
    # "continue when state advances").
    fingerprint = RateLimitFingerprint(
        signature=exit_detail or "rate_limited",
        progression=self._probe_brc_fingerprint() or "",
    )
    prev = self._rate_limit_fingerprint.get(dedupe_key)
    if prev is not None and prev == fingerprint:
        repeats = self._rate_limit_repeat.get(dedupe_key, 0) + 1
    else:
        # First sighting, an advancing progression, or a changed signature —
        # reset the repeat counter and clear the escalation latch so a future
        # stall that re-forms identically can escalate again.
        repeats = 1
        self._rate_limit_escalated.discard(dedupe_key)
    self._rate_limit_repeat[dedupe_key] = repeats
    self._rate_limit_fingerprint[dedupe_key] = fingerprint

    # Positive non-throttle evidence gate (the load-bearing correction): a fresh
    # transient throttle is NEVER deterministic, however many times its identical
    # fingerprint reproduces — that is the genuine cap wall cq-1 says to pace
    # through. Only a signature that does not classify as a transient rate limit
    # promotes a repeated failure to a deterministic loop. A signature carrying a
    # parseable reset hint (e.g. "retry after 900s") is also a rate-limit signal,
    # so it is treated as a throttle too — only genuinely-other error text
    # (a changed, non-throttle failure) counts as deterministic evidence.
    signature_is_non_throttle = (
        bool(exit_detail)
        and not is_transient_rate_limit_error(exit_detail)
        and parse_rate_limit_reset_seconds(exit_detail) is None
    )
    deterministic_loop = (
        signature_is_non_throttle
        and repeats >= SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS
        and dedupe_key not in self._rate_limit_escalated
    )
    if deterministic_loop:
        self._rate_limit_escalated.add(dedupe_key)

    # cq-1 threshold (AC-C5): the SOLE operator surface for a persistent cap
    # wall. Fires ONCE when the cumulative paced wait crosses the threshold;
    # there is NO hard ceiling and NO auto-halt, so a genuine multi-day cap
    # paces here indefinitely and self-heals unattended. Independent of the
    # loop-guard above (a genuine throttle crosses this threshold while
    # deterministic_loop stays False).
    threshold_crossed = total >= SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS and not (
        self._alerted_rate_limit.get(dedupe_key, False)
    )
    if threshold_crossed:
        self._alerted_rate_limit[dedupe_key] = True

    if self._rate_limited_notifier is not None:
        try:
            self._rate_limited_notifier(
                role=role,
                action=action,
                dedupe_key=dedupe_key,
                retry_count=count,
                cumulative_wait_seconds=total,
                backoff_seconds=backoff,
                threshold_crossed=threshold_crossed,
                deterministic_loop=deterministic_loop,
                fingerprint=fingerprint,
            )
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.warning(
                "JobSupervisor: rate_limited_notifier raised for key=%s (action=%s, role=%s)",
                dedupe_key,
                action,
                role,
            )


def record_session_timeout(
    self, dedupe_key: str, action: str, role: str, *, exit_detail: str | None = None
) -> None:
    """Record a session-budget expiry — a BOUNDARY, not a failure (#3658).

    The agent exited ``egg_agent.auth_errors.EX_SESSION_TIMEOUT``: it was
    working when its wall-clock budget ran out. The pod is killed mid-turn, but
    the work is not lost — the tree is checkpointed in-pod on the way out and
    the respawn re-attaches to the same worktree — so the honest classification
    is "this event needs another session", not "this agent crashed".

    For the first ``SUPERVISION_SESSION_TIMEOUT_BUDGET`` consecutive expiries
    this leaves the abnormal ``_streaks`` / ``_last_abort_time`` / ``_exhausted``
    state ENTIRELY untouched (the same contract
    :meth:`record_rate_limited` keeps), so a genuinely productive long-running
    producer can never trip the ``agent-invocation-fail-streak`` halt for the
    crime of being slow, and no backoff is imposed — the arm already waited two
    hours.

    Past that budget the boundary treatment stops: further expiries are recorded
    as ordinary aborts, handing the key back to the streak / exhaustion /
    AGENT_FAILED path. Without this an arm that times out forever would respawn
    forever, because the boundary path deliberately disables the only machinery
    that stops it. The counter clears on a clean completion of the key
    (:meth:`record_success`) or :meth:`retire` — never on the abort this method
    itself delegates to, which would make the budget unspendable.
    """
    count = self._session_timeout_count.get(dedupe_key, 0) + 1
    self._session_timeout_count[dedupe_key] = count

    if count > SUPERVISION_SESSION_TIMEOUT_BUDGET:
        logger.warning(
            "JobSupervisor: session-budget expiry #%d for key=%s (action=%s, role=%s) "
            "— past the %d-boundary budget; recording it as an abnormal termination "
            "so the streak/exhaustion path can terminate a permanently over-budget arm",
            count,
            dedupe_key,
            action,
            role,
            SUPERVISION_SESSION_TIMEOUT_BUDGET,
        )
        self.record_abort(dedupe_key, action, role, exit_detail=exit_detail)
        return

    self._last_action[dedupe_key] = (action, role)
    self._record_exit(dedupe_key, "session_timeout", exit_detail)
    logger.info(
        "JobSupervisor: session-budget expiry %d/%d for key=%s (action=%s, role=%s) "
        "— treating as a session boundary; respawning without touching the "
        "abnormal streak",
        count,
        SUPERVISION_SESSION_TIMEOUT_BUDGET,
        dedupe_key,
        action,
        role,
    )


def halt_rate_limited(self, dedupe_key: str) -> None:
    """Halt the paced retry for a deterministic rate-limit loop (#3364 PR C).

    Invoked by the executor's rate-limit loop-guard (TASK-2-7) after
    :meth:`record_rate_limited` reported ``deterministic_loop`` — the SAME
    failure fingerprint reproduced at the SAME progression point past the guard
    threshold, a deterministic failure masquerading as a throttle. Marks the
    key exhausted so the loop stops respawning it (the terminal
    :meth:`is_exhausted` gate), letting the operator surfaces (a named
    OVERSEER_ALERT + the arms-exhausted HITL) take over instead of looping
    forever. Completed slices are untouched — only this one arm halts, so
    landed work is preserved (AC-C3). Idempotent.
    """
    if dedupe_key in self._exhausted:
        return
    self._exhausted.add(dedupe_key)
    logger.warning(
        "JobSupervisor: halting paced rate-limit retry for key=%s — deterministic "
        "loop-guard tripped (identical failure reproduced at the same progression point)",
        dedupe_key,
    )


def _clear_rate_limit_state(self, dedupe_key: str) -> None:
    """Drop all #3364 paced rate-limit state for a key (recovery / retire)."""
    self._rate_limit_count.pop(dedupe_key, None)
    self._rate_limit_backoff.pop(dedupe_key, None)
    self._rate_limit_last_time.pop(dedupe_key, None)
    self._rate_limit_wait_total.pop(dedupe_key, None)
    self._rate_limit_fingerprint.pop(dedupe_key, None)
    self._rate_limit_repeat.pop(dedupe_key, None)
    self._alerted_rate_limit.pop(dedupe_key, None)
    self._rate_limit_escalated.discard(dedupe_key)


def backoff_seconds(self, dedupe_key: str) -> float:
    """Compute backoff delay for the respawn (streak * factor).

    The caller is expected NOT to spawn when exhausted AND the dedupe key
    is unchanged — the loop re-reads consensus and only spawns for a new key.
    """
    streak = self._streaks.get(dedupe_key, 0)
    return min(streak * SUPERVISION_BACKOFF_FACTOR, SUPERVISION_BACKOFF_CAP_SECONDS)


def ready_to_respawn(self, dedupe_key: str) -> bool:
    """Return True iff the backoff window since the last abort has elapsed.

    A key with no recorded abort (fresh, or just reset by
    :meth:`record_success`) is always ready. After an abort the caller
    must wait :meth:`backoff_seconds` (``streak*factor`` capped) measured
    from the abort timestamp before respawning — this is what throttles a
    deterministic fast-fail loop instead of hammering the orchestrator.

    #3364 PR C: a TRANSIENT rate-limit outcome paces the respawn across the
    (hours-scale) cap window via a SEPARATE anchor — a throttle never records
    an abort, so this gate applies even to a key with no abort. Both windows
    must have elapsed. The abnormal branch below is byte-for-byte the pre-#3364
    behaviour (AC-C6): for a key with no rate-limit outcome the rate-limit gate
    is inert.
    """
    # Transient rate-limit paced window (independent of the abnormal backoff).
    rl_last = self._rate_limit_last_time.get(dedupe_key)
    if rl_last is not None:
        rl_backoff = self._rate_limit_backoff.get(dedupe_key, 0.0)
        if rl_backoff > 0 and (self.clock() - rl_last) < rl_backoff:
            return False
    last = self._last_abort_time.get(dedupe_key)
    if last is None:
        return True
    backoff = self.backoff_seconds(dedupe_key)
    if backoff <= 0:
        return True
    return (self.clock() - last) >= backoff


def is_exhausted(self, dedupe_key: str) -> bool:
    """Return True if the given dedupe-key has exhausted its retry budget."""
    return dedupe_key in self._exhausted


def _record_exit(self, dedupe_key: str, category: str, detail: str | None) -> None:
    """Append a bounded termination-history entry for ``dedupe_key`` (#3496)."""
    history = self._exit_history.setdefault(dedupe_key, [])
    history.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "category": category,
            "detail": detail,
        }
    )
    del history[:-SUPERVISION_EXIT_HISTORY_MAX]


def _format_exit_history(self, dedupe_key: str) -> str:
    """Render the per-key termination history as a compact one-liner."""
    entries = self._exit_history.get(dedupe_key) or []
    if not entries:
        return "(no recorded terminations)"
    parts = []
    for entry in entries:
        label = entry["category"]
        if entry.get("detail"):
            label = f"{label} ({entry['detail']})"
        parts.append(f"{entry['at']} {label}")
    return "; ".join(parts)


def exhausted_report(self) -> list[dict[str, Any]]:
    """Describe every exhausted key for operator surfacing (#3496).

    One entry per exhausted dedupe key: the arm it belongs to, its failure
    streak, and the recent termination history (category + optional pod
    exit detail). This is what the arms-exhausted HITL escalation embeds so
    the operator can see WHY the arms died without grepping pod logs.
    Sorted by (role, action) for stable rendering.
    """
    report = []
    for key in self._exhausted:
        action, role = self._last_action.get(key, ("", ""))
        report.append(
            {
                "dedupe_key": key,
                "role": role,
                "action": action,
                "streak": self._streaks.get(key, 0),
                "exit_history": list(self._exit_history.get(key) or []),
                "exit_history_text": self._format_exit_history(key),
            }
        )
    report.sort(key=lambda e: (e["role"], e["action"], e["dedupe_key"]))
    return report


def reset_exhausted(self) -> list[str]:
    """Forget ALL supervision state for every exhausted key (#3496).

    The in-band recovery primitive: an exhausted key is otherwise terminal
    (``record_success`` — the only other exit — is unreachable because the
    key can no longer spawn), so an operator-initiated retry clears the
    exhausted set here, giving each key a fresh budget. Full ``retire``
    (not just ``_exhausted.discard``): leaving the streak + ``_alerted_10``
    latch in place would make the NEXT abort skip the re-exhaustion branch
    entirely (the threshold guard fires once per latch), so the arm would
    retry forever instead of re-exhausting after another full budget.

    Returns the cleared keys (sorted) so callers can report them.
    """
    cleared = sorted(self._exhausted)
    for key in cleared:
        self.retire(key)
    if cleared:
        logger.info(
            "JobSupervisor: operator reset cleared %d exhausted key(s) — fresh spawn budgets: %s",
            len(cleared),
            ", ".join(cleared),
        )
    return cleared


def noop_park_report(self) -> list[dict[str, Any]]:
    """Describe every no-op-parked key for operator surfacing (#3548).

    Mirror of :meth:`exhausted_report` for the successful-no-op park
    (#3425): one entry per key whose clean-completion streak reached
    ``SUPERVISION_NOOP_STREAK_PARK`` — the arm it belongs to and the no-op
    streak length. This is what the all-arms-parked HITL escalation embeds
    so the operator can see WHICH arms keep running to no effect without
    grepping pod logs. Sorted by (role, action) for stable rendering.
    """
    report = []
    for key, streak in self._noop_streaks.items():
        if streak < SUPERVISION_NOOP_STREAK_PARK:
            continue
        action, role = self._last_action.get(key, ("", ""))
        report.append(
            {
                "dedupe_key": key,
                "role": role,
                "action": action,
                "noop_streak": streak,
            }
        )
    report.sort(key=lambda e: (e["role"], e["action"], e["dedupe_key"]))
    return report


def reset_noop_parks(self) -> list[str]:
    """Forget ALL supervision state for every no-op-parked key (#3548).

    The in-band recovery primitive behind the all-arms-parked HITL's
    "Retry arms" resolution — the park twin of :meth:`reset_exhausted`.
    A park does self-release (fingerprint movement / retry heartbeat),
    but each release grants only a single probe spawn that re-parks on
    the next no-op; an operator who has fixed the underlying wedge wants
    the streaks gone so the arms run freely again. Full ``retire`` for
    the same latch reasons as :meth:`reset_exhausted`.

    Returns the cleared keys (sorted) so callers can report them.
    """
    cleared = sorted(
        key for key, streak in self._noop_streaks.items() if streak >= SUPERVISION_NOOP_STREAK_PARK
    )
    for key in cleared:
        self.retire(key)
    if cleared:
        logger.info(
            "JobSupervisor: operator reset cleared %d no-op-parked key(s) — "
            "fresh spawn budgets: %s",
            len(cleared),
            ", ".join(cleared),
        )
    return cleared


def noop_parked(self, dedupe_key: str) -> bool:
    """Return True iff the key is parked on a successful-no-op streak (#3425).

    Parked = ``SUPERVISION_NOOP_STREAK_PARK`` clean completions of the
    same key with zero BRC progress (the loop re-derived the identical key
    each time; an interleaved abort does not reset the count). Unlike #3138
    failure exhaustion, the park self-releases — the wedge is typically an
    unresolved operator HITL decision (``cq-N``), whose resolution writes
    only the contract file and never the tracker, so waiting for a new
    dedupe key would deadlock the slice after the operator answers:

    * immediately, when the unresolved contract-decision set differs from
      the one recorded at park time (e.g. the gating ``cq-N`` was
      resolved) — detected via ``hitl_probe``;
    * immediately, when the consensus-relevant BRC state differs from the
      one recorded at park time (#3465) — detected via ``brc_probe``. An
      arm can also park while merely racing its upstream producer (the
      tester's propose arm no-ops until the coder commits); the producer's
      proposal / reviews move the tracker but never this arm's own dedupe
      key, so without this check the slice wedges until the heartbeat;
    * every ``SUPERVISION_NOOP_PARK_RETRY_SECONDS`` as a liveness
      backstop (also the only release when no probe is wired or it
      fails).

    Each release allows exactly one probe spawn: the fingerprint /
    heartbeat anchor is refreshed, and if the pod no-ops again the streak
    keeps the key parked. Called only on the loop's would-spawn path, so
    the probe never runs for healthy keys.
    """
    if self._noop_streaks.get(dedupe_key, 0) < SUPERVISION_NOOP_STREAK_PARK:
        return False
    now = self.clock()
    current = self._probe_hitl_fingerprint()
    parked = self._noop_fingerprint.get(dedupe_key)
    current_brc = self._probe_brc_fingerprint()
    parked_brc = self._noop_brc_fingerprint.get(dedupe_key)
    hitl_moved = current is not None and parked is not None and current != parked
    brc_moved = current_brc is not None and parked_brc is not None and current_brc != parked_brc
    if hitl_moved or brc_moved:
        # #3537: record WHAT moved so the released probe spawn can be told.
        # The park released precisely because the world changed, but the
        # respawned pod re-derives an identical dedupe key + payload, so its
        # prompt would otherwise be byte-identical to the one that parked -
        # a warm-resumed session then replays its cached "still blocked"
        # conclusion and the arm livelocks. The loop consumes this delta on
        # the spawn path (consume_noop_release_context) and threads it into
        # the event prompt. Computed BEFORE the anchors are refreshed below.
        release_context: dict[str, Any] = {}
        if hitl_moved:
            release_context["resolved_decision_ids"] = sorted(parked - current)
            release_context["newly_gating_decision_ids"] = sorted(current - parked)
        if brc_moved:
            release_context["brc_moved"] = True
        self._noop_release_context[dedupe_key] = release_context
        # Refresh BOTH probe anchors on any release, not just the branch that
        # fired. If a single poll sees the contract-decision set AND the BRC
        # state move at once (the operator resolves a gating ``cq-N`` while
        # the cohort proposes), advancing only the firing anchor would leave
        # the other's park-time digest stale, so the very next poll would
        # release again — two probe spawns for one wedge. Advancing every
        # anchor whose probe currently reads a concrete value collapses this
        # to one probe per poll regardless of how many signals moved. A
        # ``None`` reading (probe unwired/failed) is left untouched: it can
        # never compare as moved, so it cannot cause a spurious re-release,
        # and preserving its park-time value avoids degrading a good anchor
        # on a transient probe failure.
        if current is not None:
            self._noop_fingerprint[dedupe_key] = current
        if current_brc is not None:
            self._noop_brc_fingerprint[dedupe_key] = current_brc
        self._noop_last_probe[dedupe_key] = now
        # Re-arm the once-per-key alert latch only when the contract-decision
        # set moved AND a decision is *still* gating (``current`` non-empty).
        # The alert names the decisions recorded at park time; if the probe
        # spawn no-ops again on a freshly-gating ``cq-N`` the arm re-parks,
        # and without re-arming the latch would suppress a new alert, leaving
        # the operator staring at a stale one naming an already-resolved
        # cq-N. This keeps "one alert per distinct wedge" rather than "one
        # alert per key lifetime".
        #
        # An *empty* new set means the wedge cleared (the operator resolved
        # the last gating decision), and a pure BRC-movement release means
        # the cohort progressed; in both cases the released probe will
        # proceed and make real progress. Re-arming there would let the next
        # ``record_success`` — which cannot distinguish a productive
        # completion from a repeat no-op (any rc=0 exit maps to SUCCESS) —
        # fire a spurious high-priority alert on the common happy path,
        # falsely claiming zero BRC progress with no visible gating decision.
        # So only re-arm when a gating decision remains.
        if hitl_moved and current:
            self._alerted_noop.pop(dedupe_key, None)
        if hitl_moved:
            logger.info(
                "JobSupervisor: contract-decision set changed for parked key=%s "
                "(was %s, now %s) — releasing the no-op park for a probe spawn",
                dedupe_key,
                sorted(parked),
                sorted(current),
            )
        if brc_moved:
            logger.info(
                "JobSupervisor: BRC state moved for parked key=%s "
                "— releasing the no-op park for a probe spawn",
                dedupe_key,
            )
        return False
    last = self._noop_last_probe.get(dedupe_key)
    if last is None or (now - last) >= SUPERVISION_NOOP_PARK_RETRY_SECONDS:
        self._noop_last_probe[dedupe_key] = now
        logger.info(
            "JobSupervisor: no-op park retry heartbeat elapsed for key=%s — allowing a probe spawn",
            dedupe_key,
        )
        return False
    return True


def consume_noop_release_context(self, dedupe_key: str) -> dict[str, Any] | None:
    """Pop and return the park-release delta recorded for ``dedupe_key`` (#3537).

    Set only by a fingerprint-change release in :meth:`noop_parked`; a retry
    heartbeat release records nothing (nothing observably changed, so there
    is no delta to carry). The loop calls this once, immediately before
    dispatching the spawn, so the delta rides exactly the probe spawn the
    release granted and never leaks onto a later, unrelated spawn of the
    same key. Keys:

    * ``resolved_decision_ids`` - contract decisions unresolved at park time
      that have since left the unresolved set (resolved, or removed).
    * ``newly_gating_decision_ids`` - decisions that became unresolved since
      park (a fresh wedge the probe should surface, not fight).
    * ``brc_moved`` - the consensus-state digest moved (a peer proposal /
      verdict / confirm progressed while this arm was parked).
    """
    return self._noop_release_context.pop(dedupe_key, None)


def _probe_hitl_fingerprint(self) -> frozenset[str] | None:
    """Snapshot the unresolved contract-decision id set (best-effort).

    ``None`` means "unknown" (no probe wired, probe failed, or the probe
    itself signalled unknown) — the caller must then never treat the
    fingerprint as comparable, falling back to the retry heartbeat.
    """
    if self._hitl_probe is None:
        return None
    try:
        result = self._hitl_probe()
    except Exception as exc:  # noqa: BLE001 — probing is best-effort
        logger.warning(
            "JobSupervisor: hitl probe failed — treating fingerprint as unknown: %s",
            exc,
        )
        return None
    if result is None:
        return None
    return frozenset(result)


def _probe_waiting_on(self, role: str) -> tuple[str, bool] | None:
    """Snapshot ``role``'s latest WAITING_ON_ROLE self-report (best-effort, #3520).

    Returns ``(waiting_on, waited_on_live)`` when the role's most recent
    HEARTBEAT in the current phase self-reports ``WAITING_ON_ROLE``:
    ``waiting_on`` names the waited-on role(s) as self-reported and
    ``waited_on_live`` is True iff every waited-on role shows recent bus
    activity. ``None`` means "no such self-report or unknown" (no probe
    wired, probe failed, or the latest heartbeat is some other state) —
    the caller then keeps the wedge-shaped high-priority alert.
    """
    if self._waiting_probe is None:
        return None
    try:
        return self._waiting_probe(role)
    except Exception as exc:  # noqa: BLE001 — probing is best-effort
        logger.warning(
            "JobSupervisor: waiting-on probe failed for role=%s — "
            "treating self-report as unknown: %s",
            role,
            exc,
        )
        return None


def _probe_brc_fingerprint(self) -> str | None:
    """Snapshot the consensus-state fingerprint (best-effort, #3465).

    ``None`` means "unknown" (no probe wired, or the probe failed) — the
    caller must then never treat the fingerprint as comparable, falling
    back to the retry heartbeat. Same contract as
    :meth:`_probe_hitl_fingerprint`.
    """
    if self._brc_probe is None:
        return None
    try:
        return self._brc_probe()
    except Exception as exc:  # noqa: BLE001 — probing is best-effort
        logger.warning(
            "JobSupervisor: brc probe failed — treating fingerprint as unknown: %s",
            exc,
        )
        return None


def reconcile(self, live_dedupe_keys: Iterable[str]) -> None:
    """When restarting, reconcile from live Job labels.

    After a simulated orchestrator restart (e.g. crash), live pods may
    be running. We do NOT persist supervision state; a fresh loop
    starts with empty streaks. This means the first-old-dedupe-key
    starts a fresh budget, which is the intended stateless design.

    NOTE: this clears ``_exhausted`` along with the streaks, so it is the
    *restart* path only — it is NOT called per-poll. The live driver
    (``OrchestratorEventLoop.poll_once``) observes Job status and drives
    ``record_*`` directly; it never calls ``reconcile`` on the steady-state
    path, so per-poll exhaustion is never wiped.
    """
    self._streaks.clear()
    self._last_abort_time.clear()
    self._alerted_warn.clear()
    self._alerted_10.clear()
    self._exhausted.clear()
    self._exit_history.clear()
    self._noop_streaks.clear()
    self._noop_fingerprint.clear()
    self._noop_brc_fingerprint.clear()
    self._noop_last_probe.clear()
    self._noop_release_context.clear()
    self._alerted_noop.clear()
    # #3364 PR C: the paced rate-limit state is process-local like the rest —
    # a restart re-derives + re-paces from scratch.
    self._rate_limit_count.clear()
    self._rate_limit_backoff.clear()
    self._rate_limit_last_time.clear()
    self._rate_limit_wait_total.clear()
    self._rate_limit_fingerprint.clear()
    self._rate_limit_repeat.clear()
    self._alerted_rate_limit.clear()
    self._rate_limit_escalated.clear()
    # #3658: the session-boundary budget is process-local too — a restarted
    # loop grants a fresh one, same stateless design as the streaks above.
    self._session_timeout_count.clear()
    # Re-initialise live-key set if the caller provides it.
    # We only need to know which keys exist, not the full history.
    for key in live_dedupe_keys:
        if key:
            self._last_action[key] = ("(reconciled)", "(reconciled)")


def _emit_alert(self, dedupe_key: str, streak: int, action: str, role: str) -> None:
    """Emit an OVERSEER_ALERT for an exhausted key.

    The wrapper's ``raise_agent_fail_alert`` path (``consensus_wrapper.py:690``)
    is the reference for the message payload; we mirror the anomaly
    name and format here.
    """
    if self._overseer_alert is not None:
        self._overseer_alert(
            anomaly="agent-invocation-fail-streak",
            priority="high",
            summary=(f"agent invocation failing repeatedly (action={action}, streak={streak})"),
            detail=(
                f"Event-pump for role={role} has had {streak} consecutive "
                f"agent-invocation failures on action={action}. "
                f"The orchestrator has exhausted retries for the current "
                f"dedupe key ({dedupe_key}). No further pods will be "
                f"spawned until the BRC state changes (new dedupe key). "
                f"Threshold: streak >= {SUPERVISION_FAILURE_STREAK_ALERT}. "
                f"Recent terminations: {self._format_exit_history(dedupe_key)}."
            ),
        )


def _emit_noop_alert(
    self,
    dedupe_key: str,
    streak: int,
    action: str,
    role: str,
    fingerprint: frozenset[str] | None,
    waiting: tuple[str, bool] | None = None,
) -> None:
    """Emit a named, once-per-key alert for a successful-no-op park (#3425).

    Distinct from :meth:`_emit_alert`'s ``agent-invocation-fail-streak``:
    these invocations *succeed* — the slice is wedged on something a
    respawn cannot resolve (typically an unresolved operator HITL
    ``cq-N``), so the message points the operator at the pending decision
    rather than at agent health.

    #3520 severity split: ``waiting`` is the parked role's latest
    WAITING_ON_ROLE self-report, probed by the caller only when no gating
    contract decision was visible. A role waiting on a LIVE upstream
    producer's first proposal is normal BRC choreography (the park still
    saves the pod spawns; the arm un-parks on BRC movement), so that shape
    emits a low-priority ``agent-parked-waiting-on-role`` notice — a
    routine [high] would train operators to skim past the alert feed
    (#3364). High priority is kept for the genuine wedges: a visible
    gating ``cq-N``, a WAITING_ON_ROLE report whose waited-on role shows
    no recent bus activity (a real stall — the parked role's own
    escalation threshold can no longer fire once its pod stops spawning),
    or a streak with no self-report at all (silent wedge).
    """
    if self._overseer_alert is None:
        return
    if not fingerprint and waiting is not None:
        waited_on, waited_on_live = waiting
        if waited_on_live:
            self._overseer_alert(
                anomaly="agent-parked-waiting-on-role",
                priority="low",
                summary=(f"agent parked waiting on {waited_on} (action={action}, streak={streak})"),
                detail=(
                    f"Event-pump for role={role} has had {streak} consecutive "
                    f"one-shot invocations on action={action} with zero BRC "
                    f"progress and is parked (dedupe key {dedupe_key}). Its "
                    f"latest HEARTBEAT self-reports WAITING_ON_ROLE on "
                    f"{waited_on}, which is live on the bus — this is normal "
                    f"dependency choreography, not a wedge. The arm un-parks "
                    f"as soon as the BRC state moves (e.g. {waited_on} "
                    f"proposes); a probe spawn is retried every "
                    f"{SUPERVISION_NOOP_PARK_RETRY_SECONDS}s as a backstop. "
                    f"No operator action needed."
                ),
            )
            return
        self._overseer_alert(
            anomaly="agent-invocation-noop-streak",
            priority="high",
            summary=(
                f"agent parked waiting on {waited_on}, which shows no recent "
                f"bus activity (action={action}, streak={streak})"
            ),
            detail=(
                f"Event-pump for role={role} has had {streak} consecutive "
                f"one-shot invocations on action={action} with zero BRC "
                f"progress and is parked (dedupe key {dedupe_key}). Its "
                f"latest HEARTBEAT self-reports WAITING_ON_ROLE on "
                f"{waited_on}, but {waited_on} has emitted nothing on the "
                f"bus recently — the waited-on producer looks stalled, and "
                f"the parked role's own escalation threshold cannot fire "
                f"while its pod no longer spawns. Check the {waited_on} "
                f"arm's health (pod status, failure streaks) and the "
                f"slice's BRC transcript."
            ),
        )
        return
    if fingerprint:
        gating = (
            f" Unresolved contract HITL decision(s) likely gating it: "
            f"{', '.join(sorted(fingerprint))} — check get_status "
            f"pending_contract_decisions and resolve via provide_input."
        )
    else:
        gating = (
            " No unresolved contract decision was visible at park time; "
            "check the slice's BRC transcript for what the agent is "
            "blocked on."
        )
    self._overseer_alert(
        anomaly="agent-invocation-noop-streak",
        priority="high",
        summary=(
            f"agent invocations completing with zero BRC progress "
            f"(action={action}, streak={streak})"
        ),
        detail=(
            f"Event-pump for role={role} has had {streak} consecutive one-shot "
            f"invocations on action={action} that exited cleanly WITHOUT any "
            f"BRC-bus progress (dedupe key {dedupe_key} re-derived unchanged "
            f"each time). The arm is parked: no further pods spawn for this key "
            f"until the unresolved contract-decision set changes (e.g. the "
            f"gating cq-N is resolved) or the BRC state moves; a probe spawn is "
            f"retried every {SUPERVISION_NOOP_PARK_RETRY_SECONDS}s as a "
            f"backstop.{gating}"
        ),
    )


def _emit_fatal_alert(self, dedupe_key: str, action: str, role: str) -> None:
    """Emit a named, actionable alert for an auth-fatal exhaustion (#3373).

    Distinct from :meth:`_emit_alert`'s generic
    ``agent-invocation-fail-streak``: this names the credential cause and
    the remediation so the operator is not left reading a generic "failing
    repeatedly" message with no pointer to the fix.
    """
    if self._overseer_alert is not None:
        self._overseer_alert(
            anomaly="agent-credential-fatal",
            priority="high",
            summary=(f"agent credential rejected — non-retryable (action={action}, role={role})"),
            detail=(
                f"Event-pump for role={role} (action={action}) failed with a "
                f"non-retryable credential error: the agent's Claude credential "
                f"was rejected (subscription weekly/usage limit, expired/invalid "
                f"token, 401, or exhausted credit balance). The orchestrator "
                f"exhausted dedupe key {dedupe_key} on the first failure rather "
                f"than retrying — a retry would re-use the same rejected "
                f"credential. Remediation: rotate the Claude credential (set the "
                f"intended account as the active CLAUDE_CODE_OAUTH_TOKEN in "
                f"secrets.env and apply the gateway secret), then restart this "
                f"phase to mint a fresh dedupe key so pods respawn."
            ),
        )
