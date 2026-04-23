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
    """Standard message types for inter-agent communication.

    Note on QUESTION (issue #1897 Phase 7): the QUESTION message type was
    removed. It is no longer a valid enum member. Inbound messages that
    still carry ``message_type="QUESTION"`` (e.g. replayed from an older
    checkpoint) are coerced to ``MessageType.PROGRESS`` by
    :func:`coerce_deprecated_message_type` below so they still appear in
    history and downstream pipelines don't crash on unknown types.  A
    follow-up issue will introduce a structured REQUEST/REPLY peer-Q&A
    subsystem that names a target peer and times out.
    """

    PROGRESS = "PROGRESS"
    STATUS = "STATUS"
    AGENT_FAILED = "AGENT_FAILED"
    HANDOFF = "HANDOFF"
    # Structured per-agent state heartbeat (issue #1897).
    # ``metadata`` is a JSON object with
    # ``{"state": ..., "waiting_on": ..., "since": ...}``;
    # ``body`` is a short human-readable summary (or empty string).
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

# Deprecated-in-#1897 message types that are tolerated on inbound/replay
# paths so existing on-disk brc-history files and in-flight pipelines
# don't crash on a now-unknown type. These map to a still-valid type
# that preserves the audit trail without reintroducing the deprecated
# channel.
_DEPRECATED_TYPE_COERCIONS: dict[str, str] = {
    # QUESTION became a PROGRESS-tier status message in #1897. Kept here
    # only for replay / deserialization safety; no code should emit
    # QUESTION at write time. See module docstring and reviewer_contract
    # blocker 2 on #1897.
    "QUESTION": "PROGRESS",
}


def coerce_deprecated_message_type(raw_type: str) -> str:
    """Normalise a deprecated message_type to its live replacement.

    Used by the Redis/in-memory deserialization paths so replayed
    messages whose ``message_type`` no longer exists on this version
    of the orchestrator still land in the in-memory representation
    with a valid type. Unknown-but-not-deprecated types pass through
    unchanged — the rest of the pipeline treats unknown types as
    opaque.

    Returns the coerced type (e.g. ``"PROGRESS"``) if ``raw_type`` is
    in the deprecation map, otherwise the original ``raw_type``.
    """
    return _DEPRECATED_TYPE_COERCIONS.get(raw_type, raw_type)


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
            # Notify any blocked get_messages() callers waiting on this
            # pipeline. Per issue #1897 RISK-5 + reviewer_code blocker 2
            # on v4: we ALSO create a cv if one is absent, so the very
            # next get_messages(wait=N) caller observes ``self._cond[pid]``
            # pre-seeded and doesn't race with a later clear(). Without
            # this, a reader that arrived AFTER an earlier clear() but
            # BEFORE any add_message landed could end up on a cv that
            # subsequently gets detached.
            cv = self._cond.get(pid)
            if cv is None:
                cv = threading.Condition(self._lock)
                self._cond[pid] = cv
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
        from_role: str | None = None,
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
            from_role: If set, further filter to only messages whose
                ``from_role`` equals this value.  Applied inside the blocking
                loop so a message from the wrong sender does NOT unblock the
                wait (prevents spinning — issue #1897 reviewer_code non-blocker).

        Returns:
            List of matching messages, oldest first. Empty list on timeout.
        """

        def _filter(all_msgs: list[Message]) -> list[Message]:
            msgs = list(all_msgs)
            # Filter by since_id. If the cursor is unknown, degrade to
            # "return all" instead of returning empty, so a stale cursor
            # doesn't silently hide new messages from a polling agent.
            if since_id:
                found_idx = next((i for i, m in enumerate(msgs) if m.id == since_id), None)
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

            # from_role filter — applied here so it participates in the
            # wait-for-match decision (wrong sender does not unblock).
            if from_role:
                msgs = [m for m in msgs if m.from_role == from_role]

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
                # Orphan-cv detection (#1897 reviewer_code blocker 2 on v4):
                # if clear(pipeline_id) ran since we grabbed ``cv``, the
                # canonical cv in ``self._cond`` either disappeared or was
                # replaced by a fresh instance installed by a subsequent
                # add_message().  Our local ``cv`` is then detached: future
                # notifications from add_message go to the new canonical cv
                # and we would hang on this one until the timeout.
                # Detect that and return empty so the caller can re-enter
                # cleanly instead of sleeping out the budget.
                current_cv = self._cond.get(pipeline_id)
                if current_cv is not cv:
                    return []

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
        The condition variable is also popped so long-lived orchestrators
        with many pipelines don't accumulate stale ``_cond`` entries.

        Returns:
            Number of messages cleared.
        """
        with self._lock:
            msgs = self._messages.pop(pipeline_id, [])
            cv = self._cond.pop(pipeline_id, None)
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
