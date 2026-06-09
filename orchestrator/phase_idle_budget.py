"""Orchestrator-side phase-level idle-budget timer (#3023 slice-1 task-1-1).

Replaces the per-pod wrapper-side idle-budget emitter
(``consensus_wrapper.py``'s ``check_idle_budget`` / ``raise_idle_alert``)
with a single orchestrator-side timer that fires once per phase per
threshold rather than once per role. The wrapper-side emitter is silenced
in coexistence (slice-1 task-1-3 ``EGG_PHASE_IDLE_BUDGET_OWNER=orchestrator``
env var); slice-3 deletes it outright.

Threshold model:
  - 1x ``DEFAULT_PHASE_IDLE_BUDGET_MIN`` (30 min) -> medium priority
    ``stuck-phase-transition`` alert.
  - 2x ``DEFAULT_PHASE_IDLE_BUDGET_MIN`` (60 min) -> high priority
    follow-up alert.
  - Each threshold fires at most once per phase entry. Re-arming is by
    per-phase instance lifetime: ``routes/pipelines.py`` constructs a
    fresh ``PhaseIdleBudgetTimer`` inside the per-phase concurrent
    runner and drops it when the phase returns, so a new phase entry
    always starts with the latches cleared. ``reset()`` is exposed for
    tests and for future cross-phase reuse (slice-2 / slice-3) but is
    not on the slice-1 production hot path.
  - HITL suppression (AC-R13): when ``pending_hitl_count > 0`` the 1x
    alert downgrades to priority=low and includes the pending HITL IDs
    in ``reason``; the 2x alert is suppressed entirely. The operator is
    already in the loop, so re-paging at 2x adds noise without signal.

Per-role-state payload (AC-R4):
  Every alert carries a ``per_role_state`` dict mapping role -> last
  action so the operator sees the BRC matrix without having to query
  pipeline status separately. The wrapper-side emitter
  (consensus_wrapper.py:493-521) embedded a free-form BRC snapshot in
  ``--detail``; the structured payload here replaces that with a
  machine-readable dict the alert handler can render however it likes.

Alert dispatch:
  The class takes an injected ``alert_emitter`` callable. Production
  binds the emitter to the orchestrator's ``OVERSEER_ALERT`` message-
  store path (see :func:`routes.pipelines._tick_phase_idle_budget`);
  tests pass a ``MagicMock`` and assert on the kwargs. The emitter
  contract:

      emitter(
          *,
          anomaly: str,                       # always "stuck-phase-transition"
          priority: str,                      # "low" | "medium" | "high"
          pipeline_id: str,
          phase: str,
          per_role_state: dict[str, str],     # role -> last_action
          pending_hitl_ids: tuple[str, ...],  # () when not pending
          threshold_multiplier: int,          # 1 or 2
          summary: str,
          reason: str,
      )
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

# Parity with ``consensus_wrapper.EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT``
# (the wrapper emitter that this orchestrator-side timer replaces, see
# consensus_wrapper.py:59). 30 min was the architect od-4 default,
# well above the WS7-observed 10-13 min legitimate-idle ceiling.
# The cross-default parity is unit-tested
# (test_phase_idle_budget.test_default_constant_matches_wrapper_default)
# so a drift during the slice-1 coexistence window fails CI directly.
DEFAULT_PHASE_IDLE_BUDGET_MIN = 30

# Anomaly tag preserved verbatim from the wrapper emitter
# (consensus_wrapper.py:524) so the operator UX (SDLC skill alert
# detection) is unchanged across the wrapper -> orchestrator migration.
STUCK_PHASE_TRANSITION_ANOMALY = "stuck-phase-transition"


# Signature of the injected ``alert_emitter`` callable. We use Any
# kwargs because the production binding (in ``routes.pipelines``)
# adds the message-store dispatch on top of the kwargs the timer
# emits; the timer must not depend on the production binding's exact
# shape. Tests pass a ``MagicMock`` and assert via ``call_args.kwargs``.
AlertEmitter = Callable[..., Any]


class PhaseIdleBudgetTimer:
    """Phase-level idle budget for the orchestrator's per-phase tick.

    A single instance owns the state for one phase entry. The
    orchestrator's per-phase tick (``routes/pipelines.py``, wired in
    slice-1 task-1-2) calls:

      * :meth:`record_spawn` when a BRC spawn / verb fires. The first
        call binds the timer to a ``(pipeline_id, phase)`` pair; later
        calls from a sibling pipeline / earlier phase are ignored so a
        stray ``record_spawn`` cannot silence this timer.
      * :meth:`check` once per loop iteration.
      * :meth:`reset` (optional) when reusing one instance across phases.
        Slice-1 production does *not* call ``reset`` — it constructs a
        fresh instance per phase entry inside ``_run_concurrent_phase``
        and drops it when the phase returns, so re-arming happens by
        instance lifetime. ``reset`` is kept for tests and for slice-2
        / slice-3 cross-phase reuse paths that may need to re-bind a
        long-lived timer without throwing it away.

    The threshold latches (``_alerted_1x`` / ``_alerted_2x``) are sticky
    for the lifetime of the instance — only :meth:`reset` (or
    constructing a new instance) re-arms them. Re-arming on every spawn
    would let a single brief progress signal silence an otherwise-stuck
    phase, defeating the safety net.

    Construction:
        timer = PhaseIdleBudgetTimer(alert_emitter=my_emitter, budget_minutes=30)
    """

    def __init__(
        self,
        *,
        alert_emitter: AlertEmitter,
        budget_minutes: int = DEFAULT_PHASE_IDLE_BUDGET_MIN,
        now: float | None = None,
    ) -> None:
        self._emit = alert_emitter
        self.budget_minutes = budget_minutes
        self._budget_secs = budget_minutes * 60
        # ``pipeline_id`` / ``phase`` are bound by the first
        # ``record_spawn`` call. Before then the timer is in a
        # "unbound" state and ``check`` silently no-ops (the operator
        # doesn't have a phase to alert about yet).
        self.pipeline_id: str | None = None
        self.phase: str | None = None
        # ``last_spawn_at`` defaults to "now" so the budget is measured
        # from instance creation, not unix epoch. Tests pin ``now`` so
        # the threshold semantics are independent of wall-clock time.
        start = time.monotonic() if now is None else now
        self.last_spawn_at: float = start
        self.last_alert_at: float | None = None
        self.per_role_last_action: dict[str, str] = {}
        self._alerted_1x = False
        self._alerted_2x = False

    # -- mutation ---------------------------------------------------------

    # Phase-warmup debounce (#3023 slice-1). Precise rule:
    #
    #   record_spawn updates ``last_spawn_at`` iff
    #       (first_call) OR (now - last_spawn_at >= _BUDGET_RESET_GAP_SECS)
    #
    # ``per_role_last_action`` is updated unconditionally (the AC-R4
    # payload reflects every spawn regardless of debounce). What the
    # debounce gates is the budget rebasing.
    #
    # Why: continuous activity (spawns < 60s apart) still rebases on
    # each crossing of the 60s boundary, so a steadily active phase
    # never tips the 30-min budget — the rebase cadence is at most one
    # per 60s, not one per spawn. A flurry of spawns at phase fan-out
    # collapses to a single rebase (the first spawn binds the timer and
    # rebases unconditionally; immediate follow-on spawns within 60s
    # are absorbed). Slice-2 ``record_spawn`` call sites can therefore
    # fire freely without worrying about whether they will "starve" a
    # legitimate idle alert.
    _BUDGET_RESET_GAP_SECS = 60

    def record_spawn(
        self,
        *,
        pipeline_id: str,
        phase: str,
        role: str,
        action: str,
        now: float | None = None,
    ) -> None:
        """Note that a spawn / BRC action for ``role`` just fired.

        First call binds the timer to ``(pipeline_id, phase)``. Subsequent
        calls with a mismatched ``(pipeline_id, phase)`` are no-ops so a
        stray call from a sibling pipeline / earlier phase cannot
        accidentally silence this timer. To re-bind, call :meth:`reset`
        first.

        Resets ``last_spawn_at`` ONLY when the new ``now`` is more than
        :attr:`_BUDGET_RESET_GAP_SECS` after the previous one — see the
        phase-warmup debounce note above the constant for rationale.

        Does NOT re-arm the threshold latches: only :meth:`reset` does.
        See the class docstring for the rationale.
        """
        first_call = self.pipeline_id is None
        if first_call:
            self.pipeline_id = pipeline_id
            self.phase = phase
        elif pipeline_id != self.pipeline_id or phase != self.phase:
            # Mismatch: ignore. Re-binding requires an explicit reset()
            # at phase transition.
            return
        ts = time.monotonic() if now is None else now
        # Always update the per-role action snapshot (AC-R4 payload).
        self.per_role_last_action[role] = action
        # Only treat this spawn as a budget-resetting "progress" event
        # when there's been a meaningful gap since the last one. The
        # first call always rebases (the budget starts when the phase
        # starts, not when the timer was constructed).
        if first_call or (ts - self.last_spawn_at) >= self._BUDGET_RESET_GAP_SECS:
            self.last_spawn_at = ts

    def reset(self) -> None:
        """Clear all state. Exposed for tests and cross-phase reuse.

        Re-bases ``last_spawn_at`` to "now" so the new phase gets a
        fresh budget window, unbinds the ``(pipeline_id, phase)`` pair
        so the next ``record_spawn`` can re-bind to a different phase,
        drops the per-role action snapshot, and re-arms both threshold
        latches.

        Slice-1 production does NOT call ``reset``: the per-phase
        runner in ``routes/pipelines.py`` builds a fresh instance per
        phase entry, so re-arming happens by instance lifetime. Reach
        for ``reset`` only when reusing one instance across phases (a
        slice-2 / slice-3 affordance) or in tests that pin the threshold
        semantics of a re-armed timer.
        """
        self.pipeline_id = None
        self.phase = None
        self.last_alert_at = None
        self.per_role_last_action = {}
        self._alerted_1x = False
        self._alerted_2x = False
        self.last_spawn_at = time.monotonic()

    # -- query ------------------------------------------------------------

    def check(
        self,
        *,
        now: float,
        pending_hitl_count: int = 0,
        pending_hitl_ids: list[str] | None = None,
    ) -> None:
        """Run one tick of the threshold check; emit an alert on cross.

        At-most-once-per-threshold-per-phase. Idempotent across check
        calls in the same bucket (a second call at idle=31 min returns
        without re-emitting because ``_alerted_1x`` is set).

        HITL suppression (AC-R13): when ``pending_hitl_count > 0`` the
        1x alert downgrades to priority="low" and includes the pending
        HITL IDs in the reason; the 2x alert is suppressed entirely
        because the operator is already in the loop.

        No-ops silently before the first ``record_spawn`` binds a
        ``(pipeline_id, phase)`` pair — there's nothing to alert about.

        Args:
            now: Monotonic-clock timestamp (seconds, matching the value
                passed to ``__init__(now=...)`` and to ``record_spawn``).
                Wall-clock drift / NTP steps would fire spurious alerts;
                callers MUST use ``time.monotonic()``.
            pending_hitl_count: Number of open HITL decisions for this
                pipeline at the moment of the check. ``> 0`` triggers
                the AC-R13 suppression branches.
            pending_hitl_ids: Optional list of decision IDs the
                ``pending_hitl_count`` refers to. Surfaces in the
                ``reason`` of the 1x downgraded alert so the operator
                can correlate the alert with the decision they're on.
        """
        if self.pipeline_id is None or self.phase is None:
            # Unbound timer: nothing to alert about yet.
            return

        idle = now - self.last_spawn_at
        double_secs = 2 * self._budget_secs

        # 2x boundary first: a long-paused tick that jumped straight
        # past 2x without seeing the 1x bucket should still emit both
        # alerts (each exactly once) so the operator sees the gradient,
        # but the 1x latch is set as a side effect so a subsequent
        # check at idle=70 min doesn't retroactively re-fire 1x.
        # Mirrors the wrapper emitter's ``ALERTED_AT_BUDGET=true``
        # carry-over (consensus_wrapper.py:543) for parity.
        if idle >= double_secs and not self._alerted_2x:
            # Fire the 1x first if it hasn't fired yet (parity with the
            # wrapper's "1x then 2x" emission order — operators reading
            # the SDLC skill timeline see both gradient steps even on a
            # paused tick).
            if not self._alerted_1x:
                # Even on a paused tick that lands past 2x with HITL
                # pending, the 1x downgrade still fires; the 2x branch
                # below is what gets suppressed.
                self._emit_threshold(
                    threshold_multiplier=1,
                    idle_secs=idle,
                    now=now,
                    pending_hitl_count=pending_hitl_count,
                    pending_hitl_ids=pending_hitl_ids,
                )
                self._alerted_1x = True
            # 2x branch.
            if pending_hitl_count > 0:
                # AC-R13: HITL-pending suppression of the 2x alert.
                # Still mark the latch so a follow-up tick (e.g. after
                # the HITL resolves) does not retroactively re-fire 2x.
                self._alerted_2x = True
                return
            self._emit_threshold(
                threshold_multiplier=2,
                idle_secs=idle,
                now=now,
                pending_hitl_count=0,
                pending_hitl_ids=None,
            )
            self._alerted_2x = True
            return
        if idle >= self._budget_secs and not self._alerted_1x:
            self._emit_threshold(
                threshold_multiplier=1,
                idle_secs=idle,
                now=now,
                pending_hitl_count=pending_hitl_count,
                pending_hitl_ids=pending_hitl_ids,
            )
            self._alerted_1x = True

    # -- internal ---------------------------------------------------------

    def _emit_threshold(
        self,
        *,
        threshold_multiplier: int,
        idle_secs: float,
        now: float,
        pending_hitl_count: int,
        pending_hitl_ids: list[str] | None,
    ) -> None:
        """Build the alert payload and invoke the injected emitter."""
        # Priority resolution:
        #   1x + no HITL    -> medium
        #   1x + HITL       -> low (AC-R13 downgrade)
        #   2x + no HITL    -> high
        #   (2x + HITL is suppressed upstream of this helper)
        if threshold_multiplier == 1:
            priority = "low" if pending_hitl_count > 0 else "medium"
        else:
            priority = "high"

        idle_min = int(idle_secs // 60)
        bucket_label = (
            f"{threshold_multiplier}x ({threshold_multiplier * self.budget_minutes} min)"
            if threshold_multiplier > 1
            else f"{self.budget_minutes} min"
        )
        summary = (
            f"phase '{self.phase}' has been idle for ~{idle_min} min "
            f"(crossed {bucket_label} budget) [{priority}]"
        )
        reason_parts = [
            (
                f"No BRC spawn for phase '{self.phase}' in {idle_min} min "
                f"(configured budget {self.budget_minutes} min; threshold "
                f"{threshold_multiplier}x)."
            ),
        ]
        ids_tuple: tuple[str, ...] = ()
        if pending_hitl_count > 0:
            ids = list(pending_hitl_ids or [])
            ids_tuple = tuple(ids)
            ids_render = ", ".join(ids) if ids else "?"
            reason_parts.append(
                f"{pending_hitl_count} pending HITL decision(s) "
                f"(ids: {ids_render}); priority downgraded to '{priority}' "
                "while the operator is in the loop."
            )
        reason = " ".join(reason_parts)
        # Snapshot per_role_last_action at emission so a later
        # mutation cannot edit the alert payload retroactively.
        per_role_state = dict(self.per_role_last_action)
        self.last_alert_at = now
        self._emit(
            anomaly=STUCK_PHASE_TRANSITION_ANOMALY,
            priority=priority,
            pipeline_id=self.pipeline_id,
            phase=self.phase,
            per_role_state=per_role_state,
            pending_hitl_ids=ids_tuple,
            threshold_multiplier=threshold_multiplier,
            summary=summary,
            reason=reason,
        )
