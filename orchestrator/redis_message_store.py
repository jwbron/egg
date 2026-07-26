"""Redis Streams-backed message store for inter-agent communication.

The orchestrator's only message-store backend (#3159 removed the
in-memory ``MessageStore`` it originally shadowed). Each pipeline gets a
single stream: pipeline:{id}:messages. Agents interact via the
orchestrator API, not directly with Redis. Shared message types live in
``message_store`` (:class:`Message`, :class:`MessageType`); the
singleton accessor is ``message_store.get_message_store()``.
"""

import json
import sys
import threading
import time
from collections.abc import Sequence
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
from message_store import GetMessagesMeta, Message, coerce_deprecated_message_type

logger = get_logger("orchestrator.redis_message_store")


# Connection-pool socket timeout, in seconds. redis-py enforces this on
# the blocked read itself, so a single XREAD BLOCK >= this dies with
# "Timeout reading from socket" before the server can answer. Named so
# ``_MAX_BLOCK_MS`` and its guard test derive from the one real value
# rather than duplicating the literal.
_SOCKET_TIMEOUT_SEC = 5

# Upper bound for a single XREAD BLOCK, in milliseconds. MUST stay safely
# below the connection pool's ``socket_timeout`` (``_SOCKET_TIMEOUT_SEC``,
# applied in ``get_redis_message_store``): a single BLOCK >= socket_timeout
# dies with "Timeout reading from socket" before the server can answer —
# every agent long-poll (25-60 s) 500'd at the 5 s mark. Long waits are
# therefore chunked into BLOCK slices of at most this length; XREAD
# returns immediately when data arrives, so chunking costs one extra
# round-trip per idle slice, not delivery latency. Caught live by the
# first deployed canary pipeline for #2662 — fakeredis has no sockets,
# so the unit tier structurally cannot regress-test the timeout itself;
# the slice-cap contract is pinned in test_redis_message_store.py
# instead. The 1 s margin below the socket timeout absorbs round-trip
# and scheduling slack so the slice returns before redis-py trips.
_MAX_BLOCK_MS = (_SOCKET_TIMEOUT_SEC - 1) * 1000

# Largest sequence number in a Redis Stream ID (64-bit unsigned). Used to
# name the exclusive predecessor of ``<ms>-0`` when seeking by ``since``
# timestamp so the exclusive XREAD (long-poll) and inclusive XRANGE
# (non-blocking) paths agree on the cutoff-millisecond boundary (#3481).
_MAX_STREAM_SEQ = (1 << 64) - 1


def _resolve_epoch(pipeline_id: str, run_epoch: str | None = None) -> str:
    """Resolve a run_epoch string for key composition.

    When ``run_epoch`` is explicitly supplied (e.g. from a Pipeline object's
    ``run_epoch`` field), use it directly. When ``None``, fall back to
    ``pipeline_id`` itself as the epoch marker — this preserves backward
    compatibility for callers that have not yet been migrated to pass
    ``run_epoch`` explicitly, and for fresh pipelines where ``run_epoch``
    has not been set yet (the create-path clear will wipe all epochs
    anyway).

    The returned string is used as a namespace component in stream keys
    so that a resumed pipeline (fresh ``run_epoch``) gets a clean message
    stream and cannot replay pre-cancel CONSENSUS_* messages (#3632).
    """
    if run_epoch is not None:
        return run_epoch
    return pipeline_id


def _stream_key(pipeline_id: str, run_epoch: str | None = None) -> str:
    """Get the Redis Stream key for a pipeline.

    Namespaced by ``(pipeline_id, run_epoch)`` so a resumed pipeline
    (fresh ``run_epoch``) gets a clean message stream and cannot
    replay pre-cancel CONSENSUS_* messages (#3632).
    """
    epoch = _resolve_epoch(pipeline_id, run_epoch)
    return f"pipeline:{pipeline_id}:{epoch}:messages"


def _counts_key(pipeline_id: str, run_epoch: str | None = None) -> str:
    """Get the Redis hash key for message type counters.

    Namespaced by ``(pipeline_id, run_epoch)`` for consistency with
    :func:`_stream_key` (#3632).
    """
    epoch = _resolve_epoch(pipeline_id, run_epoch)
    return f"pipeline:{pipeline_id}:{epoch}:msg_counts"


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
    except json.JSONDecodeError, TypeError:
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
        # Coerce deprecated types (e.g. replayed ``QUESTION`` from an
        # older checkpoint) to their current replacement so downstream
        # code doesn't need to handle removed enum members (#1897).
        message_type=coerce_deprecated_message_type(_get("message_type")),
        subject=_get("subject"),
        body=_get("body"),
        metadata=metadata,
        timestamp=timestamp,
        phase=phase,
    )


