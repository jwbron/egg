"""OrchestratorEventLoop method bodies for the event_loop sub-package (#3447).

Method bodies extracted verbatim from the pre-split ``event_loop.py`` as
module-level functions taking ``self`` explicitly (decomposition-pattern.md
§c). The barrel binds these back onto the ``OrchestratorEventLoop`` class.

The module-global function seams the loop deliberately calls through the
package (``_derive_next_action`` — monkeypatched by the suite —
``event_identity``, ``compute_dedupe_key``, ``get_idle_budget_minutes``,
``_idle_budget_anomaly_name``) are reached via ``import event_loop as _pkg`` so
``setattr(event_loop, "_derive_next_action", ...)`` keeps intercepting the
loop's call. ``time`` is a plain ``import time`` so ``setattr(event_loop.time,
"time", ...)`` — which patches the shared ``time`` module object — is seen.
Non-patched constants / ``EventDecision`` / ``logger`` are value-imported.
"""

from __future__ import annotations

import threading  # noqa: F401 — used by start()/stop()
import time
from collections.abc import Iterable  # noqa: F401 — used in method annotations
from typing import Any

import event_loop as _pkg

from . import (
    AGENT_FREE_ACTIONS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    JOB_OUTCOME_ABNORMAL,
    JOB_OUTCOME_FATAL,
    JOB_OUTCOME_LEGITIMATE,
    JOB_OUTCOME_RATE_LIMITED,
    JOB_OUTCOME_SUCCESS,
    SPAWN_ACTIONS,
    EventDecision,
    logger,
)


def reconcile(self, live_dedupe_keys: Iterable[str]) -> None:
    """Seed the live-key set from live Job labels (restart path).

    A fresh loop holds no in-memory state; this rebuilds the set from the
    dedupe-key labels carried by still-running Jobs so a re-derived event
    a live pod already owns is not duplicated.
    """
    self._live_keys = {k for k in live_dedupe_keys if k}
    self.supervisor.reconcile(live_dedupe_keys)


def live_dedupe_keys(self) -> set[str]:
    """Return a snapshot of the tracked live dedupe keys."""
    return set(self._live_keys)


def _observe_jobs(self) -> None:
    """Reconcile live one-shot Jobs against their termination status (slice-3).

    For every live dedupe key, ask the injected ``job_status_view`` how the
    Job finished and drive the supervisor accordingly:

      * ``success``    → reset the streak; the key leaves the live set.
      * ``legitimate`` → leave the streak untouched (stale-event exit 0 /
        a cast NACK vote are explicit non-triggers); key leaves the set.
      * ``abnormal``   → increment the streak; the key leaves the live set
        so the next poll re-derives and (after backoff) respawns it.
      * ``running`` / unknown → still in flight; leave it.

    No view wired ⇒ no observation (slice-2 behavior preserved). Failures
    of the view are best-effort: a key is left live rather than risk a
    spurious abort streak from a transient status-read error.
    """
    if self._job_status_view is None:
        return
    for key in list(self._live_keys):
        try:
            outcome = self._job_status_view.outcome_for(key)
        except Exception as exc:  # noqa: BLE001 — observation is best-effort
            logger.warning(
                "event-loop: job-status observation failed",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                dedupe_key=key,
                error=str(exc),
            )
            continue
        action, role = self._key_meta.get(key, ("", ""))
        if outcome == JOB_OUTCOME_SUCCESS:
            self.supervisor.record_success(key, action=action, role=role)
            self._live_keys.discard(key)
            self._key_meta.pop(key, None)
        elif outcome == JOB_OUTCOME_LEGITIMATE:
            self.supervisor.record_legitimate_outcome(key, "legitimate")
            self._live_keys.discard(key)
            self._key_meta.pop(key, None)
        elif outcome == JOB_OUTCOME_FATAL:
            # #3373: non-retryable credential failure. Exhaust the key now
            # (record_fatal) and reap the terminated Job, exactly like the
            # abnormal branch — but skip the streak: a respawn would re-use
            # the same rejected credential. The key is left exhausted so the
            # next poll's is_exhausted guard blocks respawn until an operator
            # rotates the credential and restarts the phase (new dedupe key).
            self.supervisor.record_fatal(key, action, role, exit_detail=self._exit_detail(key))
            reaper = getattr(self._job_status_view, "reap_terminated", None)
            if reaper is not None:
                try:
                    reaper(key)
                except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                    logger.warning(
                        "event-loop: reap of fatal job failed",
                        pipeline_id=self.pipeline_id,
                        slice_id=self.slice_id,
                        dedupe_key=key,
                        error=str(exc),
                    )
            self._live_keys.discard(key)
            self._key_meta.pop(key, None)
        elif outcome == JOB_OUTCOME_RATE_LIMITED:
            # #3364 PR C: a TRANSIENT throttle / cap wall. Route to
            # ``record_rate_limited`` — NOT ``record_abort`` — so the throttle
            # NEVER touches the abnormal streak (AC-C1: it cannot trip the
            # fail-streak halt) and the respawn is PACED across the rolling cap
            # window (``ready_to_respawn`` honours the per-key rate-limit
            # backoff). Reap the terminated Job and drop the key from the live
            # set exactly like the abnormal branch, so the next poll re-derives
            # and respawns once the paced window elapses; ``_key_meta`` is kept
            # so the respawn re-labels the same arm.
            #
            # NO ``exit_detail`` is passed (unlike the abnormal/fatal branches):
            # the outcome is a JOB_OUTCOME_RATE_LIMITED, i.e. the pod already
            # classified its error as a transient throttle (it exited
            # EX_RATE_LIMITED), and the pod EXIT CODE ("exit_code=69") is not
            # classifiable error TEXT. Feeding it to the loop-guard's
            # non-throttle discriminator would mis-read a genuine cap wall as a
            # deterministic failure and halt it — the exact v1 regression. So a
            # bare production throttle carries no signature and never trips the
            # guard; it paces indefinitely (binding cq-1). The ``exit_detail``
            # parameter remains for a future enriched observer that can supply
            # real error text.
            self.supervisor.record_rate_limited(key, action, role)
            reaper = getattr(self._job_status_view, "reap_terminated", None)
            if reaper is not None:
                try:
                    reaper(key)
                except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                    logger.warning(
                        "event-loop: reap of rate-limited job failed",
                        pipeline_id=self.pipeline_id,
                        slice_id=self.slice_id,
                        dedupe_key=key,
                        error=str(exc),
                    )
            self._live_keys.discard(key)
        elif outcome == JOB_OUTCOME_ABNORMAL:
            # #3496: read the pod's exit detail BEFORE reaping (the reap
            # below deletes the Job, after which the exit code is gone).
            self.supervisor.record_abort(key, action, role, exit_detail=self._exit_detail(key))
            # Reap the terminated Job now that the abort is recorded. Its
            # FAILED status lingers for the ~600s TTL window; left in
            # place it would (a) be re-read next poll and re-increment the
            # streak against one dead pod, and (b) be adopted as "live" by
            # the respawn — both dead-end the bounded respawn and falsely
            # escalate a transient crash to AGENT_FAILED (#3181 re-review).
            # Best-effort: the spawner's live-only adoption filter is the
            # backstop if the delete fails.
            reaper = getattr(self._job_status_view, "reap_terminated", None)
            if reaper is not None:
                try:
                    reaper(key)
                except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                    logger.warning(
                        "event-loop: reap of terminated job failed",
                        pipeline_id=self.pipeline_id,
                        slice_id=self.slice_id,
                        dedupe_key=key,
                        error=str(exc),
                    )
            # Drop from the live set so the next poll re-derives and (once
            # the backoff window elapses) respawns. Keep ``_key_meta`` so a
            # respawn re-labels the same arm; the respawn refreshes it.
            self._live_keys.discard(key)


