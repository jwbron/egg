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


class OrchestratorEventLoop:
    """Drive BRC forward by spawning one-shot pods per derived event.

    Collaborators are injected so the loop is unit-testable with no cluster:
    ``spawner`` exposes ``spawn_event(role=, action=, dedupe_key=, payload=)``;
    ``agent_free_handler`` performs the agent-free confirm/complete side
    effect as ``handler(action=, role=, payload=)``; ``clock`` is a monotonic
    source the timing field reads. ``reconcile(live_dedupe_keys)`` seeds the
    in-memory live set from live Job labels (the stateless-restart path).
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
        # In-memory live dedupe-key set — process-local; intentionally NOT
        # persisted (restart re-derives + reconciles against live Jobs).
        self._live_keys: set[str] = set()
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

        requested_at = self.clock()
        self.spawner.spawn_event(role=role, action=action, dedupe_key=key, payload=payload)
        dispatched_at = self.clock()
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
