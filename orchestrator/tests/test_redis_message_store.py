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
    """``from_tip=True`` uses Redis ``$`` so XREAD only matches entries
    added after the call starts.

    Backs the ``/messages/wait`` endpoint fix for issue #1925.
    """

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

        # fakeredis's XREAD with $ is a no-op on streams with data — it
        # returns empty immediately because no "later" entry exists. This
        # is the correct behaviour contract even though real Redis would
        # actually block for the timeout.
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