def _exit_detail(self, dedupe_key: str) -> str | None:
    """Read a short exit-detail string from the status view (#3496).

    Best-effort and optional on the view (``exit_detail_for``): a view
    without the method, or any read failure, yields ``None`` — the
    supervisor's history entry then carries the category alone.
    """
    probe = getattr(self._job_status_view, "exit_detail_for", None)
    if probe is None:
        return None
    try:
        return probe(dedupe_key)
    except Exception as exc:  # noqa: BLE001 — detail capture is best-effort
        logger.warning(
            "event-loop: exit-detail read failed",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            dedupe_key=dedupe_key,
            error=str(exc),
        )
        return None


def poll_once(self, roles: list[str]) -> list[EventDecision]:
    """Run one derivation→action pass over ``roles``.

    Observes finished Jobs first (slice-3 supervision), then derives the
    next action per role. Convergence-stall check runs after observation
    so the tracker's bus timestamp is up to date. Returns a decision per
    role (role order). Never raises: a per-role failure is logged and
    recorded as a no-op so one bad role can't wedge the loop.
    """
    self._observe_jobs()
    try:
        self._check_convergence_stall()
    except Exception as exc:  # noqa: BLE001 — never wedge the loop
        logger.warning(
            "event-loop convergence-stall check failed",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            error=str(exc),
        )
    decisions: list[EventDecision] = []
    for role in roles:
        try:
            decisions.append(self._handle_role(role))
        except Exception as exc:  # noqa: BLE001 — isolate per-role failures
            logger.warning(
                "event-loop poll failed for role",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=role,
                error=str(exc),
            )
            decisions.append(EventDecision(role=role, action="error"))
    # #3496: judge the all-arms-exhausted wedge from this tick's decisions
    # (never wedge the loop on the check itself).
    try:
        self._check_arms_exhausted(decisions)
    except Exception as exc:  # noqa: BLE001 — never wedge the loop
        logger.warning(
            "event-loop arms-exhausted check failed",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            error=str(exc),
        )
    # #3548: judge the all-arms-parked wedge (the no-op-park sibling of the
    # exhausted wedge) from the same tick's decisions.
    try:
        self._check_arms_parked(decisions)
    except Exception as exc:  # noqa: BLE001 — never wedge the loop
        logger.warning(
            "event-loop arms-parked check failed",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            error=str(exc),
        )
    # Publish the post-spawn live-Job role set to the health monitor so
    # its orchestrator-mode tripwires scope to roles that actually have a
    # pod this tick (newly spawned keys above are included).
    self._publish_active_roles()
    return decisions


