"""Unit tests for Redis Streams-backed message store.

Tests RedisMessageStore operations using fakeredis to simulate
a real Redis backend without requiring a running Redis server.
"""

import sys
import threading
import time
from pathlib import Path

import fakeredis
import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from message_store import Message, MessageType
from redis_message_store import RedisMessageStore, _counts_key, _stream_key


@pytest.fixture
def redis_client():
    """Create a fakeredis client for testing."""
    return fakeredis.FakeRedis()


@pytest.fixture
def store(redis_client):
    """Create a RedisMessageStore with a fake Redis client."""
    return RedisMessageStore(redis_client)


@pytest.fixture
def sample_message():
    """Create a sample message for testing."""
    return Message(
        pipeline_id="test-pipeline",
        from_role="coder",
        to_role="all",
        message_type=MessageType.PROGRESS,
        subject="Update",
        body="Completed task 1",
        metadata={"task": "task-1"},
    )


class TestAddMessage:
    """Test adding messages to Redis Streams."""

    def test_add_single_message(self, store, sample_message):
        result = store.add_message(sample_message)
        assert result.id == sample_message.id
        assert result.pipeline_id == "test-pipeline"

    def test_add_multiple_messages(self, store):
        for i in range(5):
            msg = Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject=f"Update {i}",
                body=f"Task {i}",
            )
            store.add_message(msg)

        messages = store.get_messages("test-pipeline")
        assert len(messages) == 5

    def test_message_persists_in_stream(self, store, redis_client, sample_message):
        store.add_message(sample_message)
        key = _stream_key("test-pipeline")
        length = redis_client.xlen(key)
        assert length == 1

    def test_counter_incremented_on_add(self, store, redis_client, sample_message):
        store.add_message(sample_message)
        counts_key = _counts_key("test-pipeline")
        count = redis_client.hget(counts_key, MessageType.PROGRESS)
        assert int(count) == 1

    def test_atomic_xadd_and_hincrby(self, store, redis_client):
        """Verify xadd and hincrby are atomic via pipeline."""
        for i in range(10):
            msg = Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject=f"msg {i}",
            )
            store.add_message(msg)

        key = _stream_key("test-pipeline")
        counts_key = _counts_key("test-pipeline")
        stream_len = redis_client.xlen(key)
        counter_val = int(redis_client.hget(counts_key, MessageType.PROGRESS))
        assert stream_len == counter_val == 10


