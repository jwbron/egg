"""
Status reporter for real-time collaborator updates.

Provides mechanisms for reporting pipeline status to the collaborator
(the egg instance where the human initialized the workflow).
"""

import json
import sys
import threading
from collections.abc import Callable
from datetime import datetime
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

from dag_visualizer import (
    generate_status_report,
    render_compact_status,
    render_pipeline_dag,
    render_progress_bar,
)
from events import Event, EventBus, EventType, get_event_bus
from models import Pipeline

logger = get_logger("orchestrator.status_reporter")


class StatusUpdate:
    """A status update to send to collaborator."""

    def __init__(
        self,
        pipeline_id: str,
        event_type: str,
        status: str,
        current_phase: str,
        message: str | None = None,
        visualization: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ):
        self.pipeline_id = pipeline_id
        self.event_type = event_type
        self.status = status
        self.current_phase = current_phase
        self.message = message
        self.visualization = visualization or {}
        self.data = data or {}
        self.timestamp = datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pipeline_id": self.pipeline_id,
            "event_type": self.event_type,
            "status": self.status,
            "current_phase": self.current_phase,
            "message": self.message,
            "visualization": self.visualization,
            "data": self.data,
            "timestamp": self.timestamp.isoformat() + "Z",
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)


# Type alias for status update handlers
StatusHandler = Callable[[StatusUpdate], None]


class StatusReporter:
    """Reports pipeline status updates to collaborators.

    Subscribes to the event bus and transforms events into status updates,
    optionally pushing them to registered handlers (e.g., file writers,
    HTTP endpoints, or notification systems).
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        use_ascii: bool = False,
    ):
        """Initialize status reporter.

        Args:
            event_bus: Event bus to subscribe to (uses singleton if None)
            use_ascii: Use ASCII-only characters in visualizations
        """
        self.event_bus = event_bus or get_event_bus()
        self.use_ascii = use_ascii
        self._handlers: list[StatusHandler] = []
        self._lock = threading.Lock()
        self._subscribed = False

        # Pipeline state cache for generating visualizations
        self._pipeline_cache: dict[str, Pipeline] = {}

    def add_handler(self, handler: StatusHandler) -> None:
        """Add a status update handler.

        Args:
            handler: Function to call with each status update
        """
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def remove_handler(self, handler: StatusHandler) -> None:
        """Remove a status update handler.

        Args:
            handler: Handler to remove
        """
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def update_pipeline_cache(self, pipeline: Pipeline) -> None:
        """Update cached pipeline state for visualization.

        Args:
            pipeline: Pipeline to cache
        """
        with self._lock:
            self._pipeline_cache[pipeline.id] = pipeline

    def _get_cached_pipeline(self, pipeline_id: str) -> Pipeline | None:
        """Get cached pipeline state."""
        with self._lock:
            return self._pipeline_cache.get(pipeline_id)

    def subscribe(self) -> None:
        """Subscribe to event bus for automatic updates."""
        if self._subscribed:
            return

        self.event_bus.subscribe(None, self._handle_event)
        self._subscribed = True
        logger.info("StatusReporter subscribed to event bus")

    def unsubscribe(self) -> None:
        """Unsubscribe from event bus."""
        if not self._subscribed:
            return

        self.event_bus.unsubscribe(None, self._handle_event)
        self._subscribed = False
        logger.info("StatusReporter unsubscribed from event bus")

    def _handle_event(self, event: Event) -> None:
        """Handle an event from the event bus.

        Transforms events into status updates and dispatches to handlers.
        """
        # Build status update from event
        update = self._event_to_status_update(event)
        if update:
            self._dispatch_update(update)

    def _event_to_status_update(self, event: Event) -> StatusUpdate | None:
        """Convert an event to a status update.

        Args:
            event: Event to convert

        Returns:
            StatusUpdate or None if event should not generate update
        """
        # Map event types to messages
        message_map = {
            EventType.PIPELINE_CREATED: "Pipeline created",
            EventType.PIPELINE_STARTED: "Pipeline execution started",
            EventType.PIPELINE_COMPLETED: "Pipeline completed successfully",
            EventType.PIPELINE_FAILED: "Pipeline failed",
            EventType.PIPELINE_CANCELLED: "Pipeline cancelled",
            EventType.PHASE_STARTED: "Phase started",
            EventType.PHASE_COMPLETED: "Phase completed",
            EventType.PHASE_FAILED: "Phase failed",
            EventType.AGENT_STARTED: "Agent started",
            EventType.AGENT_COMPLETED: "Agent completed",
            EventType.AGENT_FAILED: "Agent failed",
            EventType.DECISION_CREATED: "Awaiting human decision",
            EventType.DECISION_RESOLVED: "Human decision received",
        }

        message = message_map.get(event.event_type)
        if not message:
            return None

        # Get current phase from event data or cache
        current_phase = event.data.get("phase", "unknown")
        status = event.data.get("status", "unknown")

        # Try to get visualization from cached pipeline
        visualization = {}
        pipeline = self._get_cached_pipeline(event.pipeline_id)
        if pipeline:
            visualization = {
                "dag": render_pipeline_dag(pipeline, use_ascii=self.use_ascii),
                "compact": render_compact_status(pipeline, use_ascii=self.use_ascii),
                "progress": render_progress_bar(pipeline, use_ascii=self.use_ascii),
            }

        return StatusUpdate(
            pipeline_id=event.pipeline_id,
            event_type=event.event_type.value,
            status=status,
            current_phase=current_phase,
            message=message,
            visualization=visualization,
            data=event.data,
        )

    def _dispatch_update(self, update: StatusUpdate) -> None:
        """Dispatch status update to all handlers.

        Args:
            update: Status update to dispatch
        """
        with self._lock:
            handlers = list(self._handlers)

        for handler in handlers:
            try:
                handler(update)
            except Exception as e:
                logger.error(
                    "Status handler error",
                    pipeline_id=update.pipeline_id,
                    event_type=update.event_type,
                    error=str(e),
                )

    def report_status(
        self,
        pipeline: Pipeline,
        event_type: str = "status_update",
        message: str | None = None,
    ) -> StatusUpdate:
        """Manually report pipeline status.

        This method allows direct status reporting without going through
        the event bus. Useful for periodic status updates or on-demand
        reporting.

        Args:
            pipeline: Pipeline to report status for
            event_type: Type of status event
            message: Optional message

        Returns:
            The generated status update
        """
        # Update cache
        self.update_pipeline_cache(pipeline)

        # Generate visualization
        visualization = {
            "dag": render_pipeline_dag(pipeline, use_ascii=self.use_ascii),
            "compact": render_compact_status(pipeline, use_ascii=self.use_ascii),
            "progress": render_progress_bar(pipeline, use_ascii=self.use_ascii),
        }

        update = StatusUpdate(
            pipeline_id=pipeline.id,
            event_type=event_type,
            status=pipeline.status.value,
            current_phase=pipeline.current_phase.value,
            message=message or f"Pipeline status: {pipeline.status.value}",
            visualization=visualization,
            data=generate_status_report(pipeline, use_ascii=self.use_ascii),
        )

        self._dispatch_update(update)
        return update


# File-based status handler for writing updates to disk
def create_file_handler(output_dir: Path) -> StatusHandler:
    """Create a handler that writes status updates to files.

    Args:
        output_dir: Directory to write status files

    Returns:
        StatusHandler function
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def handler(update: StatusUpdate) -> None:
        # Write latest status to pipeline-specific file
        status_file = output_dir / f"{update.pipeline_id}-status.json"
        status_file.write_text(update.to_json())

        # Also append to history file
        history_file = output_dir / f"{update.pipeline_id}-history.jsonl"
        with history_file.open("a") as f:
            f.write(json.dumps(update.to_dict()) + "\n")

        logger.debug(
            "Status written to file",
            pipeline_id=update.pipeline_id,
            file=str(status_file),
        )

    return handler