def _check_arms_exhausted(self, decisions: list[EventDecision]) -> None:
    """Detect the exhausted-key livelock and escalate once per episode (#3496).

    The wedge shape (the #3496 incident): every arm the tracker currently
    derives a spawn action for is blocked on an exhausted dedupe key, no
    one-shot Job is in flight, and no agent-free (confirm/complete) side
    effect ran this tick. Exhaustion is terminal — only ``record_success``
    (unreachable: the key can no longer spawn) or an operator reset clears
    it — so once this condition holds the loop spins silently forever,
    re-logging "spawn blocked" every poll while the pipeline reports
    ``running``. Roles deriving ``wait`` don't break the wedge: they are
    waiting on exactly the arms that can no longer spawn.

    Fires the ``arms_exhausted_notifier`` (production: OVERSEER_ALERT +
    HITL decision) with the supervisor's per-key exhaustion report, once
    per episode via a sticky latch. The latch clears when the condition
    stops holding — a spawn happened, a fresh key was derived, or an
    operator reset (:meth:`reset_exhausted_arms`) cleared the exhausted
    set — so a wedge that re-forms after a failed retry re-escalates.
    """
    spawn_decisions = [d for d in decisions if d.action in SPAWN_ACTIONS]
    wedged = (
        bool(spawn_decisions)
        and all(d.blocked == "exhausted" for d in spawn_decisions)
        and not self._live_keys
        and not any(d.agent_free for d in decisions)
    )
    if not wedged:
        if self._arms_exhausted_alerted:
            # Wedged→clear transition (#3496 review): this loop escalated a
            # wedge that has since recovered by another route — a fresh key
            # was derived, a spawn succeeded, or an operator resolved a
            # different decision that re-keyed the arms. Clear the latch
            # first (so the pipeline-wide withdrawal guard sees only the
            # *other* slices' latches), then auto-withdraw the now-stale
            # HITL. Fires at most once per episode — the transition edge.
            self._arms_exhausted_alerted = False
            self._notify_arms_exhausted_cleared()
        return
    if self._arms_exhausted_alerted:
        return
    self._arms_exhausted_alerted = True
    # Scope the report to the keys that are *currently* blocking a
    # derivable spawn arm. ``exhausted_report()`` covers every key in the
    # supervisor's exhausted set, which can include stale keys from
    # superseded BRC rounds (a re-propose re-keys the reviewer's arm but
    # nothing retires the old exhausted key) — surfacing those in the
    # operator-facing detail would list arms that are not the blockers.
    blocked_keys = {d.dedupe_key for d in spawn_decisions if d.dedupe_key}
    report = [e for e in self.supervisor.exhausted_report() if e["dedupe_key"] in blocked_keys]
    logger.warning(
        "event-loop: all derivable spawn arms are exhausted — the slice "
        "cannot advance without operator intervention (blocked arms: %s)",
        ", ".join(f"{d.role}/{d.action}" for d in spawn_decisions),
        pipeline_id=self.pipeline_id,
        slice_id=self.slice_id,
        phase=self.phase,
    )
    if self._arms_exhausted_notifier is None:
        return
    self._arms_exhausted_notifier(
        report=report,
        blocked_arms=[(d.role, d.action) for d in spawn_decisions],
    )


def arms_exhausted_escalated(self) -> bool:
    """True while this loop is inside an escalated arms-exhausted episode.

    The pipeline-wide withdrawal guard reads this across the live-loop
    registry: the shared arms-exhausted HITL must not be auto-withdrawn
    while any slice of the pipeline is still wedged (#3496 review).
    """
    return self._arms_exhausted_alerted


def _notify_arms_exhausted_cleared(self) -> None:
    """Fire the wedge-cleared notifier best-effort (#3496 review).

    Isolated so a withdrawal-side failure (state-store read, lock, save)
    can never propagate into ``poll_once`` and wedge the loop — the exact
    failure mode the escalation exists to surface.
    """
    if self._arms_exhausted_cleared_notifier is None:
        return
    try:
        self._arms_exhausted_cleared_notifier()
    except Exception:  # noqa: BLE001 — withdrawal must never wedge the loop
        logger.warning(
            "event-loop: arms-exhausted cleared notifier raised; ignoring",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            exc_info=True,
        )


def reset_exhausted_arms(self) -> list[str]:
    """Clear every exhausted key so blocked arms respawn (#3496).

    The in-band recovery surface behind the arms-exhausted HITL's "Retry
    arms" resolution (reached via the live-loop registry): gives each
    exhausted key a fresh spawn budget and re-arms the wedge latch so a
    retry that fails all the way back to exhaustion re-escalates rather
    than being swallowed by the spent latch. Returns the cleared keys.

    Cross-thread note: this runs on the Flask resolve-route thread while
    the event-loop daemon thread mutates the same supervisor dicts/sets
    in ``poll_once`` — a new sharing pattern on a previously
    single-threaded structure (#3496 review). It is lock-free by design
    and safe under CPython: every mutation here is an atomic dict/set op
    under the GIL, and ``reset_exhausted`` snapshots the exhausted set
    (``sorted()``) before iterating so a concurrent add/discard cannot
    invalidate the iterator. Worst case a key the loop re-exhausts on the
    same tick is cleared and re-exhausts on the next — benign.
    """
    cleared = self.supervisor.reset_exhausted()
    self._arms_exhausted_alerted = False
    if cleared:
        logger.info(
            "event-loop: exhausted keys reset by operator — blocked arms "
            "will respawn on the next poll",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            cleared=len(cleared),
        )
    return cleared


