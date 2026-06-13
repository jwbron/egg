"""Orchestrator-owned BRC event loop (#3064 slice-2, TASK-2-1; slice-3, TASK-3-1).

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

**Slice-3 (#3138): supervision, backoff, respawn, and alerting**

The ``JobSupervisor`` watches, per-dupe-key, the health of spawned jobs,
applies backoff (streak*backoff capped), emits OVERSEER_ALERT on persistent
streaks, and resets on success. The wrapper template imports the constants
from the same source (``supervision_policy.py``).
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

# Import the single source of truth for supervision constants.
try:
    from orchestrator import supervision_policy as _supervision_policy
except ImportError:
    import supervision_policy as _supervision_policy  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Re-export supervision policy constants (#3138).
# ---------------------------------------------------------------------------
SUPERVISION_BACKOFF_FACTOR = _supervision_policy.SUPERVISION_BACKOFF_FACTOR
SUPERVISION_BACKOFF_CAP_SECONDS = _supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
SUPERVISION_FAILURE_STREAK_WARN = _supervision_policy.SUPERVISION_FAILURE_STREAK_WARN
SUPERVISION_FAILURE_STREAK_ALERT = _supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT

# Verb partitioning — the single source of truth for the verb→lifecycle
# mapping the loop enforces. ``confirm``/``complete`` run orchestrator-side
# with no pod; ``wait`` (and any unknown verb) is a no-op.
SPAWN_ACTIONS: frozenset[str] = frozenset({"propose", "ack", "nack"})
AGENT_FREE_ACTIONS: frozenset[str] = frozenset({"confirm", "complete"})

# Job-outcome classification the loop feeds the supervisor (slice-3, #3138).
# A ``job_status_view`` maps a one-shot Job's termination onto one of these:
#   * ``running``    — still in flight (or status unreadable); leave it.
#   * ``success``    — clean rc=0 agent completion; reset the streak.
#   * ``legitimate`` — a non-abnormal BRC outcome (stale-event exit 0, a cast
#                      NACK vote); an explicit non-trigger — streak untouched.
#                      RESERVED for a future richer observer: the current k8s
#                      ``_EventJobStatusView`` cannot distinguish a clean
#                      exit-0 stale/NACK from a success exit-0 (both are a Job
#                      ``EXITED``), so it never emits ``legitimate`` — such
#                      exits classify as ``success`` (also a non-trigger; it
#                      resets the streak, which is harmless for a clean exit).
#                      The path and its ``record_legitimate_outcome`` driver
#                      exist for an observer that can read BRC intent.
#   * ``abnormal``   — pod died mid-event / non-zero rc; increment the streak.
JOB_OUTCOME_RUNNING = "running"
JOB_OUTCOME_SUCCESS = "success"
JOB_OUTCOME_LEGITIMATE = "legitimate"
JOB_OUTCOME_ABNORMAL = "abnormal"

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
        # ``raw`` is always a ``str`` (env get + strip), so a non-numeric
        # value only ever raises ``ValueError`` — no ``TypeError`` arm needed.
        val = float(raw)
    except ValueError:
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
    * ``propose`` — the open NACK set against the producer's proposal, plus
      the target version *when the payload carries one*. In practice only the
      2+-reviewer barrier ``_derive_next_action`` payload carries
      ``current_version``; the WORKING first-propose (``{"producer": role}``)
      and the single-NACK PROPOSED payload (``{"unresolved_nacks": […],
      "producer": role}``) do not, so ``v{version}`` collapses to ``"v"`` in
      those common cases. Cross-cycle distinctness therefore rides on the
      NACK entries' own ``version`` field, NOT on ``current_version``: the
      first WORKING propose has no version and no NACKs (key ``"v|"``), and
      after a NACK→re-propose cycle the NACK set (and its per-entry versions)
      move, yielding a distinct key for the corrective propose. ``current_version``
      only sharpens the key in the barrier case.
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


# ---------------------------------------------------------------------------
# Supervision state (slice-3, #3138) — tracked per-dedupe-key.
# ---------------------------------------------------------------------------


class JobSupervisor:
    """Track per-event failures, enforce backoff, raise OVERSEER_ALERT.

    #3138: extract persistent supervision state out of the bash wrapper's
    runtime memory into the orchestrator-side process so pod crashes
    (abnormal termination, OOM, etc.) trigger backoff/respawn. NACKs and
    other BRC legitimate outcomes do NOT increment the streak. The wrapper
    imports the SAME constants from ``supervision_policy.py``.

    ``agent_failed`` is the orchestrator-mode relocation of the wrapper's
    #2806 propose-arm-exhaustion path: when a *producer* propose arm
    exhausts its retry budget, this callback engages the existing
    ``AGENT_FAILED`` flow so the cohort and operator learn the producer is
    stuck. Reviewer arms (ack/nack) do not engage it.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        overseer_alert: Callable[..., Any] | None = None,
        agent_failed: Callable[..., Any] | None = None,
        on_exhausted: Callable[..., Any] | None = None,
    ) -> None:
        self.clock = clock
        self._overseer_alert = overseer_alert
        self._agent_failed = agent_failed
        # #3064 slice-4: fired once when a dedupe key crosses into the
        # exhausted set (the ``_exhausted`` transition). The orchestrator
        # wires this to tear down the role's reused gateway session — an
        # exhausted arm spawns no further events, so its long-lived
        # orchestrator-mode session is released here rather than lingering to
        # pipeline cleanup. Called as ``on_exhausted(role=, action=,
        # dedupe_key=)``; best-effort (a teardown error never wedges
        # supervision).
        self._on_exhausted = on_exhausted
        # Per-dedupe-key streaks — each key gets a fresh budget
        # The counter resets when the dedupe key changes, giving a fresh
        # budget for each distinct event.
        self._streaks: dict[str, int] = {}
        # Timestamp of the last recorded *abort* per dedupe key — the anchor
        # the respawn backoff window is measured from (written only by
        # ``record_abort``). A key with no recorded abort is always ready.
        self._last_abort_time: dict[str, float] = {}
        # {dedupe_key: (action, role)}
        self._last_action: dict[str, tuple[str, str]] = {}
        # Once-per-key sticky latches so alert re-fires don't re-emit.
        self._alerted_warn: dict[str, bool] = {}
        self._alerted_10: dict[str, bool] = {}
        # Set to track keys that have exhausted budget (per #3138).
        self._exhausted: set[str] = set()

    # ------------------------------------------------------------------
    #  Public API (used by the orchestrator loop)
    # ------------------------------------------------------------------

    def record_success(self, dedupe_key: str) -> None:
        """Reset the streak and latches for a given dedupe key.

        Called when a finished Job with ``dedupe_key`` returns success
        (rc=0, agent completed the event cleanly).
        """
        self._streaks.pop(dedupe_key, None)
        self._alerted_warn.pop(dedupe_key, None)
        self._alerted_10.pop(dedupe_key, None)
        self._exhausted.discard(dedupe_key)
        logger.debug("JobSupervisor: success for key=%s — streak reset", dedupe_key)

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

    def record_abort(self, dedupe_key: str, action: str, role: str) -> None:
        """Called when a Job terminates abnormally (non-zero, non-BRC-legitimate).

        Increments the per-key streak and (the caller applies backoff if the
        key is not exhausted) for scheduling the respawn. Proposes sending
        ``sticky OVERSEER_ALERT`` when crossing thresholds.
        """
        streak = self._streaks.get(dedupe_key, 0) + 1
        self._streaks[dedupe_key] = streak
        self._last_abort_time[dedupe_key] = self.clock()
        self._last_action[dedupe_key] = (action, role)
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
        if streak >= SUPERVISION_FAILURE_STREAK_WARN and not self._alerted_warn.get(
            dedupe_key, False
        ):
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
        if streak >= SUPERVISION_FAILURE_STREAK_ALERT and not self._alerted_10.get(
            dedupe_key, False
        ):
            self._alerted_10[dedupe_key] = True
            self._exhausted.add(dedupe_key)
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
                        "JobSupervisor: on_exhausted teardown failed for key=%s "
                        "(action=%s, role=%s)",
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

    @property
    def backoff_factor(self) -> int:
        """Backoff multiplication factor. ``streak * fac`` → seconds."""
        return SUPERVISION_BACKOFF_FACTOR

    @property
    def backoff_cap(self) -> int:
        """Maximum backoff seconds (caps the linear growth)."""
        return SUPERVISION_BACKOFF_CAP_SECONDS

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
        """
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
        # Re-initialise live-key set if the caller provides it.
        # We only need to know which keys exist, not the full history.
        for key in live_dedupe_keys:
            if key:
                self._last_action[key] = ("(reconciled)", "(reconciled)")
                # No abort timestamp: a reconciled key starts a fresh budget
                # (streak 0 ⇒ zero backoff ⇒ immediately ready to respawn).

    # ------------------------------------------------------------------
    #  Alert integration
    # ------------------------------------------------------------------

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
                    f"Threshold: streak >= {SUPERVISION_FAILURE_STREAK_ALERT}."
                ),
            )


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
        job_supervisor: JobSupervisor | None = None,
        job_status_view: Any | None = None,
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
        # {dedupe_key: (action, role)} for each spawned key — lets the
        # supervisor attribute an abnormal-termination abort to the right
        # producer arm (so a propose exhaustion can engage AGENT_FAILED).
        self._key_meta: dict[str, tuple[str, str]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.supervisor = job_supervisor or JobSupervisor(clock=self.clock)
        # Optional Job-status observer (slice-3). Exposes
        # ``outcome_for(dedupe_key) -> str`` (one of the JOB_OUTCOME_* values).
        # ``None`` (pure slice-2 mode / most unit tests) ⇒ no observation, so
        # spawn behavior is byte-identical to slice-2.
        self._job_status_view = job_status_view

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
        self.supervisor.reconcile(live_dedupe_keys)

    def live_dedupe_keys(self) -> set[str]:
        """Return a snapshot of the tracked live dedupe keys."""
        return set(self._live_keys)

    # ------------------------------------------------------------------
    # Single poll iteration
    # ------------------------------------------------------------------
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
                self.supervisor.record_success(key)
                self._live_keys.discard(key)
                self._key_meta.pop(key, None)
            elif outcome == JOB_OUTCOME_LEGITIMATE:
                self.supervisor.record_legitimate_outcome(key, "legitimate")
                self._live_keys.discard(key)
                self._key_meta.pop(key, None)
            elif outcome == JOB_OUTCOME_ABNORMAL:
                self.supervisor.record_abort(key, action, role)
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
            # else: still running (or unknown) — leave the key live.

    def poll_once(self, roles: list[str]) -> list[EventDecision]:
        """Run one derivation→action pass over ``roles``.

        Observes finished Jobs first (slice-3 supervision), then derives the
        next action per role. Returns a decision per role (role order). Never
        raises: a per-role failure is logged and recorded as a no-op so one
        bad role can't wedge the loop.
        """
        self._observe_jobs()
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

        # Slice-3: Respect exhaustion — do NOT re-spawn exhausted keys
        if self.supervisor.is_exhausted(key):
            logger.info(
                "event-loop: spawn blocked due to exhausted key",
                pipeline_id=self.pipeline_id,
                role=role,
                action=action,
                dedupe_key=key,
            )
            return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

        # Dedupe: an in-flight (or reconciled) Job already owns this event.
        if key in self._live_keys:
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

        requested_at = self.clock()
        spawn_result = self.spawner.spawn_event(
            role=role, action=action, dedupe_key=key, payload=payload
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