class TestGetMessages:
    """Test retrieving messages from Redis Streams."""

    def _add_messages(self, store, count=3, pipeline_id="test-pipeline"):
        msgs = []
        for i in range(count):
            msg = Message(
                pipeline_id=pipeline_id,
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject=f"msg-{i}",
                body=f"body-{i}",
            )
            store.add_message(msg)
            msgs.append(msg)
        return msgs

    def test_get_all_messages(self, store):
        self._add_messages(store, 5)
        messages = store.get_messages("test-pipeline")
        assert len(messages) == 5

    def test_get_empty_pipeline(self, store):
        messages = store.get_messages("nonexistent")
        assert messages == []

    def test_role_filtering(self, store):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="tester",
                message_type=MessageType.PROGRESS,
                subject="For tester",
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.STATUS,
                subject="Broadcast",
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="reviewer_code",
                message_type=MessageType.PROGRESS,
                subject="For reviewer",
            )
        )

        tester_msgs = store.get_messages("test-pipeline", role="tester")
        assert len(tester_msgs) == 2  # targeted + broadcast
        assert all(m.to_role in ("tester", "all") for m in tester_msgs)

    def test_since_id_filtering(self, store):
        msgs = self._add_messages(store, 5)
        # Get messages after the second one
        since_msgs = store.get_messages("test-pipeline", since_id=msgs[1].id)
        assert len(since_msgs) == 3  # msgs[2], msgs[3], msgs[4]

    def test_stale_since_id_returns_all_messages(self, store):
        """A cursor that isn't in the stream (e.g., survived a phase
        clear or came from a stale anchor) must degrade to full-history
        replay rather than silently returning empty — matches the
        in-memory backend and prevents the stall in issue #1814."""
        self._add_messages(store, 3)
        msgs = store.get_messages("test-pipeline", since_id="nonexistent-cursor-xyz")
        assert len(msgs) == 3

    def test_get_messages_with_meta_signals_stale_cursor(self, store):
        """Issue #2464 — Redis backend mirrors the in-memory staleness
        signal: an unknown ``since_id`` produces ``since_id_stale=True``
        on the meta sibling, while ``get_messages`` keeps its full-history
        fallback."""
        self._add_messages(store, 3)
        msgs, meta = store.get_messages_with_meta(
            "test-pipeline", since_id="nonexistent-cursor-xyz"
        )
        assert meta.since_id_stale is True
        assert len(msgs) == 3

    def test_get_messages_with_meta_clean_when_cursor_known(self, store):
        """A resolvable ``since_id`` reports ``since_id_stale=False``."""
        msgs = self._add_messages(store, 3)
        _result, meta = store.get_messages_with_meta("test-pipeline", since_id=msgs[0].id)
        assert meta.since_id_stale is False

    def test_get_messages_with_meta_clean_when_no_since_id(self, store):
        """No ``since_id`` → never stale (no cursor to resolve)."""
        self._add_messages(store, 3)
        _msgs, meta = store.get_messages_with_meta("test-pipeline")
        assert meta.since_id_stale is False

    def test_get_messages_with_meta_signals_stale_after_clear(self, store):
        """After ``clear()`` the previously-valid cursor is stale —
        exactly the post-phase-boundary case #2464 surfaces."""
        msgs = self._add_messages(store, 3)
        anchor = msgs[0].id
        # Pre-clear, anchor is fresh.
        _, meta_before = store.get_messages_with_meta("test-pipeline", since_id=anchor)
        assert meta_before.since_id_stale is False

        store.clear("test-pipeline")
        self._add_messages(store, 1)

        _, meta_after = store.get_messages_with_meta("test-pipeline", since_id=anchor)
        assert meta_after.since_id_stale is True

    def test_get_messages_with_meta_transient_redis_error_is_not_stale(self, store):
        """Issue #2464 reviewer note #3 — a transient ``RedisError``
        raised from the scan fallback (e.g., a connection blip during
        ``XRANGE``) must NOT be reported as cursor staleness. Pre-fix
        this path swallowed the exception inside
        ``_find_stream_id_by_message_id`` and returned ``None``, which
        the caller treated as "scan miss → since_id_stale=True". A
        well-behaved consumer would then drop a still-live cursor on a
        momentary blip.

        Post-fix the scan helper re-raises ``RedisError`` so the caller
        can distinguish "scan completed empty" from "scan errored out"
        — the route preserves the cursor on the latter."""
        from unittest.mock import patch

        import redis

        # Pre-populate the stream so the cache resolve fails (cache cleared
        # below) and the scan fallback path is taken.
        msgs = self._add_messages(store, 3)
        target_id = msgs[1].id
        with store._lock:
            store._id_to_stream_id.clear()

        # Force *only* the scan helper to raise — leaves the downstream
        # XRANGE / XREAD calls in ``_read_once`` (which serve the
        # full-history fallback) backed by real fakeredis, so we
        # exercise the route's branch without breaking the rest of
        # the call. Pre-fix the swallow turned this into
        # ``since_id_stale=True``; post-fix the caller catches the
        # raise and reports ``False`` so consumers keep the cursor
        # through the blip.
        with patch.object(
            store,
            "_find_stream_id_by_message_id",
            side_effect=redis.RedisError("connection blip"),
        ):
            _msgs, meta = store.get_messages_with_meta("test-pipeline", since_id=target_id)

        assert meta.since_id_stale is False, (
            "transient RedisError during scan must not be reported as "
            "cursor staleness — would tell consumers to drop a live cursor"
        )

    def test_find_stream_id_by_message_id_propagates_redis_error(self, store):
        """Pin the contract change at the helper level: ``_find_stream_id_by_message_id``
        used to swallow ``RedisError`` and return ``None`` (indistinguishable
        from a genuine scan miss). It must now propagate the exception so
        :meth:`get_messages_with_meta` can branch on it."""
        from unittest.mock import patch

        import redis

        self._add_messages(store, 1)
        with patch.object(
            store._redis,
            "xrange",
            side_effect=redis.RedisError("connection blip"),
        ):
            with pytest.raises(redis.RedisError):
                store._find_stream_id_by_message_id("test-pipeline", "any-id")

    def test_limit(self, store):
        self._add_messages(store, 10)
        messages = store.get_messages("test-pipeline", limit=3)
        assert len(messages) == 3

    def test_pipeline_isolation(self, store):
        self._add_messages(store, 3, pipeline_id="pipeline-a")
        self._add_messages(store, 5, pipeline_id="pipeline-b")

        msgs_a = store.get_messages("pipeline-a")
        msgs_b = store.get_messages("pipeline-b")
        assert len(msgs_a) == 3
        assert len(msgs_b) == 5


class TestGetStatus:
    """Test message status/statistics."""

    def test_status_empty_pipeline(self, store):
        status = store.get_status("nonexistent")
        assert status["total"] == 0
        assert status["by_type"] == {}

    def test_status_counts_by_type(self, store):
        for msg_type in [MessageType.PROGRESS, MessageType.PROGRESS, MessageType.STATUS]:
            store.add_message(
                Message(
                    pipeline_id="test-pipeline",
                    from_role="coder",
                    to_role="all",
                    message_type=msg_type,
                    subject="test",
                )
            )

        status = store.get_status("test-pipeline")
        assert status["total"] == 3
        assert status["by_type"][MessageType.PROGRESS] == 2
        assert status["by_type"][MessageType.STATUS] == 1

    def test_status_counter_matches_stream(self, store, redis_client):
        """Counter hash and XINFO STREAM total should agree."""
        for i in range(5):
            store.add_message(
                Message(
                    pipeline_id="test-pipeline",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject=f"msg {i}",
                )
            )

        status = store.get_status("test-pipeline")
        assert status["total"] == 5
        assert sum(status["by_type"].values()) == 5


class TestClear:
    """Test clearing pipeline messages."""

    def test_clear_removes_stream(self, store, redis_client):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="test",
            )
        )

        count = store.clear("test-pipeline")
        assert count == 1

        # Stream and counter should be deleted
        key = _stream_key("test-pipeline")
        assert not redis_client.exists(key)
        counts_key = _counts_key("test-pipeline")
        assert not redis_client.exists(counts_key)

    def test_clear_resets_id_cache(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="test",
        )
        store.add_message(msg)
        store.clear("test-pipeline")

        # After clearing, since_id should not resolve
        assert store._resolve_stream_id("test-pipeline", msg.id) is None

    def test_clear_empty_pipeline(self, store):
        count = store.clear("nonexistent")
        assert count == 0