def _check_arms_parked(self, decisions: list[EventDecision]) -> None:
    """Detect the all-arms-parked wedge and escalate once per episode (#3548).

    The no-op-park sibling of :meth:`_check_arms_exhausted`. The wedge shape
    (the #3548 incident): every arm the tracker currently derives a spawn
    action for is blocked — at least one on a no-op park (#3425), the rest
    parked or exhausted — no one-shot Job is in flight, and no agent-free
    side effect ran this tick. Unlike exhaustion a park self-releases, but
    only for a single probe spawn per fingerprint change or per
    ``SUPERVISION_NOOP_PARK_RETRY_SECONDS`` heartbeat; a round that is one
    verdict away from convergence otherwise sits silent for the full
    heartbeat window with ``pending_decisions`` empty — exactly the
    zero-operator-signal stall the incident showed.

    Fires the ``arms_parked_notifier`` (production: OVERSEER_ALERT + HITL
    decision) with the supervisor's per-key park report, once per episode
    via a sticky latch. The latch clears when the condition stops holding —
    a probe spawn released, a fresh key was derived, or an operator reset
    (:meth:`reset_parked_arms`) cleared the parks — so a wedge that
    re-forms after a no-op probe re-escalates. The heartbeat probe cycle
    therefore re-alerts at most once per ``SUPERVISION_NOOP_PARK_RETRY_
    SECONDS`` while the wedge persists, which is the intended "still
    wedged" signal, not churn.

    A tick where EVERY blocked arm is exhausted belongs to
    :meth:`_check_arms_exhausted`; this detector requires at least one
    parked arm, so mixed parked+exhausted rounds (which the exhausted
    detector's ``all(== "exhausted")`` predicate cannot see) are covered
    here rather than falling between the two.
    """
    spawn_decisions = [d for d in decisions if d.action in SPAWN_ACTIONS]
    wedged = (
        bool(spawn_decisions)
        and all(d.blocked in ("parked", "exhausted") for d in spawn_decisions)
        and any(d.blocked == "parked" for d in spawn_decisions)
        and not self._live_keys
        and not any(d.agent_free for d in decisions)
    )
    if not wedged:
        if self._arms_parked_alerted:
            self._arms_parked_alerted = False
            self._notify_arms_parked_cleared()
        return
    if self._arms_parked_alerted:
        return
    self._arms_parked_alerted = True
    # Scope the report to the keys currently blocking a derivable spawn arm
    # (same rationale as the exhausted check: stale keys from superseded BRC
    # rounds must not be listed as blockers).
    blocked_keys = {d.dedupe_key for d in spawn_decisions if d.dedupe_key}
    report = [e for e in self.supervisor.noop_park_report() if e["dedupe_key"] in blocked_keys]
    exhausted = [e for e in self.supervisor.exhausted_report() if e["dedupe_key"] in blocked_keys]
    logger.warning(
        "event-loop: all derivable spawn arms are no-op-parked (or exhausted) "
        "— the slice cannot advance before the park retry heartbeat "
        "(blocked arms: %s)",
        ", ".join(f"{d.role}/{d.action}" for d in spawn_decisions),
        pipeline_id=self.pipeline_id,
        slice_id=self.slice_id,
        phase=self.phase,
    )
    if self._arms_parked_notifier is None:
        return
    self._arms_parked_notifier(
        report=report,
        exhausted_report=exhausted,
        blocked_arms=[(d.role, d.action) for d in spawn_decisions],
    )


def arms_parked_escalated(self) -> bool:
    """True while this loop is inside an escalated all-arms-parked episode.

    Read across the live-loop registry by the pipeline-wide withdrawal
    guard, mirroring :meth:`arms_exhausted_escalated` (#3548).
    """
    return self._arms_parked_alerted


def _notify_arms_parked_cleared(self) -> None:
    """Fire the parked-wedge-cleared notifier best-effort (#3548).

    Isolated so a withdrawal-side failure can never propagate into
    ``poll_once`` and wedge the loop — same posture as
    :meth:`_notify_arms_exhausted_cleared`.
    """
    if self._arms_parked_cleared_notifier is None:
        return
    try:
        self._arms_parked_cleared_notifier()
    except Exception:  # noqa: BLE001 — withdrawal must never wedge the loop
        logger.warning(
            "event-loop: arms-parked cleared notifier raised; ignoring",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            exc_info=True,
        )


def reset_parked_arms(self) -> list[str]:
    """Clear every no-op-parked key so blocked arms respawn (#3548).

    The in-band recovery surface behind the all-arms-parked HITL's "Retry
    arms" resolution — the park twin of :meth:`reset_exhausted_arms`, with
    the same lock-free cross-thread reasoning (atomic dict/set ops under
    the GIL; ``reset_noop_parks`` snapshots before iterating). Returns the
    cleared keys.
    """
    cleared = self.supervisor.reset_noop_parks()
    self._arms_parked_alerted = False
    if cleared:
        logger.info(
            "event-loop: no-op-parked keys reset by operator — blocked arms "
            "will respawn on the next poll",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            cleared=len(cleared),
        )
    return cleared