# Console handler for printing status updates
def create_console_handler(show_dag: bool = True) -> StatusHandler:
    """Create a handler that prints status updates to console.

    Args:
        show_dag: Whether to show full DAG visualization

    Returns:
        StatusHandler function
    """

    def handler(update: StatusUpdate) -> None:
        print(f"\n{'=' * 60}")
        print(f"Pipeline: {update.pipeline_id}")
        print(f"Event: {update.event_type}")
        print(f"Status: {update.status}")
        print(f"Phase: {update.current_phase}")
        if update.message:
            print(f"Message: {update.message}")
        print(f"Time: {update.timestamp.isoformat()}")

        if show_dag and update.visualization.get("dag"):
            print(f"\n{update.visualization['dag']}")
        elif update.visualization.get("compact"):
            print(f"\n{update.visualization['compact']}")
            print(update.visualization.get("progress", ""))

        print("=" * 60)

    return handler


# Singleton reporter
_status_reporter: StatusReporter | None = None


def get_status_reporter() -> StatusReporter:
    """Get the singleton status reporter.

    Returns:
        StatusReporter instance
    """
    global _status_reporter
    if _status_reporter is None:
        _status_reporter = StatusReporter()
    return _status_reporter


def report_pipeline_status(
    pipeline: Pipeline,
    event_type: str = "status_update",
    message: str | None = None,
) -> StatusUpdate:
    """Convenience function to report pipeline status.

    Args:
        pipeline: Pipeline to report status for
        event_type: Type of status event
        message: Optional message

    Returns:
        The generated status update
    """
    return get_status_reporter().report_status(pipeline, event_type, message)
