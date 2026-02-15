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

# How often to send visualization refreshes (keeps elapsed time updating)
REFRESH_INTERVAL = 1

# Maximum time a client can be connected (1 hour)
MAX_CONNECTION_TIME = 3600

# Event types that indicate a pipeline has reached a terminal state
TERMINAL_EVENT_TYPES = {
    EventType.PIPELINE_COMPLETED,
    EventType.PIPELINE_FAILED,
    EventType.PIPELINE_CANCELLED,
}

# Pipeline status values that indicate a terminal state
TERMINAL_STATUSES = {"complete", "failed", "cancelled"}


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
        self._client_prefs: dict[int, bool] = {}  # id(queue) -> use_ascii
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

    def add_client(self, pipeline_id: str, use_ascii: bool = False) -> Queue:
        """Register a new SSE client for a pipeline.

        Args:
            pipeline_id: Pipeline to watch
            use_ascii: Whether client prefers ASCII-only DAG rendering

        Returns:
            Queue that will receive SSE event dicts
        """
        q: Queue = Queue(maxsize=1000)
        with self._lock:
            self._clients[pipeline_id].append(q)
            self._client_prefs[id(q)] = use_ascii
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
            self._client_prefs.pop(id(q), None)
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
            client_prefs = {id(q): self._client_prefs.get(id(q), False) for q in clients}

        if not clients:
            return

        # Build base SSE payload (without per-client visualization)
        payload = {
            "event_type": event.event_type.value,
            "pipeline_id": pipeline_id,
            "timestamp": event.timestamp.isoformat() + "Z",
            "data": event.data,
        }

        # Try to include visualization and pipeline state from state store.
        # Note: this runs on the EventBus worker thread, so we cannot use
        # Flask's get_repo_path() (no request context). Resolve from the
        # instance attribute or EGG_REPO_PATH env var instead.
        pipeline = None
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
            payload["status"] = pipeline.status.value
            payload["current_phase"] = pipeline.current_phase.value
            payload["pending_decisions"] = len(pipeline.get_pending_decisions())
        except Exception:
            logger.debug(
                "Failed to attach pipeline state to SSE event",
                pipeline_id=pipeline_id,
                exc_info=True,
            )

        # Pre-render DAG for each unique use_ascii preference to avoid
        # duplicate renders when multiple clients share the same preference.
        dag_cache: dict[bool, str | None] = {}
        if pipeline is not None:
            for use_ascii in set(client_prefs.values()):
                try:
                    dag_cache[use_ascii] = render_pipeline_dag(pipeline, use_ascii=use_ascii)
                except Exception:
                    logger.debug(
                        "Failed to render DAG for SSE event",
                        pipeline_id=pipeline_id,
                        use_ascii=use_ascii,
                        exc_info=True,
                    )
                    dag_cache[use_ascii] = None

        is_terminal = event.event_type in TERMINAL_EVENT_TYPES

        for q in clients:
            use_ascii = client_prefs.get(id(q), False)
            client_payload = dict(payload)
            dag = dag_cache.get(use_ascii)
            if dag is not None:
                client_payload["visualization"] = {"dag": dag}
            try:
                q.put_nowait(("event", client_payload, is_terminal))
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
    q = manager.add_client(pipeline_id, use_ascii=use_ascii)

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
                yield format_sse_event(initial, event="snapshot", retry=5000)

                # If pipeline is already terminal, send done and return
                if pipeline.status.value in TERMINAL_STATUSES:
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
        last_heartbeat = time.monotonic()
        last_refresh_dag: str | None = None

        while True:
            # Check max connection time
            now = time.monotonic()
            if now - start_time > MAX_CONNECTION_TIME:
                yield format_sse_event(
                    {"pipeline_id": pipeline_id, "reason": "timeout"},
                    event="done",
                )
                return

            try:
                msg_type, payload, is_terminal = q.get(timeout=REFRESH_INTERVAL)

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
                # Send heartbeat comment periodically to keep connection alive
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    yield format_sse_comment(
                        f"heartbeat {datetime.utcnow().isoformat()}Z"
                    )
                    last_heartbeat = now

                # Send a visualization refresh so the client stays
                # current between real events.  Skip if the DAG string
                # is unchanged (helps for idle/awaiting pipelines; running
                # pipelines change every second due to elapsed-time counters).
                try:
                    if repo_path is None:
                        continue
                    store = get_state_store(repo_path)
                    pipeline = store.load_pipeline(pipeline_id)

                    # Only refresh for non-terminal pipelines
                    if pipeline.status.value in TERMINAL_STATUSES:
                        continue

                    refresh = generate_status_report(
                        pipeline, use_ascii=use_ascii
                    )

                    # Skip if the DAG visualization is identical to the
                    # last refresh we sent.  This mainly helps when the
                    # pipeline is idle (e.g., awaiting human input).
                    current_dag = refresh.get("visualization", {}).get("dag")
                    if current_dag is not None and current_dag == last_refresh_dag:
                        continue
                    last_refresh_dag = current_dag

                    refresh["event_type"] = "refresh"
                    refresh["timestamp"] = (
                        datetime.utcnow().isoformat() + "Z"
                    )
                    yield format_sse_event(refresh, event="refresh")
                except Exception:
                    # If refresh fails, keep the stream alive
                    logger.debug(
                        "Failed to send SSE refresh",
                        pipeline_id=pipeline_id,
                        exc_info=True,
                    )

    finally:
        manager.remove_client(pipeline_id, q)
