"""Redis Streams-backed message store for inter-agent communication.

Replaces the in-memory MessageStore with persistent Redis Streams.
Each pipeline gets a single stream: pipeline:{id}:messages.
Agents interact via the orchestrator API, not directly with Redis.
"""

import json
import sys
import threading
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


import redis
from message_store import Message

logger = get_logger("orchestrator.redis_message_store")


def _stream_key(pipeline_id: str) -> str:
    """Get the Redis Stream key for a pipeline."""
    return f"pipeline:{pipeline_id}:messages"


def _counts_key(pipeline_id: str) -> str:
    """Get the Redis hash key for message type counters."""
    return f"pipeline:{pipeline_id}:msg_counts"


def _message_to_redis(msg: Message) -> dict[str, str]:
    """Serialize a Message to Redis hash fields."""
    return {
        "id": msg.id,
        "pipeline_id": msg.pipeline_id,
        "from_role": msg.from_role,
        "to_role": msg.to_role,
        "message_type": msg.message_type,
        "subject": msg.subject,
        "body": msg.body,
        "metadata": json.dumps(msg.metadata),
        "timestamp": msg.timestamp.isoformat(),
        "phase": msg.phase or "",
    }


def _message_from_redis(stream_id: str, fields: dict[bytes | str, bytes | str]) -> Message:
    """Deserialize a Message from Redis hash fields."""

    # Redis returns bytes by default; handle both bytes and str
    def _get(key: str) -> str:
        val = fields.get(key) or fields.get(key.encode(encoding="utf-8"), b"")
        if isinstance(val, bytes):
            return val.decode("utf-8")
        return str(val)

    metadata_str = _get("metadata")
    try:
        metadata = json.loads(metadata_str) if metadata_str else {}
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    timestamp_str = _get("timestamp")
    try:
        timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now(UTC)
    except ValueError:
        timestamp = datetime.now(UTC)

    phase = _get("phase") or None

    # Use the Redis stream ID as the message's cursor ID for since_id support
    msg_id = _get("id")

    return Message(
        id=msg_id,
        pipeline_id=_get("pipeline_id"),
        from_role=_get("from_role"),
        to_role=_get("to_role"),
        message_type=_get("message_type"),
        subject=_get("subject"),
        body=_get("body"),
        metadata=metadata,
        timestamp=timestamp,
        phase=phase,
    )


