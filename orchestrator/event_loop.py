"""Orchestrator-owned BRC event loop (#3064 slice-2, TASK-2-1; slice-3, TASK-3-1;
slice-5, TASK-5-1).

The orchestrator — not a long-lived in-pod wait-loop — owns the BRC
event loop (unconditionally, post-#3164). For every role it consumes the
logic backing ``routes.consensus._derive_next_action`` IN-PROCESS and
maps the derived verb onto a lifecycle action:

* ``propose | ack | nack`` → request a one-shot Job via an injectable
  spawner (the pod handles exactly one event and exits — the wrapper's
  one-shot event handler).
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

**Same-role serialization (#3337)**

Per-key dedupe alone does NOT bound how many pods a *role* has live: when the
open-NACK set grows reviewer-by-reviewer, each increment yields a *new*
identity → a new key → a fresh spawn, while the prior key's pod is still
running. Because every event for a ``(role, slice)`` re-attaches to the SAME
shared worktree (the draft artifact lives there, keyed by role — not by
event), those concurrent same-role pods race and corrupt each other's
in-progress draft. So before spawning a fresh event for a role the loop
**reaps any other live key for that same role**: the newest event reflects
the fullest tracker state (the complete NACK set), so the older sibling — now
working a stale subset — is superseded and torn down. The invariant the loop
holds is **at most one live one-shot Job per (role, slice)**.

**Slice-3 (#3138): supervision, backoff, respawn, and alerting**

The ``JobSupervisor`` watches, per-dupe-key, the health of spawned jobs,
applies backoff (streak*backoff capped), emits OVERSEER_ALERT on persistent
streaks, and resets on success. The wrapper template imports the constants
from the same source (``supervision_policy.py``).

**#3425: successful-no-op park**

The failure-streak park cannot catch a slice wedged on an unresolved operator
HITL decision (contract ``cq-N``): each spawned pod discovers the block and
exits cleanly, so nothing fails, the BRC state never moves, the identical
dedupe key is re-derived, and the loop re-spawns forever (~50 pods observed).
The supervisor therefore also counts clean completions per key (an interleaved
abort does not reset the count — a crash/no-op-flapping wedged arm should still
park) and parks the arm at ``SUPERVISION_NOOP_STREAK_PARK`` (sticky alert once).
Because resolving the ``cq-N`` writes only the contract file — never the
tracker — the park self-releases on a change in the unresolved
contract-decision set (``hitl_probe``), on a change in the consensus-relevant
BRC state (``brc_probe``, #3465 — an arm can park while merely racing its
upstream producer, e.g. the tester's propose arm no-oping before the coder
has committed; the producer's proposal moves the tracker but never this arm's
own dedupe key), and, as a liveness backstop, every
``SUPERVISION_NOOP_PARK_RETRY_SECONDS``.

**Slice-5 (#3064): convergence-stall detection (re-homed idle-budget)**

In orchestrator mode the in-pod ``check_idle_budget`` no longer runs.
Instead the event loop judges convergence stall from tracker timestamps:
when a role's derived actionable event (propose|ack|nack) has been pending
longer than ``EGG_BRC_IDLE_BUDGET_MIN`` with no BRC-bus activity, the loop
raises the same ``stuck-phase-transition`` anomaly the in-pod alert uses
today.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
SUPERVISION_NOOP_STREAK_PARK = _supervision_policy.SUPERVISION_NOOP_STREAK_PARK
SUPERVISION_NOOP_PARK_RETRY_SECONDS = _supervision_policy.SUPERVISION_NOOP_PARK_RETRY_SECONDS

# Verb partitioning — the single source of truth for the verb→lifecycle
# mapping the loop enforces. ``confirm``/``complete`` run orchestrator-side
# with no pod; ``wait`` (and any unknown verb) is a no-op.
SPAWN_ACTIONS: frozenset[str] = frozenset({"propose", "ack", "nack"})
AGENT_FREE_ACTIONS: frozenset[str] = frozenset({"confirm", "complete"})

# Job-outcome classification the loop feeds the supervisor (slice-3, #3138).
# A ``job_status_view`` maps a one-shot Job's termination onto one of these:
#   * ``running``    — still in flight (or status unreadable); leave it.
#   * ``success``    — clean rc=0 agent completion; reset the failure streak.
#                      Also counts toward the #3425 successful-no-op streak:
#                      a clean exit that produced zero BRC progress re-derives
#                      the identical dedupe key, and after
#                      ``SUPERVISION_NOOP_STREAK_PARK`` such completions the
#                      arm parks instead of re-spawning forever.
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
#   * ``fatal``      — the agent exited with the auth-fatal code
#                      (``egg_agent.auth_errors.EX_AUTH_FATAL``): a
#                      non-retryable credential / quota failure (#3373). Skip
#                      the streak entirely — exhaust the key on the first
#                      occurrence and raise a named, actionable alert. Retrying
#                      only re-uses the same rejected credential.
JOB_OUTCOME_RUNNING = "running"
JOB_OUTCOME_SUCCESS = "success"
JOB_OUTCOME_LEGITIMATE = "legitimate"
JOB_OUTCOME_ABNORMAL = "abnormal"
JOB_OUTCOME_FATAL = "fatal"

# Poll cadence (#3064 slice-2: "poll interval env-tunable (default 5s)").
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# Bound on the per-key termination history the supervisor retains for the
# exhaustion report (#3496). Only the most recent entries matter — the report
# exists so an operator can see WHY an arm died (crash vs credential-fatal,
# pod exit codes) without grepping pod logs; a handful of samples is enough.
SUPERVISION_EXIT_HISTORY_MAX = 5


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


# Convergence-stall idle budget (#3064 slice-5, re-homed from the in-pod
# ``check_idle_budget``).  A role whose derived event has been pending
# longer than this many minutes without tracker-bus movement raises a
# ``stuck-phase-transition`` anomaly.  Mirrors the wrapper's
# ``EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT = 30``.
_IDLE_BUDGET_MIN_DEFAULT = 30


def get_idle_budget_minutes() -> int:
    """Return the idle-budget threshold in minutes (default 30).

    Reads ``EGG_BRC_IDLE_BUDGET_MIN``.  Malformed or non-positive values
    fall back to the default with a warning.  The wrapper template reads
    the same env var (``consensus_wrapper.py:151``); this function is the
    orchestrator-side accessor for the re-homed convergence-stall check.
    """
    raw = os.environ.get("EGG_BRC_IDLE_BUDGET_MIN", "").strip()
    if not raw:
        return _IDLE_BUDGET_MIN_DEFAULT
    try:
        # ``raw`` is always a ``str`` (env get + strip), so a non-integer
        # value only ever raises ``ValueError`` — no ``TypeError`` arm needed
        # (mirrors ``get_event_loop_poll_interval`` above).
        val = int(raw)
    except ValueError:
        logger.warning(
            "EGG_BRC_IDLE_BUDGET_MIN=%r is not an integer; falling back to %d",
            raw,
            _IDLE_BUDGET_MIN_DEFAULT,
        )
        return _IDLE_BUDGET_MIN_DEFAULT
    if val <= 0:
        logger.warning(
            "EGG_BRC_IDLE_BUDGET_MIN=%d must be > 0; falling back to %d",
            val,
            _IDLE_BUDGET_MIN_DEFAULT,
        )
        return _IDLE_BUDGET_MIN_DEFAULT
    return val


def _idle_budget_anomaly_name() -> str:
    """Return the OVERSEER_ALERT anomaly name for an idle-budget breach.

    Single-sourced from the in-pod wrapper
    (``consensus_wrapper.EVENT_PUMP_IDLE_BUDGET_ANOMALY``) so the
    orchestrator-side convergence-stall alert and the in-pod
    ``check_idle_budget`` raise the identical anomaly the overseer
    classifies on. Lazy import (the wrapper module is import-heavy and not
    needed at event-loop import time); falls back to the literal if the
    import is unavailable in a stripped environment.
    """
    try:
        from consensus_wrapper import EVENT_PUMP_IDLE_BUDGET_ANOMALY

        return EVENT_PUMP_IDLE_BUDGET_ANOMALY
    except Exception:  # noqa: BLE001 — never let alerting fail on an import edge
        return "stuck-phase-transition"


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
    fresh spawn, ``None`` otherwise. ``blocked`` (#3496) names why a
    spawn-action decision did not spawn when the block is terminal —
    currently only ``"exhausted"`` — so the all-arms-exhausted detection can
    distinguish it from the benign not-spawned shapes (dedupe, backoff, park).
    """

    role: str
    action: str
    dedupe_key: str | None = None
    spawned: bool = False
    agent_free: bool = False
    timing: dict[str, Any] | None = None
    blocked: str | None = None


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
        hitl_probe: Callable[[], Iterable[str] | None] | None = None,
        brc_probe: Callable[[], str | None] | None = None,
    ) -> None:
        self.clock = clock
        self._overseer_alert = overseer_alert
        self._agent_failed = agent_failed
        # #3425: best-effort probe returning the ids of unresolved
        # contract-resident decisions (``cq-N``). Resolving such a decision
        # writes only the contract file — never the BRC tracker — so a parked
        # arm cannot observe the unblock through its dedupe key; a change in
        # this set is what releases a successful-no-op park. ``None`` from the
        # probe (or no probe wired) means "unknown": the park then releases
        # only via the retry heartbeat.
        self._hitl_probe = hitl_probe
        # #3465: best-effort probe returning an opaque fingerprint of the
        # consensus-relevant BRC state (tracker phases / proposals / verdicts).
        # An arm parked while racing its upstream producer (the tester's
        # propose arm no-oping before the coder commits) is un-wedged by BRC
        # movement — but that movement never changes the parked arm's OWN
        # dedupe key, so without this probe the only wake path is the retry
        # heartbeat. ``None`` means "unknown", same semantics as ``hitl_probe``.
        self._brc_probe = brc_probe
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
        # #3496: bounded per-key termination history — the last
        # ``SUPERVISION_EXIT_HISTORY_MAX`` abnormal/fatal exits, each a
        # ``{"at": iso-wallclock, "category": ..., "detail": ...}`` entry.
        # Surfaced by :meth:`exhausted_report` so the operator can see WHY an
        # arm died (crash vs credential-fatal, pod exit code) at escalation
        # time instead of the cause being unrecoverable from the logs.
        # Wall-clock (not the injected monotonic ``clock``) because the
        # entries are operator-facing display data, never compared.
        self._exit_history: dict[str, list[dict[str, Any]]] = {}
        # #3425 successful-no-op park state, per dedupe key. The streak counts
        # clean completions of the SAME key (an interleaved abort does not reset
        # it — a crash/no-op-flapping wedged arm should still park): only an
        # invocation that produced zero BRC progress is re-derived under an
        # identical key, so a productive success is inert at 1. At
        # ``SUPERVISION_NOOP_STREAK_PARK`` the arm parks (sticky alert once);
        # the park self-releases on a contract-decision fingerprint change or
        # the retry heartbeat — never permanently, unlike ``_exhausted``.
        #
        # Asymmetry with ``_streaks`` (intentional, do not "fix" by popping in
        # ``record_success``): ``record_success`` IS the no-op increment path,
        # so it cannot pop here the way it pops the failure streak — productive
        # vs. no-op is indistinguishable at success time (that is the design;
        # the dedupe-key identity is what separates them). A productive success
        # therefore leaves a permanent ``{key: 1}`` entry. This is bounded by
        # the distinct BRC events over a single loop's lifetime (the supervisor
        # is per-pipeline-loop, torn down with it — not process-global), and
        # ``retire()`` / ``reconcile()`` prune it on the paths that can, so it
        # is an accounting asymmetry, not a leak.
        self._noop_streaks: dict[str, int] = {}
        # {dedupe_key: fingerprint-at-park} — the unresolved contract-decision
        # id set observed when the key parked (or on the last probe release).
        self._noop_fingerprint: dict[str, frozenset[str] | None] = {}
        # {dedupe_key: brc-fingerprint-at-park} — the consensus-state digest
        # observed when the key parked (or on the last BRC-movement release).
        self._noop_brc_fingerprint: dict[str, str | None] = {}
        # {dedupe_key: clock() of the last allowed park-probe spawn} — anchors
        # the retry heartbeat.
        self._noop_last_probe: dict[str, float] = {}
        # Once-per-key sticky latch for the no-op park alert.
        self._alerted_noop: dict[str, bool] = {}

    # ------------------------------------------------------------------
    #  Public API (used by the orchestrator loop)
    # ------------------------------------------------------------------

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
        streak = self._noop_streaks.get(dedupe_key, 0) + 1
        self._noop_streaks[dedupe_key] = streak
        logger.debug(
            "JobSupervisor: success for key=%s — failure streak reset (clean completions=%d)",
            dedupe_key,
            streak,
        )
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
            self._emit_noop_alert(dedupe_key, streak, action, role, fingerprint)

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
        self._alerted_noop.pop(dedupe_key, None)
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
                    "JobSupervisor: on_exhausted teardown failed for fatal key=%s "
                    "(action=%s, role=%s)",
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
                "JobSupervisor: operator reset cleared %d exhausted key(s) — "
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
                "JobSupervisor: no-op park retry heartbeat elapsed for key=%s "
                "— allowing a probe spawn",
                dedupe_key,
            )
            return False
        return True

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
        self._alerted_noop.clear()
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
    ) -> None:
        """Emit a named, once-per-key alert for a successful-no-op park (#3425).

        Distinct from :meth:`_emit_alert`'s ``agent-invocation-fail-streak``:
        these invocations *succeed* — the slice is wedged on something a
        respawn cannot resolve (typically an unresolved operator HITL
        ``cq-N``), so the message points the operator at the pending decision
        rather than at agent health.
        """
        if self._overseer_alert is None:
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
                summary=(
                    f"agent credential rejected — non-retryable (action={action}, role={role})"
                ),
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