def invalidate_role_arms(self, role: str) -> list[str]:
    """Drop all in-memory arm state for ``role`` so its next event derives fresh (#3548).

    The ``restart_agent`` companion: the route deletes the role's Job and
    resets its consensus state, but the re-derived event carries the same
    identity — and therefore the same dedupe key — as before the restart.
    Loop-local state then silently blocks the respawn twice over:

    * the key is still in ``_live_keys`` (the route deleted the Job by
      label, and ``_observe_jobs`` maps a missing Job to ``running``, so
      the key never leaves the live set) — the dedupe branch eats every
      re-derivation;
    * the supervisor's exhaustion / no-op-park latches for the key survive
      the restart untouched.

    This drops the role's keys from ``_live_keys`` / ``_key_meta`` and
    retires their supervisor state, so the next poll re-derives the same
    key as *fresh* and actually spawns — making the route's "respawn
    delegated to event loop" claim true. Same lock-free cross-thread
    reasoning as :meth:`reset_exhausted_arms` (atomic dict/set ops under
    the GIL, snapshot before iterating). Returns the invalidated keys.

    Key discovery must NOT rely on ``_key_meta`` alone (#3548 review): a
    no-op-parked key has already been popped from ``_key_meta`` (and
    ``_live_keys``) by ``_observe_jobs`` on the clean completion that
    parked it, and the park early-return in :meth:`_handle_role` never
    re-adds it. That is precisely the incident shape — every spawn arm
    no-op-parked — so a ``_key_meta``-only scan would find nothing and the
    park latch would survive, re-parking the re-derived key on the next
    poll and making ``restart_agent`` a silent no-op. So union the
    ``_key_meta`` keys with the supervisor's own parked/exhausted keys for
    the role (from :meth:`noop_park_report` / :meth:`exhausted_report`,
    which carry the role via ``_last_action``), mirroring how
    :meth:`reset_parked_arms` / :meth:`reset_exhausted_arms` reach into the
    supervisor's ``_noop_streaks`` / ``_exhausted`` directly.
    """
    keys = {key for key, (_action, key_role) in list(self._key_meta.items()) if key_role == role}
    keys.update(e["dedupe_key"] for e in self.supervisor.noop_park_report() if e["role"] == role)
    keys.update(e["dedupe_key"] for e in self.supervisor.exhausted_report() if e["role"] == role)
    invalidated = sorted(keys)
    for key in invalidated:
        self._live_keys.discard(key)
        self._key_meta.pop(key, None)
        self.supervisor.retire(key)
    if invalidated:
        logger.info(
            "event-loop: invalidated arms for restarted role — next poll derives them fresh",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            role=role,
            invalidated=len(invalidated),
        )
    return invalidated


def _publish_active_roles(self) -> None:
    """Push the set of roles with a live one-shot Job to the monitor.

    Derives the active-role set from ``_live_keys`` (the in-flight
    dedupe keys) via ``_key_meta`` (``{key: (action, role)}``) and hands
    it to ``active_roles_notifier`` — wired in production to the health
    monitor's ``set_active_roles``.  Roles absent from this set are
    treated as legitimately idle in orchestrator mode.

    Reconciled keys (restart path) populate ``_live_keys`` without a
    ``_key_meta`` entry.  But ``poll_once`` runs the ``_handle_role`` pass
    (which labels the key via ``_key_meta.setdefault`` on its dedupe
    early-return — it no longer waits for a fresh spawn) *before* calling
    this method, so an adopted/reconciled role is labeled and therefore
    published on the very first published tick after ``reconcile()``; it is
    never actually absent from a real publish.  Were the label ever missing
    it would only suppress (never false-alert) the role's tripwires.
    Best-effort: a notifier failure must not wedge the loop.
    """
    notifier = self._active_roles_notifier
    if notifier is None:
        return
    active_roles = {self._key_meta[key][1] for key in self._live_keys if key in self._key_meta}
    try:
        notifier(active_roles)
    except Exception as exc:  # noqa: BLE001 — publishing must not wedge the loop
        logger.warning(
            "event-loop: active-roles publish failed",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            error=str(exc),
        )


