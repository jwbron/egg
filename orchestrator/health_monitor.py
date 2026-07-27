"""
Deterministic tripwire processor for pipeline health monitoring.

Implements six rules:
1. Heartbeat timeout - escalate to overseer/HITL when no heartbeat within threshold
2. Container exit - immediate HITL escalation
3. Repeated identical errors - escalate after threshold
4. Message volume spike - auto-throttle above rate limit
5. Progress stall - escalate to overseer/HITL on stall detection
6. Infrastructure error - escalate on blocked progress with infra error keywords

The HealthMonitor subscribes to EventBus events and maintains per-agent
state. It does NOT use LLM classifiers — those belong to the overseer.
Nudge messages are only sent by the Tier 2 overseer after classifying
the alert; Tier 1 raises alerts and escalates but never nudges directly.
"""

import re
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from events import Event, EventBus, EventType
from models import PipelineConfig

logger = get_logger("orchestrator.health_monitor")

INFRA_ERROR_PATTERNS = [
    re.compile(r"git\s+(add|push|commit|checkout)\s+failed", re.IGNORECASE),
    re.compile(r"permission\s+denied", re.IGNORECASE),
    re.compile(r"\bEROFS\b", re.IGNORECASE),
    re.compile(r"403\s+Forbidden", re.IGNORECASE),
    re.compile(r"gateway.*(error|timeout|refused|unavailable)", re.IGNORECASE),
    re.compile(r"error.*gateway", re.IGNORECASE),
    re.compile(r"\.gitignore.*(block|reject|ignor|exclud|prevent|fail)", re.IGNORECASE),
    re.compile(r"500\s+Internal\s+Server\s+Error", re.IGNORECASE),
    re.compile(r"read-only\s+file\s*system", re.IGNORECASE),
    re.compile(r"docker\s+socket", re.IGNORECASE),
    re.compile(r"DNS\s+resolution\s+fail", re.IGNORECASE),
    re.compile(r"disk\s+space", re.IGNORECASE),
    re.compile(r"connection\s+refused.*gateway", re.IGNORECASE),
]


# Type alias for callbacks
EscalationCallback = Callable[[dict[str, Any]], None]
ThrottleCallback = Callable[[dict[str, Any]], None]

# Sentinel for AgentState.last_activity meaning "no CONTAINER_ACTIVITY event
# has ever arrived for this agent." Compared with `<=` so any non-positive
# float counts as never-seen. See `_has_recent_activity` (#2190).
_NEVER_SEEN_ACTIVITY: float = 0.0

# Canonical agent_id for the overseer. The watchdog's silence is the signal
# we most need to surface, so the alive-signal gates and the escalation
# router both special-case it. Matches AgentRole.OVERSEER.value but kept
# literal here to avoid pulling shared/egg_contracts into health_monitor.
_OVERSEER_AGENT_ID: str = "overseer"

# Slack (seconds) allowed when testing an elapsed value against the monitor's
# own age. Every anchor the monitor reads is written after construction, so
# ``elapsed > monitor_age`` can only come from a corrupt anchor; the slack
# only absorbs float/clock jitter. See ``_sanitize_elapsed`` (#3605).
_ELAPSED_SANITY_SLACK_SECONDS: float = 5.0


@dataclass
class AgentState:
    """Per-agent tracking state."""

    agent_id: str
    last_heartbeat: float = field(default_factory=time.time)
    last_progress: float = field(default_factory=time.time)
    # Wall-clock timestamp of the most recent CONTAINER_ACTIVITY event for
    # this agent (e.g. successful git commit registration).  Used to
    # suppress heartbeat/progress stall alerts against agents that are
    # legitimately blocked in long tool calls but still making real
    # progress (issue #2190).  Defaults to ``_NEVER_SEEN_ACTIVITY`` so a
    # freshly spawned agent's silence is governed by the heartbeat anchor
    # alone.
    last_activity: float = _NEVER_SEEN_ACTIVITY
    error_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    message_timestamps: list[float] = field(default_factory=list)
    heartbeat_escalated: bool = False
    progress_escalated: bool = False
    infra_error_escalated: bool = False
    brc_progress_escalated: bool = False
    last_progress_data: dict = field(default_factory=dict)


