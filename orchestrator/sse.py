"""
Server-Sent Events (SSE) support for pipeline streaming.

Provides SSE response helpers and a per-pipeline client manager
that bridges the EventBus to connected HTTP clients.
"""

import json
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
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


from dag_visualizer import generate_status_report, render_pipeline_dag
from events import Event, EventBus, EventType, get_event_bus
from state_store import PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.sse")

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 15

# Maximum time a client can be connected (1 hour)
MAX_CONNECTION_TIME = 3600

# Event types that indicate a pipeline has reached a terminal state
TERMINAL_EVENT_TYPES = {
    EventType.PIPELINE_COMPLETED,
    EventType.PIPELINE_FAILED,
    EventType.PIPELINE_CANCELLED,
}


def format_sse_event(
    data: dict[str, Any],
    event: str | None = None,
    event_id: str | None = None,
    retry: int | None = None,
) -> str:
    """Format data as an SSE event string.

    Args:
        data: Event data (will be JSON-encoded)
        event: Optional event type name
        event_id: Optional event ID for reconnection
        retry: Optional retry interval in milliseconds

    Returns:
        Formatted SSE event string
    """
    lines = []

    if event_id is not None:
        lines.append(f"id: {event_id}")

    if event is not None:
        lines.append(f"event: {event}")

    if retry is not None:
        lines.append(f"retry: {retry}")

    # JSON-encode data and split across multiple data: lines if needed
    json_str = json.dumps(data, default=str)
    for line in json_str.split("\n"):
        lines.append(f"data: {line}")

    # SSE events are terminated by a blank line
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def format_sse_comment(text: str) -> str:
    """Format a comment line (used for heartbeats).

    Args:
        text: Comment text

    Returns:
        Formatted SSE comment string
    """
    return f": {text}\n\n"


class SSEClientManager:
    """Manages SSE client connections for pipeline streaming.

    Each pipeline can have multiple connected clients. The manager
    subscribes to the EventBus and fans out events to all clients
    watching a given pipeline.
    """

    def __init__(self, event_bus: EventBus | None = None, repo_path: Path | None = None):
        """Initialize the client manager.

        Args:
            event_bus: Event bus to subscribe to (uses singleton if None)
            repo_path: Path to repository for loading pipeline state
        """
        self.event_bus = event_bus or get_event_bus()
        self.repo_path = repo_path
        self._clients: dict[str, list[Queue]] = defaultdict(list)
        self._lock = threading.Lock()
        self._subscribed = False

    def subscribe_to_events(self) -> None:
        """Subscribe to the event bus for all pipeline events."""
        if self._subscribed:
            return
        self.event_bus.subscribe(None, self._handle_event)
        self._subscribed = True
        logger.info("SSEClientManager subscribed to event bus")

    def unsubscribe_from_events(self) -> None:
        """Unsubscribe from the event bus."""
        if not self._subscribed:
            return
        self.event_bus.unsubscribe(None, self._handle_event)
        self._subscribed = False

    def add_client(self, pipeline_id: str) -> Queue:
        """Register a new SSE client for a pipeline.

        Args:
            pipeline_id: Pipeline to watch

        Returns:
            Queue that will receive SSE event dicts
        """
        q: Queue = Queue(maxsize=1000)
        with self._lock:
            self._clients[pipeline_id].append(q)
            client_count = len(self._clients[pipeline_id])
        logger.info(
            "SSE client connected",
            pipeline_id=pipeline_id,
            total_clients=client_count,
        )
        return q

    def remove_client(self, pipeline_id: str, q: Queue) -> None:
        """Remove an SSE client.

        Args:
            pipeline_id: Pipeline the client was watching
            q: The client's queue
        """
        with self._lock:
            clients = self._clients.get(pipeline_id, [])
            if q in clients:
                clients.remove(q)
            if not clients:
                self._clients.pop(pipeline_id, None)
            remaining = len(self._clients.get(pipeline_id, []))
        logger.info(
            "SSE client disconnected",
            pipeline_id=pipeline_id,
            remaining_clients=remaining,
        )

    def _handle_event(self, event: Event) -> None:
        """Handle an event from the EventBus and fan out to clients."""
        pipeline_id = event.pipeline_id

        with self._lock:
            clients = list(self._clients.get(pipeline_id, []))

        if not clients:
            return

        # Build SSE payload
        payload = {
            "event_type": event.event_type.value,
            "pipeline_id": pipeline_id,
            "timestamp": event.timestamp.isoformat() + "Z",
            "data": event.data,
        }

        # Try to include visualization from state store.
        # Note: this runs on the EventBus worker thread, so we cannot use
        # Flask's get_repo_path() (no request context). Resolve from the
        # instance attribute or EGG_REPO_PATH env var instead.
        try:
            repo_path = self.repo_path
            if repo_path is None:
                env_path = os.environ.get("EGG_REPO_PATH")
                if env_path:
                    repo_path = Path(env_path)
            if repo_path is None:
                raise RuntimeError("repo_path not available for visualization")
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            payload["visualization"] = {
                "dag": render_pipeline_dag(pipeline),
            }
            payload["status"] = pipeline.status.value
            payload["current_phase"] = pipeline.current_phase.value
        except Exception:
            logger.debug(
                "Failed to attach visualization to SSE event",
                pipeline_id=pipeline_id,
                exc_info=True,
            )

        is_terminal = event.event_type in TERMINAL_EVENT_TYPES

        for q in clients:
            try:
                q.put_nowait(("event", payload, is_terminal))
            except Full:
                logger.warning(
                    "SSE client queue full, dropping event",
                    pipeline_id=pipeline_id,
                    event_type=event.event_type.value,
                )
            except Exception:
                logger.debug(
                    "Failed to enqueue SSE event",
                    pipeline_id=pipeline_id,
                    exc_info=True,
                )

    def get_client_count(self, pipeline_id: str | None = None) -> int:
        """Get number of connected clients.

        Args:
            pipeline_id: Count for specific pipeline, or all if None

        Returns:
            Number of connected clients
        """
        with self._lock:
            if pipeline_id:
                return len(self._clients.get(pipeline_id, []))
            return sum(len(clients) for clients in self._clients.values())


