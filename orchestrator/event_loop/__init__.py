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
    fresh spawn, ``None`` otherwise.
    """

    role: str
    action: str
    dedupe_key: str | None = None
    spawned: bool = False
    agent_free: bool = False
    timing: dict[str, Any] | None = None


# Bind the extracted method bodies (below) back onto the class shells.
# Imported after the module globals above so the submodules' value-imports
# (logger, constants, EventDecision) resolve during their module load.
from . import _loop, _supervisor  # noqa: E402

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

    record_success = _supervisor.record_success
    retire = _supervisor.retire
    record_legitimate_outcome = _supervisor.record_legitimate_outcome
    record_abort = _supervisor.record_abort
    record_fatal = _supervisor.record_fatal

    @property
    def backoff_factor(self) -> int:
        """Backoff multiplication factor. ``streak * fac`` → seconds."""
        return SUPERVISION_BACKOFF_FACTOR

    @property
    def backoff_cap(self) -> int:
        """Maximum backoff seconds (caps the linear growth)."""
        return SUPERVISION_BACKOFF_CAP_SECONDS

    backoff_seconds = _supervisor.backoff_seconds
    ready_to_respawn = _supervisor.ready_to_respawn
    is_exhausted = _supervisor.is_exhausted
    noop_parked = _supervisor.noop_parked
    _probe_hitl_fingerprint = _supervisor._probe_hitl_fingerprint
    _probe_brc_fingerprint = _supervisor._probe_brc_fingerprint
    reconcile = _supervisor.reconcile

    # ------------------------------------------------------------------
    #  Alert integration
    # ------------------------------------------------------------------

    _emit_alert = _supervisor._emit_alert
    _emit_noop_alert = _supervisor._emit_noop_alert
    _emit_fatal_alert = _supervisor._emit_fatal_alert


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

    reconcile = _loop.reconcile
    live_dedupe_keys = _loop.live_dedupe_keys
    _observe_jobs = _loop._observe_jobs
    poll_once = _loop.poll_once
    _publish_active_roles = _loop._publish_active_roles
    _handle_role = _loop._handle_role
    _reap_superseded_siblings = _loop._reap_superseded_siblings
    _check_convergence_stall = _loop._check_convergence_stall
    run = _loop.run
    _is_complete = _loop._is_complete
    start = _loop.start
    stop = _loop.stop


def make_role_list(roles: Iterable[Any]) -> list[str]:
    """Normalise a roster of role enums/strings to a list of role values."""
    return [r.value if hasattr(r, "value") else str(r) for r in roles]