class TestStreamIdLookup:
    """Test message UUID -> stream ID resolution."""

    def test_cache_populated_on_add(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="test",
        )
        store.add_message(msg)

        stream_id = store._resolve_stream_id("test-pipeline", msg.id)
        assert stream_id is not None

    def test_fallback_scan_finds_message(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="test",
        )
        store.add_message(msg)

        # Clear the cache to force fallback scan
        with store._lock:
            store._id_to_stream_id.clear()

        stream_id = store._find_stream_id_by_message_id("test-pipeline", msg.id)
        assert stream_id is not None

    def test_paginated_scan(self, store):
        """Scan should handle multiple pages correctly."""
        msgs = []
        for i in range(10):
            msg = Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject=f"msg-{i}",
            )
            store.add_message(msg)
            msgs.append(msg)

        # Clear cache, force scan
        with store._lock:
            store._id_to_stream_id.clear()

        # Find the last message (forces scanning through all pages)
        found = store._find_stream_id_by_message_id("test-pipeline", msgs[-1].id)
        assert found is not None


class TestIncrementStreamId:
    """Test stream ID increment for exclusive range queries."""

    def test_normal_increment(self):
        assert RedisMessageStore._increment_stream_id("1234-5") == "1234-6"

    def test_zero_sequence(self):
        assert RedisMessageStore._increment_stream_id("1234-0") == "1234-1"

    def test_zero_zero_returns_zero_one(self):
        """0-0 should return 0-1 for exclusive-after semantics."""
        assert RedisMessageStore._increment_stream_id("0-0") == "0-1"


class TestLongPolling:
    """Test long-polling via wait parameter on get_messages."""

    def test_wait_returns_immediately_with_existing_data(self, store):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="existing",
            )
        )

        start = time.monotonic()
        messages = store.get_messages("test-pipeline", wait=5)
        elapsed = time.monotonic() - start

        assert len(messages) == 1
        # Should return much faster than the 5s wait
        assert elapsed < 2.0

    def test_wait_zero_is_non_blocking(self, store):
        start = time.monotonic()
        messages = store.get_messages("test-pipeline", wait=0)
        elapsed = time.monotonic() - start

        assert messages == []
        assert elapsed < 1.0


class TestConsensusMessageTypes:
    """Test that BRC consensus message types work through Redis store."""

    def test_consensus_propose(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body="Implemented auth",
            metadata={"version": 1},
        )
        store.add_message(msg)

        messages = store.get_messages("test-pipeline")
        assert len(messages) == 1
        assert messages[0].message_type == MessageType.CONSENSUS_PROPOSE
        assert messages[0].metadata == {"version": 1}

    def test_consensus_ack(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="reviewer_code",
            to_role="coder",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from reviewer",
        )
        store.add_message(msg)

        messages = store.get_messages("test-pipeline", role="coder")
        assert len(messages) == 1
        assert messages[0].message_type == MessageType.CONSENSUS_ACK

    def test_consensus_nack(self, store):
        msg = Message(
            pipeline_id="test-pipeline",
            from_role="checker",
            to_role="coder",
            message_type=MessageType.CONSENSUS_NACK,
            subject="NACK from checker",
            body="SQL injection found",
        )
        store.add_message(msg)

        messages = store.get_messages("test-pipeline", role="coder")
        assert len(messages) == 1
        assert messages[0].body == "SQL injection found"

    def test_full_consensus_message_flow(self, store):
        """Test the complete BRC message flow through Redis."""
        # Producer proposes
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
            )
        )
        # Reviewer ACKs
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="reviewer_code",
                to_role="coder",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
            )
        )
        # Producer confirms
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
            )
        )

        status = store.get_status("test-pipeline")
        assert status["total"] == 3
        assert status["by_type"][MessageType.CONSENSUS_PROPOSE] == 1
        assert status["by_type"][MessageType.CONSENSUS_ACK] == 1
        assert status["by_type"][MessageType.CONSENSUS_CONFIRMED] == 1


