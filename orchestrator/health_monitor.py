"""
Deterministic tripwire processor for pipeline health monitoring.

Implements five rules:
1. Heartbeat timeout - auto-nudge when no heartbeat within threshold
2. Container exit - immediate HITL escalation
3. Repeated identical errors - escalate after threshold
4. Message volume spike - auto-throttle above rate limit
5. Progress stall - nudge, then escalate if unresolved

The HealthMonitor subscribes to EventBus events and maintains per-agent
state. It does NOT use LLM classifiers — those belong to the overseer.
"""

import sys
import threading
import time
import uuid
from collections import defaultdict
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
    nudge_count: int = 0
    progress_nudge_count: int = 0


class HealthMonitor:
    """Deterministic tripwire processor for pipeline health.

    Subscribes to EventBus events and evaluates five tripwire rules
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

        # Per-agent state
        self._agents: dict[str, AgentState] = {}

        # Callbacks
        self._escalation_callbacks: list[EscalationCallback] = []
        self._throttle_callbacks: list[ThrottleCallback] = []

        # Active alerts
        self._active_alerts: list[dict[str, Any]] = []

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
            else:
                agent.last_progress = now
                agent.last_heartbeat = now
                self._last_heartbeat[agent_id] = now
                # Reset progress nudge count on new progress
                agent.progress_nudge_count = 0

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

        Returns:
            List of action dicts for agents that have timed out.
        """
        actions: list[dict[str, Any]] = []
        threshold = self._config.orchestrator_heartbeat_timeout_seconds
        now = time.time()

        with self._lock:
            # Snapshot all state inside the lock to avoid TOCTOU races
            snapshot = [
                (agent.agent_id, self._last_heartbeat.get(agent.agent_id, agent.last_heartbeat))
                for agent in self._agents.values()
            ]

        for agent_id, last_hb in snapshot:
            elapsed = now - last_hb
            if elapsed > threshold:
                action = {
                    "action": "nudge",
                    "agent_id": agent_id,
                    "reason": f"No heartbeat for {int(elapsed)}s (threshold: {threshold}s)",
                    "elapsed_seconds": int(elapsed),
                }
                actions.append(action)

                with self._lock:
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

        return actions

    def check_progress(self) -> list[dict[str, Any]]:
        """Evaluate progress stall rule.

        First stall triggers a nudge. If the agent doesn't resume
        progress after nudge, escalate to overseer or HITL.

        Returns:
            List of action dicts.
        """
        actions: list[dict[str, Any]] = []
        threshold = self._config.orchestrator_heartbeat_timeout_seconds
        now = time.time()

        with self._lock:
            # Snapshot all state inside the lock to avoid TOCTOU races
            snapshot = [
                (agent.agent_id, agent.last_progress, agent.progress_nudge_count)
                for agent in self._agents.values()
            ]

        for agent_id, last_progress, nudge_count in snapshot:
            elapsed = now - last_progress
            if elapsed > threshold:
                if nudge_count == 0:
                    # First stall — nudge
                    action = {
                        "action": "nudge",
                        "agent_id": agent_id,
                        "reason": f"No progress for {int(elapsed)}s",
                    }
                    actions.append(action)
                    with self._lock:
                        agent_state = self._agents.get(agent_id)
                        if agent_state:
                            agent_state.progress_nudge_count += 1
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
                else:
                    # Second stall — escalate
                    action = {
                        "action": "escalate",
                        "agent_id": agent_id,
                        "reason": f"Progress stall unresolved after nudge ({int(elapsed)}s)",
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
                        self._active_alerts.append(
                            {
                                "id": str(uuid.uuid4()),
                                "pipeline_id": self._pipeline_id,
                                "agent_id": agent_id,
                                "alert_type": "progress_stall_escalated",
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
            self._active_alerts = [
                a
                for a in self._active_alerts
                if not (a.get("agent_id") == agent_id and a.get("alert_type") == alert_type)
            ]

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

    def check_tripwires(self, pipeline_id: str | None = None) -> list[dict[str, Any]]:
        """Evaluate all tripwire rules and return combined actions.

        Returns:
            List of all actions/alerts generated.
        """
        actions: list[dict[str, Any]] = []
        actions.extend(self.check_heartbeats())
        actions.extend(self.check_progress())
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