class RedisMessageStore:
    """Thread-safe Redis Streams-backed message storage.

    Same interface as MessageStore but persists messages in Redis Streams.
    Each pipeline has its own stream: pipeline:{id}:messages.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client
        # Map message UUID -> Redis stream ID for since_id lookups
        self._id_to_stream_id: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def add_message(self, message: Message) -> Message:
        """Add a message to the Redis Stream."""
        key = _stream_key(message.pipeline_id)
        counts_key_val = _counts_key(message.pipeline_id)
        fields = _message_to_redis(message)

        try:
            # Use a Redis pipeline for atomic xadd + hincrby
            pipe = self._redis.pipeline()
            pipe.xadd(key, fields)
            pipe.hincrby(counts_key_val, message.message_type, 1)
            results = pipe.execute()

            stream_id = results[0]
            if isinstance(stream_id, bytes):
                stream_id = stream_id.decode("utf-8")

            # Cache the mapping from message UUID to stream ID
            with self._lock:
                if message.pipeline_id not in self._id_to_stream_id:
                    self._id_to_stream_id[message.pipeline_id] = {}
                self._id_to_stream_id[message.pipeline_id][message.id] = stream_id

        except redis.RedisError as e:
            logger.error(
                "Failed to add message to Redis Stream",
                pipeline_id=message.pipeline_id,
                error=str(e),
            )
            raise

        return message

    def get_messages(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        limit: int = 100,
        wait: int = 0,
    ) -> list[Message]:
        """Get messages from the Redis Stream.

        Args:
            pipeline_id: Pipeline ID to query.
            role: If set, return messages where to_role is this role or 'all'.
            since_id: If set, return only messages after this message ID.
            limit: Maximum messages to return.
            wait: If > 0, block for this many seconds waiting for new messages.

        Returns:
            List of matching messages, oldest first.
        """
        key = _stream_key(pipeline_id)

        # Resolve since_id (message UUID) to Redis stream ID
        start_id = "0-0"
        if since_id:
            stream_id = self._resolve_stream_id(pipeline_id, since_id)
            if stream_id:
                start_id = stream_id
            else:
                # Fallback: scan the stream to find this message ID
                start_id = self._find_stream_id_by_message_id(pipeline_id, since_id) or "0-0"

        try:
            if wait > 0:
                # Blocking read — XREAD BLOCK
                result = self._redis.xread(
                    {key: start_id},
                    count=limit * 3,  # Over-read to account for role filtering
                    block=wait * 1000,  # milliseconds
                )
            else:
                # Non-blocking read — XRANGE for everything after start_id
                # Use XRANGE with exclusive start (add increment to start_id)
                exclusive_start = self._increment_stream_id(start_id) if since_id else start_id
                result_entries = self._redis.xrange(key, min=exclusive_start, count=limit * 3)
                # Normalize to same format as xread
                result = (
                    [(key.encode() if isinstance(key, str) else key, result_entries)]
                    if result_entries
                    else []
                )
        except redis.RedisError as e:
            logger.error(
                "Failed to read from Redis Stream",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            raise

        messages = []
        if result:
            for _sk, entries in result:
                for stream_id, fields in entries:
                    if isinstance(stream_id, bytes):
                        stream_id = stream_id.decode("utf-8")
                    msg = _message_from_redis(stream_id, fields)

                    # Cache the ID mapping
                    with self._lock:
                        if pipeline_id not in self._id_to_stream_id:
                            self._id_to_stream_id[pipeline_id] = {}
                        self._id_to_stream_id[pipeline_id][msg.id] = stream_id

                    messages.append(msg)

        # Role filtering (Python-side)
        if role:
            messages = [m for m in messages if m.to_role == role or m.to_role == "all"]

        # Apply limit
        return messages[-limit:] if len(messages) > limit else messages

    def get_status(self, pipeline_id: str) -> dict[str, Any]:
        """Get message statistics for a pipeline.

        Uses a Redis hash counter (pipeline:{id}:msg_counts) for O(1) type
        aggregation instead of scanning the entire stream.

        Note: The counter is increment-only (no decrement on message deletion
        or stream trimming). ``total`` comes from XINFO STREAM (authoritative)
        while ``by_type`` comes from the counter hash, so the two may diverge
        if the stream is externally trimmed. ``clear()`` resets both.
        """
        key = _stream_key(pipeline_id)
        counts_key = _counts_key(pipeline_id)
        try:
            info = self._redis.xinfo_stream(key)
            length = info.get("length", 0) if isinstance(info, dict) else 0

            # Read type counts from the hash counter (O(1) per type)
            raw_counts = self._redis.hgetall(counts_key)
            by_type: dict[str, int] = {}
            for k, v in raw_counts.items():
                type_name = k.decode("utf-8") if isinstance(k, bytes) else k
                count_val = v.decode("utf-8") if isinstance(v, bytes) else v
                by_type[type_name] = int(count_val)

            return {"total": length, "by_type": by_type}
        except redis.ResponseError:
            # Stream doesn't exist yet
            return {"total": 0, "by_type": {}}
        except redis.RedisError as e:
            logger.error("Failed to get stream status", pipeline_id=pipeline_id, error=str(e))
            return {"total": 0, "by_type": {}}

    def clear(self, pipeline_id: str) -> int:
        """Delete the Redis Stream and counters for a pipeline."""
        key = _stream_key(pipeline_id)
        counts_key_val = _counts_key(pipeline_id)
        try:
            length_before = self._redis.xlen(key)
            self._redis.delete(key, counts_key_val)
            with self._lock:
                self._id_to_stream_id.pop(pipeline_id, None)
            return length_before
        except redis.RedisError as e:
            logger.error("Failed to clear stream", pipeline_id=pipeline_id, error=str(e))
            return 0

    def _resolve_stream_id(self, pipeline_id: str, message_id: str) -> str | None:
        """Resolve a message UUID to a Redis Stream ID from cache."""
        with self._lock:
            return self._id_to_stream_id.get(pipeline_id, {}).get(message_id)

    def _find_stream_id_by_message_id(self, pipeline_id: str, message_id: str) -> str | None:
        """Scan the stream to find a message by its UUID. Fallback for cache miss.

        Uses paginated XRANGE with count=500 to avoid unbounded scans on large streams.
        """
        key = _stream_key(pipeline_id)
        batch_size = 500
        cursor = "-"
        try:
            while True:
                entries = self._redis.xrange(key, min=cursor, count=batch_size)
                if not entries:
                    break
                for stream_id, fields in entries:
                    if isinstance(stream_id, bytes):
                        stream_id = stream_id.decode("utf-8")
                    msg_id = fields.get(b"id", fields.get("id", b""))
                    if isinstance(msg_id, bytes):
                        msg_id = msg_id.decode("utf-8")
                    if msg_id == message_id:
                        # Cache it for next time
                        with self._lock:
                            if pipeline_id not in self._id_to_stream_id:
                                self._id_to_stream_id[pipeline_id] = {}
                            self._id_to_stream_id[pipeline_id][message_id] = stream_id
                        return stream_id
                # Advance cursor past the last entry in this batch
                last_id = entries[-1][0]
                if isinstance(last_id, bytes):
                    last_id = last_id.decode("utf-8")
                cursor = self._increment_stream_id(last_id)
                if len(entries) < batch_size:
                    break
        except redis.RedisError:
            pass
        return None

    @staticmethod
    def _increment_stream_id(stream_id: str) -> str:
        """Increment a Redis Stream ID for exclusive start in XRANGE."""
        parts = stream_id.split("-")
        if len(parts) == 2:
            return f"{parts[0]}-{int(parts[1]) + 1}"
        return stream_id


# Singleton
_redis_store: RedisMessageStore | None = None
_redis_lock = threading.Lock()


def get_redis_message_store(
    host: str = "localhost",
    port: int = 6379,
    db: int = 0,
) -> RedisMessageStore:
    """Get the singleton Redis message store."""
    global _redis_store
    if _redis_store is None:
        with _redis_lock:
            if _redis_store is None:
                pool = redis.ConnectionPool(
                    host=host,
                    port=port,
                    db=db,
                    decode_responses=False,  # Handle decoding ourselves
                    max_connections=20,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                )
                client = redis.Redis(connection_pool=pool)
                # Test connection
                try:
                    client.ping()
                except redis.RedisError as e:
                    raise ConnectionError(f"Cannot connect to Redis at {host}:{port}: {e}") from e
                _redis_store = RedisMessageStore(client)
    return _redis_store


def reset_redis_message_store() -> None:
    """Reset the singleton (for testing)."""
    global _redis_store
    _redis_store = None