class HealthMonitor:
    """Deterministic tripwire processor for pipeline health.

    Subscribes to EventBus events and evaluates six tripwire rules
    to detect and respond to agent health issues.

    Args:
        event_bus: The EventBus to subscribe to.
        pipeline_id: Pipeline being monitored.
        config: Pipeline configuration with threshold values.
    """

    def __init__(
        self,
        event_bus: EventBus,
        pipeline_id: str,
        config: PipelineConfig,
    ) -> None:
        self._event_bus = event_bus
        self._pipeline_id = pipeline_id
        self._config = config
        self._lock = threading.Lock()

        # Phase-aware threshold support
        self._current_phase: str | None = None

        # Per-agent state
        self._agents: dict[str, AgentState] = {}

        # Callbacks
        self._escalation_callbacks: list[EscalationCallback] = []
        self._throttle_callbacks: list[ThrottleCallback] = []

        # Active alerts (bounded to prevent unbounded growth)
        self._active_alerts: deque[dict[str, Any]] = deque(maxlen=200)

        # Last heartbeat times (exposed for test manipulation)
        self._last_heartbeat: dict[str, float] = {}

        # BRC progress tracking: first time each fully-ACKed producer was seen
        self._fully_acked_first_seen: dict[str, float] = {}

        # #3164: the orchestrator unconditionally owns the BRC event loop.
        #   * roles with no active one-shot Job are legitimately idle (the
        #     loop has not derived an actionable event for them yet) and are
        #     skipped from heartbeat/progress/container-exit tripwires.
        #   * a role WITH an active Job is checked normally — a silent
        #     one-shot pod mid-event still trips.
        # ``_active_jobs`` is the set of roles with an active one-shot Job,
        # populated by the event loop on each poll tick via
        # ``set_active_roles``. Empty means "no active Jobs — every role is
        # idle", so all tripwires are skipped.
        self._active_jobs: set[str] = set()

        # Heartbeat anchor for roles that have an active one-shot Job but have
        # never emitted anything the monitor can key state on. Valued with the
        # wall-clock time the role's Job was first observed active, maintained
        # by ``set_active_roles``. Before #3605 ``check_heartbeats`` anchored
        # these roles at 0.0, so ``now - 0`` reported a Unix epoch timestamp
        # (1784944189s ≈ 56 years) as the elapsed stall time.
        self._job_active_since: dict[str, float] = {}

        # Roles from ``_job_active_since`` that have already produced a
        # ``heartbeat_timeout`` escalation: the equivalent of
        # ``AgentState.heartbeat_escalated`` for roles that have no
        # ``AgentState`` because nothing was ever received from them (#3605).
        self._never_seen_escalated: set[str] = set()

        # Wall-clock time the monitor started observing this pipeline. Every
        # anchor it reads is written after this instant, so an ``elapsed``
        # larger than the monitor's own age is necessarily corrupt (#3605).
        # Like every other ``now - last_*`` in this module this is wall-clock,
        # not monotonic. A backward clock step (NTP correction, host suspend)
        # shrinks ``monitor_age``, but that alone does not break the guard's
        # inequality: only a step exceeding the monitor's own age *plus*
        # ``_ELAPSED_SANITY_SLACK_SECONDS`` drags post-step anchors behind
        # ``_created_at`` far enough to make ``_sanitize_elapsed``
        # flag-and-clamp them. A smaller step — an ordinary sub-second NTP
        # correction — flags nothing at all, and anchors written before any
        # backward step shrink by the same delta and stay unflagged either
        # way. Nor is such flagging self-limiting: once ``now`` climbs back
        # past ``_created_at`` the condition reduces to ``anchor <
        # _created_at - slack``, which no longer involves ``now``, so a
        # ``_job_active_since`` anchor written inside the skew window keeps
        # being flagged (and keeps bypassing the alive-signal gate) after
        # the window closes — until the Job ends, the role heartbeats, or
        # ``reset_agent`` drops it. Forward steps do not defeat this guard
        # either: ``now`` inflates ``elapsed`` and ``monitor_age`` by the
        # same amount, so ``elapsed <= monitor_age`` survives. They are not
        # harmless, though — every ``elapsed`` grows by the step, so a role
        # that heartbeated seconds ago can trip the timeout itself, and the
        # guard cannot catch that precisely because ``monitor_age`` inflated
        # too. Worth revisiting if this module ever moves to
        # ``time.monotonic``.
        self._created_at: float = time.time()

        # Subscribe to events
        self._event_bus.subscribe(EventType.PROGRESS_EMITTED, self._on_progress)
        self._event_bus.subscribe(EventType.ERROR, self._on_error)
        self._event_bus.subscribe(EventType.CONTAINER_STOPPED, self._on_container_stopped)
        self._event_bus.subscribe(EventType.MESSAGE_SENT, self._on_message_sent)
        self._event_bus.subscribe(EventType.CONTAINER_ACTIVITY, self._on_container_activity)

    # -----------------------------------------------------------------
    # Callback registration
    # -----------------------------------------------------------------

    def on_escalation(self, callback: EscalationCallback) -> None:
        """Register a callback for escalation events."""
        self._escalation_callbacks.append(callback)

    def on_throttle(self, callback: ThrottleCallback) -> None:
        """Register a callback for throttle events."""
        self._throttle_callbacks.append(callback)

    # -----------------------------------------------------------------
    # Phase-aware threshold and BRC-idle suppression
    # -----------------------------------------------------------------

    def set_current_phase(self, phase: str) -> None:
        """Set the current pipeline phase for threshold selection.

        Must be called before agents are spawned for each phase so that
        the correct heartbeat/progress timeout is used.

        Args:
            phase: The pipeline phase name (e.g. "implement", "plan").
        """
        with self._lock:
            self._current_phase = phase

    def get_current_phase(self) -> str | None:
        """Return the phase last set via :meth:`set_current_phase`."""
        with self._lock:
            return self._current_phase

    def set_active_roles(self, roles: set[str]) -> None:
        """Set the set of roles with currently active one-shot Jobs.

        Called by the concurrent executor or event loop on each poll tick
        from live Job labels.  In orchestrator mode roles NOT in this set
        are treated as legitimately idle and are skipped from tripwire
        checks.

        Also maintains ``_job_active_since``, the heartbeat anchor
        ``check_heartbeats`` uses for roles that have an active Job but have
        never emitted a heartbeat (#3605). A newly-active role is anchored at
        the time its Job was first observed here; a role that leaves the set
        drops its anchor (and its never-seen escalation marker) so a later Job
        for the same role re-anchors from scratch instead of inheriting the
        previous Job's clock.

        Args:
            roles: Set of role names with active one-shot Jobs.
        """
        now = time.time()
        with self._lock:
            self._active_jobs = set(roles)
            for role in roles:
                self._job_active_since.setdefault(role, now)
            for role in list(self._job_active_since):
                if role not in roles:
                    del self._job_active_since[role]
                    self._never_seen_escalated.discard(role)

    def reset_agent(self, agent_id: str) -> None:
        """Drop all per-agent tracking state for *agent_id*.

        Called when an agent is respawned (``restart_agent`` /
        ``restart_phase``).  The pre-respawn ``_last_heartbeat`` anchor would
        otherwise survive the restart and let ``check_heartbeats`` fire a
        ``heartbeat_timeout`` alert with a stale ``elapsed`` value, which the
        Tier-2 overseer faithfully escalates as a false-positive
        ``agent-heartbeat-stall`` (issue #2084).

        Drops:
          * ``_last_heartbeat[agent_id]`` — clock anchor.
          * ``_agents[agent_id]`` — escalation flags, error counts, message
            timestamps, last progress payload.
          * ``_fully_acked_first_seen[agent_id]`` — BRC progress tracker.
          * ``_job_active_since[agent_id]`` / ``_never_seen_escalated``:
            the never-heartbeated anchor for the pre-restart Job (#3605).
            The next ``set_active_roles`` tick re-anchors the role at the
            time the respawned Job is first observed, so the restarted agent
            is measured from its own restart rather than from the dead Job.
          * any ``_active_alerts`` whose ``agent_id`` matches — those
            reference the dead container's anchor.

        No-op if the agent is unknown.
        """
        with self._lock:
            self._agents.pop(agent_id, None)
            self._last_heartbeat.pop(agent_id, None)
            self._fully_acked_first_seen.pop(agent_id, None)
            self._job_active_since.pop(agent_id, None)
            self._never_seen_escalated.discard(agent_id)
            # _active_alerts is a bounded deque (maxlen=200).  Filter in
            # place via clear()+extend to preserve the bound — rebinding to
            # ``deque(kept)`` would silently drop the maxlen and let the
            # buffer grow unboundedly.
            kept = [a for a in self._active_alerts if a.get("agent_id") != agent_id]
            self._active_alerts.clear()
            self._active_alerts.extend(kept)

    def _get_heartbeat_threshold(self) -> int:
        """Return the heartbeat timeout threshold for the current phase.

        During the implement phase, uses
        ``orchestrator_implement_heartbeat_timeout_seconds`` (default 600s).
        All other phases use ``orchestrator_heartbeat_timeout_seconds``
        (default 120s).
        """
        with self._lock:
            phase = self._current_phase
        if phase == "implement":
            return self._config.orchestrator_implement_heartbeat_timeout_seconds
        return self._config.orchestrator_heartbeat_timeout_seconds

    def _get_post_ack_confirmation_timeout(self) -> int:
        """Return the post-ACK confirm timeout for the current phase.

        Plan-phase post-ACK reconciliation (resolved decisions, feedback
        bodies, slice-DAG sanity) legitimately exceeds the 180s default on
        heavy pipelines (#2242). Plan uses
        ``orchestrator_plan_post_ack_confirmation_timeout_seconds`` (default
        300s); other phases use
        ``orchestrator_post_ack_confirmation_timeout_seconds`` (default 180s).
        """
        with self._lock:
            phase = self._current_phase
        if phase == "plan":
            return self._config.orchestrator_plan_post_ack_confirmation_timeout_seconds
        return self._config.orchestrator_post_ack_confirmation_timeout_seconds

    def _has_recent_activity(self, agent_id: str, now: float) -> tuple[bool, str | None]:
        """Return (defer, reason) for the focal-agent activity gate (#2190).

        Suppresses ``heartbeat_timeout`` / ``progress_stall`` alerts against
        an agent that is demonstrably alive — i.e. has emitted a
        :class:`EventType.CONTAINER_ACTIVITY` event within
        ``orchestrator_activity_quiet_seconds`` — even if no
        bus-level ``HEARTBEAT`` has arrived. The repro from #2190 was a
        coder mid-pytest with multi-minute blocking ``TaskOutput`` calls,
        committing along the way; the bus saw silence but the agent was
        making real progress.

        Returns ``(False, None)`` when the agent has never emitted an
        activity event (``last_activity == _NEVER_SEEN_ACTIVITY``) so a
        freshly spawned but truly silent agent still escalates on the
        heartbeat anchor.

        Setting ``orchestrator_activity_quiet_seconds=0`` disables the
        gate entirely — operator escape hatch if the gate produces
        false negatives in production.

        OR'd with :func:`_has_recent_peer_progress` (#2242) at the
        per-agent alert sites: focal-agent activity OR peer progress
        defers. The escalated flag is intentionally not set on defer so
        the next poll re-checks once activity goes stale.
        """
        threshold = self._config.orchestrator_activity_quiet_seconds
        if threshold <= 0:
            return False, None

        with self._lock:
            agent = self._agents.get(agent_id)
            last_activity = agent.last_activity if agent is not None else _NEVER_SEEN_ACTIVITY

        if last_activity <= _NEVER_SEEN_ACTIVITY:
            return False, None

        age = now - last_activity
        if 0 <= age < threshold:
            return True, f"container activity {int(age)}s ago"
        return False, None

    def _has_recent_peer_progress(
        self, exclude_agent_id: str, now: float
    ) -> tuple[bool, str | None]:
        """Return (defer, reason) for the per-agent alert progress gate (#2242).

        Sibling of :func:`routes.pipelines._check_brc_progress_gate`. Defers
        a per-agent ``heartbeat_timeout`` / ``progress_stall`` alert when
        *any* of the following has fired within
        ``orchestrator_alert_progress_gate_seconds``:

        * The BRC tracker's most recent ``CONSENSUS_PROPOSE`` or ACK/NACK
          timestamp on this pipeline.
        * A heartbeat from any peer agent other than ``exclude_agent_id``
          that is currently in the active-agent set.

        Self-exclusion only applies to the peer-heartbeat path. The BRC-bus
        path is NOT filtered by focal agent — ``get_latest_progress_timestamp``
        aggregates proposals + matrix ACK/NACK timestamps across the whole
        tracker. On a single-producer pipeline (BRC tracker registered, no
        peers) the producer's own recent propose/ACK therefore defers its
        own heartbeat alert until ``gate_seconds`` elapses past that
        timestamp; the effective stall-detection window in that case is
        ``heartbeat_threshold + gate_seconds`` (≈360s with defaults) rather
        than ``heartbeat_threshold`` (60s). For genuinely-dead containers
        this is fine — ``CONTAINER_STOPPED`` covers that — but for a hung
        process inside a live container, detection is delayed by up to
        ``gate_seconds``. Filtering the tracker by focal agent would
        require a new ``peer_consensus`` API; deferred until either the
        delay matters in practice or we ship per-role tracker timestamp
        accessors.

        Failures in any signal source are logged at WARNING and treated as
        "no signal from that source" — a crashed collector must never
        silently keep us off the alert surface (mirrors the consensus-gate
        contract).

        ``orchestrator_alert_progress_gate_seconds <= 0`` disables the gate.

        Active-agent filter: when a BRC tracker is registered for the
        pipeline, peer heartbeats are filtered to the tracker graph's
        ``all_roles()`` set — i.e. the current phase's roster as
        installed by :meth:`concurrent_executor.ConcurrentPhaseExecutor.spawn_all`
        — so prior-phase ghosts in ``_last_heartbeat`` (a singleton state
        not phase-stamped on transition) cannot defer current-phase
        alerts. This mirrors :func:`_check_brc_progress_gate`'s
        ``active_role_names`` filter; using ``self._agents.keys()``
        instead would be a no-op because every heartbeat write also
        populates ``_agents``. When no tracker is registered (early
        startup, between phases, or non-BRC phases) the filter is
        skipped — the gate falls back to peer heartbeats from any
        known agent. Same-role cross-phase pollution (``coder``
        reappearing across implement / implement-fix) is still possible
        because the heartbeat key is not phase-stamped; tracked under
        #2242 alongside the equivalent TODO in
        :func:`_check_brc_progress_gate`.
        """
        gate_seconds = self._config.orchestrator_alert_progress_gate_seconds
        if gate_seconds <= 0:
            return False, None

        # 1. BRC bus signals (pipeline-scope tracker; per-slice trackers are
        # not consulted from HealthMonitor — peer heartbeats below cover
        # sliced pipelines where bus activity may be partitioned). The same
        # tracker also supplies the active-role set used by step 2.
        active_role_set: set[str] | None = None
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(self._pipeline_id)
            if tracker is not None:
                ts = tracker.get_latest_progress_timestamp()
                if ts is not None:
                    age = now - ts.timestamp()
                    if 0 <= age < gate_seconds:
                        return True, f"BRC bus active {age:.0f}s ago"
                # The tracker's graph is rebuilt per phase (see
                # ``ConcurrentPhaseExecutor.spawn_all``), so
                # ``all_roles()`` is the current-phase active set.
                try:
                    active_role_set = set(tracker.graph.all_roles())
                except Exception as e:
                    logger.warning(
                        "Alert progress-gate active-role lookup failed",
                        pipeline_id=self._pipeline_id,
                        error=str(e),
                    )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "Alert progress-gate tracker check failed",
                pipeline_id=self._pipeline_id,
                error=str(e),
            )

        # 2. Peer heartbeats. ``active_role_set`` (from the tracker graph
        # above) drops stale heartbeats whose role isn't part of the
        # current phase. When no tracker is registered, the filter is
        # skipped so the gate degrades to "any known peer heartbeat
        # within the window defers" — the pre-#2242 fallback behavior.
        with self._lock:
            hb_snapshot = dict(self._last_heartbeat)

        latest_peer_hb: float | None = None
        for agent_id, hb_time in hb_snapshot.items():
            if agent_id == exclude_agent_id:
                continue
            if active_role_set is not None and agent_id not in active_role_set:
                continue
            if latest_peer_hb is None or hb_time > latest_peer_hb:
                latest_peer_hb = hb_time

        if latest_peer_hb is not None:
            age = now - latest_peer_hb
            if 0 <= age < gate_seconds:
                return True, f"peer heartbeat {age:.0f}s ago"

        return False, None

    def _orchestrator_skip_tripwire(self, agent_id: str) -> bool:
        """Return True if this agent should be skipped by tripwire checks.

        The orchestrator owns the BRC event loop (#3164): a role with no
        active one-shot Job is legitimately idle — the loop simply has not
        derived an actionable event for it yet — so alerting on it would be
        a false positive.

        An empty ``_active_jobs`` (default from init or explicitly set via
        ``set_active_roles(set())``) means there are zero active one-shot
        Jobs — every role is legitimately idle; all tripwires are skipped.
        """
        # Empty active_jobs (no active one-shot Jobs) → every role is idle
        if not self._active_jobs:
            return True
        return agent_id not in self._active_jobs

    def get_agent_activity_ages(self) -> dict[str, dict[str, float | None]]:
        """Return per-agent activity ages for the convergence-stall check (#3665).

        Returns a dict mapping agent_id -> {
            "last_heartbeat_age_s": seconds since last heartbeat (or None),
            "last_progress_age_s": seconds since last progress event (or None),
            "last_activity_age_s": seconds since last CONTAINER_ACTIVITY (or None),
        }

        Used by the event loop's convergence-stall check to suppress false
        alerts against agents that are actively working (recent heartbeats or
        container activity) even when the BRC bus is quiet.
        """
        now = time.time()
        result: dict[str, dict[str, float | None]] = {}
        with self._lock:
            for agent_id, agent in self._agents.items():
                hb_age = now - agent.last_heartbeat if agent.last_heartbeat > 0 else None
                progress_age = now - agent.last_progress if agent.last_progress > 0 else None
                activity_age = (
                    now - agent.last_activity
                    if agent.last_activity > _NEVER_SEEN_ACTIVITY
                    else None
                )
                result[agent_id] = {
                    "last_heartbeat_age_s": hb_age,
                    "last_progress_age_s": progress_age,
                    "last_activity_age_s": activity_age,
                }
        return result


    def _build_agent_evidence(self, agent_id: str) -> dict[str, Any]:
        """Build structured evidence for an escalation about ``agent_id``.

        Per #3665 priority 4: enriches OVERSEER_ALERT payloads with
        per-agent activity ages so operators can act without hand-investigation.
        Returns a dict with:

        * ``latest_heartbeat_age_s`` — seconds since last heartbeat
        * ``latest_progress_age_s`` — seconds since last progress event
        * ``latest_tool_call_age_s`` — seconds since last CONTAINER_ACTIVITY
        * ``last_progress_event`` — the most recent progress event data
        * ``blocking_agents`` — the BRC consensus blocking set (if tracker
          is available)

        NOTE: This method acquires ``self._lock``. Callers that are
        already inside a ``with self._lock:`` block (the escalation sites in
        ``check_heartbeats``, ``check_progress``, etc.) must use
        ``_build_agent_evidence_locked`` instead.
        """
        with self._lock:
            return self._build_agent_evidence_locked(agent_id)

    def _build_agent_evidence_locked(self, agent_id: str) -> dict[str, Any]:
        """Build evidence for ``agent_id`` — caller MUST hold ``self._lock``.

        Split from :meth:`_build_agent_evidence` to avoid deadlock: the
        escalation sites in ``check_heartbeats`` / ``check_progress`` / etc.
        already hold ``self._lock`` (a non-reentrant ``threading.Lock``), so
        calling ``_build_agent_evidence`` (which re-acquires the lock) would
        deadlock.
        """
        now = time.time()
        evidence: dict[str, Any] = {}

        agent = self._agents.get(agent_id)
        if agent is not None:
            evidence["latest_heartbeat_age_s"] = now - agent.last_heartbeat
            evidence["latest_progress_age_s"] = now - agent.last_progress
            if agent.last_activity > _NEVER_SEEN_ACTIVITY:
                evidence["latest_tool_call_age_s"] = now - agent.last_activity
            else:
                evidence["latest_tool_call_age_s"] = None
            evidence["last_progress_event"] = dict(agent.last_progress_data)

        # BRC blocking set (best-effort — does not need the lock)
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(self._pipeline_id)
            if tracker is not None:
                consensus = tracker.evaluate()
                evidence["blocking_agents"] = consensus.get("blocking_agents", [])
                evidence["consensus_state"] = {
                    "is_complete": consensus.get("is_complete", False),
                    "producer_phases": dict(tracker._producer_phases),
                    "reviewer_phases": dict(tracker._reviewer_phases),
                }
        except Exception:
            pass

        return evidence

    def _is_brc_idle(self, agent_id: str) -> bool:
        """Check if an agent is idle waiting for BRC upstream producers.

        A reviewer-only agent whose upstream producers are all still in
        WORKING phase is legitimately idle — it has nothing to review yet.
        Such agents should be excluded from heartbeat/progress alerts.

        Additionally, a reviewer-only agent within the post-propose grace
        period (after a producer proposes but before the reviewer has had
        time to start reviewing) is also suppressed from alerts.

        Returns True if the agent should be suppressed from alerts.
        """
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            return False

        tracker = get_peer_consensus_tracker(self._pipeline_id)
        if tracker is None:
            return False

        graph = tracker.graph

        # Only suppress reviewer-only roles (not dual-role producers)
        if not graph.is_reviewer(agent_id) or graph.is_producer(agent_id):
            return False

        # Suppress when all producers are still working (nothing to review)
        if tracker.are_all_producers_working(agent_id):
            return True

        # Post-propose grace: suppress reviewers within the grace window
        # after a producer proposes, giving them time to begin reviewing.
        earliest_proposal = tracker.get_earliest_proposal_time(agent_id)

        if earliest_proposal is not None:
            grace = self._config.post_proposal_grace_seconds
            if time.time() - earliest_proposal < grace:
                return True

        return False

    # -----------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------

    def _get_or_create_agent(self, agent_id: str) -> AgentState:
        """Get or create agent state."""
        if agent_id not in self._agents:
            now = time.time()
            self._agents[agent_id] = AgentState(agent_id=agent_id)
            self._agents[agent_id].last_heartbeat = now
            self._agents[agent_id].last_progress = now
            self._last_heartbeat[agent_id] = now
            # The role is no longer "never seen": ``_last_heartbeat`` is now
            # its anchor, so drop the Job-start fallback (#3605).
            self._job_active_since.pop(agent_id, None)
            self._never_seen_escalated.discard(agent_id)
        return self._agents[agent_id]

    def _sanitize_elapsed(
        self, agent_id: str, elapsed: float, now: float, anchor_kind: str
    ) -> tuple[float, bool]:
        """Return ``(elapsed, anchor_corrupt)``, clamping impossible values (#3605).

        Every anchor this monitor reads is written after the monitor was
        constructed, so no real stall can be older than the monitor itself.
        An ``elapsed`` beyond that bound means the anchor was corrupted. The
        #3605 repro was a restarted role anchored at the Unix epoch, reporting
        1784944189s (≈56 years) of "stall".

        Such a value is a bug signal, not a measurement, so it is logged at
        ERROR and the caller exempts the resulting alert from the alive-signal
        gates rather than letting a peer heartbeat silently defer it. The
        returned elapsed is clamped to the monitor's age so downstream
        consumers keyed on magnitude (notably the ``>= 300s`` BRC
        stall-demotion path in ``_run_concurrent``) cannot be tripped by the
        corrupt value.

        The only anchor that can go corrupt today by *bookkeeping* is the
        heartbeat one (``_job_active_since``, via the never-seen path this
        issue fixed). ``check_progress`` calls this too, but
        ``AgentState.last_progress`` is only ever assigned ``now``, so its
        corrupt branch is purely defensive symmetry — kept so a future writer
        of that anchor inherits the guard rather than reintroducing the epoch
        bug on the progress side. The one thing that does reach it is the
        clock skew documented at ``_created_at``: an ``AgentState`` created
        after a large backward step is anchored behind ``_created_at`` and is
        flagged like any other such anchor, on either tripwire.
        """
        monitor_age = max(0.0, now - self._created_at)
        if elapsed <= monitor_age + _ELAPSED_SANITY_SLACK_SECONDS:
            return elapsed, False

        logger.error(
            "Corrupt health anchor: elapsed exceeds monitor age",
            pipeline_id=self._pipeline_id,
            agent_id=agent_id,
            anchor_kind=anchor_kind,
            raw_elapsed_seconds=int(elapsed),
            monitor_age_seconds=int(monitor_age),
        )
        return monitor_age, True

    def _on_progress(self, event: Event) -> None:
        """Handle PROGRESS_EMITTED event (heartbeat or progress)."""
        if event.pipeline_id != self._pipeline_id:
            return

        # Accept both agent_id and agent_role for compatibility
        agent_id = event.data.get("agent_id") or event.data.get("agent_role")
        if not agent_id:
            return

        with self._lock:
            agent = self._get_or_create_agent(agent_id)
            now = time.time()

            event_type = event.data.get("type", "progress")
            if event_type == "heartbeat":
                agent.last_heartbeat = now
                self._last_heartbeat[agent_id] = now
                # Reset escalation state so future stalls are detected
                agent.heartbeat_escalated = False
            else:
                agent.last_progress = now
                agent.last_heartbeat = now
                self._last_heartbeat[agent_id] = now
                # Save old blocker before overwriting progress data
                old_blocker = agent.last_progress_data.get("blocker", "")
                agent.last_progress_data = event.data
                # Reset escalation state on new progress
                agent.heartbeat_escalated = False
                agent.progress_escalated = False
                # Only reset infra_error_escalated when the agent is no longer
                # blocked, or when the blocker text changes (indicating a new
                # infrastructure error).  Resetting on every progress event —
                # including re-emitted blocked events with the same blocker —
                # would defeat dedup for persistent blockers (#1489 review).
                new_state = event.data.get("state", "")
                new_blocker = event.data.get("blocker", "")
                if new_state != "blocked" or new_blocker != old_blocker:
                    agent.infra_error_escalated = False

    def _on_container_activity(self, event: Event) -> None:
        """Handle CONTAINER_ACTIVITY event — record focal-agent activity (#2190).

        Activity events fire on demonstrable signs of life that don't go
        through the bus-level HEARTBEAT path: today, successful commit
        registrations from the gateway commit observer. Used by
        :func:`_has_recent_activity` to suppress heartbeat/progress
        alerts against agents legitimately blocked in long tool calls.
        """
        if event.pipeline_id != self._pipeline_id:
            return

        agent_id = event.data.get("agent_id") or event.data.get("agent_role")
        if not agent_id:
            return

        with self._lock:
            agent = self._get_or_create_agent(agent_id)
            agent.last_activity = time.time()

    def _on_error(self, event: Event) -> None:
        """Handle ERROR event — track repeated identical errors."""
        if event.pipeline_id != self._pipeline_id:
            return

        agent_id = event.data.get("agent_id")
        error_msg = event.data.get("error", "")
        if not agent_id:
            return

        with self._lock:
            agent = self._get_or_create_agent(agent_id)
            agent.error_counts[error_msg] += 1
            count = agent.error_counts[error_msg]

        threshold = self._config.orchestrator_error_repeat_threshold
        if count >= threshold:
            self._escalate_error(agent_id, error_msg, count)

    def _on_container_stopped(self, event: Event) -> None:
        """Handle CONTAINER_STOPPED event — immediate HITL escalation."""
        if event.pipeline_id != self._pipeline_id:
            return

        agent_id = event.data.get("agent_id")
        exit_code = event.data.get("exit_code", -1)
        if not agent_id:
            return

        # #3064 slice-5: in orchestrator mode a container exit for a role
        # with no active Job is a ghost container (a pod that finished its
        # event and was reaped).  Only escalate when the role has an active
        # Job — a silent one-shot pod that died mid-event still alerts.  This
        # uses the same gate as the heartbeat/progress tripwires so the
        # criterion "container-exit tripwires apply ONLY while a Job is
        # active for that role" holds: with ``_active_jobs`` empty (no live
        # pods) every exit is a ghost and is suppressed, matching the
        # heartbeat path rather than inverting it.
        if self._orchestrator_skip_tripwire(agent_id):
            return

        escalation = {
            "type": "hitl",
            "agent_id": agent_id,
            "reason": f"Container exited unexpectedly (exit code {exit_code})",
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
            # #3665 priority 4: bundle structured evidence
            "evidence": self._build_agent_evidence(agent_id),
        }

        with self._lock:
            self._active_alerts.append(
                {
                    "id": str(uuid.uuid4()),
                    "pipeline_id": self._pipeline_id,
                    "agent_id": agent_id,
                    "alert_type": "container_exit",
                    "message": escalation["reason"],
                    "severity": "critical",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        for cb in self._escalation_callbacks:
            try:
                cb(escalation)
            except Exception as e:
                logger.error("Escalation callback error", error=str(e))

    def _on_message_sent(self, event: Event) -> None:
        """Handle MESSAGE_SENT event — track message rate and HEARTBEAT.

        Issue #1897: HEARTBEAT is a first-class heartbeat signal, equivalent
        to the legacy PROGRESS-type=heartbeat path in ``_on_progress``.  When
        an agent emits a HEARTBEAT message we reset ``last_heartbeat`` and
        clear ``heartbeat_escalated`` so Tier-1 alarms do not falsely trip
        on agents that adopt the new heartbeat type (RISK-2 mitigation).

        The legacy PROGRESS-heartbeat path remains as a fallback so existing
        agents that haven't migrated keep working — follow-up issue will
        remove it once HEARTBEAT adoption is 100%.
        """
        if event.pipeline_id != self._pipeline_id:
            return

        # Accept both agent_id and from_role for compatibility — routes/messages.py
        # emits MESSAGE_SENT events with ``from_role`` but the health monitor's
        # per-agent state is keyed by ``agent_id``. Use from_role as fallback.
        agent_id = event.data.get("agent_id") or event.data.get("from_role")
        if not agent_id:
            return

        message_type = event.data.get("message_type", "")

        now = time.time()
        rate_limit = self._config.orchestrator_message_rate_limit

        with self._lock:
            agent = self._get_or_create_agent(agent_id)
            agent.message_timestamps.append(now)

            # Keep only timestamps within the last 60 seconds
            cutoff = now - 60.0
            agent.message_timestamps = [ts for ts in agent.message_timestamps if ts >= cutoff]

            count = len(agent.message_timestamps)

            # HEARTBEAT resets last_heartbeat + clears escalation flag.
            if message_type == "HEARTBEAT":
                agent.last_heartbeat = now
                self._last_heartbeat[agent_id] = now
                agent.heartbeat_escalated = False

        if count > rate_limit:
            throttle_data = {
                "agent_id": agent_id,
                "message_count": count,
                "rate_limit": rate_limit,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            with self._lock:
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": agent_id,
                        "alert_type": "message_rate",
                        "message": f"Message rate {count}/min exceeds limit {rate_limit}/min",
                        "severity": "warning",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )

            for cb in self._throttle_callbacks:
                try:
                    cb(throttle_data)
                except Exception as e:
                    logger.error("Throttle callback error", error=str(e))

    # -----------------------------------------------------------------
    # Tripwire checks (called periodically)
    # -----------------------------------------------------------------

    def check_heartbeats(self) -> list[dict[str, Any]]:
        """Evaluate heartbeat timeout rule.

        When no heartbeat is received within the threshold, immediately
        escalate to the overseer (or HITL if overseer is disabled).
        The ``heartbeat_escalated`` flag prevents duplicate escalations
        on subsequent poll cycles.

        Phase-aware: uses a higher threshold during the implement phase.
        BRC-idle suppression: skips reviewer-only agents whose upstream
        producers have not yet proposed.

        Returns:
            List of action dicts for agents that have timed out.
        """
        actions: list[dict[str, Any]] = []
        threshold = self._get_heartbeat_threshold()
        now = time.time()

        with self._lock:
            # Snapshot all state inside the lock to avoid TOCTOU races
            snapshot: list[tuple[str, float, bool]] = [
                (
                    agent.agent_id,
                    self._last_heartbeat.get(agent.agent_id, agent.last_heartbeat),
                    agent.heartbeat_escalated,
                )
                for agent in self._agents.values()
            ]
            # #3064 slice-5: also include active-job roles that have never
            # sent a heartbeat (pod spawned but agent never started).
            # Without this a silent mid-event pod would be invisible to the
            # snapshot and never trip the heartbeat timeout.
            #
            # #3605: anchor those roles at the time their Job was first
            # observed active, NOT at 0.0; ``now - 0.0`` reported a Unix
            # epoch timestamp as the elapsed stall time.
            #
            # ``set_active_roles`` is the sole writer of ``_active_jobs``, so
            # it normally anchors every role reached here. ``setdefault``
            # covers the gap ``reset_agent`` opens: it drops the anchor while
            # deliberately leaving the role in ``_active_jobs``, so a poll
            # landing before the next ``set_active_roles`` tick re-anchors at
            # check time (≈ the restart) instead of raising ``KeyError`` or
            # resurrecting the dead Job's clock.
            known_agent_ids = {a_id for a_id, _, _ in snapshot}
            for role in self._active_jobs:
                if role not in known_agent_ids:
                    anchor = self._job_active_since.setdefault(role, now)
                    snapshot.append((role, anchor, role in self._never_seen_escalated))

        for agent_id, last_hb, already_escalated in snapshot:
            elapsed, anchor_corrupt = self._sanitize_elapsed(
                agent_id, now - last_hb, now, "heartbeat"
            )
            if elapsed <= threshold:
                continue

            if already_escalated:
                continue  # already escalated — don't re-escalate

            # BRC-idle suppression: skip reviewer-only agents waiting for
            # upstream producers to propose
            if self._is_brc_idle(agent_id):
                continue

            # #3064 slice-5: orchestrator-mode suppression — skip roles
            # with no active Job. This is a separate gate from BRC-idle
            # because in orchestrator mode even producer roles have no pod
            # between events, and that is normal.
            if self._orchestrator_skip_tripwire(agent_id):
                continue

            # Focal-agent activity gate (#2190) OR alive-signal peer-progress
            # gate (#2242): defer if either fires. The activity gate catches
            # an agent legitimately blocked in a long tool call (e.g. a
            # multi-minute background pytest) that is still committing but
            # not emitting bus-level HEARTBEAT. The peer-progress gate
            # catches the case where the broader pipeline is clearly alive
            # via BRC bus signals or peer heartbeats. Don't set
            # heartbeat_escalated on defer — the next poll re-checks.
            #
            # Overseer exemption (#2430): the watchdog's silence is the
            # signal regardless of peer activity. Without this, a healthy
            # BRC roster defers overseer escalations indefinitely.
            #
            # Corrupt-anchor exemption (#3605): an impossible elapsed value is
            # a bug in the monitor's own bookkeeping. Deferring it on a peer's
            # liveness is how a role with no pod and no Job stayed silently
            # suppressed for the whole pipeline; surface it instead.
            if agent_id != _OVERSEER_AGENT_ID and not anchor_corrupt:
                defer, gate_reason = self._has_recent_activity(agent_id, now)
                if not defer:
                    defer, gate_reason = self._has_recent_peer_progress(agent_id, now)
                if defer:
                    logger.info(
                        "Heartbeat alert deferred by alive-signal gate",
                        pipeline_id=self._pipeline_id,
                        agent_id=agent_id,
                        elapsed_seconds=int(elapsed),
                        reason=gate_reason,
                    )
                    continue

            reason = f"No heartbeat for {int(elapsed)}s (threshold: {threshold}s)"
            if anchor_corrupt:
                reason += " [corrupt heartbeat anchor: elapsed clamped to monitor age]"
            action = {
                "action": "escalate",
                "agent_id": agent_id,
                "reason": reason,
                "elapsed_seconds": int(elapsed),
                "anchor_corrupt": anchor_corrupt,
            }
            actions.append(action)

            escalation_type = self._resolve_escalation_target(agent_id)
            escalation = {
                "type": escalation_type,
                "agent_id": agent_id,
                "reason": action["reason"],
                "timestamp": datetime.now(UTC).isoformat(),
                # #3665 priority 4: bundle structured evidence
                "evidence": self._build_agent_evidence(agent_id),
            }

            with self._lock:
                agent_state = self._agents.get(agent_id)
                if agent_state:
                    agent_state.heartbeat_escalated = True
                else:
                    # Never-seen role: it has no ``AgentState`` to carry the
                    # flag, so dedupe through the parallel marker set (#3605).
                    # Without this the alert re-fires on every poll tick.
                    self._never_seen_escalated.add(agent_id)
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": agent_id,
                        "alert_type": "heartbeat_timeout",
                        "message": action["reason"],
                        "severity": "warning",
                        "timestamp": datetime.now(UTC).isoformat(),
                        # #3665 priority 4: include evidence in the alert record
                        "evidence": self._build_agent_evidence_locked(agent_id),
                    }
                )

            for cb in self._escalation_callbacks:
                try:
                    cb(escalation)
                except Exception as e:
                    logger.error("Escalation callback error", error=str(e))

        return actions

    def check_progress(self) -> list[dict[str, Any]]:
        """Evaluate progress stall rule.

        When no progress is received within the threshold, immediately
        escalate to the overseer (or HITL if overseer is disabled).
        The ``progress_escalated`` flag prevents duplicate escalations
        on subsequent poll cycles.

        Phase-aware: uses a higher threshold during the implement phase.
        BRC-idle suppression: skips reviewer-only agents whose upstream
        producers have not yet proposed.

        Returns:
            List of action dicts.
        """
        actions: list[dict[str, Any]] = []
        threshold = self._get_heartbeat_threshold()
        now = time.time()

        with self._lock:
            # Snapshot all state inside the lock to avoid TOCTOU races
            snapshot = [
                (
                    agent.agent_id,
                    agent.last_progress,
                    agent.progress_escalated,
                )
                for agent in self._agents.values()
            ]

        for agent_id, last_progress, already_escalated in snapshot:
            # Defensive symmetry only: ``last_progress`` is never anchored at
            # the epoch, so ``anchor_corrupt`` cannot fire here absent the
            # clock skew documented at ``_created_at``. The real #3605 trigger
            # is heartbeat-side. See ``_sanitize_elapsed``.
            elapsed, anchor_corrupt = self._sanitize_elapsed(
                agent_id, now - last_progress, now, "progress"
            )
            if elapsed <= threshold:
                continue

            if already_escalated:
                continue  # already escalated — don't re-escalate

            # BRC-idle suppression: skip reviewer-only agents waiting for
            # upstream producers to propose
            if self._is_brc_idle(agent_id):
                continue

            # #3064 slice-5: orchestrator-mode suppression — skip roles
            # with no active Job.
            if self._orchestrator_skip_tripwire(agent_id):
                continue

            # Focal-agent activity gate (#2190) OR alive-signal peer-progress
            # gate (#2242): same OR pattern as check_heartbeats — defer when
            # either fires.
            #
            # Overseer exemption (#2430): see check_heartbeats — the
            # watchdog's silence must surface even when peers are alive.
            # Corrupt-anchor exemption (#3605): see check_heartbeats.
            if agent_id != _OVERSEER_AGENT_ID and not anchor_corrupt:
                defer, gate_reason = self._has_recent_activity(agent_id, now)
                if not defer:
                    defer, gate_reason = self._has_recent_peer_progress(agent_id, now)
                if defer:
                    logger.info(
                        "Progress alert deferred by alive-signal gate",
                        pipeline_id=self._pipeline_id,
                        agent_id=agent_id,
                        elapsed_seconds=int(elapsed),
                        reason=gate_reason,
                    )
                    continue

            reason = f"No progress for {int(elapsed)}s (threshold: {threshold}s)"
            if anchor_corrupt:
                reason += " [corrupt progress anchor: elapsed clamped to monitor age]"
            action = {
                "action": "escalate",
                "agent_id": agent_id,
                "reason": reason,
                "elapsed_seconds": int(elapsed),
                "anchor_corrupt": anchor_corrupt,
            }
            actions.append(action)

            escalation_type = self._resolve_escalation_target(agent_id)
            escalation = {
                "type": escalation_type,
                "agent_id": agent_id,
                "reason": action["reason"],
                "timestamp": datetime.now(UTC).isoformat(),
                # #3665 priority 4: bundle structured evidence
                "evidence": self._build_agent_evidence(agent_id),
            }

            with self._lock:
                agent_state = self._agents.get(agent_id)
                if agent_state:
                    agent_state.progress_escalated = True
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": agent_id,
                        "alert_type": "progress_stall",
                        "message": action["reason"],
                        "severity": "warning",
                        "timestamp": datetime.now(UTC).isoformat(),
                        # #3665 priority 4: include evidence in the alert record
                        "evidence": self._build_agent_evidence_locked(agent_id),
                    }
                )

            for cb in self._escalation_callbacks:
                try:
                    cb(escalation)
                except Exception as e:
                    logger.error("Escalation callback error", error=str(e))

        return actions

    # -----------------------------------------------------------------
    # Escalation helpers
    # -----------------------------------------------------------------

    def _resolve_escalation_target(self, agent_id: str) -> str:
        """Return the escalation route for an alert about ``agent_id``.

        The overseer's own escalations always go to HITL — routing them to
        the agent that is itself the problem would silently swallow them
        (#2430). All other agents go to the overseer when enabled, HITL
        otherwise.
        """
        if agent_id == _OVERSEER_AGENT_ID:
            return "hitl"
        return "overseer" if self._config.overseer_enabled else "hitl"

    def _escalate_error(self, agent_id: str, error_msg: str, count: int) -> None:
        """Escalate repeated identical errors."""
        escalation_type = self._resolve_escalation_target(agent_id)
        escalation = {
            "type": escalation_type,
            "agent_id": agent_id,
            "reason": f"Repeated error ({count}x): {error_msg[:200]}",
            "error_message": error_msg,
            "count": count,
            "timestamp": datetime.now(UTC).isoformat(),
            # #3665 priority 4: bundle structured evidence
            "evidence": self._build_agent_evidence(agent_id),
        }

        with self._lock:
            self._active_alerts.append(
                {
                    "id": str(uuid.uuid4()),
                    "pipeline_id": self._pipeline_id,
                    "agent_id": agent_id,
                    "alert_type": "repeated_error",
                    "message": escalation["reason"],
                    "severity": "warning",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        for cb in self._escalation_callbacks:
            try:
                cb(escalation)
            except Exception as e:
                logger.error("Escalation callback error", error=str(e))

    # -----------------------------------------------------------------
    # Alert management
    # -----------------------------------------------------------------

    def get_active_alerts(self) -> list[dict[str, Any]]:
        """Return all active alerts.

        Returns:
            List of alert dicts.
        """
        with self._lock:
            return list(self._active_alerts)

    def resolve_alerts(self, agent_id: str, alert_type: str) -> None:
        """Remove alerts matching agent_id and alert_type.

        Args:
            agent_id: Agent whose alerts to resolve.
            alert_type: Type of alert to resolve.
        """
        with self._lock:
            filtered = deque(
                (
                    a
                    for a in self._active_alerts
                    if not (a.get("agent_id") == agent_id and a.get("alert_type") == alert_type)
                ),
                maxlen=200,
            )
            self._active_alerts = filtered

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def start(self, pipeline_id: str | None = None) -> None:
        """Begin monitoring (currently a no-op, event subscriptions are in __init__)."""
        pass

    def stop(self, pipeline_id: str | None = None) -> None:
        """Stop monitoring and unsubscribe from events."""
        self._event_bus.unsubscribe(EventType.PROGRESS_EMITTED, self._on_progress)
        self._event_bus.unsubscribe(EventType.ERROR, self._on_error)
        self._event_bus.unsubscribe(EventType.CONTAINER_STOPPED, self._on_container_stopped)
        self._event_bus.unsubscribe(EventType.MESSAGE_SENT, self._on_message_sent)
        self._event_bus.unsubscribe(EventType.CONTAINER_ACTIVITY, self._on_container_activity)

    def _check_infra_errors(self) -> list[dict[str, Any]]:
        """Detect blocked progress events with infrastructure error keywords.

        Inspects each agent's latest progress event for state=blocked with
        blocker text matching INFRA_ERROR_PATTERNS. Creates a critical alert
        and fires escalation callbacks when detected.

        Returns:
            List of action dicts for agents with infrastructure errors.
        """
        actions: list[dict[str, Any]] = []

        with self._lock:
            snapshot = [
                (
                    agent.agent_id,
                    agent.last_progress_data.copy(),
                    agent.infra_error_escalated,
                )
                for agent in self._agents.values()
            ]

        for agent_id, progress_data, already_escalated in snapshot:
            if already_escalated:
                continue

            state = progress_data.get("state", "")
            if state != "blocked":
                continue

            blocker = progress_data.get("blocker", "")
            if not blocker:
                continue

            # Check if blocker matches any infrastructure error pattern
            if not any(pattern.search(blocker) for pattern in INFRA_ERROR_PATTERNS):
                continue

            action = {
                "action": "escalate",
                "agent_id": agent_id,
                "reason": f"Infrastructure error detected: {blocker[:200]}",
                "blocker": blocker,
                "alert_type": "infrastructure_error",
            }
            actions.append(action)

            escalation = {
                "type": "hitl",
                "agent_id": agent_id,
                "reason": action["reason"],
                "blocker": blocker,
                "timestamp": datetime.now(UTC).isoformat(),
                # #3665 priority 4: bundle structured evidence
                "evidence": self._build_agent_evidence(agent_id),
            }

            with self._lock:
                agent_state = self._agents.get(agent_id)
                if agent_state:
                    agent_state.infra_error_escalated = True
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": agent_id,
                        "alert_type": "infrastructure_error",
                        "message": action["reason"],
                        "severity": "critical",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )

            for cb in self._escalation_callbacks:
                try:
                    cb(escalation)
                except Exception as e:
                    logger.error("Escalation callback error", error=str(e))

        return actions

    def check_brc_progress(self) -> list[dict[str, Any]]:
        """Evaluate BRC protocol progress for fully-ACKed producers.

        Producers that have been fully ACKed by all reviewers but have not
        yet sent CONFIRMED are tracked. If they remain in this state longer
        than ``orchestrator_post_ack_confirmation_timeout_seconds``, an
        escalation is raised regardless of heartbeat activity.

        This catches producers stuck in a heartbeat loop post-ACK that
        would otherwise go undetected by liveness-based checks.

        Returns:
            List of action dicts for producers that have timed out.
        """
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            return []

        tracker = get_peer_consensus_tracker(self._pipeline_id)
        if tracker is None:
            return []

        fully_acked = tracker.get_fully_acked_producers()
        now = time.time()
        timeout = self._get_post_ack_confirmation_timeout()
        actions: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []

        with self._lock:
            # Clean up tracking for producers no longer in the fully-acked set
            # (they confirmed or withdrew)
            stale_keys = [k for k in self._fully_acked_first_seen if k not in fully_acked]
            for key in stale_keys:
                del self._fully_acked_first_seen[key]
                # Reset escalation flag when producer leaves fully-acked state
                agent_state = self._agents.get(key)
                if agent_state:
                    agent_state.brc_progress_escalated = False

            # Per-iteration breadcrumb so post-mortems can verify the check
            # ran and what it observed (#2079).
            if fully_acked:
                logger.info(
                    "BRC progress check observed fully-acked producers",
                    pipeline_id=self._pipeline_id,
                    producers={
                        p: round(now - self._fully_acked_first_seen.get(p, now), 1)
                        for p in fully_acked
                    },
                    timeout_seconds=timeout,
                )

            # Timeout is measured from when the monitor first observed the
            # fully-acked state (_fully_acked_first_seen), not from the
            # original proposal timestamp, because the monitor may start
            # after proposals were already made.
            for producer, _proposal_ts in fully_acked.items():
                # Record first time we saw this producer as fully-acked
                if producer not in self._fully_acked_first_seen:
                    self._fully_acked_first_seen[producer] = now

                first_seen = self._fully_acked_first_seen[producer]
                elapsed = now - first_seen

                if elapsed <= timeout:
                    continue

                # Skip producers with no agent state (never emitted
                # heartbeat/progress).  This branch is unexpected in
                # practice — every producer in the fully-acked set has
                # at minimum proposed, which routes through MESSAGE_SENT
                # and registers agent_state.  Log loudly so future
                # post-mortems can audit (#2079).
                agent_state = self._agents.get(producer)
                if not agent_state:
                    logger.warning(
                        "BRC progress timeout but producer has no agent_state",
                        pipeline_id=self._pipeline_id,
                        producer=producer,
                        elapsed_seconds=int(elapsed),
                    )
                    continue
                if agent_state.brc_progress_escalated:
                    continue

                action = {
                    "action": "escalate",
                    "agent_id": producer,
                    "reason": (
                        f"Producer {producer} fully ACKed but not confirmed "
                        f"for {int(elapsed)}s (timeout: {timeout}s)"
                    ),
                    "elapsed_seconds": int(elapsed),
                    "alert_type": "brc_confirmation_timeout",
                }
                actions.append(action)

                logger.warning(
                    "BRC progress timeout — escalating",
                    pipeline_id=self._pipeline_id,
                    producer=producer,
                    elapsed_seconds=int(elapsed),
                    timeout_seconds=timeout,
                )

                escalation_type = self._resolve_escalation_target(producer)
                escalation = {
                    "type": escalation_type,
                    "agent_id": producer,
                    "reason": action["reason"],
                    "alert_type": "brc_confirmation_timeout",
                    "elapsed_seconds": int(elapsed),
                    "timestamp": datetime.now(UTC).isoformat(),
                    # #3665 priority 4: bundle structured evidence
                    "evidence": self._build_agent_evidence_locked(producer),
                }
                escalations.append(escalation)

                agent_state.brc_progress_escalated = True
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": producer,
                        "alert_type": "brc_confirmation_timeout",
                        "message": action["reason"],
                        "severity": "warning",
                        "timestamp": datetime.now(UTC).isoformat(),
                        # #3665 priority 4: include evidence in the alert record
                        "evidence": self._build_agent_evidence_locked(producer),
                    }
                )

        # Fire callbacks outside the lock to avoid holding it during external calls
        for escalation in escalations:
            for cb in self._escalation_callbacks:
                try:
                    cb(escalation)
                except Exception as e:
                    logger.error("Escalation callback error", error=str(e))

        return actions

    def check_tripwires(self, pipeline_id: str | None = None) -> list[dict[str, Any]]:
        """Evaluate all tripwire rules and return combined actions.

        Returns:
            List of all actions/alerts generated.
        """
        actions: list[dict[str, Any]] = []
        actions.extend(self.check_heartbeats())
        actions.extend(self.check_progress())
        actions.extend(self._check_infra_errors())
        actions.extend(self.check_brc_progress())
        return actions


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_health_monitor: HealthMonitor | None = None
_health_monitor_lock = threading.Lock()


def get_health_monitor() -> HealthMonitor | None:
    """Get the singleton health monitor instance.

    Returns None if not yet initialized (requires event bus and config).

    Returns:
        HealthMonitor instance or None.
    """
    return _health_monitor


def init_health_monitor(
    event_bus: EventBus,
    pipeline_id: str,
    config: PipelineConfig,
) -> HealthMonitor:
    """Initialize the singleton health monitor.

    Args:
        event_bus: The EventBus to subscribe to.
        pipeline_id: Pipeline being monitored.
        config: Pipeline configuration.

    Returns:
        HealthMonitor instance.
    """
    global _health_monitor
    with _health_monitor_lock:
        _health_monitor = HealthMonitor(
            event_bus=event_bus,
            pipeline_id=pipeline_id,
            config=config,
        )
    return _health_monitor
