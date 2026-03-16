"""In-memory per-pipeline message storage for inter-agent communication.

Provides thread-safe storage for messages exchanged between agents during
concurrent phase execution. Messages are ephemeral within a phase and
captured in checkpoints at session end for auditability.
"""

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("orchestrator.message_store")


class MessageType:
    """Standard message types for inter-agent communication."""

    PROGRESS = "PROGRESS"
    QUESTION = "QUESTION"
    STATUS = "STATUS"
    AGENT_FAILED = "AGENT_FAILED"
    HANDOFF = "HANDOFF"
    # Consensus protocol (BRC)
    CONSENSUS_PROPOSE = "CONSENSUS_PROPOSE"
    CONSENSUS_ACK = "CONSENSUS_ACK"
    CONSENSUS_NACK = "CONSENSUS_NACK"
    CONSENSUS_WITHDRAW = "CONSENSUS_WITHDRAW"
    CONSENSUS_CONFIRMED = "CONSENSUS_CONFIRMED"
    CONSENSUS_RE_REVIEW = "CONSENSUS_RE_REVIEW"


class Message(BaseModel):
    """A message exchanged between agents via the orchestrator message bus."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:16])
    pipeline_id: str = Field(..., description="Pipeline this message belongs to")
    from_role: str = Field(..., description="Sender agent role")
    to_role: str = Field(default="all", description="Target role or 'all' for broadcast")
    message_type: str = Field(..., description="Message type (e.g., PROGRESS, QUESTION)")
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
    """

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}
        self._lock = threading.RLock()

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
        return message

    def get_messages(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Get messages for a pipeline, optionally filtered.

        Args:
            pipeline_id: Pipeline ID to query.
            role: If set, return messages where to_role is this role or 'all'.
            since_id: If set, return only messages after this message ID.
            limit: Maximum messages to return.

        Returns:
            List of matching messages, oldest first.
        """
        with self._lock:
            msgs = list(self._messages.get(pipeline_id, []))

        # Filter by since_id
        if since_id:
            found = False
            filtered = []
            for m in msgs:
                if found:
                    filtered.append(m)
                elif m.id == since_id:
                    found = True
            msgs = filtered

        # Filter by role (messages targeted to this role or broadcast)
        if role:
            msgs = [m for m in msgs if m.to_role == role or m.to_role == "all"]

        # Apply limit
        return msgs[-limit:] if len(msgs) > limit else msgs

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

        Returns:
            Number of messages cleared.
        """
        with self._lock:
            msgs = self._messages.pop(pipeline_id, [])
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