class TestThreadSafety:
    """Test thread-safe operations."""

    def test_concurrent_add_messages(self, store):
        """Multiple threads adding messages concurrently."""
        errors = []

        def add_messages(thread_id: int):
            try:
                for i in range(10):
                    store.add_message(
                        Message(
                            pipeline_id="test-pipeline",
                            from_role=f"agent-{thread_id}",
                            to_role="all",
                            message_type=MessageType.PROGRESS,
                            subject=f"t{thread_id}-msg{i}",
                        )
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_messages, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        messages = store.get_messages("test-pipeline", limit=100)
        assert len(messages) == 40


class TestWaitForTypes:
    """issue #1897: ``wait_for_types`` filters which messages unblock a
    blocking read.  Unwanted types keep the caller blocked on the remaining
    time budget, up to the inner-loop cap."""

    def test_matching_type_returned(self, store):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="done",
            )
        )
        messages = store.get_messages(
            "test-pipeline",
            wait=2,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        assert len(messages) == 1
        assert messages[0].message_type == MessageType.CONSENSUS_CONFIRMED

    def test_non_matching_type_does_not_return(self, store):
        """A PROGRESS message pre-populated in the stream must NOT satisfy a
        wait for CONSENSUS_CONFIRMED — caller should block and time out."""
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="progress",
            )
        )
        start = time.monotonic()
        messages = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        elapsed = time.monotonic() - start
        assert messages == []
        # Must have actually blocked, not returned instantly.
        assert elapsed >= 0.5

    def test_mixed_stream_filters_to_matching_only(self, store):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="p",
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="c",
            )
        )
        messages = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        # Only the CONSENSUS_CONFIRMED row comes back.
        assert len(messages) == 1
        assert messages[0].message_type == MessageType.CONSENSUS_CONFIRMED

    def test_multiple_wait_types_act_as_or_filter(self, store):
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="rr",
            )
        )
        messages = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[
                MessageType.CONSENSUS_CONFIRMED,
                MessageType.CONSENSUS_RE_REVIEW,
            ],
        )
        assert len(messages) == 1
        assert messages[0].message_type == MessageType.CONSENSUS_RE_REVIEW

    def test_inner_loop_cap_prevents_infinite_spin(self, store):
        """RISK (issue #1897): a pathological flood of non-matching types
        must not cause the XREAD BLOCK loop to spin forever.  The inner-loop
        cap is ``_WAIT_FOR_TYPES_MAX_INNER_LOOPS`` (100)."""
        from redis_message_store import RedisMessageStore

        assert RedisMessageStore._WAIT_FOR_TYPES_MAX_INNER_LOOPS == 100

    def test_inner_loop_cap_functional_stress(self, store):
        """Plan TASK-1-2 acceptance (c) + reviewer_code non-blocking
        item: XADD >100 non-matching rows, invoke a blocking
        ``wait_for_types=[CONSENSUS_CONFIRMED]`` read, and assert it
        returns within ``wait + epsilon`` rather than spinning forever.

        A constant assertion (test_inner_loop_cap_prevents_infinite_spin
        above) only proves the attribute exists — it doesn't prove the
        cap is actually consulted at runtime. This test XADD-s 150
        PROGRESS rows (>100) and verifies the read returns within a
        bounded wall-clock budget.
        """
        # Flood the stream with 150 non-matching PROGRESS rows.
        for i in range(150):
            store.add_message(
                Message(
                    pipeline_id="stress-test-pipeline",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject=f"progress {i}",
                )
            )

        # Blocking read with a 2s budget. Must return within 2.5s even
        # though there are 150 rows to churn through — the cap kicks in
        # at 100 and bails with empty. Without the cap, fakeredis
        # wouldn't actually block (its XREAD block=ms behaviour diverges
        # from real Redis), but the loop would still iterate 150 times
        # rapidly — we confirm the return happens in a sane window.
        wait_seconds = 2
        start = time.monotonic()
        messages = store.get_messages(
            "stress-test-pipeline",
            wait=wait_seconds,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        elapsed = time.monotonic() - start

        # No matching row exists — must return empty.
        assert messages == []
        # Must not exceed wait + a generous epsilon (3s). A bug that
        # re-XREADs the same rows on every loop would take much longer.
        assert elapsed < wait_seconds + 1.0, (
            f"Flood-of-150 stress test took {elapsed:.2f}s "
            f"(expected < {wait_seconds + 1}s). "
            "Inner-loop cap may not be functioning — look for "
            "unbounded re-reads of the same stream range."
        )

    def test_wait_zero_with_filter_returns_empty(self, store):
        """A non-blocking read with a type filter returns [] immediately if
        no matching row is present, even when other rows exist."""
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="p",
            )
        )
        messages = store.get_messages(
            "test-pipeline",
            wait=0,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        assert messages == []


class TestRedisFromTipSemantics:
    """``from_tip=True`` snapshots the stream tip to a concrete id once at
    call entry, so XREAD only matches entries added after the call starts.

    The concrete id (rather than Redis's ``$`` sentinel) is what keeps the
    chunked blocking read from dropping a message XADDed between idle
    slices — ``$`` re-resolves to the live tip on every re-issue, a fixed
    id does not. Backs the ``/messages/wait`` endpoint fix for issue #1925.
    """

    def test_resolve_tip_stream_id_returns_concrete_id(self, store):
        """A non-empty stream resolves to its greatest concrete stream id."""
        store.add_message(
            Message(
                pipeline_id="tip-pipeline",
                from_role="coder",
                message_type=MessageType.PROGRESS,
                subject="first",
            )
        )
        tip = store._resolve_tip_stream_id("tip-pipeline")
        assert tip != "$"
        assert "-" in tip  # concrete Redis stream id, e.g. "1700000000000-0"

    def test_resolve_tip_stream_id_empty_stream_is_zero(self, store):
        """An empty/missing stream resolves to ``0-0`` (read everything)."""
        assert store._resolve_tip_stream_id("never-seen-pipeline") == "0-0"

    def test_from_tip_never_passes_dollar_to_xread(self, redis_client, monkeypatch):
        """The from_tip blocking read must issue a CONCRETE start id, not ``$``.

        Pins the BLOCKING-2 fix: ``$`` re-resolves server-side on every
        slice and would skip a message added between idle slices.
        """
        redis_client.xadd(
            _stream_key("tip-pipeline"),
            {
                "id": "x",
                "pipeline_id": "tip-pipeline",
                "from_role": "coder",
                "to_role": "all",
                "message_type": "PROGRESS",
                "subject": "pre",
                "body": "",
                "metadata": "{}",
                "timestamp": "",
                "phase": "",
            },
        )
        captured: list[str] = []

        def capturing_xread(streams, count=None, block=None):
            captured.append(next(iter(streams.values())))
            raise RuntimeError("stop")

        monkeypatch.setattr(redis_client, "xread", capturing_xread)
        store = RedisMessageStore(redis_client)
        with pytest.raises(RuntimeError):
            store.get_messages("tip-pipeline", wait=1, from_tip=True)

        assert captured, "from_tip never issued an XREAD"
        assert captured[0] != "$"
        assert "-" in captured[0]

    def test_pre_existing_match_ignored_with_from_tip(self, store):
        """A matching pre-existing entry must NOT satisfy a from_tip wait."""
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="already seen",
            )
        )

        # Production resolves the tip to a concrete id before XREAD (see
        # _resolve_tip_stream_id), so the read starts from that id
        # exclusively and returns empty — the pre-existing match is never
        # delivered. Regressing _resolve_tip_stream_id to always-"0-0"
        # would surface the pre-existing match and fail this assertion.
        start = time.monotonic()
        messages = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
            from_tip=True,
        )
        elapsed = time.monotonic() - start
        assert messages == []
        # Must not take longer than the wait budget.
        assert elapsed < 2.0, f"from_tip wait over budget: {elapsed:.2f}s"

    def test_explicit_since_id_disables_from_tip(self, store):
        """When both are set, since_id wins — the caller wants the
        cursor-passing path, not the stream tip."""
        anchor = store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="anchor",
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="after",
            )
        )

        messages = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
            since_id=anchor.id,
            from_tip=True,  # should be ignored in favor of since_id
        )
        assert len(messages) == 1
        assert messages[0].subject == "after"

    def test_from_tip_ignored_when_wait_zero(self, store):
        """``wait=0`` + ``from_tip=True`` degrades to non-blocking read
        from 0-0 (start_id = 0-0), not ``$`` — XRANGE doesn't accept ``$``
        and we avoid the footgun."""
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="p",
            )
        )
        # Should not raise; returns the pre-existing message unfiltered.
        messages = store.get_messages(
            "test-pipeline",
            wait=0,
            from_tip=True,
        )
        assert len(messages) == 1


