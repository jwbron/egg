"""Orchestrator-owned BRC event loop (#3064 slice-2, TASK-2-1).

Under ``EGG_EVENT_LOOP_OWNER=orchestrator`` the orchestrator — not a
long-lived in-pod wait-loop — owns the BRC event loop. For every role it
consumes the logic backing ``routes.consensus._derive_next_action``
IN-PROCESS and maps the derived verb onto a lifecycle action:

* ``propose | ack | nack`` → request a one-shot Job via an injectable
  spawner (the pod handles exactly one event and exits — the slice-1
  wrapper arm).
* ``confirm | complete``   → executed orchestrator-side, agent-free, with
  **no pod** (mirrors the wrapper's ``egg-orch consensus confirmed`` arm).
* ``wait``                 → nothing.

Spawns are deduped on ``compute_dedupe_key`` =
``sha256(pipeline, slice, phase, role, action, identity)`` (full hex), where
the identity is ``proposal_commit_sha`` for the review verbs and the target
version + open-NACK set for proposes. The loop keeps an in-memory live-key
set; ``reconcile`` seeds it from live Job labels so a repeated poll AND a
simulated orchestrator restart never double-spawn. No spawn bookkeeping is
persisted — the tracker plus live-Job labels are the only sources of truth,
so a restart is stateless by construction.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover - logging shim parity with siblings
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)

# Shared #3138 failure-streak policy (backoff factor/cap, warn/alert
# thresholds, anomaly name). Imported as the SAME source of truth the wrapper
# template reads (#3064 slice-3, TASK-3-1) so the orchestrator-side supervisor
# and the in-pod wrapper never fork their backoff/escalation constants. The
# dual-path import mirrors ``_derive_next_action`` below: the orchestrator may
# run with the repo root on ``sys.path`` or only ``orchestrator/`` itself.
try:
    import supervision_policy
except ImportError:  # pragma: no cover - import-path parity with siblings
    from orchestrator import supervision_policy  # type: ignore[no-redef]


logger = get_logger("orchestrator.event_loop")

# Verb partitioning — the single source of truth for the verb→lifecycle
# mapping the loop enforces. ``confirm``/``complete`` run orchestrator-side
# with no pod; ``wait`` (and any unknown verb) is a no-op.
SPAWN_ACTIONS: frozenset[str] = frozenset({"propose", "ack", "nack"})
AGENT_FREE_ACTIONS: frozenset[str] = frozenset({"confirm", "complete"})

# Poll cadence (#3064 slice-2: "poll interval env-tunable (default 5s)").
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


def get_event_loop_poll_interval() -> float:
    """Return the event-loop poll cadence in seconds (default 5.0).

    Reads ``EGG_EVENT_LOOP_POLL_INTERVAL_SECONDS``. Malformed or
    non-positive values fall back to the default with a warning — the loop
    must keep polling on a sane cadence rather than busy-spin or stall.
    """
    raw = os.environ.get("EGG_EVENT_LOOP_POLL_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "EGG_EVENT_LOOP_POLL_INTERVAL_SECONDS=%r is not a number; falling back to %.1fs",
            raw,
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
        return DEFAULT_POLL_INTERVAL_SECONDS
    if val <= 0:
        logger.warning(
            "EGG_EVENT_LOOP_POLL_INTERVAL_SECONDS=%.3f must be > 0; falling back to %.1fs",
            val,
            DEFAULT_POLL_INTERVAL_SECONDS,
        )
        return DEFAULT_POLL_INTERVAL_SECONDS
    return val


def compute_dedupe_key(
    pipeline_id: str,
    slice_id: str | None,
    phase: str | None,
    role: str,
    action: str,
    identity: str,
) -> str:
    """Compute the spawn dedupe key for one derived event.

    ``sha256(pipeline, slice, phase, role, action, identity)`` over a
    NUL-joined tuple (NUL can't appear in any component, so the join is
    unambiguous), returned as the full 64-char hex digest. Deterministic
    across orchestrator restarts: identical inputs always yield the
    identical key, which is what makes live-Job reconciliation able to
    recognise an in-flight event after a restart. Flipping ANY field
    changes the digest, so two distinct events can never collide.
    """
    payload = "\x00".join(
        (
            pipeline_id,
            slice_id or "",
            phase or "",
            role,
            action,
            identity,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_identity(action: str, payload: dict[str, Any] | None) -> str:
    """Derive the per-event identity string folded into the dedupe key.

    * review verbs (``ack``/``nack``) — the set of producer proposals under
      review keyed by ``proposal_commit_sha`` (the reviewer-side dedupe
      identity, ``routes/consensus.py``). A re-proposed commit changes the
      sha, which changes the key, which lets a fresh review spawn.
    * ``propose`` — the producer's target proposal version plus the open
      NACK set against it (read from the payload's ``current_version`` /
      ``unresolved_nacks`` / barrier ``nacks``). The first WORKING propose
      has no version and no NACKs; after a NACK→re-propose cycle the version
      and/or NACK set move, yielding a distinct key for the corrective
      propose.
    """
    payload = payload or {}
    if action in ("ack", "nack"):
        reviews = payload.get("pending_reviews") or []
        parts: list[str] = []
        for review in reviews:
            producer = str(review.get("producer", ""))
            ident = str(review.get("proposal_commit_sha") or "")
            if not ident:
                ident = "v" + str(review.get("current_version", ""))
            parts.append(f"{producer}@{ident}")
        return "|".join(sorted(parts))

    # propose — target version + open NACK set, read from the payload.
    version = payload.get("current_version", "")
    nacks = payload.get("unresolved_nacks") or payload.get("nacks") or []
    nack_parts = sorted(
        f"{n.get('reviewer', '')}:{n.get('version', '')}" for n in nacks if isinstance(n, dict)
    )
    return f"v{version}|" + ",".join(nack_parts)


def _derive_next_action(tracker: Any, role: str) -> tuple[str, dict[str, Any] | None, str]:
    """Module-level seam over ``routes.consensus._derive_next_action``.

    Defined at module scope (not imported as a name) so tests can
    monkeypatch ``event_loop._derive_next_action`` to script verbs without
    standing up a real consensus tracker, and so importing this module never
    drags in Flask. The loop always calls the module global, so a
    monkeypatch is seen.
    """
    from routes.consensus import _derive_next_action as _impl

    return _impl(tracker, role)


@dataclass
class EventDecision:
    """Structured per-role outcome of one :meth:`OrchestratorEventLoop.poll_once`.

    ``spawned`` is True only when this poll requested a *new* one-shot Job
    (a deduped repeat is False). ``agent_free`` is True for confirm/complete.
    ``timing`` is a structured mapping for the slice-4 latency budget on a
    fresh spawn, ``None`` otherwise.
    """

    role: str
    action: str
    dedupe_key: str | None = None
    spawned: bool = False
    agent_free: bool = False
    timing: dict[str, Any] | None = None
    # Set when a spawn that WOULD otherwise fire was suppressed by the
    # slice-3 supervisor: ``"backoff"`` (still inside the linear-backoff
    # window after an abnormal termination) or ``"exhausted"`` (the streak
    # reached the alert threshold; no respawn until the derived event
    # changes). ``None`` on the normal spawn / dedupe-hit paths.
    suppressed_reason: str | None = None


@dataclass
class JobTermination:
    """One observed terminal outcome of a one-shot BRC Job (#3064 slice-3).

    Fed to :meth:`OrchestratorEventLoop.handle_job_termination`. The supervisor
    classifies the outcome from the pod's exit:

    * ``exit_code == 0`` — a **clean handoff**: the wrapper ran to completion
      and exited 0. This covers a successful ``propose``/``ack``/``nack``, a
      ``nack`` *verdict* (the agent ran and decided NACK — still a healthy
      invocation), and the one-shot arm's stale-event ``exit 0`` (no agent
      invocation). All are non-triggers — the streak resets, never increments.
    * anything else — an **abnormal termination** (the pod died mid-event: a
      non-zero ``#2908``-classified agent rc, a signal/OOM/eviction kill, or a
      deadline). This is the only outcome that increments the streak.

    ``reason`` is an optional human string (e.g. the k8s pod termination reason
    ``OOMKilled`` / ``DeadlineExceeded`` / ``Error``) folded into the alert
    detail; it never affects classification.
    """

    dedupe_key: str
    role: str
    action: str
    exit_code: int | None = None
    reason: str | None = None

    @property
    def abnormal(self) -> bool:
        """True iff this is an abnormal (pod-died-mid-event) termination.

        A clean ``exit 0`` is normal; everything else (non-zero rc, kill, or a
        missing exit code paired with a kill ``reason``) is abnormal.
        """
        if self.exit_code == 0:
            return False
        return True


@dataclass
class SupervisionDecision:
    """Structured outcome of one :meth:`OrchestratorEventLoop.handle_job_termination`.

    ``streak`` is the post-increment consecutive-abnormal-termination count for
    the key (``0`` after a clean handoff reset). ``warned`` / ``alerted`` are
    True only on the single poll that first crossed the respective threshold
    (the latches are sticky). ``exhausted`` marks the streak having reached the
    alert threshold — the supervisor stops respawning the key until the derived
    event (dedupe key) changes. ``failed`` is True when a producer ``propose``
    arm's exhaustion engaged the existing ``AGENT_FAILED`` path.
    ``backoff_seconds`` is the linear-backoff delay scheduled before the next
    respawn of the key (``0`` on a clean handoff or at exhaustion).
    """

    role: str
    dedupe_key: str
    action: str
    abnormal: bool
    streak: int = 0
    backoff_seconds: int = 0
    warned: bool = False
    alerted: bool = False
    exhausted: bool = False
    failed: bool = False


class OrchestratorEventLoop:
    """Drive BRC forward by spawning one-shot pods per derived event.

    Collaborators are injected so the loop is unit-testable with no cluster:
    ``spawner`` exposes ``spawn_event(role=, action=, dedupe_key=, payload=)``;
    ``agent_free_handler`` performs the agent-free confirm/complete side
    effect as ``handler(action=, role=, payload=)``; ``clock`` is a monotonic
    source the timing field reads. ``reconcile(live_dedupe_keys)`` seeds the
    in-memory live set from live Job labels (the stateless-restart path).

    Slice-3 supervision collaborators (#3064 TASK-3-1), all optional so the
    slice-2 behavior is unchanged when they are absent:

    * ``job_monitor`` — a zero-arg callable returning an iterable of
      :class:`JobTermination` observed since the last poll. When present,
      :meth:`poll_once` drains it BEFORE deriving so abnormal terminations
      drive respawn/backoff/escalation.
    * ``failure_handler`` — invoked as ``handler(role=, error=)`` when a
      producer ``propose`` arm exhausts its respawn budget, engaging the
      existing ``AGENT_FAILED`` path (#2806 semantics, relocated for
      orchestrator mode).
    * ``alert_fn`` — invoked as ``alert_fn(anomaly=, priority=, summary=,
      detail=, role=)`` to raise the sticky ``OVERSEER_ALERT`` at the alert
      threshold.
    """

    def __init__(
        self,
        tracker: Any,
        spawner: Any,
        *,
        pipeline_id: str,
        slice_id: str | None,
        phase: str | None,
        clock: Callable[[], float] = time.monotonic,
        agent_free_handler: Callable[..., Any] | None = None,
        roles: list[str] | None = None,
        poll_interval: float | None = None,
        job_monitor: Callable[[], Iterable[JobTermination]] | None = None,
        failure_handler: Callable[..., Any] | None = None,
        alert_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.tracker = tracker
        self.spawner = spawner
        self.pipeline_id = pipeline_id
        self.slice_id = slice_id
        self.phase = phase
        self.clock = clock
        self.agent_free_handler = agent_free_handler
        self._roles = roles or []
        self.poll_interval = (
            poll_interval if poll_interval is not None else get_event_loop_poll_interval()
        )
        self._job_monitor = job_monitor
        self._failure_handler = failure_handler
        self._alert_fn = alert_fn
        # In-memory live dedupe-key set — process-local; intentionally NOT
        # persisted (restart re-derives + reconciles against live Jobs).
        self._live_keys: set[str] = set()
        # ---- Slice-3 supervision state (all keyed by dedupe key, all
        # process-local: a restart re-derives the event and reconciles the
        # live set, so a fresh budget after restart is correct by design).
        # Consecutive ABNORMAL-termination streak per key.
        self._streaks: dict[str, int] = {}
        # Earliest monotonic time the key may be respawned (linear backoff).
        self._respawn_not_before: dict[str, float] = {}
        # Sticky latches: warn fired / alert fired / budget exhausted.
        self._warned_keys: set[str] = set()
        self._alerted_keys: set[str] = set()
        self._exhausted_keys: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Dedupe state
    # ------------------------------------------------------------------
    def reconcile(self, live_dedupe_keys: Iterable[str]) -> None:
        """Seed the live-key set from live Job labels (restart path).

        A fresh loop holds no in-memory state; this rebuilds the set from the
        dedupe-key labels carried by still-running Jobs so a re-derived event
        a live pod already owns is not duplicated.
        """
        self._live_keys = {k for k in live_dedupe_keys if k}

    def live_dedupe_keys(self) -> set[str]:
        """Return a snapshot of the tracked live dedupe keys."""
        return set(self._live_keys)

    # ------------------------------------------------------------------
    # Single poll iteration
    # ------------------------------------------------------------------
    def poll_once(self, roles: list[str]) -> list[EventDecision]:
        """Run one derivation→action pass over ``roles``.

        Returns a decision per role (role order). Never raises: a per-role
        failure is logged and recorded as a no-op so one bad role can't wedge
        the loop.
        """
        # Slice-3: drain observed one-shot Job terminations FIRST so an
        # abnormal termination updates streak / backoff / exhaustion state
        # before this poll's derivation decides whether to respawn the key.
        # A no-op when no ``job_monitor`` is injected (slice-2 behavior).
        self._drain_terminations()
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
        return decisions

    def _handle_role(self, role: str) -> EventDecision:
        action, payload, _reason = _derive_next_action(self.tracker, role)

        if action in AGENT_FREE_ACTIONS:
            # confirm/complete: orchestrator-side, never a pod.
            if self.agent_free_handler is not None:
                self.agent_free_handler(action=action, role=role, payload=payload)
            return EventDecision(role=role, action=action, agent_free=True)

        if action not in SPAWN_ACTIONS:
            # wait / unknown — nothing to spawn.
            return EventDecision(role=role, action=action)

        identity = event_identity(action, payload)
        key = compute_dedupe_key(
            self.pipeline_id, self.slice_id, self.phase, role, action, identity
        )

        # Dedupe: an in-flight (or reconciled) Job already owns this event.
        if key in self._live_keys:
            return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

        # Supervision gates (slice-3). An exhausted key (its streak reached the
        # alert threshold) is NOT respawned until the derived event — and thus
        # the dedupe key — changes; a key still inside its linear-backoff
        # window after an abnormal termination waits out the backoff. Both
        # short-circuit before any spawn so a deterministic fast-fail can't
        # hot-loop the spawner.
        if key in self._exhausted_keys:
            return EventDecision(
                role=role,
                action=action,
                dedupe_key=key,
                spawned=False,
                suppressed_reason="exhausted",
            )
        not_before = self._respawn_not_before.get(key)
        if not_before is not None and self.clock() < not_before:
            return EventDecision(
                role=role,
                action=action,
                dedupe_key=key,
                spawned=False,
                suppressed_reason="backoff",
            )

        requested_at = self.clock()
        self.spawner.spawn_event(role=role, action=action, dedupe_key=key, payload=payload)
        dispatched_at = self.clock()
        # The backoff gate (if any) is now consumed — this spawn IS the
        # respawn it was throttling.
        self._respawn_not_before.pop(key, None)
        self._live_keys.add(key)
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
        return EventDecision(
            role=role, action=action, dedupe_key=key, spawned=True, timing=timing
        )

    # ------------------------------------------------------------------
    # Supervision (slice-3, TASK-3-1): respawn + backoff + escalation on
    # abnormal one-shot Job termination.
    # ------------------------------------------------------------------
    def _drain_terminations(self) -> list[SupervisionDecision]:
        """Drain the injected job monitor and supervise each termination.

        A no-op (empty list) when no ``job_monitor`` was injected. A monitor
        blip is logged and swallowed — a failure to read Job status must never
        wedge the event loop (same isolation stance as :meth:`poll_once`).
        """
        if self._job_monitor is None:
            return []
        try:
            terminations = list(self._job_monitor() or [])
        except Exception as exc:  # noqa: BLE001 — a monitor blip can't wedge the loop
            logger.warning(
                "event-loop job monitor poll failed",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                error=str(exc),
            )
            return []
        return self.observe_terminations(terminations)

    def observe_terminations(
        self, terminations: Iterable[JobTermination]
    ) -> list[SupervisionDecision]:
        """Supervise a batch of one-shot Job terminations (input order preserved)."""
        return [self.handle_job_termination(t) for t in terminations]

    def handle_job_termination(self, termination: JobTermination) -> SupervisionDecision:
        """Supervise one terminal one-shot Job outcome.

        Clean handoff (``exit 0`` — success, NACK verdict, or stale-event exit)
        resets the key's whole supervision budget; it never increments the
        streak. An abnormal termination (pod died mid-event) advances the
        consecutive-failure streak and:

        * warns once at :data:`supervision_policy.WARN_STREAK_THRESHOLD`;
        * at :data:`supervision_policy.ALERT_STREAK_THRESHOLD` exhausts the
          key — raises the sticky ``agent-invocation-fail-streak`` overseer
          alert exactly once, stops respawning the key until the derived event
          changes, and (for a producer ``propose`` arm) engages the existing
          ``AGENT_FAILED`` path;
        * otherwise schedules the next respawn after linear backoff
          (``streak × 2 s``, capped 30 s) — the next poll's derivation re-fires
          the spawn once the window elapses.

        The Job is removed from the live set either way so the next derivation
        can act (respawn the same key, or spawn the new one once consensus
        moves on).
        """
        key = termination.dedupe_key
        role = termination.role
        action = termination.action

        self._live_keys.discard(key)

        if not termination.abnormal:
            # Clean handoff (success / NACK verdict / stale exit-0): a
            # non-trigger by definition — reset the whole budget for the key.
            self._reset_key(key)
            return SupervisionDecision(
                role=role, dedupe_key=key, action=action, abnormal=False, streak=0
            )

        # Abnormal: the pod died mid-event. Advance the streak.
        streak = self._streaks.get(key, 0) + 1
        self._streaks[key] = streak
        decision = SupervisionDecision(
            role=role, dedupe_key=key, action=action, abnormal=True, streak=streak
        )

        if (
            streak >= supervision_policy.WARN_STREAK_THRESHOLD
            and key not in self._warned_keys
        ):
            self._warned_keys.add(key)
            decision.warned = True
            logger.warning(
                "one-shot event respawn streak crossed warn threshold; likely a "
                "permanent (configuration-class) failure rather than a transient",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=role,
                action=action,
                dedupe_key=key,
                streak=streak,
                reason=termination.reason,
            )

        if streak >= supervision_policy.ALERT_STREAK_THRESHOLD:
            # Exhaustion. Stop respawning the key until the derived event
            # changes; clear any pending backoff gate; fire the sticky alert
            # (and AGENT_FAILED for a producer propose) exactly once.
            decision.exhausted = True
            self._exhausted_keys.add(key)
            self._respawn_not_before.pop(key, None)
            if key not in self._alerted_keys:
                self._alerted_keys.add(key)
                decision.alerted = True
                self._raise_streak_alert(termination, streak)
                if action == "propose":
                    decision.failed = True
                    self._engage_agent_failed(termination, streak)
            return decision

        # Not yet exhausted: schedule the respawn after linear backoff.
        backoff = supervision_policy.backoff_seconds(streak)
        decision.backoff_seconds = backoff
        self._respawn_not_before[key] = self.clock() + backoff
        logger.info(
            "one-shot event abnormal termination; scheduling respawn after backoff",
            event_type="event_loop_respawn",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            role=role,
            action=action,
            dedupe_key=key,
            streak=streak,
            backoff_seconds=backoff,
            reason=termination.reason,
        )
        return decision

    def _reset_key(self, key: str) -> None:
        """Clear ALL supervision state for a key (clean handoff / event change).

        A fresh dedupe key (consensus moved on) never had state; this is the
        explicit reset for a key that DID accumulate a streak and then saw a
        clean handoff, so a later unrelated reuse starts from a fresh budget.
        """
        self._streaks.pop(key, None)
        self._respawn_not_before.pop(key, None)
        self._warned_keys.discard(key)
        self._alerted_keys.discard(key)
        self._exhausted_keys.discard(key)

    def _raise_streak_alert(self, termination: JobTermination, streak: int) -> None:
        """Raise the sticky ``agent-invocation-fail-streak`` overseer alert."""
        summary = (
            f"agent invocation failing repeatedly (role={termination.role}, "
            f"action={termination.action}, streak={streak})"
        )
        detail = (
            f"Orchestrator event loop observed {streak} consecutive abnormal "
            f"one-shot Job terminations for role={termination.role} "
            f"slice={self.slice_id or 'none'} on action={termination.action} "
            f"(last reason={termination.reason or 'unknown'}). This is a strong "
            "permanent/configuration-class signal (unknown model alias, auth "
            "misconfiguration, prompt-rendering crash). The supervisor stopped "
            "respawning this event key; no respawn resumes until the derived "
            "BRC event changes. No FAILED transition is forced here."
        )
        if self._alert_fn is None:
            logger.warning(
                "overseer alert (no alert_fn injected): " + summary,
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                anomaly=supervision_policy.FAIL_STREAK_ANOMALY,
                detail=detail,
            )
            return
        try:
            self._alert_fn(
                anomaly=supervision_policy.FAIL_STREAK_ANOMALY,
                priority="high",
                summary=summary,
                detail=detail,
                role=termination.role,
            )
        except Exception as exc:  # noqa: BLE001 — alerting is best-effort
            logger.warning(
                "failed to raise streak overseer alert",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=termination.role,
                error=str(exc),
            )

    def _engage_agent_failed(self, termination: JobTermination, streak: int) -> None:
        """Engage the existing AGENT_FAILED path for a producer propose exhaustion.

        Relocates the wrapper-side #2806 producer-failure semantics into
        orchestrator mode (the wrapper's own #2806 code is untouched). Absent
        an injected ``failure_handler`` this degrades to a warning — the sticky
        overseer alert has already surfaced the exhaustion to the operator.
        """
        if self._failure_handler is None:
            logger.warning(
                "producer propose arm exhausted but no failure_handler injected; "
                "AGENT_FAILED not engaged (sticky overseer alert already raised)",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=termination.role,
                streak=streak,
            )
            return
        error = (
            f"producer {termination.role} one-shot propose Job terminated "
            f"abnormally {streak} times (last reason="
            f"{termination.reason or 'unknown'}); respawn budget exhausted."
        )
        try:
            self._failure_handler(role=termination.role, error=error)
        except Exception as exc:  # noqa: BLE001 — failure handoff is best-effort
            logger.warning(
                "failed to engage AGENT_FAILED path",
                pipeline_id=self.pipeline_id,
                slice_id=self.slice_id,
                role=termination.role,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Background driver (production; not exercised by the unit contract)
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Poll ``self._roles`` until stopped or consensus completes.

        Sleeps one poll interval BEFORE the first poll so the tracker / pods
        settle (and so a freshly-started loop never races a synchronous
        caller). Stops on :meth:`stop` or when the slice has fully converged.
        """
        interval = self.poll_interval or DEFAULT_POLL_INTERVAL_SECONDS
        while not self._stop.wait(interval):
            self.poll_once(self._roles)
            if self._is_complete():
                logger.info(
                    "event loop: consensus complete, stopping",
                    pipeline_id=self.pipeline_id,
                    slice_id=self.slice_id,
                )
                break

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
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)


def make_role_list(roles: Iterable[Any]) -> list[str]:
    """Normalise a roster of role enums/strings to a list of role values."""
    return [r.value if hasattr(r, "value") else str(r) for r in roles]
