"""
Event system for pipeline state changes.

Provides a pub/sub mechanism for pipeline lifecycle events,
enabling webhooks, plugins, and monitoring integrations.
"""

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from queue import Empty, Queue
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


logger = get_logger("orchestrator.events")


class EventType(StrEnum):
    """Types of pipeline events."""

    # Pipeline lifecycle
    PIPELINE_CREATED = "pipeline.created"
    PIPELINE_STARTED = "pipeline.started"
    PIPELINE_COMPLETED = "pipeline.completed"
    PIPELINE_FAILED = "pipeline.failed"
    PIPELINE_CANCELLED = "pipeline.cancelled"

    # Phase transitions
    PHASE_STARTED = "phase.started"
    PHASE_COMPLETED = "phase.completed"
    PHASE_FAILED = "phase.failed"

    # Agent lifecycle
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    AGENT_TIMEOUT = "agent.timeout"

    # Container events
    CONTAINER_SPAWNED = "container.spawned"
    CONTAINER_STOPPED = "container.stopped"
    CONTAINER_REMOVED = "container.removed"

    # HITL events
    DECISION_CREATED = "decision.created"
    DECISION_RESOLVED = "decision.resolved"

    # System events
    HEALTH_CHECK = "system.health_check"
    HEALTH_CHECK_STARTED = "system.health_check.started"
    HEALTH_CHECK_COMPLETED = "system.health_check.completed"
    HEALTH_CHECK_DEGRADED = "system.health_check.degraded"
    HEALTH_CHECK_FAILED = "system.health_check.failed"
    ERROR = "system.error"


@dataclass
class Event:
    """A pipeline event."""

    event_type: EventType
    pipeline_id: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "orchestrator"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_type": self.event_type.value,
            "pipeline_id": self.pipeline_id,
            "timestamp": self.timestamp.isoformat() + "Z",
            "data": self.data,
            "source": self.source,
        }


# Type alias for event handlers
EventHandler = Callable[[Event], None]


class EventBus:
    """Central event bus for pub/sub messaging.

    Supports:
    - Subscribing handlers to specific event types
    - Subscribing handlers to all events (wildcard)
    - Async event processing
    - Event history for debugging
    """

    def __init__(
        self,
        max_history: int = 100,
        async_delivery: bool = False,
    ):
        """Initialize the event bus.

        Args:
            max_history: Maximum events to keep in history
            async_delivery: Whether to deliver events asynchronously
        """
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._wildcard_handlers: list[EventHandler] = []
        self._history: list[Event] = []
        self._max_history = max_history
        self._async_delivery = async_delivery
        self._lock = threading.RLock()

        # Async delivery queue
        self._event_queue: Queue[Event] = Queue()
        self._worker_thread: threading.Thread | None = None
        self._running = False

        if async_delivery:
            self._start_worker()

    def _start_worker(self) -> None:
        """Start async delivery worker."""
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        """Process events asynchronously."""
        while self._running:
            try:
                event = self._event_queue.get(timeout=1.0)
                self._deliver_event(event)
                self._event_queue.task_done()
            except Empty:
                continue

    def _deliver_event(self, event: Event) -> None:
        """Deliver event to all handlers."""
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))
            handlers.extend(self._wildcard_handlers)

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    "Event handler error",
                    event_type=event.event_type.value,
                    pipeline_id=event.pipeline_id,
                    error=str(e),
                )

    def subscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        """Subscribe a handler to an event type.

        Args:
            event_type: Event type to subscribe to (None for all events)
            handler: Handler function
        """
        with self._lock:
            if event_type is None:
                if handler not in self._wildcard_handlers:
                    self._wildcard_handlers.append(handler)
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                if handler not in self._handlers[event_type]:
                    self._handlers[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        """Unsubscribe a handler from an event type.

        Args:
            event_type: Event type to unsubscribe from
            handler: Handler function
        """
        with self._lock:
            if event_type is None:
                if handler in self._wildcard_handlers:
                    self._wildcard_handlers.remove(handler)
            elif event_type in self._handlers:
                if handler in self._handlers[event_type]:
                    self._handlers[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        """Publish an event.

        Args:
            event: Event to publish
        """
        # Add to history
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)

        logger.debug(
            "Event published",
            event_type=event.event_type.value,
            pipeline_id=event.pipeline_id,
        )

        if self._async_delivery:
            self._event_queue.put(event)
        else:
            self._deliver_event(event)

    def emit(
        self,
        event_type: EventType,
        pipeline_id: str,
        data: dict[str, Any] | None = None,
        source: str = "orchestrator",
    ) -> Event:
        """Convenience method to create and publish an event.

        Args:
            event_type: Type of event
            pipeline_id: Pipeline ID
            data: Event data
            source: Event source

        Returns:
            The published event
        """
        event = Event(
            event_type=event_type,
            pipeline_id=pipeline_id,
            data=data or {},
            source=source,
        )
        self.publish(event)
        return event

    def get_history(
        self,
        pipeline_id: str | None = None,
        event_type: EventType | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Get event history.

        Args:
            pipeline_id: Filter by pipeline ID
            event_type: Filter by event type
            limit: Maximum events to return

        Returns:
            List of matching events (newest first)
        """
        with self._lock:
            events = list(self._history)

        # Filter
        if pipeline_id:
            events = [e for e in events if e.pipeline_id == pipeline_id]
        if event_type:
            events = [e for e in events if e.event_type == event_type]

        # Reverse for newest first
        events.reverse()

        # Apply limit
        if limit:
            events = events[:limit]

        return events

    def stop(self) -> None:
        """Stop async delivery."""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None


# Singleton event bus
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the singleton event bus.

    Returns:
        EventBus instance
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus(async_delivery=True)
    return _event_bus


def emit_event(
    event_type: EventType,
    pipeline_id: str,
    data: dict[str, Any] | None = None,
    source: str = "orchestrator",
) -> Event:
    """Convenience function to emit an event.

    Args:
        event_type: Type of event
        pipeline_id: Pipeline ID
        data: Event data
        source: Event source

    Returns:
        The published event
    """
    return get_event_bus().emit(event_type, pipeline_id, data, source)