class TestGetLatestId:
    """Tests for ``RedisMessageStore.get_latest_id``."""

    def test_empty_pipeline_returns_none(self, store):
        assert store.get_latest_id("nonexistent-pipeline") is None

    def test_single_message(self, store, sample_message):
        store.add_message(sample_message)
        assert store.get_latest_id("test-pipeline") == sample_message.id

    def test_returns_most_recent(self, store):
        m1 = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="first",
        )
        m2 = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="second",
        )
        store.add_message(m1)
        store.add_message(m2)
        assert store.get_latest_id("test-pipeline") == m2.id

    def test_pipeline_isolation(self, store):
        m1 = Message(
            pipeline_id="pipeline-a",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="a",
        )
        m2 = Message(
            pipeline_id="pipeline-b",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="b",
        )
        store.add_message(m1)
        store.add_message(m2)
        assert store.get_latest_id("pipeline-a") == m1.id
        assert store.get_latest_id("pipeline-b") == m2.id


def _slice_message(
    pipeline_id: str = "test-pipeline",
    message_type: str = MessageType.PROGRESS,
    from_role: str = "coder",
    to_role: str = "all",
    slice_id: str | None = None,
) -> Message:
    """Helper for #2725 redis-store filter tests."""
    metadata: dict[str, object] = {}
    if slice_id is not None:
        metadata["slice_id"] = slice_id
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role,
        message_type=message_type,
        subject="filter-test",
        metadata=metadata,
    )


class TestRedisSingularFromRoleAndSliceCombined:
    """``from_role`` (singular) + ``slice_id`` compose on the Redis path (#2725).

    The redis store applies ``from_role`` and the slice filter on
    *different* code paths — singular ``from_role`` is filtered inline
    in ``get_messages_with_meta`` (redis_message_store.py:385-386 / 411-412),
    while ``slice_id`` is filtered inside ``_passes_filters``. The
    cross-backend integration tests cover the plural ``from_roles=``
    form; this class closes the matrix for singular + slice on the
    redis path so a future reorder of the two filter blocks (or a
    refactor that drops one branch) is caught before production.
    """

    def test_both_match_no_wait(self, store):
        store.add_message(_slice_message(from_role="coder", slice_id="slice-1"))
        msgs = store.get_messages(
            "test-pipeline",
            from_role="coder",
            slice_id="slice-1",
            wait=0,
        )
        assert len(msgs) == 1
        assert msgs[0].from_role == "coder"

    def test_wrong_slice_right_sender_drops(self, store):
        store.add_message(_slice_message(from_role="coder", slice_id="slice-2"))
        msgs = store.get_messages(
            "test-pipeline",
            from_role="coder",
            slice_id="slice-1",
            wait=0,
        )
        assert msgs == []

    def test_right_slice_wrong_sender_drops(self, store):
        store.add_message(_slice_message(from_role="documenter", slice_id="slice-1"))
        msgs = store.get_messages(
            "test-pipeline",
            from_role="coder",
            slice_id="slice-1",
            wait=0,
        )
        assert msgs == []

    def test_null_slice_passthrough_with_singular_from_role(self, store):
        """A pipeline-level message (null ``slice_id``) from the same sender
        still passes the combined filter — the null-passthrough invariant
        composes with the singular sender filter the same way it composes
        with the plural form."""
        store.add_message(_slice_message(from_role="coder", slice_id=None))
        msgs = store.get_messages(
            "test-pipeline",
            from_role="coder",
            slice_id="slice-1",
            wait=0,
        )
        assert len(msgs) == 1
        assert msgs[0].from_role == "coder"

    def test_combined_filter_inside_wait_for_types(self, store):
        """Singular ``from_role`` + ``slice_id`` must compose inside the
        ``wait_for_types`` inner loop (redis_message_store.py:409-413) —
        a wrong-slice or wrong-sender message must NOT unblock the wait.
        """
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "test-pipeline",
                    from_role="coder",
                    slice_id="slice-1",
                    wait=2,
                    wait_for_types=[MessageType.CONSENSUS_PROPOSE],
                    from_tip=True,
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        store.add_message(
            _slice_message(
                message_type=MessageType.CONSENSUS_PROPOSE,
                from_role="coder",
                slice_id="slice-2",
            )
        )
        store.add_message(
            _slice_message(
                message_type=MessageType.CONSENSUS_PROPOSE,
                from_role="documenter",
                slice_id="slice-1",
            )
        )
        store.add_message(
            _slice_message(
                message_type=MessageType.CONSENSUS_PROPOSE,
                from_role="coder",
                slice_id="slice-1",
            )
        )

        t.join(timeout=3)
        assert not t.is_alive()
        assert len(got[0]) == 1
        assert got[0][0].from_role == "coder"
        assert got[0][0].metadata.get("slice_id") == "slice-1"


