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
            from_role="reviewer_code",
            to_role="coder",
            message_type=MessageType.CONSENSUS_NACK,
            subject="NACK from reviewer",
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
