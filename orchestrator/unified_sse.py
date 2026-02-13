"""
Unified SSE stream for all active pipelines.

Provides a single SSE connection that streams status updates for
every running pipeline, avoiding the per-pipeline thread pool
exhaustion from issue #620.
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Generator

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
from models import Pipeline, PipelineStatus
from sse import (
    HEARTBEAT_INTERVAL,
    MAX_CONNECTION_TIME,
    TERMINAL_EVENT_TYPES,
    format_sse_comment,
    format_sse_event,
)
from state_store import get_state_store

logger = get_logger("orchestrator.unified_sse")


class UnifiedSSEManager:
    """Manages a unified SSE stream across all pipelines.

    Subscribes to EventBus wildcard to receive ALL pipeline events,
    then fans them out to connected unified-stream clients.
    """

    def __init__(self, event_bus: EventBus | None = None):
        self.event_bus = event_bus or get_event_bus()
        self._clients: list[Queue] = []
        self._lock = threading.Lock()
        self._subscribed = False

    def subscribe_to_events(self) -> None:
        """Subscribe to all events via wildcard."""
        if self._subscribed:
            return
        self.event_bus.subscribe(None, self._handle_event)
        self._subscribed = True
        logger.info("UnifiedSSEManager subscribed to event bus")

    def unsubscribe_from_events(self) -> None:
        """Unsubscribe from the event bus."""
        if not self._subscribed:
            return
        self.event_bus.unsubscribe(None, self._handle_event)
        self._subscribed = False

    def add_client(self) -> Queue:
        """Register a new unified SSE client.

        Returns:
            Queue that will receive SSE event dicts.
        """
        q: Queue = Queue(maxsize=1000)
        with self._lock:
            self._clients.append(q)
            count = len(self._clients)
        logger.info("Unified SSE client connected", total_clients=count)
        return q

    def remove_client(self, q: Queue) -> None:
        """Remove a unified SSE client."""
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)
            remaining = len(self._clients)
        logger.info("Unified SSE client disconnected", remaining_clients=remaining)

    def _handle_event(self, event: Event) -> None:
        """Handle an event from EventBus and fan out to all unified clients."""
        with self._lock:
            clients = list(self._clients)

        if not clients:
            return

        is_terminal = event.event_type in TERMINAL_EVENT_TYPES

        payload = {
            "event_type": event.event_type.value,
            "pipeline_id": event.pipeline_id,
            "timestamp": event.timestamp.isoformat() + "Z",
            "data": event.data,
            "is_terminal": is_terminal,
        }

        # Try to enrich with visualization from state store
        try:
            repo_path = _resolve_repo_path()
            if repo_path:
                store = get_state_store(repo_path)
                pipeline = store.load_pipeline(event.pipeline_id)
                payload["status"] = pipeline.status.value
                payload["current_phase"] = pipeline.current_phase.value
                payload["compact"] = render_compact_status(pipeline)
                payload["progress"] = render_progress_bar(pipeline)
        except Exception:
            logger.debug(
                "Failed to enrich unified SSE event",
                pipeline_id=event.pipeline_id,
                exc_info=True,
            )

        for q in clients:
            try:
                q.put_nowait(payload)
            except Full:
                logger.warning(
                    "Unified SSE client queue full, dropping event",
                    pipeline_id=event.pipeline_id,
                )

    def get_client_count(self) -> int:
        """Get number of connected unified clients."""
        with self._lock:
            return len(self._clients)


# Singleton
_unified_manager: UnifiedSSEManager | None = None
_unified_manager_lock = threading.Lock()


def get_unified_sse_manager() -> UnifiedSSEManager:
    """Get the singleton unified SSE manager."""
    global _unified_manager
    if _unified_manager is None:
        with _unified_manager_lock:
            if _unified_manager is None:
                manager = UnifiedSSEManager()
                manager.subscribe_to_events()
                _unified_manager = manager
    return _unified_manager


def _resolve_repo_path() -> Path | None:
    """Resolve repo path from environment."""
    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path:
        return Path(env_path)
    return None


def _collect_all_pipelines_safe(repo_path: Path) -> list[Pipeline]:
    """Collect all pipelines, suppressing errors for individual loads."""
    pipelines: list[Pipeline] = []

    # Check base path
    if (repo_path / ".egg-state" / "pipelines").exists():
        store = get_state_store(repo_path)
        for pid in store.list_pipelines():
            try:
                pipelines.append(store.load_pipeline(pid))
            except Exception:
                continue

    # Check repo subdirectories if base_path is not a git repo
    if not (repo_path / ".git").exists():
        for child in sorted(repo_path.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                try:
                    store = get_state_store(child)
                    for pid in store.list_pipelines():
                        try:
                            pipelines.append(store.load_pipeline(pid))
                        except Exception:
                            continue
                except Exception:
                    continue

    return pipelines


def create_unified_sse_stream(
    repo_path: Path | None = None,
    use_ascii: bool = False,
    active_only: bool = True,
    full_dag: bool = False,
) -> Generator[str, None, None]:
    """Create a unified SSE stream for all pipelines.

    Yields SSE-formatted strings for use as a Flask streaming response.
    Unlike create_sse_stream, terminal events for individual pipelines
    do NOT end the stream — only timeout or client disconnect.

    Args:
        repo_path: Path to repository root (resolved from env if None)
        use_ascii: Use ASCII-only characters in visualizations
        active_only: Only include active pipelines in snapshot
        full_dag: Include full DAG visualization instead of compact

    Yields:
        SSE-formatted event strings
    """
    manager = get_unified_sse_manager()
    q = manager.add_client()

    if repo_path is None:
        repo_path = _resolve_repo_path()

    try:
        # Send initial snapshot of all pipelines
        if repo_path:
            try:
                all_pipelines = _collect_all_pipelines_safe(repo_path)

                if active_only:
                    terminal = {
                        PipelineStatus.COMPLETE,
                        PipelineStatus.FAILED,
                        PipelineStatus.CANCELLED,
                    }
                    all_pipelines = [
                        p for p in all_pipelines if p.status not in terminal
                    ]

                snapshot_data: list[dict[str, Any]] = []
                for pipeline in all_pipelines:
                    entry: dict[str, Any] = {
                        "pipeline_id": pipeline.id,
                        "status": pipeline.status.value,
                        "current_phase": pipeline.current_phase.value,
                        "repo": pipeline.repo,
                        "branch": pipeline.branch,
                        "compact": render_compact_status(
                            pipeline, use_ascii=use_ascii
                        ),
                        "progress": render_progress_bar(
                            pipeline, use_ascii=use_ascii
                        ),
                    }
                    if full_dag:
                        entry["dag"] = render_pipeline_dag(
                            pipeline, use_ascii=use_ascii
                        )
                    snapshot_data.append(entry)

                yield format_sse_event(
                    {"pipelines": snapshot_data},
                    event="snapshot",
                    retry=5000,
                )
            except Exception as e:
                logger.warning(
                    "Failed to send unified SSE snapshot",
                    error=str(e),
                )
                yield format_sse_event(
                    {"pipelines": [], "error": str(e)},
                    event="snapshot",
                    retry=5000,
                )
        else:
            yield format_sse_event(
                {"pipelines": []},
                event="snapshot",
                retry=5000,
            )

        start_time = time.monotonic()

        while True:
            # Check max connection time
            if time.monotonic() - start_time > MAX_CONNECTION_TIME:
                yield format_sse_event(
                    {"reason": "timeout"},
                    event="done",
                )
                return

            try:
                payload = q.get(timeout=HEARTBEAT_INTERVAL)

                # Enrich with compact/dag if requested and not already present
                if full_dag and "dag" not in payload:
                    try:
                        if repo_path:
                            store = get_state_store(repo_path)
                            pipeline = store.load_pipeline(
                                payload["pipeline_id"]
                            )
                            payload["dag"] = render_pipeline_dag(
                                pipeline, use_ascii=use_ascii
                            )
                    except Exception:
                        pass

                yield format_sse_event(
                    payload,
                    event=payload.get("event_type", "update"),
                )

                # NOTE: Terminal events for individual pipelines do NOT
                # end the unified stream — we keep watching all pipelines.

            except Empty:
                yield format_sse_comment(
                    f"heartbeat {datetime.utcnow().isoformat()}Z"
                )

    finally:
        manager.remove_client(q)
