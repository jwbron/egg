"""In-memory per-pipeline message storage for inter-agent communication.

Provides thread-safe storage for messages exchanged between agents during
concurrent phase execution. Messages are ephemeral within a phase and
captured in checkpoints at session end for auditability.
"""

import logging
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("orchestrator.message_store")


class MessageType:
    """Standard message types for inter-agent communication."""

    PROGRESS = "PROGRESS"
    STATUS = "STATUS"
    AGENT_FAILED = "AGENT_FAILED"
    HANDOFF = "HANDOFF"
    # Structured per-agent state heartbeat (issue #1897).
    # Body is a JSON document with {"state": ..., "waiting_on": ..., "since": ...}.
    # See docs/reference/agent-wait-patterns.md.
    HEARTBEAT = "HEARTBEAT"
    # Consensus protocol (BRC)
    CONSENSUS_PROPOSE = "CONSENSUS_PROPOSE"
    CONSENSUS_ACK = "CONSENSUS_ACK"
    CONSENSUS_NACK = "CONSENSUS_NACK"
    CONSENSUS_WITHDRAW = "CONSENSUS_WITHDRAW"
    CONSENSUS_CONFIRMED = "CONSENSUS_CONFIRMED"
    CONSENSUS_RE_REVIEW = "CONSENSUS_RE_REVIEW"
    # Overseer anomaly broadcasts (issue #1413)
    OVERSEER_ALERT = "OVERSEER_ALERT"
    # Tier 1 health monitor nudge messages (issue #1428)
    NUDGE = "NUDGE"


# Valid HEARTBEAT states (issue #1897). Validated server-side in routes/messages.py.
HEARTBEAT_STATES: frozenset[str] = frozenset(
    {
        "WORKING",
        "WAITING_ON_ROLE",
        "PROPOSED",
        "IDLE",
    }
)