def _handle_role(self, role: str) -> EventDecision:
    action, payload, _reason = _pkg._derive_next_action(self.tracker, role)

    if action in AGENT_FREE_ACTIONS:
        # confirm/complete: orchestrator-side, never a pod.
        if self.agent_free_handler is not None:
            self.agent_free_handler(action=action, role=role, payload=payload)
        return EventDecision(role=role, action=action, agent_free=True)

    if action not in SPAWN_ACTIONS:
        # wait / unknown — nothing to spawn.
        return EventDecision(role=role, action=action)

    # #3633: never spawn once the loop has been stopped. ``run()`` checks the
    # stop event between ticks, but ``stop()`` is also called from outside the
    # loop's own thread — the cancel route stops every live loop for a
    # pipeline synchronously — so a stop that lands mid-tick would otherwise
    # still get one final cohort of one-shot Jobs out the door. Re-checking
    # immediately before the spawn decision closes that window.
    if self._stop.is_set():
        logger.info(
            "event-loop: spawn blocked, loop is stopping",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            role=role,
            action=action,
        )
        return EventDecision(role=role, action=action, spawned=False, blocked="stopped")

    identity = _pkg.event_identity(action, payload)
    key = _pkg.compute_dedupe_key(
        self.pipeline_id, self.slice_id, self.phase, role, action, identity
    )

    # Slice-3: Respect exhaustion — do NOT re-spawn exhausted keys
    if self.supervisor.is_exhausted(key):
        logger.info(
            "event-loop: spawn blocked due to exhausted key",
            pipeline_id=self.pipeline_id,
            role=role,
            action=action,
            dedupe_key=key,
        )
        return EventDecision(
            role=role, action=action, dedupe_key=key, spawned=False, blocked="exhausted"
        )

    # Dedupe: an in-flight (or reconciled) Job already owns this event.
    if key in self._live_keys:
        # Label the key even on the dedupe path so a key first seen via
        # ``reconcile()`` (which seeds ``_live_keys`` without ``_key_meta``)
        # is picked up by ``_publish_active_roles`` on this tick rather than
        # staying silently unlabeled — and thus tripwire-suppressed — for the
        # pod's lifetime. ``setdefault`` never clobbers an existing label.
        self._key_meta.setdefault(key, (action, role))
        return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

    # Slice-3: throttle respawn after an abnormal termination until the
    # backoff window (streak*factor capped) has elapsed. A fresh key (no
    # recorded abort) is always ready, so the common path is unchanged.
    if not self.supervisor.ready_to_respawn(key):
        logger.debug(
            "event-loop: respawn backing off",
            pipeline_id=self.pipeline_id,
            role=role,
            action=action,
            dedupe_key=key,
            backoff_seconds=self.supervisor.backoff_seconds(key),
        )
        return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

    # #3425: park after repeated successful no-ops. A clean exit that
    # produced zero BRC progress re-derives this identical key next poll;
    # after SUPERVISION_NOOP_STREAK_PARK such completions the slice is
    # wedged on something a respawn cannot resolve (typically an
    # unresolved operator HITL cq-N). The supervisor self-releases the
    # park on a contract-decision change or the retry heartbeat.
    if self.supervisor.noop_parked(key):
        logger.debug(
            "event-loop: spawn parked after successful no-op streak",
            pipeline_id=self.pipeline_id,
            role=role,
            action=action,
            dedupe_key=key,
        )
        return EventDecision(
            role=role, action=action, dedupe_key=key, spawned=False, blocked="parked"
        )

    # #3337: serialize same-role producers. We are about to spawn a *fresh*
    # event for ``role``; any other live key for this same role belongs to
    # an older event working a now-stale tracker state, and its pod shares
    # this role's worktree (the draft artifact). Reap it first so at most
    # one one-shot Job per (role, slice) is ever live and concurrent
    # siblings can't corrupt the shared draft.
    self._reap_superseded_siblings(role, keep_key=key)

    requested_at = self.clock()
    # #3537: if this spawn is the probe granted by a fingerprint-change park
    # release, carry the release delta (resolved cq-N ids / BRC movement) so
    # the spawner can surface it in the event prompt - the respawned pod's
    # prompt is otherwise byte-identical to the one that parked, and a
    # warm-resumed session just replays its cached "still blocked" plan.
    # Passed only when present so spawner fakes without the kwarg keep
    # working on the common path.
    release_context = self.supervisor.consume_noop_release_context(key)
    spawn_kwargs: dict[str, Any] = {}
    if release_context:
        spawn_kwargs["release_context"] = release_context
    spawn_result = self.spawner.spawn_event(
        role=role, action=action, dedupe_key=key, payload=payload, **spawn_kwargs
    )
    dispatched_at = self.clock()
    self._live_keys.add(key)
    # Record the arm labels for the now-live key so supervision can
    # attribute an abnormal termination to the right (action, role) — this
    # holds for adopted keys too, so it must precede the adoption return.
    self._key_meta[key] = (action, role)

    # Cross-process adoption: the spawner returns None when an already-live
    # Job owns this dedupe key (the restart/race backstop in
    # spawn_event_job). The key is now tracked either way, but no *new* pod
    # was created, so this is not a fresh spawn — record spawned=False and
    # emit no timing, so the slice-4 p50<60s budget (which reads `timing`)
    # never counts an adoption as a spawn→invoke latency sample.
    #
    # Note: any release_context consumed above is forfeited on this branch —
    # it was popped before the spawn but the adopted, already-running pod
    # never receives it. This is acceptable: the delta is a best-effort
    # accelerator, and a still-blocked arm re-derives it on the next
    # fingerprint move (with the retry-heartbeat backstop underneath).
    if spawn_result is None:
        return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

    timing = {
        "spawn_requested_at": requested_at,
        "spawn_dispatch_seconds": round(dispatched_at - requested_at, 6),
    }
    # Structured spawn→invoke timing field (#3064 slice-2; slice-4 reads
    # it for the p50<60s budget).
    logger.info(
        "event-loop spawn dispatched",
        event_type="event_loop_spawn",
        pipeline_id=self.pipeline_id,
        slice_id=self.slice_id,
        phase=self.phase,
        role=role,
        action=action,
        dedupe_key=key,
        spawn_requested_at=requested_at,
    )
    return EventDecision(role=role, action=action, dedupe_key=key, spawned=True, timing=timing)