# Singleton
_sse_manager: SSEClientManager | None = None
_sse_manager_lock = threading.Lock()


def get_sse_manager() -> SSEClientManager:
    """Get the singleton SSE client manager.

    Returns:
        SSEClientManager instance
    """
    global _sse_manager
    if _sse_manager is None:
        with _sse_manager_lock:
            if _sse_manager is None:
                manager = SSEClientManager()
                manager.subscribe_to_events()
                _sse_manager = manager
    return _sse_manager


def create_sse_stream(
    pipeline_id: str,
    repo_path: Path | None = None,
    use_ascii: bool = False,
    include_initial: bool = True,
) -> Generator[str, None, None]:
    """Create an SSE event stream generator for a pipeline.

    This generator yields SSE-formatted strings and is designed to be
    used directly as a Flask streaming response.

    Args:
        pipeline_id: Pipeline to stream events for
        repo_path: Path to repository (resolved from Flask context if None)
        use_ascii: Use ASCII-only characters in visualizations
        include_initial: Send initial state as first event

    Yields:
        SSE-formatted event strings
    """
    manager = get_sse_manager()
    q = manager.add_client(pipeline_id)

    # Resolve repo_path from Flask context if not provided
    if repo_path is None:
        try:
            from routes import get_repo_path
            repo_path = get_repo_path()
        except Exception:
            pass

    try:
        # Send initial state snapshot
        if include_initial:
            try:
                store = get_state_store(repo_path)
                pipeline = store.load_pipeline(pipeline_id)

                initial = generate_status_report(pipeline, use_ascii=use_ascii)
                initial["event_type"] = "snapshot"
                initial["visualization"] = {
                    "dag": render_pipeline_dag(pipeline, use_ascii=use_ascii),
                }
                yield format_sse_event(initial, event="snapshot", retry=5000)

                # If pipeline is already terminal, send done and return
                terminal_statuses = {"complete", "failed", "cancelled"}
                if pipeline.status.value in terminal_statuses:
                    yield format_sse_event(
                        {"pipeline_id": pipeline_id, "reason": "already_terminal"},
                        event="done",
                    )
                    return
            except PipelineNotFoundError:
                yield format_sse_event(
                    {"error": f"Pipeline {pipeline_id} not found"},
                    event="error",
                )
                return
            except Exception as e:
                logger.warning(
                    "Failed to send initial SSE snapshot",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

        start_time = time.monotonic()

        while True:
            # Check max connection time
            if time.monotonic() - start_time > MAX_CONNECTION_TIME:
                yield format_sse_event(
                    {"pipeline_id": pipeline_id, "reason": "timeout"},
                    event="done",
                )
                return

            try:
                msg_type, payload, is_terminal = q.get(timeout=HEARTBEAT_INTERVAL)

                yield format_sse_event(
                    payload,
                    event=payload.get("event_type", "update"),
                )

                # If this was a terminal event, send done and stop
                if is_terminal:
                    yield format_sse_event(
                        {"pipeline_id": pipeline_id, "reason": "completed"},
                        event="done",
                    )
                    return

            except Empty:
                # No events — send heartbeat to keep connection alive
                yield format_sse_comment(
                    f"heartbeat {datetime.utcnow().isoformat()}Z"
                )

    finally:
        manager.remove_client(pipeline_id, q)