class Message(BaseModel):
    """A message exchanged between agents via the orchestrator message bus."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:16])
    pipeline_id: str = Field(..., description="Pipeline this message belongs to")
    from_role: str = Field(..., description="Sender agent role")
    to_role: str = Field(default="all", description="Target role or 'all' for broadcast")
    message_type: str = Field(..., description="Message type (e.g., PROGRESS, HEARTBEAT)")
    subject: str = Field(default="", description="Message subject line")
    body: str = Field(default="", description="Message body content")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str | None = Field(default=None, description="Pipeline phase when sent")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "pipeline_id": self.pipeline_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "message_type": self.message_type,
            "subject": self.subject,
            "body": self.body,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase,
        }


class MessageStore:
    """Thread-safe in-memory message storage, keyed by pipeline ID.

    Supports add, get-since, status, and clear operations for
    managing inter-agent messages during concurrent execution.

    When ``wait > 0`` is passed to :meth:`get_messages`, the in-memory
    backend blocks on a per-pipeline :class:`threading.Condition` until a
    matching message is appended or the timeout expires. This keeps the
    in-memory backend behaviorally identical to the Redis backend
    (XREAD BLOCK), so ``EGG_MESSAGE_STORE_BACKEND=memory`` local-dev
    runs exhibit the same blocking semantics as production.

    See issue #1897.
    """

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}
        # Per-pipeline condition variables for blocking reads (issue #1897).
        # Using per-pipeline (not a global cv) avoids spurious wake-ups across
        # unrelated pipelines.
        self._cond: dict[str, threading.Condition] = {}
        self._lock = threading.RLock()

    def _get_cond(self, pipeline_id: str) -> threading.Condition:
        """Get or create the per-pipeline condition variable."""
        with self._lock:
            cv = self._cond.get(pipeline_id)
            if cv is None:
                cv = threading.Condition(self._lock)
                self._cond[pipeline_id] = cv
            return cv

    def add_message(self, message: Message) -> Message:
        """Add a message to the store.

        Args:
            message: The message to store.

        Returns:
            The stored message (with generated ID if not set).
        """
        with self._lock:
            pid = message.pipeline_id
            if pid not in self._messages:
                self._messages[pid] = []
            self._messages[pid].append(message)
            # Notify any blocked get_messages() callers waiting on this pipeline.
            # Per issue #1897 RISK-5: wake ALL waiters; each re-filters and
            # may continue blocking if the new message doesn't match its
            # wait_for_types filter.
            cv = self._cond.get(pid)
            if cv is not None:
                cv.notify_all()
        return message

    def get_messages(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        limit: int = 100,
        wait: int = 0,
        wait_for_types: Sequence[str] | None = None,
    ) -> list[Message]:
        """Get messages for a pipeline, optionally filtered.

        Args:
            pipeline_id: Pipeline ID to query.
            role: If set, return messages where to_role is this role or 'all'.
            since_id: If set, return only messages after this message ID. If the
                cursor is not present in the store (e.g., after a phase-boundary
                clear or a post-compaction anchor recovery), fall back to
                returning all messages rather than silently dropping to empty.
                This matches the Redis backend's behavior and avoids a silent
                delivery failure that can stall agents.
            limit: Maximum messages to return.
            wait: If > 0, block up to this many seconds waiting for matching
                messages to arrive.  Issue #1897.
            wait_for_types: If set (and ``wait > 0``), only treat a read as
                "matched" when at least one message of these types is
                available after applying role/since_id filters. Unwanted types
                are left in the store and do not unblock the caller.

        Returns:
            List of matching messages, oldest first. Empty list on timeout.
        """

        def _filter(all_msgs: list[Message]) -> list[Message]:
            msgs = list(all_msgs)
            # Filter by since_id. If the cursor is unknown, degrade to
            # "return all" instead of returning empty, so a stale cursor
            # doesn't silently hide new messages from a polling agent.
            if since_id:
                found_idx = next(
                    (i for i, m in enumerate(msgs) if m.id == since_id), None
                )
                if found_idx is not None:
                    msgs = msgs[found_idx + 1 :]
                else:
                    logger.warning(
                        "since_id not found in store; returning full history",
                        extra={
                            "pipeline_id": pipeline_id,
                            "since_id": since_id,
                        },
                    )

            # Filter by role (messages targeted to this role or broadcast)
            if role:
                msgs = [m for m in msgs if m.to_role == role or m.to_role == "all"]

            return msgs

        # Fast path: check once under the lock.
        with self._lock:
            matches = _filter(self._messages.get(pipeline_id, []))
            if wait_for_types:
                typed = [m for m in matches if m.message_type in set(wait_for_types)]
                if typed:
                    return typed[-limit:] if len(typed) > limit else typed
                # fall through to blocking branch only if wait > 0
            else:
                if matches:
                    return matches[-limit:] if len(matches) > limit else matches
                # fall through to blocking branch only if wait > 0

            if wait <= 0:
                # Non-blocking: return whatever we have (empty or filtered).
                if wait_for_types:
                    return []
                return matches[-limit:] if len(matches) > limit else matches

            # Blocking branch: wait on the per-pipeline condition variable.
            # We already hold the lock via `with self._lock:`; the cv shares
            # the same lock so wait()/notify_all() coordinate correctly.
            cv = self._cond.get(pipeline_id)
            if cv is None:
                cv = threading.Condition(self._lock)
                self._cond[pipeline_id] = cv

            deadline = time.monotonic() + float(wait)
            want_types = set(wait_for_types) if wait_for_types else None
            # Track whether the pipeline was observed at some point.  If a
            # clear() removes a pipeline we *had* observed we should wake
            # and return empty (RISK-5). But if the pipeline simply never
            # existed we keep waiting — add_message() will create the entry
            # and also notify_all().
            observed = pipeline_id in self._messages
            while True:
                if pipeline_id in self._messages:
                    observed = True
                elif observed:
                    # Pipeline existed and was cleared while we were waiting.
                    return []

                matches = _filter(self._messages.get(pipeline_id, []))
                if want_types is not None:
                    typed = [m for m in matches if m.message_type in want_types]
                    if typed:
                        return typed[-limit:] if len(typed) > limit else typed
                elif matches:
                    return matches[-limit:] if len(matches) > limit else matches

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []

                # wait() releases the lock, waits for notify, re-acquires it.
                cv.wait(timeout=remaining)

    def get_status(self, pipeline_id: str) -> dict[str, Any]:
        """Get message statistics for a pipeline.

        Returns:
            Dict with total count and counts by message type.
        """
        with self._lock:
            msgs = self._messages.get(pipeline_id, [])
            by_type: dict[str, int] = {}
            for m in msgs:
                by_type[m.message_type] = by_type.get(m.message_type, 0) + 1
            return {
                "total": len(msgs),
                "by_type": by_type,
            }

    def clear(self, pipeline_id: str) -> int:
        """Clear all messages for a pipeline (e.g., on phase transition).

        RISK-5 (issue #1897): after popping the list we notify_all on the
        per-pipeline condition variable so any threads currently blocked
        on ``get_messages(..., wait=N)`` wake up, observe the empty/absent
        pipeline, and return [] — rather than hanging until the timeout.

        Returns:
            Number of messages cleared.
        """
        with self._lock:
            msgs = self._messages.pop(pipeline_id, [])
            cv = self._cond.get(pipeline_id)
            if cv is not None:
                cv.notify_all()
            return len(msgs)


# Singleton
_message_store: MessageStore | None = None
_store_lock = threading.Lock()


def get_message_store() -> MessageStore:
    """Get the singleton message store.

    Uses Redis Streams when Redis is available, falling back to
    in-memory storage for tests or when Redis is not configured.
    """
    global _message_store
    if _message_store is None:
        with _store_lock:
            if _message_store is None:
                _message_store = _create_message_store()
    return _message_store


def _create_message_store() -> MessageStore:
    """Create the appropriate message store backend."""
    import os

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_db = int(os.environ.get("REDIS_MESSAGE_DB", "1"))  # Separate DB from other Redis usage
    use_redis = os.environ.get("EGG_MESSAGE_STORE_BACKEND", "auto")

    if use_redis == "memory":
        return MessageStore()

    if use_redis in ("redis", "auto"):
        try:
            from redis_message_store import get_redis_message_store

            store = get_redis_message_store(host=redis_host, port=redis_port, db=redis_db)
            logger.info(
                "Using Redis Streams message store",
                extra={"host": redis_host, "port": redis_port, "db": redis_db},
            )
            return store  # type: ignore[return-value]
        except Exception as e:
            if use_redis == "redis":
                raise  # Explicit Redis mode — fail hard
            logger.warning(
                "Redis unavailable, falling back to in-memory message store",
                extra={"error": str(e)},
            )

    return MessageStore()


def reset_message_store() -> None:
    """Reset the singleton message store (for testing)."""
    global _message_store
    _message_store = None