class TestRedisRestartSemanticsVsPhaseBoundaryWipe:
    """Bounded-durability restart contract for the Redis backend (#3077 slice-6).

    This class pins the two distinct wipe semantics the orchestrator
    relies on and that have repeatedly been conflated in incident
    triage. Both are asserted in this module by design — the explicit
    test_id naming pattern (``test_mid_phase_restart_*`` vs
    ``test_phase_boundary_clear_*``) is the readable distinction so a
    future reader cannot mistake the intentional wipe for the
    accidental loss this slice is hardening against.

    1. **Mid-phase orchestrator restart — transcript MUST survive.**
       The orchestrator process can be replaced (kubelet liveness reset,
       deploy roll, OOM kill) at any moment during a phase. With the
       Redis backend, transcripts and consensus state live in
       process-external Redis streams; re-instantiating
       :class:`RedisMessageStore` against the same Redis MUST observe
       every previously-added message. This is the #3076 invariant —
       silent loss here is the failure class slice-6's fail-loud signal
       names; the Redis path is the answer.

    2. **Phase-boundary wipe — transcript MUST be cleared.**
       ``orchestrator/routes/phases.py:113::_clear_concurrent_state``
       calls ``get_message_store().clear(pipeline_id)`` on every phase
       transition. This is *designed* state reset (the new phase's BRC
       cycle must start clean), not a defect, and the Redis backend's
       ``clear()`` MUST honour it.

    The fail mode worse than losing the transcript on restart is
    quietly relaxing the phase-boundary wipe in pursuit of fixing
    restart loss — the BRC tracker reconstruction would then replay
    stale prior-phase signals into the new phase. Both behaviours are
    pinned together so neither can drift.
    """

    def _make_progress(
        self,
        pipeline_id: str,
        subject: str,
        slice_id: str | None = None,
    ) -> Message:
        metadata: dict[str, object] = {}
        if slice_id is not None:
            metadata["slice_id"] = slice_id
        return Message(
            pipeline_id=pipeline_id,
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject=subject,
            metadata=metadata,
        )

    def test_mid_phase_restart_preserves_transcript_via_shared_redis(
        self, redis_client: fakeredis.FakeRedis
    ) -> None:
        """Mid-phase restart: a NEW ``RedisMessageStore`` instantiated
        against the SAME Redis backend observes every message the
        previous instance added. ``fakeredis.FakeRedis`` is a faithful
        substitute here because the contract under test is the
        store-to-Redis side of the boundary — not Redis-the-process
        durability. Persistence across instances is the moral
        equivalent of "the orchestrator restarted while the phase was
        in flight"."""
        pipeline_id = "pipeline-restart"

        # Pre-restart store: simulate the in-flight phase's first half.
        pre_store = RedisMessageStore(redis_client)
        seeded = [
            pre_store.add_message(self._make_progress(pipeline_id, "early-1", "slice-6")),
            pre_store.add_message(self._make_progress(pipeline_id, "early-2", "slice-6")),
            pre_store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_PROPOSE,
                    subject="proposal v1",
                    metadata={"slice_id": "slice-6", "version": 1},
                )
            ),
        ]
        # Drop the in-process state to mimic the orchestrator process
        # exiting (caches, locks, and any per-instance bookkeeping go).
        del pre_store

        # Post-restart store: NEW instance, SAME Redis. No call to
        # clear() in between — the restart is mid-phase.
        post_store = RedisMessageStore(redis_client)
        recovered = post_store.get_messages(pipeline_id, limit=100)

        assert len(recovered) == len(seeded), (
            "Mid-phase orchestrator restart silently lost messages — "
            "this is the #3076 failure mode the Redis backend is "
            "supposed to prevent. Expected "
            f"{len(seeded)} messages, observed {len(recovered)}."
        )
        # Identity by message id — the Redis path uses the message
        # UUID as the persistent identity (the stream id may differ
        # across re-encoded payloads, but the id field is stable).
        assert [m.id for m in recovered] == [m.id for m in seeded]
        # Spot-check that the BRC-shaped message survived intact —
        # consensus state replay specifically requires
        # CONSENSUS_PROPOSE rows to come back with their metadata.
        proposes = [m for m in recovered if m.message_type == MessageType.CONSENSUS_PROPOSE]
        assert len(proposes) == 1
        assert proposes[0].metadata.get("version") == 1
        assert proposes[0].metadata.get("slice_id") == "slice-6"

    def test_mid_phase_restart_preserves_type_counters(
        self, redis_client: fakeredis.FakeRedis
    ) -> None:
        """The per-type counter hash (``pipeline:{id}:msg_counts``) is
        the other half of the store's durable state — used by
        ``get_status`` to drive health dashboards and BRC progress
        accounting. Restart MUST preserve it. Without this, the
        post-restart store would under-report message counts on a
        phase that was already in flight, which surfaces as bogus
        zero-progress dashboards even though transcripts are intact."""
        pipeline_id = "pipeline-counters"
        pre_store = RedisMessageStore(redis_client)
        for i in range(3):
            pre_store.add_message(self._make_progress(pipeline_id, f"p-{i}"))
        pre_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="proposal",
            )
        )
        pre_status = pre_store.get_status(pipeline_id)
        assert pre_status["total"] == 4

        del pre_store

        post_store = RedisMessageStore(redis_client)
        post_status = post_store.get_status(pipeline_id)
        assert post_status == pre_status, (
            "Restart-semantics: get_status (XLEN + counter hash) must "
            "be byte-identical across instances of the Redis-backed "
            "store; mismatched counters indicate the pre-restart "
            "instance owned local state that didn't make it to Redis."
        )

    def test_mid_phase_restart_preserves_since_id_resolution(
        self, redis_client: fakeredis.FakeRedis
    ) -> None:
        """The since_id cursor protocol must survive a restart.
        Pre-restart, an agent records a cursor; post-restart the new
        store instance must resolve that cursor without bouncing the
        consumer to a full-history replay. The in-memory mapping
        ``_id_to_stream_id`` is per-instance, so this test pins that
        the scan-fallback path inside ``_find_stream_id_by_message_id``
        recovers the mapping from the persistent stream (otherwise
        every restart silently invalidates every live agent cursor)."""
        pipeline_id = "pipeline-cursor"
        pre_store = RedisMessageStore(redis_client)
        anchor = pre_store.add_message(self._make_progress(pipeline_id, "anchor"))
        for i in range(3):
            pre_store.add_message(self._make_progress(pipeline_id, f"after-{i}"))

        del pre_store

        post_store = RedisMessageStore(redis_client)
        # The fresh instance has an empty ``_id_to_stream_id`` cache;
        # the resolver MUST fall back to the persistent stream scan.
        after_anchor = post_store.get_messages(pipeline_id, since_id=anchor.id)
        assert [m.subject for m in after_anchor] == ["after-0", "after-1", "after-2"], (
            "since_id must resolve across a mid-phase restart via the "
            "scan-fallback; if every restart loses cursors, agents "
            "would replay full history and the BRC reconstruction "
            "would treat already-handled signals as fresh events."
        )

    def test_phase_boundary_clear_concurrent_state_still_wipes(
        self,
        redis_client: fakeredis.FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The DESIGNED phase-boundary wipe via ``_clear_concurrent_state``
        (orchestrator/routes/phases.py:113) MUST still drain the Redis
        stream. This is intentional state reset, not the accidental
        mid-phase loss the restart-semantics tests above harden against
        — same module, distinct semantics, distinct test_ids."""
        pipeline_id = "pipeline-phase-wipe"
        store = RedisMessageStore(redis_client)
        for i in range(4):
            store.add_message(self._make_progress(pipeline_id, f"phase-1-msg-{i}"))
        assert store.get_status(pipeline_id)["total"] == 4

        # Make _clear_concurrent_state pick up our Redis store via the
        # singleton accessor it imports. Patch the same name the
        # function resolves at call time so the wipe goes to OUR Redis,
        # not a fresh in-memory MessageStore.
        import message_store as ms

        monkeypatch.setattr(ms, "get_message_store", lambda: store)
        # peer_consensus.remove_peer_consensus_tracker is best-effort
        # inside _clear_concurrent_state — stub it so an unrelated
        # import failure can't be misread as a wipe-semantics failure.
        try:
            import peer_consensus

            monkeypatch.setattr(peer_consensus, "remove_peer_consensus_tracker", lambda _pid: None)
        except ImportError:  # pragma: no cover - module always present in repo
            pass

        from routes.phases import _clear_concurrent_state

        _clear_concurrent_state(pipeline_id)

        # Phase-boundary wipe: stream gone, status reset.
        assert store.get_messages(pipeline_id, limit=100) == [], (
            "_clear_concurrent_state must drain the Redis-backed "
            "transcript at the phase boundary — without this the new "
            "phase's BRC cycle reconstructs from stale prior-phase "
            "signals (the failure mode worse than restart loss)."
        )
        assert store.get_status(pipeline_id)["total"] == 0

    def test_restart_after_phase_boundary_wipe_stays_clean(
        self, redis_client: fakeredis.FakeRedis
    ) -> None:
        """Combined invariant: once ``_clear_concurrent_state`` has wiped
        the stream at a phase boundary, a subsequent orchestrator
        restart MUST NOT resurrect the wiped messages. The wipe is
        through Redis itself (XDEL / DEL on the stream key), so a new
        store instance sees the same empty state — there is no per-
        instance shadow copy of the cleared transcript."""
        pipeline_id = "pipeline-wipe-then-restart"
        pre_store = RedisMessageStore(redis_client)
        for i in range(3):
            pre_store.add_message(self._make_progress(pipeline_id, f"phase-1-{i}"))

        # Phase boundary: explicit wipe through the store's clear() —
        # the same call _clear_concurrent_state makes, exercised
        # directly here so the test does not depend on importing the
        # routes layer.
        cleared = pre_store.clear(pipeline_id)
        assert cleared == 3

        del pre_store

        # Restart: new store instance, same Redis. No messages must
        # surface — the wipe is persistent.
        post_store = RedisMessageStore(redis_client)
        assert post_store.get_messages(pipeline_id, limit=100) == []
        assert post_store.get_status(pipeline_id) == {"total": 0, "by_type": {}}


class TestBlockingChunkCap:
    """Live-canary regression for #2662: XREAD BLOCK vs client socket_timeout.

    The production connection pool (``get_redis_message_store``) sets
    ``socket_timeout=5``. redis-py enforces that timeout on the blocked
    read itself, so a single ``XREAD BLOCK`` longer than the socket
    timeout dies with ``redis.TimeoutError`` before the server can
    answer — on the first deployed pipeline every agent long-poll
    (``wait=25``) errored at the 5 s mark. fakeredis has no sockets, so
    the timeout itself cannot be reproduced at unit tier; these tests
    pin the two halves of the fix instead:

    * no single blocking read ever requests more than ``_MAX_BLOCK_MS``;
    * a ``redis.TimeoutError`` on a blocking slice degrades to an idle
      slice instead of killing the whole wait (non-blocking reads keep
      raising).
    """

    class _BlockCaptured(Exception):
        """Sentinel to stop the store after the first blocking read."""

    def _capture_first_block(self, redis_client, monkeypatch):
        captured: list[int | None] = []

        def capturing_xread(streams, count=None, block=None):
            captured.append(block)
            raise self._BlockCaptured()

        monkeypatch.setattr(redis_client, "xread", capturing_xread)
        return captured

    def test_cap_stays_below_pool_socket_timeout(self):
        import redis_message_store

        # Derive the bound from the *same* constant the pool applies
        # (_SOCKET_TIMEOUT_SEC), not a duplicated literal — lowering the
        # socket timeout then regresses here instead of silently in prod.
        socket_timeout_ms = redis_message_store._SOCKET_TIMEOUT_SEC * 1000
        assert redis_message_store._MAX_BLOCK_MS < socket_timeout_ms

    def test_fast_path_block_is_capped(self, redis_client, monkeypatch):
        import redis_message_store

        monkeypatch.setattr(redis_message_store, "_MAX_BLOCK_MS", 50)
        captured = self._capture_first_block(redis_client, monkeypatch)
        store = RedisMessageStore(redis_client)

        with pytest.raises(self._BlockCaptured):
            store.get_messages("cap-pipeline", wait=10)

        # Pre-fix this was wait * 1000 == 10000 in a single XREAD.
        assert captured == [50]

    def test_wait_for_types_block_is_capped(self, redis_client, monkeypatch):
        import redis_message_store

        monkeypatch.setattr(redis_message_store, "_MAX_BLOCK_MS", 50)
        captured = self._capture_first_block(redis_client, monkeypatch)
        store = RedisMessageStore(redis_client)

        with pytest.raises(self._BlockCaptured):
            store.get_messages(
                "cap-pipeline",
                wait=10,
                wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
            )

        assert captured == [50]

    def test_blocking_timeout_degrades_to_idle_slice(self, redis_client, monkeypatch):
        import redis

        store = RedisMessageStore(redis_client)
        store.add_message(
            Message(
                pipeline_id="timeout-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="survives the flaky slice",
            )
        )

        real_xread = redis_client.xread
        calls = {"n": 0}

        def flaky_xread(streams, count=None, block=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise redis.TimeoutError("Timeout reading from socket")
            return real_xread(streams, count=count, block=block)

        monkeypatch.setattr(redis_client, "xread", flaky_xread)

        # Pre-fix the TimeoutError propagated and the route 500'd; now
        # the first slice is treated as idle and the retry delivers.
        messages = store.get_messages("timeout-pipeline", wait=2)

        assert calls["n"] >= 2
        assert [m.subject for m in messages] == ["survives the flaky slice"]

    def test_nonblocking_timeout_still_raises(self, redis_client, monkeypatch):
        import redis

        def timeout_xrange(*args, **kwargs):
            raise redis.TimeoutError("Timeout reading from socket")

        monkeypatch.setattr(redis_client, "xrange", timeout_xrange)
        store = RedisMessageStore(redis_client)

        # The idle-slice degradation is scoped to blocking reads only —
        # a timeout on a non-blocking read is a real error and must
        # propagate, not silently return [].
        with pytest.raises(redis.TimeoutError):
            store.get_messages("timeout-pipeline", wait=0)
