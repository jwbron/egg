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


@dataclass
class AgentState:
    """Per-agent tracking state."""

    agent_id: str
    last_heartbeat: float = field(default_factory=time.time)
    last_progress: float = field(default_factory=time.time)
    error_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    message_timestamps: list[float] = field(default_factory=list)
    heartbeat_escalated: bool = False
    progress_escalated: bool = False
    infra_error_escalated: bool = False
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

        # Subscribe to events
        self._event_bus.subscribe(EventType.PROGRESS_EMITTED, self._on_progress)
        self._event_bus.subscribe(EventType.ERROR, self._on_error)
        self._event_bus.subscribe(EventType.CONTAINER_STOPPED, self._on_container_stopped)
        self._event_bus.subscribe(EventType.MESSAGE_SENT, self._on_message_sent)

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
        self._current_phase = phase

    def _get_heartbeat_threshold(self) -> int:
        """Return the heartbeat timeout threshold for the current phase.

        During the implement phase, uses
        ``orchestrator_implement_heartbeat_timeout_seconds`` (default 600s).
        All other phases use ``orchestrator_heartbeat_timeout_seconds``
        (default 120s).
        """
        if self._current_phase == "implement":
            return self._config.orchestrator_implement_heartbeat_timeout_seconds
        return self._config.orchestrator_heartbeat_timeout_seconds

    def _is_brc_idle(self, agent_id: str) -> bool:
        """Check if an agent is idle waiting for BRC upstream producers.

        A reviewer-only agent whose upstream producers are all still in
        WORKING phase is legitimately idle — it has nothing to review yet.
        Such agents should be excluded from heartbeat/progress alerts.

        Returns True if the agent should be suppressed from alerts.
        """
        try:
            from peer_consensus import get_peer_consensus_tracker
            from review_graph import ReviewGraph
        except ImportError:
            return False

        tracker = get_peer_consensus_tracker(self._pipeline_id)
        if tracker is None:
            return False

        graph: ReviewGraph = tracker.graph

        # Only suppress reviewer-only roles (not dual-role producers)
        if not graph.is_reviewer(agent_id) or graph.is_producer(agent_id):
            return False

        # Check if all upstream producers are still in WORKING phase
        producers = graph.producers_for(agent_id)
        if not producers:
            return False

        from egg_orchestrator.types import ConsensusPhase

        with tracker._lock:
            for producer in producers:
                phase = tracker._producer_phases.get(producer)
                if phase != ConsensusPhase.WORKING:
                    # At least one producer has proposed — reviewer should be active
                    return False

        return True

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
        return self._agents[agent_id]

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

        escalation = {
            "type": "hitl",
            "agent_id": agent_id,
            "reason": f"Container exited unexpectedly (exit code {exit_code})",
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
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
        """Handle MESSAGE_SENT event — track message rate."""
        if event.pipeline_id != self._pipeline_id:
            return

        agent_id = event.data.get("agent_id")
        if not agent_id:
            return

        now = time.time()
        rate_limit = self._config.orchestrator_message_rate_limit

        with self._lock:
            agent = self._get_or_create_agent(agent_id)
            agent.message_timestamps.append(now)

            # Keep only timestamps within the last 60 seconds
            cutoff = now - 60.0
            agent.message_timestamps = [ts for ts in agent.message_timestamps if ts >= cutoff]

            count = len(agent.message_timestamps)

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
            snapshot = [
                (
                    agent.agent_id,
                    self._last_heartbeat.get(agent.agent_id, agent.last_heartbeat),
                    agent.heartbeat_escalated,
                )
                for agent in self._agents.values()
            ]

        for agent_id, last_hb, already_escalated in snapshot:
            elapsed = now - last_hb
            if elapsed <= threshold:
                continue

            if already_escalated:
                continue  # already escalated — don't re-escalate

            # BRC-idle suppression: skip reviewer-only agents waiting for
            # upstream producers to propose
            if self._is_brc_idle(agent_id):
                continue

            action = {
                "action": "escalate",
                "agent_id": agent_id,
                "reason": f"No heartbeat for {int(elapsed)}s (threshold: {threshold}s)",
                "elapsed_seconds": int(elapsed),
            }
            actions.append(action)

            escalation_type = "overseer" if self._config.overseer_enabled else "hitl"
            escalation = {
                "type": escalation_type,
                "agent_id": agent_id,
                "reason": action["reason"],
                "timestamp": datetime.now(UTC).isoformat(),
            }

            with self._lock:
                agent_state = self._agents.get(agent_id)
                if agent_state:
                    agent_state.heartbeat_escalated = True
                self._active_alerts.append(
                    {
                        "id": str(uuid.uuid4()),
                        "pipeline_id": self._pipeline_id,
                        "agent_id": agent_id,
                        "alert_type": "heartbeat_timeout",
                        "message": action["reason"],
                        "severity": "warning",
                        "timestamp": datetime.now(UTC).isoformat(),
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
            elapsed = now - last_progress
            if elapsed <= threshold:
                continue

            if already_escalated:
                continue  # already escalated — don't re-escalate

            # BRC-idle suppression: skip reviewer-only agents waiting for
            # upstream producers to propose
            if self._is_brc_idle(agent_id):
                continue

            action = {
                "action": "escalate",
                "agent_id": agent_id,
                "reason": f"No progress for {int(elapsed)}s (threshold: {threshold}s)",
                "elapsed_seconds": int(elapsed),
            }
            actions.append(action)

            escalation_type = "overseer" if self._config.overseer_enabled else "hitl"
            escalation = {
                "type": escalation_type,
                "agent_id": agent_id,
                "reason": action["reason"],
                "timestamp": datetime.now(UTC).isoformat(),
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

    def _escalate_error(self, agent_id: str, error_msg: str, count: int) -> None:
        """Escalate repeated identical errors."""
        escalation_type = "overseer" if self._config.overseer_enabled else "hitl"
        escalation = {
            "type": escalation_type,
            "agent_id": agent_id,
            "reason": f"Repeated error ({count}x): {error_msg[:200]}",
            "error_message": error_msg,
            "count": count,
            "timestamp": datetime.now(UTC).isoformat(),
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

    def check_tripwires(self, pipeline_id: str | None = None) -> list[dict[str, Any]]:
        """Evaluate all tripwire rules and return combined actions.

        Returns:
            List of all actions/alerts generated.
        """
        actions: list[dict[str, Any]] = []
        actions.extend(self.check_heartbeats())
        actions.extend(self.check_progress())
        actions.extend(self._check_infra_errors())
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
