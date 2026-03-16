"""
In-memory per-pipeline progress event storage.

Thread-safe store for structured progress events emitted by agents.
Events are partitioned by pipeline_id and pruned when a configurable
max retention limit is exceeded.
"""

import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from models import ProgressEvent as _ProgressEventBase, ProgressState
except ImportError:
    from orchestrator.models import ProgressEvent as _ProgressEventBase, ProgressState  # type: ignore[no-redef]

from pydantic import Field


class ProgressEvent(_ProgressEventBase):
    """ProgressEvent with auto-generated ID when not provided.

    Extends the base model to make ``id`` optional — the store assigns
    a UUID automatically when one is not supplied.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique event ID")


class ProgressStore:
    """Thread-safe in-memory store for per-pipeline progress events.

    Args:
        max_events_per_pipeline: Maximum events to retain per pipeline.
            When exceeded, the oldest events are pruned.
    """

    DEFAULT_MAX_EVENTS = 1000

    def __init__(self, max_events_per_pipeline: int = DEFAULT_MAX_EVENTS) -> None:
        self._max_events = max_events_per_pipeline
        self._lock = threading.Lock()
        # pipeline_id -> list of ProgressEvent
        self._events: dict[str, list[ProgressEvent]] = {}

    def add_event(self, event: ProgressEvent) -> None:
        """Add a progress event to the store.

        Assigns an ID and timestamp if not already set, then stores the event.
        Prunes oldest events if the pipeline exceeds max retention.

        Args:
            event: The progress event to store.
        """
        # Ensure event has an ID
        if not event.id:
            event.id = str(uuid.uuid4())

        # Ensure event has a timestamp
        if event.timestamp is None:
            event.timestamp = datetime.now(timezone.utc)

        with self._lock:
            pipeline_events = self._events.setdefault(event.pipeline_id, [])
            pipeline_events.append(event)

            # Prune if over limit
            if len(pipeline_events) > self._max_events:
                self._events[event.pipeline_id] = pipeline_events[-self._max_events :]

    def get_events(
        self,
        pipeline_id: str,
        since: datetime | None = None,
        agent_role: str | None = None,
        limit: int | None = None,
    ) -> list[ProgressEvent]:
        """Query progress events for a pipeline.

        Args:
            pipeline_id: Pipeline to query.
            since: Only return events after this timestamp.
            agent_role: Filter to events from this agent role.
            limit: Maximum number of events to return.

        Returns:
            List of matching events in chronological order.
        """
        with self._lock:
            events = list(self._events.get(pipeline_id, []))

        # Apply filters
        if agent_role is not None:
            events = [e for e in events if e.agent_role == agent_role]

        if since is not None:
            # Normalize since to timezone-aware if needed
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            events = [
                e
                for e in events
                if (e.timestamp.replace(tzinfo=timezone.utc) if e.timestamp.tzinfo is None else e.timestamp)
                >= since
            ]

        if limit is not None and limit > 0:
            events = events[:limit]

        return events

    def get_latest_per_agent(self, pipeline_id: str) -> list[ProgressEvent]:
        """Get the most recent event per agent role for a pipeline.

        Args:
            pipeline_id: Pipeline to query.

        Returns:
            List of the latest event for each agent role.
        """
        with self._lock:
            events = list(self._events.get(pipeline_id, []))

        if not events:
            return []

        # Walk backwards to find the latest event per role
        latest: dict[str, ProgressEvent] = {}
        for event in reversed(events):
            if event.agent_role not in latest:
                latest[event.agent_role] = event

        return list(latest.values())

    def clear(self, pipeline_id: str) -> None:
        """Remove all events for a pipeline.

        Args:
            pipeline_id: Pipeline to clear.
        """
        with self._lock:
            self._events.pop(pipeline_id, None)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_progress_store: ProgressStore | None = None
_progress_store_lock = threading.Lock()


def get_progress_store() -> ProgressStore:
    """Get the singleton progress store instance.

    Returns:
        ProgressStore instance.
    """
    global _progress_store
    if _progress_store is None:
        with _progress_store_lock:
            if _progress_store is None:
                _progress_store = ProgressStore()
    return _progress_store


def reset_progress_store() -> None:
    """Reset the singleton (for testing)."""
    global _progress_store
    _progress_store = None
