"""Orchestrator-owned BRC event loop (#3064 slice-2).

Under ``EGG_EVENT_LOOP_OWNER=orchestrator`` the orchestrator — not a
long-lived in-pod wait-loop — owns the BRC event loop. For every role it
consumes the *same* ``_derive_next_action`` logic the in-pod wrapper polls
via ``egg-orch brc next-action`` (``orchestrator/routes/consensus.py``) and
maps the derived verb onto a lifecycle action:

* ``propose | ack | nack`` → spawn a one-shot Job for that event (the pod
  handles exactly one event and exits — see the slice-1 wrapper arm).
* ``confirm | complete``   → execute orchestrator-side with **no pod**
  (mirrors the wrapper's agent-free ``egg-orch consensus confirmed`` arm).
* ``wait``                 → nothing.

Duplicate-spawn protection has three layers, all keyed on a **dedupe key**
``sha256(pipeline, slice, phase, role, action, event-identity)`` (truncated
to a k8s-label-safe length so it can ride on the spawned Job as a label):

1. an in-memory ``_handled`` set — repeated polls within one orchestrator
   process spawn at most once per event;
2. an at-most-one-live-pod-per-role+slice guard — a role with a live pod is
   never given a second one;
3. reconciliation against the live Jobs' dedupe-key labels — a fresh
   orchestrator process (restart) re-derives every event from the tracker
   (#2761) and adopts the in-flight Job rather than duplicating it.

No spawn bookkeeping is persisted to disk or the contract store: the tracker
plus live-Job labels are the only sources of truth, so an orchestrator
restart is stateless by construction.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover - logging shim parity with siblings
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.event_loop")

# Verb partitioning — the single source of truth for the verb→lifecycle
# mapping the loop enforces. ``confirm``/``complete`` run orchestrator-side
# with no pod; ``wait`` (and any unknown verb) is a no-op.
SPAWN_ACTIONS: frozenset[str] = frozenset({"propose", "ack", "nack"})
AGENT_FREE_ACTIONS: frozenset[str] = frozenset({"confirm", "complete"})

# k8s label *values* are limited to 63 characters from the RFC-1123 set
# ([A-Za-z0-9] separated by ``-_.``). A full sha256 hexdigest is 64 chars,
# so we truncate to fit — 63 hex chars (252 bits) is collision-free for any
# realistic event population, and the same truncation is applied everywhere
# (env var, in-memory set, Job label) so the three never disagree.
DEDUPE_KEY_MAXLEN = 63

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
    *,
    pipeline_id: str,
    slice_id: str | None,
    phase: str | None,
    role: str,
    action: str,
    event_identity: str,
) -> str:
    """Compute the spawn dedupe key for one derived event.

    ``sha256(pipeline, slice, phase, role, action, event-identity)`` over a
    NUL-joined tuple (NUL can't appear in any component, so the join is
    unambiguous), truncated to :data:`DEDUPE_KEY_MAXLEN` so the value is a
    legal k8s label. Deterministic across orchestrator restarts: identical
    inputs always yield the identical key, which is what makes live-Job
    reconciliation able to recognise an in-flight event after a restart.
    """
    payload = "\x00".join(
        (
            pipeline_id,
            slice_id or "",
            phase or "",
            role,
            action,
            event_identity,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:DEDUPE_KEY_MAXLEN]


def event_identity(tracker: Any, role: str, action: str, payload: dict[str, Any] | None) -> str:
    """Derive the per-event identity string folded into the dedupe key.

    * review verbs (``ack``/``nack``) — the set of producer proposals under
      review, keyed by ``proposal_commit_sha`` (the reviewer-side dedupe
      identity, ``routes/consensus.py``). A re-proposed commit changes the
      sha, which changes the key, which lets a fresh review spawn.
    * ``propose`` — the producer's target proposal version plus the open
      NACK set against it. The first WORKING propose has version 0 and no
      NACKs; after a NACK→re-propose cycle the version and/or NACK set move,
      yielding a distinct key for the corrective propose.
    """
    if action in ("ack", "nack"):
        reviews = (payload or {}).get("pending_reviews") or []
        parts: list[str] = []
        for review in reviews:
            producer = str(review.get("producer", ""))
            # Prefer the commit sha; fall back to the proposal version when a
            # degraded payload carries no sha (still a stable per-event id).
            identity = str(review.get("proposal_commit_sha") or "")
            if not identity:
                identity = "v" + str(review.get("current_version", ""))
            parts.append(f"{producer}@{identity}")
        return "|".join(sorted(parts))

    # propose — target version + open NACK set (read best-effort; identity
    # derivation must never raise into the poll loop).
    version = 0
    nack_parts: list[str] = []
    try:
        version = tracker.matrix.get_proposal_version(role)
        nack_parts = sorted(
            f"{reviewer}:{entry.version}"
            for reviewer, entry in tracker.matrix.get_nack_entries_for(role)
            if entry.version == version
        )
    except Exception:  # noqa: BLE001 — identity is best-effort, never fatal
        logger.debug("event_identity: propose identity fell back to v0", role=role)
    return f"v{version}|" + ",".join(nack_parts)


class EventSpawner(Protocol):
    """The lifecycle surface the loop drives for ``propose|ack|nack``.

    Production implementations close over the kubernetes spawner's one-shot
    entry (TASK-2-2) and the per-pipeline spawn context; tests supply a fake
    that records calls and reports synthetic live state.
    """

    def spawn(
        self,
        *,
        role: str,
        action: str,
        dedupe_key: str,
        event_payload: dict[str, Any] | None,
    ) -> Any:
        """Request a one-shot Job for the derived event."""

    def has_live_pod_for_role(self, role: str) -> bool:
        """Return True iff a non-terminal Job exists for ``role`` in scope."""

    def is_dedupe_key_live(self, dedupe_key: str) -> bool:
        """Return True iff a non-terminal Job carries this dedupe-key label."""


# Default derivation / tracker hooks are bound lazily so importing this
# module never drags in Flask (``routes.consensus``).
def _default_derive(tracker: Any, role: str) -> tuple[str, dict[str, Any] | None, str]:
    from routes.consensus import _derive_next_action

    return _derive_next_action(tracker, role)


@dataclass
class TickDecision:
    """Structured per-role outcome of one :meth:`OrchestratorEventLoop.tick`."""

    role: str
    action: str
    decision: str
    dedupe_key: str | None = None
    reason: str = ""
    spawn_dispatch_seconds: float | None = None


@dataclass
class OrchestratorEventLoop:
    """Drive BRC forward by spawning one-shot pods per derived event.

    All k8s / consensus interactions are injected so the loop is unit-
    testable with no cluster: ``spawner`` is an :class:`EventSpawner`,
    ``tracker_provider`` re-resolves the (possibly reconstructed) tracker
    each tick so a restart re-derives from durable state, ``confirm_fn``
    performs the agent-free ``confirm``/``complete`` side effect, and
    ``clock`` is a monotonic source the latency field and backoff read.
    """

    pipeline_id: str
    roles: list[str]
    spawner: EventSpawner
    confirm_fn: Callable[[str], Any]
    tracker_provider: Callable[[], Any | None]
    slice_id: str | None = None
    phase: str | None = None
    derive_fn: Callable[[Any, str], tuple[str, dict[str, Any] | None, str]] = _default_derive
    poll_interval: float | None = None
    clock: Callable[[], float] = field(default_factory=lambda: __import__("time").monotonic)

    # In-memory dedupe set — process-local; intentionally NOT persisted
    # (restart re-derives + reconciles against live Jobs instead).
    _handled: set[str] = field(default_factory=set, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.poll_interval is None:
            self.poll_interval = get_event_loop_poll_interval()

    # ------------------------------------------------------------------
    # Single poll iteration
    # ------------------------------------------------------------------
    def tick(self) -> list[TickDecision]:
        """Run one derivation→action pass over every role.

        Returns a structured decision per role (for logging and tests).
        Never raises: a per-role failure is logged and recorded so one bad
        role can't wedge the whole loop.
        """
        tracker = self.tracker_provider()
        decisions: list[TickDecision] = []
        if tracker is None:
            # No tracker yet (not created, or reconstruction failed) — there
            # is nothing to derive against this round.
            for role in self.roles:
                decisions.append(TickDecision(role=role, action="wait", decision="no_tracker"))
            return decisions

        for role in self.roles:
            try:
                decisions.append(self._handle_role(tracker, role))
            except Exception as exc:  # noqa: BLE001 — isolate per-role failures
                logger.warning(
                    "event-loop tick failed for role",
                    pipeline_id=self.pipeline_id,
                    slice_id=self.slice_id,
                    role=role,
                    error=str(exc),
                )
                decisions.append(TickDecision(role=role, action="error", decision="error"))
        return decisions

    def _handle_role(self, tracker: Any, role: str) -> TickDecision:
        action, payload, reason = self.derive_fn(tracker, role)

        if action in AGENT_FREE_ACTIONS:
            # confirm/complete: orchestrator-side, never a pod.
            self.confirm_fn(role)
            return TickDecision(role=role, action=action, decision="confirmed", reason=reason)

        if action not in SPAWN_ACTIONS:
            # wait / unknown — nothing to spawn.
            return TickDecision(role=role, action=action, decision="wait", reason=reason)

        identity = event_identity(tracker, role, action, payload)
        key = compute_dedupe_key(
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            phase=self.phase,
            role=role,
            action=action,
            event_identity=identity,
        )

        # Layer 1: in-process dedupe — already spawned for this exact event.
        if key in self._handled:
            return TickDecision(
                role=role, action=action, decision="deduped", dedupe_key=key, reason=reason
            )

        # Layer 2: at-most-one-live-pod per role+slice. A role with a live
        # pod is busy; do NOT record the key (the pod may be on a different,
        # newer event) — re-derive next tick once it exits.
        if self.spawner.has_live_pod_for_role(role):
            return TickDecision(
                role=role, action=action, decision="live_pod", dedupe_key=key, reason=reason
            )

        # Layer 3: restart reconciliation — a Job for this exact event is
        # already in flight (spawned by a prior orchestrator process). Adopt
        # it: record the key so we don't duplicate, but spawn nothing.
        if self.spawner.is_dedupe_key_live(key):
            self._handled.add(key)
            return TickDecision(
                role=role, action=action, decision="reconciled", dedupe_key=key, reason=reason
            )

        t0 = self.clock()
        self.spawner.spawn(role=role, action=action, dedupe_key=key, event_payload=payload)
        dispatch = self.clock() - t0
        self._handled.add(key)
        # Structured spawn→invoke timing field (#3064 slice-2; slice-4 reads
        # it for the p50<60s budget). ``spawn_dispatch_seconds`` is the
        # orchestrator-side dispatch latency; the pod records the remaining
        # spawn→invoke leg.
        logger.info(
            "event-loop spawn dispatched",
            event_type="event_loop_spawn",
            pipeline_id=self.pipeline_id,
            slice_id=self.slice_id,
            phase=self.phase,
            role=role,
            action=action,
            dedupe_key=key,
            spawn_dispatch_seconds=round(dispatch, 6),
        )
        return TickDecision(
            role=role,
            action=action,
            decision="spawned",
            dedupe_key=key,
            reason=reason,
            spawn_dispatch_seconds=dispatch,
        )

    # ------------------------------------------------------------------
    # Background driver
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Poll until stopped or consensus completes.

        Stops on :meth:`stop`, or when the tracker reports the slice has
        fully converged (nothing left to drive) — bounding the thread's life
        to the phase without any external teardown hook.
        """
        interval = self.poll_interval or DEFAULT_POLL_INTERVAL_SECONDS
        while not self._stop.is_set():
            self.tick()
            if self._is_complete():
                logger.info(
                    "event loop: consensus complete, stopping",
                    pipeline_id=self.pipeline_id,
                    slice_id=self.slice_id,
                )
                break
            # Interruptible sleep so stop() takes effect promptly.
            self._stop.wait(interval)

    def _is_complete(self) -> bool:
        tracker = self.tracker_provider()
        if tracker is None:
            return False
        try:
            return bool(tracker.evaluate().get("is_complete", False))
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
    out: list[str] = []
    for role in roles:
        out.append(role.value if hasattr(role, "value") else str(role))
    return out