class RedisMessageStore:
    """Thread-safe Redis Streams-backed message storage.

    Each pipeline has its own stream per run_epoch:
    pipeline:{id}:{epoch}:messages. Messages survive orchestrator
    restarts; phase transitions wipe them by design via :meth:`clear`.

    The ``run_epoch`` namespace (#3632) ensures a resumed pipeline
    (fresh ``run_epoch``) gets a clean message stream and cannot
    replay pre-cancel CONSENSUS_* messages.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client
        # Map message UUID -> Redis stream ID for since_id lookups
        self._id_to_stream_id: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def add_message(self, message: Message, run_epoch: str | None = None) -> Message:
        """Add a message to the Redis Stream.

        ``run_epoch`` namespaces the stream key so a resumed pipeline
        (fresh ``run_epoch``) gets a clean stream (#3632).
        """
        key = _stream_key(message.pipeline_id, run_epoch)
        counts_key_val = _counts_key(message.pipeline_id, run_epoch)
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

            # Cache the mapping from message UUID to stream ID.
            # Keyed by (pipeline_id, run_epoch) so a resumed pipeline's
            # cache entries don't collide with the prior epoch's (#3632).
            epoch_key = _resolve_epoch(message.pipeline_id, run_epoch)
            cache_key = f"{message.pipeline_id}:{epoch_key}"
            with self._lock:
                if cache_key not in self._id_to_stream_id:
                    self._id_to_stream_id[cache_key] = {}
                self._id_to_stream_id[cache_key][message.id] = stream_id

        except redis.RedisError as e:
            logger.error(
                "Failed to add message to Redis Stream",
                pipeline_id=message.pipeline_id,
                error=str(e),
            )
            raise

        return message

    # Maximum inner-loop iterations when filtering for wait_for_types.
    # Cap avoids pathological tight loops when the stream is flooded with
    # non-matching message types (issue #1897, RISK).
    _WAIT_FOR_TYPES_MAX_INNER_LOOPS = 100

    def get_messages(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        wait: int = 0,
        wait_for_types: Sequence[str] | None = None,
        from_role: str | None = None,
        from_roles: Sequence[str] | None = None,
        slice_id: str | None = None,
        from_tip: bool = False,
        run_epoch: str | None = None,
    ) -> list[Message]:
        """Get messages from the Redis Stream.

        Thin wrapper around :meth:`get_messages_with_meta` that drops the
        meta tuple. Existing callers that don't care about cursor staleness
        continue to use this signature unchanged (issue #2464).

        Args:
            pipeline_id: Pipeline ID to query.
            role: If set, return messages where to_role is this role or 'all'.
            since_id: If set, return only messages after this message ID.
            since: If set (and ``since_id`` is not), return only messages
                at or after this timestamp. Millisecond stream-ID
                resolution; see :meth:`get_messages_with_meta` (#3481).
            limit: Maximum messages to return.
            wait: If > 0, block for this many seconds waiting for new messages.
            wait_for_types: If set (and ``wait > 0``), only treat a read as
                "matched" when at least one message of these types is
                available after applying role filtering. Non-matching rows
                are discarded and the caller keeps blocking on the remaining
                time budget. Issue #1897.
            from_role: If set, only messages whose ``from_role`` equals this
                value count as matches and are returned. Applied inside the
                blocking loop so a wrong-sender message does NOT wake the
                waiting caller (prevents spin). Matched the in-memory
                backend's signature for backend-consistency while both
                existed — the ``routes/messages.py`` wait endpoint passes
                ``from_role=...`` unconditionally, so a Redis backend
                without this parameter raised ``TypeError`` in production
                (reviewer_code blocker 1 on #1897 proposal v4).
            from_tip: If True AND ``since_id`` is not set AND ``wait > 0``,
                start the read at the stream tip (Redis ``$``) so only
                entries added *after* the call starts can unblock the wait.
                Required by the ``/messages/wait`` endpoint's event-driven
                contract (issue #1925) — without it, a repeated wait-loop
                call returns the same already-seen event on every invocation.
            run_epoch: When set, namespace the stream key by this epoch
                so a resumed pipeline (fresh ``run_epoch``) reads only
                the new epoch's messages (#3632). When ``None``, falls
                back to ``pipeline_id`` as the epoch marker (backward
                compat).
        """
        messages, _meta = self.get_messages_with_meta(
            pipeline_id,
            role=role,
            since_id=since_id,
            since=since,
            limit=limit,
            wait=wait,
            wait_for_types=wait_for_types,
            from_role=from_role,
            from_roles=from_roles,
            slice_id=slice_id,
            from_tip=from_tip,
            run_epoch=run_epoch,
        )
        return messages

    def get_messages_with_meta(
        self,
        pipeline_id: str,
        *,
        role: str | None = None,
        since_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        wait: int = 0,
        wait_for_types: Sequence[str] | None = None,
        from_role: str | None = None,
        from_roles: Sequence[str] | None = None,
        slice_id: str | None = None,
        from_tip: bool = False,
        _suppress_stale_warning: bool = False,
        run_epoch: str | None = None,
    ) -> tuple[list[Message], GetMessagesMeta]:
        """Same as :meth:`get_messages` but also returns staleness metadata.

        ``since`` (#3481): timestamp cutoff for operators debugging a live
        pipeline. Redis Stream auto-IDs are ``<unix-ms>-<seq>``, so the
        cutoff maps directly to a start stream ID and the read seeks past
        older history instead of scanning from ``0-0`` (which made
        ``limit`` return a window hours before the cutoff). Resolution is
        the stream ID's millisecond, inclusive of the cutoff millisecond on
        both the non-blocking (XRANGE) and long-poll (XREAD, ``wait > 0``)
        paths — the seek anchors on the exclusive predecessor of
        ``<cutoff_ms>-0`` so the exclusive XREAD start and the inclusive
        XRANGE ``min`` agree on the boundary (#3485 review).
        Ignored when ``since_id`` is supplied; the explicit cursor is
        more precise (the route rejects the combination outright).

        ``meta.since_id_stale`` is ``True`` iff the caller supplied a
        non-None ``since_id`` that did not resolve to any stream entry
        (cache miss + paginated scan miss). The full-history fallback
        contract is preserved (returns from ``0-0``); the meta lets
        consumers clear cached cursors instead of re-passing the dead
        value forever (issue #2464). A transient ``RedisError`` raised
        from the scan fallback (e.g., a connection blip during
        ``XRANGE``) is caught here and treated as "preserve cursor,
        degrade to full history" — ``meta.since_id_stale`` stays
        ``False`` in that case so a polling consumer doesn't drop a
        live cursor on a momentary connectivity hiccup.

        ``_suppress_stale_warning`` mirrored the removed in-memory
        backend's kwarg for API symmetry. The Redis path does not log on
        stale resolution, so the flag is a no-op; it is still accepted
        so existing callers' kwargs keep working.

        ``run_epoch``: When set, namespace the stream key by this epoch
        so a resumed pipeline (fresh ``run_epoch``) reads only the new
        epoch's messages (#3632). When ``None``, falls back to
        ``pipeline_id`` as the epoch marker (backward compat).
        """
        key = _stream_key(pipeline_id, run_epoch)

        # Resolve since_id (message UUID) to Redis stream ID
        start_id = "0-0"
        since_id_stale = False
        if from_tip and not since_id and wait > 0:
            # from_tip = "deliver only entries added after this call
            # begins". Resolve the current tip to a CONCRETE stream id
            # once, here, rather than passing Redis's ``$`` sentinel into
            # the (chunked) blocking read below. ``$`` is re-resolved
            # server-side to the *live* tip on every XREAD re-issue, so
            # across idle BLOCK slices a message XADDed in the gap between
            # one slice returning empty and the next being issued would be
            # skipped — the next ``$`` starts after it — a silent drop in
            # the consensus path. A fixed concrete id never advances on
            # its own, so re-blocking from it re-scans that gap and cannot
            # drop a mid-wait arrival — from_tip stays race-free against
            # concurrent add_message. An empty/missing stream resolves to
            # "0-0" (read everything > 0-0), which still catches the first
            # arrival.
            start_id = self._resolve_tip_stream_id(pipeline_id)
        elif since_id:
            stream_id = self._resolve_stream_id(pipeline_id, since_id)
            if stream_id:
                start_id = stream_id
            else:
                # Fallback: scan the stream to find this message ID. If
                # the scan also misses, the cursor is genuinely unknown —
                # signal staleness so the consumer can clear it (#2464).
                # A RedisError during the scan is *transient* (connection
                # blip mid-XRANGE), not a "scan miss" — preserving the
                # consumer's cursor through the blip is preferable to
                # telling them to drop it. Degrade to the pre-PR
                # full-history fallback without flagging staleness.
                try:
                    resolved = self._find_stream_id_by_message_id(pipeline_id, since_id)
                except redis.RedisError as exc:
                    logger.warning(
                        "since_id scan failed transiently; degrading to full history",
                        pipeline_id=pipeline_id,
                        error=str(exc),
                    )
                    resolved = None
                    start_id = "0-0"
                else:
                    if resolved:
                        start_id = resolved
                    else:
                        since_id_stale = True
                        start_id = "0-0"
        elif since is not None:
            # Timestamp seek (#3481). Naive datetimes are treated as UTC
            # to match ``Message.timestamp``'s default factory. Seek to the
            # *exclusive predecessor* of ``<cutoff_ms>-0`` (i.e.
            # ``<cutoff_ms - 1>`` at the max sequence) rather than to
            # ``<cutoff_ms>-0`` itself, so both read paths land inclusive of
            # the cutoff millisecond: the non-blocking XRANGE uses this as an
            # inclusive ``min`` and the blocking XREAD (long-poll, wait > 0)
            # treats it as an exclusive start. Anchoring on ``<cutoff_ms>-0``
            # directly would make the XREAD path skip an entry sitting at
            # exactly that id, contradicting the "inclusive of the cutoff
            # millisecond" contract above (#3485 review). ``cutoff_ms == 0``
            # (epoch or earlier) floors to ``0-0``.
            cutoff = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
            cutoff_ms = max(int(cutoff.timestamp() * 1000), 0)
            start_id = f"{cutoff_ms - 1}-{_MAX_STREAM_SEQ}" if cutoff_ms else "0-0"

        meta = GetMessagesMeta(since_id_stale=since_id_stale)
        want_types = set(wait_for_types) if wait_for_types else None
        # Sender allowlist (#2725): set form of from_role. ``from_role``
        # (singular) wins when both are provided so legacy callers see no
        # behavior change.
        from_roles_set: set[str] | None
        if from_role:
            from_roles_set = None
        elif from_roles:
            from_roles_set = {r for r in from_roles if r}
            if not from_roles_set:
                from_roles_set = None
        else:
            from_roles_set = None

        def _passes_filters(m: Message) -> bool:
            """Apply slice + sender-allowlist filters (#2725).

            Null-on-message slice is a pipeline-level passthrough so
            OVERSEER_ALERT and global phase signals continue to wake
            slice-scoped waiters.
            """
            if slice_id is not None:
                msg_slice = m.metadata.get("slice_id")
                if msg_slice is not None and msg_slice != slice_id:
                    return False
            if from_roles_set is not None and m.from_role not in from_roles_set:
                return False
            return True

        # Capped tail read (#3548): a non-blocking, cursor-less read is a
        # "recent messages" query (operator views, the snapshot's
        # recent_messages, consensus inference, tracker reconstruction).
        # XRANGE from ``0-0`` with a count cap returns the OLDEST ``count``
        # entries, so once the stream outgrows the cap these callers saw a
        # frozen head-of-stream window and concluded the bus was dead — the
        # #3548 incident's "no CONSENSUS_ACK ever appears on the bus"
        # misdiagnosis. Read the NEWEST entries instead (XREVRANGE, restored
        # to chronological order). Cursor / timestamp / from_tip / blocking
        # reads keep forward semantics — their start id bounds the window.
        tail_read = since_id is None and since is None and not from_tip and wait <= 0

        def _read_once(
            read_start_id: str, block_ms: int | None
        ) -> tuple[list[Message], str | None]:
            """Perform one XREAD/XRANGE. Returns (messages, last_stream_id)."""
            try:
                if block_ms is not None:
                    result = self._redis.xread(
                        {key: read_start_id},
                        count=limit * 3,
                        block=block_ms,
                    )
                else:
                    if tail_read:
                        # Newest ``limit * 3`` entries, oldest→newest so the
                        # downstream filters and ``[-limit:]`` truncation keep
                        # working unchanged.
                        result_entries = list(reversed(self._redis.xrevrange(key, count=limit * 3)))
                    else:
                        # Non-blocking read — XRANGE for everything after
                        # read_start_id. Exclusive start when since_id is set.
                        exclusive_start = (
                            self._increment_stream_id(read_start_id)
                            if since_id and read_start_id != "0-0"
                            else read_start_id
                        )
                        result_entries = self._redis.xrange(
                            key, min=exclusive_start, count=limit * 3
                        )
                    result = (
                        [
                            (
                                key.encode() if isinstance(key, str) else key,
                                result_entries,
                            )
                        ]
                        if result_entries
                        else []
                    )
            except redis.TimeoutError as e:
                if block_ms is not None:
                    # A blocked read outlived the client socket timeout.
                    # _MAX_BLOCK_MS is sized to prevent this; if it fires
                    # anyway (e.g. an operator lowered socket_timeout),
                    # treat it as an idle slice — the caller's deadline
                    # loop bounds the retries — rather than 500ing the
                    # whole long-poll.
                    logger.warning(
                        "Blocking Redis Stream read hit the client socket "
                        "timeout; treating as an empty slice",
                        pipeline_id=pipeline_id,
                        block_ms=block_ms,
                        error=str(e),
                    )
                    return [], None
                logger.error(
                    "Failed to read from Redis Stream",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                raise
            except redis.RedisError as e:
                logger.error(
                    "Failed to read from Redis Stream",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                raise

            out: list[Message] = []
            last_sid: str | None = None
            if result:
                for _sk, entries in result:
                    for sid, fields in entries:
                        if isinstance(sid, bytes):
                            sid = sid.decode("utf-8")
                        msg = _message_from_redis(sid, fields)
                        with self._lock:
                            epoch_key = _resolve_epoch(pipeline_id, run_epoch)
                            cache_key = f"{pipeline_id}:{epoch_key}"
                            if cache_key not in self._id_to_stream_id:
                                self._id_to_stream_id[cache_key] = {}
                            self._id_to_stream_id[cache_key][msg.id] = sid
                        out.append(msg)
                        last_sid = sid
            return out, last_sid

        # No type filter: preserve the original behaviour (fast path).
        if want_types is None:
            if wait > 0:
                # Chunked blocking read (see _MAX_BLOCK_MS): re-issue
                # XREAD BLOCK in slices until rows arrive or the wait
                # budget elapses. Semantics match the former single
                # XREAD BLOCK — the wait ends at the first batch of rows
                # whether or not they survive the filters below.
                fast_deadline = time.monotonic() + float(wait)
                messages = []
                while True:
                    remaining_ms = int((fast_deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        break
                    messages, _ = _read_once(start_id, min(remaining_ms, _MAX_BLOCK_MS))
                    if messages:
                        break
            else:
                messages, _ = _read_once(start_id, None)
            if role:
                messages = [m for m in messages if m.to_role == role or m.to_role == "all"]
            if from_role:
                messages = [m for m in messages if m.from_role == from_role]
            messages = [m for m in messages if _passes_filters(m)]
            out_msgs = messages[-limit:] if len(messages) > limit else messages
            return out_msgs, meta

        # wait_for_types: re-block on remaining time budget until we find a
        # matching row or the deadline elapses. Cap the inner loop so a
        # flood of non-matching rows can't spin forever.
        deadline = time.monotonic() + float(wait)
        current_start = start_id
        inner_loops = 0
        while True:
            remaining = deadline - time.monotonic() if wait > 0 else 0.0
            if wait > 0 and remaining <= 0:
                return [], meta

            block_ms: int | None
            if wait > 0:
                # Slice the remaining budget (see _MAX_BLOCK_MS). An idle
                # slice reads nothing, leaves the cursor in place, and
                # loops back here; the deadline check above terminates
                # the wait. The inner-loop cap is no risk: 100 idle
                # slices x 4 s far exceeds any wait budget.
                block_ms = min(max(int(remaining * 1000), 1), _MAX_BLOCK_MS)
            else:
                block_ms = None

            messages, last_sid = _read_once(current_start, block_ms)
            if role:
                messages = [m for m in messages if m.to_role == role or m.to_role == "all"]
            if from_role:
                messages = [m for m in messages if m.from_role == from_role]
            messages = [m for m in messages if _passes_filters(m)]

            matching = [m for m in messages if m.message_type in want_types]
            if matching:
                out_msgs = matching[-limit:] if len(matching) > limit else matching
                return out_msgs, meta

            # No match. If wait=0, bail out. Otherwise advance the cursor
            # past what we just read and keep blocking.
            if wait <= 0:
                return [], meta

            if last_sid is not None:
                # Advance past the last sid so we don't re-read the same
                # rows. On a pure-idle slice last_sid is None, so
                # current_start stays the concrete tip id resolved at
                # entry — re-blocking from it re-scans the gap, so a
                # mid-wait arrival is never dropped.
                current_start = last_sid

            inner_loops += 1
            if inner_loops >= self._WAIT_FOR_TYPES_MAX_INNER_LOOPS:
                logger.warning(
                    "wait_for_types inner-loop cap reached",
                    pipeline_id=pipeline_id,
                    cap=self._WAIT_FOR_TYPES_MAX_INNER_LOOPS,
                    type_filter=list(want_types),
                )
                return [], meta

    def _resolve_tip_stream_id(self, pipeline_id: str) -> str:
        """Snapshot the current stream tip as a concrete id for from_tip waits.

        ``XREVRANGE … COUNT 1`` returns the greatest stream id present
        *now*; an ``XREAD`` started from it (exclusive) delivers only
        later arrivals — the from_tip contract — without ever re-resolving
        Redis's ``$`` sentinel mid-wait (see the call site for why that
        matters). Returns ``"0-0"`` for an empty/missing stream, which
        reads everything ``> 0-0`` and so still catches the first arrival.
        A ``RedisError`` degrades to ``"0-0"`` for the same reason: the
        caller's deadline loop bounds the read, and starting from the
        head of a (typically empty) from_tip stream loses nothing.
        """
        key = _stream_key(pipeline_id)
        try:
            entries = self._redis.xrevrange(key, count=1)
        except redis.RedisError as exc:
            # Mirror the since_id transient-degradation path above: log
            # before degrading so the rare event is visible. On a
            # non-empty stream this re-delivers pre-existing history as if
            # new (at-least-once), the safe direction vs. dropping a
            # message; the caller's deadline loop still bounds the read.
            logger.warning(
                "from_tip tip resolution failed transiently; degrading to 0-0",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return "0-0"
        if entries:
            stream_id = entries[0][0]
            if isinstance(stream_id, bytes):
                stream_id = stream_id.decode("utf-8")
            return stream_id
        return "0-0"

    def get_latest_id(self, pipeline_id: str, run_epoch: str | None = None) -> str | None:
        """Return the ID of the most recent message for *pipeline_id*, or ``None``.

        Uses ``XREVRANGE … COUNT 1`` for an O(1) tail read.  Extracts the
        ``id`` field directly from the Redis hash to avoid deserializing the
        full :class:`Message` (JSON metadata, ISO timestamps, etc.).

        ``run_epoch`` namespaces the stream key (#3632).
        """
        key = _stream_key(pipeline_id, run_epoch)
        try:
            entries = self._redis.xrevrange(key, count=1)
            if entries:
                _stream_id, fields = entries[0]
                msg_id = fields.get(b"id") or fields.get("id", b"")
                if isinstance(msg_id, bytes):
                    msg_id = msg_id.decode("utf-8")
                return msg_id or None
        except Exception:
            return None
        return None

    def get_status(self, pipeline_id: str, run_epoch: str | None = None) -> dict[str, Any]:
        """Get message statistics for a pipeline.

        Uses a Redis hash counter (pipeline:{id}:{epoch}:msg_counts) for O(1)
        type aggregation instead of scanning the entire stream.

        Note: The counter is increment-only (no decrement on message deletion
        or stream trimming). ``total`` comes from XINFO STREAM (authoritative)
        while ``by_type`` comes from the counter hash, so the two may diverge
        if the stream is externally trimmed. ``clear()`` resets both.

        ``run_epoch`` namespaces the keys (#3632).
        """
        key = _stream_key(pipeline_id, run_epoch)
        counts_key = _counts_key(pipeline_id, run_epoch)
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

    def clear(self, pipeline_id: str, run_epoch: str | None = None) -> int:
        """Delete the Redis Stream and counters for a pipeline.

        ``run_epoch`` namespaces the keys (#3632). When ``None``,
        clears ALL epoch namespaces for the given ``pipeline_id``
        (used by DELETE and CREATE paths to defend #2053). When
        supplied, clears only that specific epoch.
        """
        if run_epoch is None:
            # Clear all epoch namespaces for this pipeline_id.
            # Use SCAN to find all matching keys, then delete them.
            total_cleared = 0
            try:
                # Pattern: pipeline:{pipeline_id}:*:messages and
                # pipeline:{pipeline_id}:*:msg_counts
                stream_pattern = f"pipeline:{pipeline_id}:*:messages"
                counts_pattern = f"pipeline:{pipeline_id}:*:msg_counts"
                for pattern in [stream_pattern, counts_pattern]:
                    cursor = 0
                    while True:
                        cursor, keys = self._redis.scan(cursor=cursor, match=pattern, count=100)
                        if keys:
                            total_cleared += self._redis.delete(*keys)
                        if cursor == 0:
                            break
                # Also clear the legacy bare-key format (pre-#3632) for
                # pipelines that may have been created before the migration.
                legacy_stream = _stream_key(pipeline_id)
                legacy_counts = _counts_key(pipeline_id)
                total_cleared += self._redis.delete(legacy_stream, legacy_counts)
                with self._lock:
                    # Clear all cache entries for this pipeline_id
                    keys_to_remove = [
                        k for k in self._id_to_stream_id if k.startswith(f"{pipeline_id}:")
                    ]
                    for k in keys_to_remove:
                        self._id_to_stream_id.pop(k, None)
                    # Also clear legacy bare-key cache entry
                    self._id_to_stream_id.pop(pipeline_id, None)
            except redis.RedisError as e:
                logger.error("Failed to clear all streams", pipeline_id=pipeline_id, error=str(e))
            return total_cleared
        else:
            key = _stream_key(pipeline_id, run_epoch)
            counts_key_val = _counts_key(pipeline_id, run_epoch)
            try:
                length_before = self._redis.xlen(key)
                self._redis.delete(key, counts_key_val)
                with self._lock:
                    epoch_key = _resolve_epoch(pipeline_id, run_epoch)
                    cache_key = f"{pipeline_id}:{epoch_key}"
                    self._id_to_stream_id.pop(cache_key, None)
                return length_before
            except redis.RedisError as e:
                logger.error("Failed to clear stream", pipeline_id=pipeline_id, error=str(e))
                return 0

    def _resolve_stream_id(self, pipeline_id: str, message_id: str, run_epoch: str | None = None) -> str | None:
        """Resolve a message UUID to a Redis Stream ID from cache.

        ``run_epoch`` namespaces the cache lookup (#3632).
        """
        epoch_key = _resolve_epoch(pipeline_id, run_epoch)
        cache_key = f"{pipeline_id}:{epoch_key}"
        with self._lock:
            return self._id_to_stream_id.get(cache_key, {}).get(message_id)

    def _find_stream_id_by_message_id(self, pipeline_id: str, message_id: str, run_epoch: str | None = None) -> str | None:
        """Scan the stream to find a message by its UUID. Fallback for cache miss.

        Uses paginated XRANGE with count=500 to avoid unbounded scans on large streams.

        Returns ``None`` for a *genuine* miss (the scan completed and no
        entry matched). Re-raises :class:`redis.RedisError` so the
        caller can distinguish a transient connection failure from a
        completed-but-empty scan and act accordingly (the staleness
        signal in :meth:`get_messages_with_meta` is suppressed on the
        transient path so a momentary blip does not tell the consumer
        to drop a still-live cursor — issue #2464 reviewer note #3).

        ``run_epoch`` namespaces the stream key (#3632).
        """
        key = _stream_key(pipeline_id, run_epoch)
        batch_size = 500
        cursor = "-"
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
                    epoch_key = _resolve_epoch(pipeline_id, run_epoch)
                    cache_key = f"{pipeline_id}:{epoch_key}"
                    with self._lock:
                        if cache_key not in self._id_to_stream_id:
                            self._id_to_stream_id[cache_key] = {}
                        self._id_to_stream_id[cache_key][message_id] = stream_id
                    return stream_id
            # Advance cursor past the last entry in this batch
            last_id = entries[-1][0]
            if isinstance(last_id, bytes):
                last_id = last_id.decode("utf-8")
            cursor = self._increment_stream_id(last_id)
            if len(entries) < batch_size:
                break
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
                    socket_timeout=_SOCKET_TIMEOUT_SEC,
                    socket_connect_timeout=_SOCKET_TIMEOUT_SEC,
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