def _reap_superseded_siblings(self, role: str, *, keep_key: str) -> None:
    """Tear down any live one-shot Job for ``role`` other than ``keep_key`` (#3337).

    Every event for a ``(role, slice)`` re-attaches to one shared worktree
    (the draft artifact is keyed by role, not by event), so two concurrent
    same-role pods race on that draft. When the loop is about to spawn a
    fresh event for ``role`` — which reflects the fullest tracker state —
    any *other* live key for the same role is a superseded older event; we
    reap its Job and drop it from the live set so the invariant "at most one
    live Job per (role, slice)" holds.

    Reaping is best-effort: when no Job-status view is wired (pure slice-2
    mode / unit tests) there is no cluster Job to delete, but the in-memory
    live set is still pruned so the serialization invariant holds. A reap
    failure is logged and swallowed — the spawner's live-only adoption
    filter is the cross-process backstop.

    Restart-boundary limitation: this matches superseded siblings via the
    in-memory ``_key_meta`` role label, which ``reconcile()`` does *not*
    seed for keys adopted after an orchestrator restart (see
    ``reconcile`` / ``_publish_active_roles``). So a *stale* same-role key
    adopted across a restart — one whose identity no longer matches the
    freshly-derived event — is unlabeled and won't be reaped here. That
    re-opens the #3337 two-live-pods window, but only across the narrow
    restart boundary (a fresh spawn whose superseded sibling's Job deletion
    had not yet propagated when the orchestrator went down). The spawner's
    live-only adoption filter still prevents a *duplicate* Job for the new
    key, and warm-resume's dirty-state clean covers the handoff, so the
    steady-state invariant is fully held; only this restart-race tail is
    uncovered. Deriving the same-role live set from the spawner's live-Job
    labels (cross-process truth) would close it, at the cost of a cluster
    query on the hot fresh-spawn path.
    """
    superseded = [
        k
        for k in list(self._live_keys)
        if k != keep_key and self._key_meta.get(k, (None, None))[1] == role
    ]
    if not superseded:
        return
    reaper = getattr(self._job_status_view, "reap", None) if self._job_status_view else None
    for k in superseded:
        if reaper is not None:
            try:
                reaper(k)
            except Exception as exc:  # noqa: BLE001 — reaping is best-effort
                logger.warning(
                    "event-loop: reap of superseded same-role sibling failed",
                    pipeline_id=self.pipeline_id,
                    slice_id=self.slice_id,
                    role=role,
                    superseded_key=k,
                    error=str(exc),
                )
        self._live_keys.discard(k)
        self._key_meta.pop(k, None)
        # The superseded event will never be re-derived (its tracker state
        # is stale), so retire its supervision state — otherwise a leftover
        # streak/exhaustion latch for the dead key lingers for the process
        # lifetime. ``retire`` is the "this key is done, forget it"
        # primitive (clears streaks + latches + exhaustion + no-op park);
        # ``record_success`` would instead count toward the #3425 no-op
        # streak.
        self.supervisor.retire(k)
        logger.info(
            "event-loop: reaped superseded same-role sibling",
            event_type="event_loop_supersede_reap",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            phase=self.phase,
            role=role,
            superseded_key=k,
            kept_key=keep_key,
        )