class OrchestratorEventLoop:
    """Drive BRC forward by spawning one-shot pods per derived event.

    Collaborators are injected so the loop is unit-testable with no cluster:
    ``spawner`` exposes ``spawn_event(role=, action=, dedupe_key=, payload=)``;
    ``agent_free_handler`` performs the agent-free confirm/complete side
    effect as ``handler(action=, role=, payload=)``; ``clock`` is a monotonic
    source the timing field reads. ``reconcile(live_dedupe_keys)`` seeds the
    in-memory live set from live Job labels (the stateless-restart path).

    Slice-5 (convergence-stall detection): inject ``convergence_stall_notifier``
    to raise a ``stuck-phase-transition`` anomaly when a role has had a
    pending actionable event (propose|ack|nack) for longer than
    ``EGG_BRC_IDLE_BUDGET_MIN`` minutes without BRC-bus activity.
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
        convergence_stall_notifier: Callable[..., Any] | None = None,
        active_roles_notifier: Callable[[set[str]], Any] | None = None,
        arms_exhausted_notifier: Callable[..., Any] | None = None,
        arms_exhausted_cleared_notifier: Callable[[], Any] | None = None,
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
        # #3064 slice-5: convergence-stall notifier (raises the
        # ``stuck-phase-transition`` anomaly).  When unset, stall detection
        # is fully dormant — ``_check_convergence_stall`` returns early and
        # accumulates no per-role state.
        self._convergence_stall_notifier = convergence_stall_notifier
        # Per-role first-seen wall-clock timestamp of the current stall
        # window.  {role: first_seen_wallclock_float} — set when a role is
        # first observed with a pending actionable event after a period of
        # tracker bus quiet; cleared when the role's derived action changes
        # (no longer actionable) or the tracker bus moves.
        self._stall_first_seen: dict[str, float] = {}
        # Per-role sticky alert latch so the anomaly fires exactly once per
        # stall episode (mirrors the in-pod ALERTED_AT_BUDGET flag).
        self._stall_alerted: dict[str, bool] = {}
        # #3064 slice-5: callback that publishes the set of roles with a
        # currently in-flight (live) one-shot Job to the health monitor's
        # ``set_active_roles``.  Without this the monitor's ``_active_jobs``
        # stays empty in orchestrator mode and every heartbeat/progress/
        # container-exit tripwire is suppressed (the active-Job scoping and
        # silent-mid-event-pod coverage are then dead).  When unset (unit
        # tests / pod mode) no publishing happens.
        self._active_roles_notifier = active_roles_notifier
        # #3496: fired (once per wedge episode) when EVERY derivable spawn arm
        # is blocked on an exhausted key with no Job in flight and no
        # agent-free progress — the slice can no longer advance without
        # operator intervention. Wired in production to the executor's
        # arms-exhausted HITL escalation; ``None`` (unit tests / pod mode)
        # leaves detection dormant except for the WARN log.
        self._arms_exhausted_notifier = arms_exhausted_notifier
        # #3496 review: fired once on the wedged→clear transition — the
        # symmetric counterpart of the notifier above. Wired in production to
        # the executor's HITL auto-withdrawal so a decision the operator never
        # resolved (the wedge cleared by another route) is retracted rather
        # than left stale in ``pending_decisions``. ``None`` leaves the
        # decision in place (unit tests / pod mode).
        self._arms_exhausted_cleared_notifier = arms_exhausted_cleared_notifier
        # Sticky latch so the notifier fires exactly once per wedge episode;
        # cleared when the condition no longer holds (an arm spawned, a new
        # key derived, or an operator reset cleared the exhausted set).
        self._arms_exhausted_alerted = False

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
            # else: still running (or unknown) — leave the key live.

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

    @property
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
            return EventDecision(role=role, action=action, dedupe_key=key, spawned=False)

        # #3337: serialize same-role producers. We are about to spawn a *fresh*
        # event for ``role``; any other live key for this same role belongs to
        # an older event working a now-stale tracker state, and its pod shares
        # this role's worktree (the draft artifact). Reap it first so at most
        # one one-shot Job per (role, slice) is ever live and concurrent
        # siblings can't corrupt the shared draft.
        self._reap_superseded_siblings(role, keep_key=key)

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

    # ------------------------------------------------------------------
    # Convergence-stall detection (#3064 slice-5, TASK-5-1)
    # ------------------------------------------------------------------

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

        budget_min = get_idle_budget_minutes()
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
                action, _payload, _reason = _derive_next_action(self.tracker, role)
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
            identity = event_identity(action, _payload)
            key = compute_dedupe_key(
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
                    anomaly=_idle_budget_anomaly_name(),
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
            _unregister_live_loop(self)

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
        _register_live_loop(self)
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
        _unregister_live_loop(self)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)


def make_role_list(roles: Iterable[Any]) -> list[str]:
    """Normalise a roster of role enums/strings to a list of role values."""
    return [r.value if hasattr(r, "value") else str(r) for r in roles]


# ---------------------------------------------------------------------------
# Live-loop registry (#3496)
# ---------------------------------------------------------------------------
#
# Supervision state is process-local and lives on the loop's ``JobSupervisor``
# — there is no persisted copy to mutate. For the arms-exhausted HITL's
# "Retry arms" resolution to clear exhausted keys in-band, the resolution
# handler (a Flask route in the same process as the loop's daemon thread)
# needs a way to reach the live loop object. This registry is that seam:
# ``start()`` registers, ``stop()`` / natural ``run()`` completion
# unregister. Keyed by ``(pipeline_id, slice_id)`` — concurrent slices each
# run their own loop.

_LIVE_LOOPS: dict[tuple[str, str | None], OrchestratorEventLoop] = {}
_LIVE_LOOPS_LOCK = threading.Lock()


def _register_live_loop(loop: OrchestratorEventLoop) -> None:
    with _LIVE_LOOPS_LOCK:
        _LIVE_LOOPS[(loop.pipeline_id, loop.slice_id)] = loop


def _unregister_live_loop(loop: OrchestratorEventLoop) -> None:
    # Identity-checked: a phase restart can register a NEW loop under the
    # same key before the superseded loop's stop/exit runs; the stale
    # unregister must not evict the fresh loop.
    key = (loop.pipeline_id, loop.slice_id)
    with _LIVE_LOOPS_LOCK:
        if _LIVE_LOOPS.get(key) is loop:
            del _LIVE_LOOPS[key]


def get_live_event_loops(pipeline_id: str) -> list[OrchestratorEventLoop]:
    """Return every live event loop for ``pipeline_id`` (any slice)."""
    with _LIVE_LOOPS_LOCK:
        return [loop for (pid, _), loop in _LIVE_LOOPS.items() if pid == pipeline_id]