def _check_convergence_stall(self) -> None:
    """Judge convergence stall from tracker timestamps.

    Re-homed idle-budget check: for each role whose derived actionable
    event (propose|ack|nack) has been pending longer than
    ``EGG_BRC_IDLE_BUDGET_MIN`` without BRC-bus activity, raises the
    same ``stuck-phase-transition`` anomaly the in-pod
    ``check_idle_budget`` raises today (``consensus_wrapper.py:660-700``).

    ``confirm``/``complete``/``wait`` roles are never stalled — they're
    making progress (agent-free side effects) or waiting on nothing.
    A role with an in-flight Job (key in ``_live_keys``) is also not
    stalled — the pod is handling the event.

    The anomaly fires exactly once per stall episode per role (sticky
    latch cleared when the BRC bus moves or the role's action changes).
    Dormant when ``_convergence_stall_notifier`` is ``None``.
    """
    if self._convergence_stall_notifier is None:
        return

    budget_min = _pkg.get_idle_budget_minutes()
    budget_sec = budget_min * 60
    now = time.time()

    # Latest BRC-bus activity from the tracker (proposals + ACK/NACK
    # timestamps).  ``None`` means no bus activity ever — in that case
    # treat as "bus just started" so the first poll doesn't false-alert.
    bus_ts = self.tracker.get_latest_progress_timestamp()
    bus_timestamp: float = bus_ts.timestamp() if bus_ts is not None else now

    # If the bus has moved within the budget window, reset ALL per-role
    # stall state — the pipeline is clearly alive.
    if now - bus_timestamp < budget_sec:
        if self._stall_first_seen or self._stall_alerted:
            logger.debug(
                "Convergence-stall: BRC bus active %.0fs ago — resetting stall state",
                now - bus_timestamp,
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
            )
        self._stall_first_seen.clear()
        self._stall_alerted.clear()

    for role in self._roles:
        try:
            action, _payload, _reason = _pkg._derive_next_action(self.tracker, role)
        except Exception as exc:  # noqa: BLE001 — per-role isolation
            logger.debug(
                "Convergence-stall: derivation failed for role",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=role,
                error=str(exc),
            )
            continue

        # Agent-free (confirm/complete) and wait are never stalled.
        if action in AGENT_FREE_ACTIONS or action == "wait" or action not in SPAWN_ACTIONS:
            self._stall_first_seen.pop(role, None)
            self._stall_alerted.pop(role, None)
            continue

        # If there's a live (in-flight) Job for this role, the event
        # IS being handled — not stalled.
        identity = _pkg.event_identity(action, _payload)
        key = _pkg.compute_dedupe_key(
            self.pipeline_id, self.slice_id, self.phase, role, action, identity
        )
        if key in self._live_keys:
            self._stall_first_seen.pop(role, None)
            continue

        # NB: the bus-moved reset is handled by the all-roles clear at the
        # top of this method (``now - bus_timestamp < budget_sec`` empties
        # both per-role maps), so no per-role bus-activity check is needed
        # here — by this point the bus is quiet beyond the budget window.

        # First time we observe this role with a pending actionable
        # event — seed the stall window from the last BRC-bus movement
        # (when the event effectively became pending), NOT from when this
        # loop first observed it. Observation-based seeding would delay
        # the alert by up to a full budget window on a freshly-started or
        # restarted loop versus the in-pod ``check_idle_budget``, whose
        # ``idle = now - LAST_PROGRESS`` is bus-timestamp-relative
        # (#3064 review NB3). We only reach here when the bus has been
        # quiet beyond the budget (the all-roles reset above), so
        # ``bus_timestamp`` is the correct pending-since anchor.
        if role not in self._stall_first_seen:
            self._stall_first_seen[role] = bus_timestamp

        elapsed = now - self._stall_first_seen[role]
        if elapsed > budget_sec and not self._stall_alerted.get(role, False):
            self._stall_alerted[role] = True
            logger.warning(
                "Convergence-stall detected: role=%s actionable event '%s' "
                "pending %ds (budget %d min=%ds); raising stuck-phase-transition anomaly",
                role,
                action,
                int(elapsed),
                budget_min,
                budget_sec,
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                phase=self.phase,
            )
            self._convergence_stall_notifier(
                anomaly=_pkg._idle_budget_anomaly_name(),
                priority="high",
                summary=(
                    f"orchestrator convergence stall: {role} {action} "
                    f"pending {int(elapsed)}s (budget {budget_min}m)"
                ),
                detail=(
                    f"Event-loop for pipeline={self.pipeline_id} "
                    f"slice={self.slice_id} phase={self.phase} has "
                    f"derived action={action} for role={role} but the "
                    f"actionable event has been pending for {int(elapsed)}s "
                    f"without BRC-bus progress (budget={budget_min}m). "
                    f"No in-flight Job exists for this event."
                ),
            )


def run(self) -> None:
    """Poll ``self._roles`` until stopped or consensus completes.

    Sleeps one poll interval BEFORE the first poll so the tracker / pods
    settle (and so a freshly-started loop never races a synchronous
    caller). Stops on :meth:`stop` or when the slice has fully converged.
    """
    interval = self.poll_interval or DEFAULT_POLL_INTERVAL_SECONDS
    try:
        while not self._stop.wait(interval):
            self.poll_once(self._roles)
            if self._is_complete():
                logger.info(
                    "event loop: consensus complete, stopping",
                    pipeline_id=self.pipeline_id,
                    slice_id=self.slice_id,
                )
                break
    finally:
        # #3496: drop the registry entry when the loop exits naturally
        # (consensus complete) — ``stop()`` also unregisters, but the
        # natural-completion path never goes through it.
        _pkg._unregister_live_loop(self)


def _is_complete(self) -> bool:
    try:
        return bool(self.tracker.evaluate().get("is_complete", False))
    except Exception:  # noqa: BLE001 — completion check is best-effort
        return False


def start(self) -> threading.Thread:
    """Start the loop on a daemon thread and return it."""
    if self._thread is not None and self._thread.is_alive():
        return self._thread
    self._stop.clear()
    # #3496: make the loop reachable from the decision-resolution route
    # (same process) so the arms-exhausted HITL's "Retry arms" can clear
    # the supervisor's exhausted keys in-band.
    _pkg._register_live_loop(self)
    thread = threading.Thread(
        target=self.run,
        name=f"event-loop-{self.pipeline_id}-{self.slice_id or 'pipeline'}",
        daemon=True,
    )
    self._thread = thread
    thread.start()
    return thread


def stop(self, *, join_timeout: float | None = 5.0) -> None:
    """Signal the loop to stop and (best-effort) join the thread."""
    self._stop.set()
    _pkg._unregister_live_loop(self)
    thread = self._thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout)
