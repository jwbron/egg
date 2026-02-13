"""
Tests for SSE streaming module.
"""

import json
import threading
import time
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest
import sse as sse_module
from events import Event, EventBus, EventType
from models import Pipeline, PipelinePhase, PipelineStatus
from sse import (
    SSEClientManager,
    create_sse_stream,
    format_sse_comment,
    format_sse_event,
    get_sse_manager,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton SSE manager before and after each test."""
    original = sse_module._sse_manager
    sse_module._sse_manager = None
    yield
    sse_module._sse_manager = original


def create_test_pipeline(
    pipeline_id: str = "test-123",
    status: PipelineStatus = PipelineStatus.RUNNING,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    """Create a test pipeline."""
    return Pipeline(
        id=pipeline_id,
        issue_number=123,
        repo="test/repo",
        branch="egg/test-feature",
        status=status,
        current_phase=current_phase,
    )


class TestFormatSSEEvent:
    """Tests for format_sse_event helper."""

    def test_basic_event(self):
        """Test formatting a basic SSE event with data only."""
        result = format_sse_event({"key": "value"})
        assert 'data: {"key": "value"}' in result
        assert result.endswith("\n\n")

    def test_event_with_type(self):
        """Test formatting with event type."""
        result = format_sse_event({"status": "running"}, event="update")
        assert "event: update\n" in result
        assert 'data: {"status": "running"}' in result

    def test_event_with_id(self):
        """Test formatting with event ID."""
        result = format_sse_event({"x": 1}, event_id="42")
        assert "id: 42\n" in result

    def test_event_with_retry(self):
        """Test formatting with retry interval."""
        result = format_sse_event({"x": 1}, retry=5000)
        assert "retry: 5000\n" in result

    def test_full_event(self):
        """Test formatting with all fields."""
        result = format_sse_event(
            {"msg": "hello"},
            event="test",
            event_id="7",
            retry=3000,
        )
        assert "id: 7\n" in result
        assert "event: test\n" in result
        assert "retry: 3000\n" in result
        assert 'data: {"msg": "hello"}' in result

    def test_data_is_json_encoded(self):
        """Test that data dict is properly JSON encoded."""
        result = format_sse_event({"nested": {"a": [1, 2]}})
        lines = result.strip().split("\n")
        data_line = [l for l in lines if l.startswith("data: ")][0]
        json_str = data_line[6:]
        parsed = json.loads(json_str)
        assert parsed == {"nested": {"a": [1, 2]}}


class TestFormatSSEComment:
    """Tests for format_sse_comment helper."""

    def test_basic_comment(self):
        """Test formatting a comment."""
        result = format_sse_comment("heartbeat")
        assert result == ": heartbeat\n\n"

    def test_comment_with_timestamp(self):
        """Test comment with timestamp text."""
        result = format_sse_comment("heartbeat 2026-02-13T10:00:00Z")
        assert result.startswith(": heartbeat")
        assert result.endswith("\n\n")


class TestSSEClientManager:
    """Tests for SSEClientManager."""

    def test_add_and_remove_client(self):
        """Test adding and removing a client."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)

        q = manager.add_client("pipeline-1")
        assert isinstance(q, Queue)
        assert manager.get_client_count("pipeline-1") == 1

        manager.remove_client("pipeline-1", q)
        assert manager.get_client_count("pipeline-1") == 0

    def test_multiple_clients_same_pipeline(self):
        """Test multiple clients watching the same pipeline."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)

        q1 = manager.add_client("pipeline-1")
        q2 = manager.add_client("pipeline-1")
        assert manager.get_client_count("pipeline-1") == 2

        manager.remove_client("pipeline-1", q1)
        assert manager.get_client_count("pipeline-1") == 1

        manager.remove_client("pipeline-1", q2)
        assert manager.get_client_count("pipeline-1") == 0

    def test_clients_across_pipelines(self):
        """Test clients watching different pipelines."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)

        q1 = manager.add_client("pipeline-1")
        q2 = manager.add_client("pipeline-2")
        assert manager.get_client_count() == 2
        assert manager.get_client_count("pipeline-1") == 1
        assert manager.get_client_count("pipeline-2") == 1

        manager.remove_client("pipeline-1", q1)
        manager.remove_client("pipeline-2", q2)

    def test_remove_nonexistent_client(self):
        """Test removing a client that doesn't exist doesn't raise."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)
        q = Queue()
        manager.remove_client("nonexistent", q)  # Should not raise

    def test_event_fanout(self):
        """Test that events are fanned out to all clients for a pipeline."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)
        manager.subscribe_to_events()

        q1 = manager.add_client("pipeline-1")
        q2 = manager.add_client("pipeline-1")
        q_other = manager.add_client("pipeline-2")

        event = Event(
            event_type=EventType.PHASE_STARTED,
            pipeline_id="pipeline-1",
            data={"phase": "plan", "status": "running"},
        )
        bus.publish(event)

        # Both clients for pipeline-1 should receive the event
        assert not q1.empty()
        assert not q2.empty()
        # Client for pipeline-2 should NOT receive it
        assert q_other.empty()

        msg_type, payload, is_terminal = q1.get_nowait()
        assert msg_type == "event"
        assert payload["event_type"] == "phase.started"
        assert payload["pipeline_id"] == "pipeline-1"
        assert is_terminal is False

        manager.unsubscribe_from_events()
        manager.remove_client("pipeline-1", q1)
        manager.remove_client("pipeline-1", q2)
        manager.remove_client("pipeline-2", q_other)

    def test_terminal_event_sets_flag(self):
        """Test that terminal events set the is_terminal flag."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)
        manager.subscribe_to_events()

        q = manager.add_client("pipeline-1")

        event = Event(
            event_type=EventType.PIPELINE_COMPLETED,
            pipeline_id="pipeline-1",
            data={"status": "complete"},
        )
        bus.publish(event)

        msg_type, payload, is_terminal = q.get_nowait()
        assert is_terminal is True

        manager.unsubscribe_from_events()
        manager.remove_client("pipeline-1", q)

    def test_subscribe_idempotent(self):
        """Test that subscribing twice doesn't create duplicate handlers."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)
        manager.subscribe_to_events()
        manager.subscribe_to_events()  # Should not add duplicate
        assert manager._subscribed is True
        manager.unsubscribe_from_events()

    def test_unsubscribe(self):
        """Test unsubscribing stops event delivery."""
        bus = EventBus()
        manager = SSEClientManager(event_bus=bus)
        manager.subscribe_to_events()
        manager.unsubscribe_from_events()
        assert manager._subscribed is False

    def test_event_includes_visualization_when_available(self):
        """Test that events include DAG visualization when state store is available."""
        bus = EventBus()
        pipeline = create_test_pipeline()
        tmp_dir = Path("/tmp/test-sse-viz")
        manager = SSEClientManager(event_bus=bus, repo_path=tmp_dir)
        manager.subscribe_to_events()

        q = manager.add_client("test-123")

        with patch("sse.get_state_store") as mock_store, \
             patch("sse.render_pipeline_dag") as mock_dag:
            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance
            mock_dag.return_value = "TEST DAG"

            event = Event(
                event_type=EventType.PHASE_STARTED,
                pipeline_id="test-123",
                data={"phase": "plan", "status": "running"},
            )
            bus.publish(event)

        msg_type, payload, is_terminal = q.get_nowait()
        assert "visualization" in payload
        assert payload["visualization"]["dag"] == "TEST DAG"
        assert payload["status"] == "running"
        assert payload["current_phase"] == "implement"

        manager.unsubscribe_from_events()
        manager.remove_client("test-123", q)


class TestCreateSSEStream:
    """Tests for create_sse_stream generator."""

    def test_already_terminal_pipeline(self):
        """Test that stream ends immediately for terminal pipeline."""
        pipeline = create_test_pipeline(status=PipelineStatus.COMPLETE)
        tmp_dir = Path("/tmp/test-sse-stream")

        with patch("sse.get_sse_manager") as mock_mgr, \
             patch("sse.get_state_store") as mock_store, \
             patch("sse.render_pipeline_dag") as mock_dag, \
             patch("sse.generate_status_report") as mock_report:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance

            mock_dag.return_value = "DAG"
            mock_report.return_value = {"pipeline_id": "test-123", "status": "complete"}

            gen = create_sse_stream("test-123", repo_path=tmp_dir)
            events = list(gen)

            # Should have snapshot + done
            assert len(events) == 2
            assert "event: snapshot" in events[0]
            assert "event: done" in events[1]
            assert "already_terminal" in events[1]

    def test_pipeline_not_found(self):
        """Test error event when pipeline doesn't exist."""
        from state_store import PipelineNotFoundError

        tmp_dir = Path("/tmp/test-sse-notfound")

        with patch("sse.get_sse_manager") as mock_mgr, \
             patch("sse.get_state_store") as mock_store:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            store_instance = MagicMock()
            store_instance.load_pipeline.side_effect = PipelineNotFoundError("test-999")
            mock_store.return_value = store_instance

            gen = create_sse_stream("test-999", repo_path=tmp_dir)
            events = list(gen)

            assert len(events) == 1
            assert "event: error" in events[0]
            assert "not found" in events[0]

    def test_cleanup_on_generator_close(self):
        """Test that client is removed when generator is closed."""
        pipeline = create_test_pipeline()
        tmp_dir = Path("/tmp/test-sse-cleanup")

        with patch("sse.get_sse_manager") as mock_mgr, \
             patch("sse.get_state_store") as mock_store, \
             patch("sse.render_pipeline_dag") as mock_dag, \
             patch("sse.generate_status_report") as mock_report:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance
            mock_dag.return_value = "DAG"
            mock_report.return_value = {"pipeline_id": "test-123", "status": "running"}

            gen = create_sse_stream("test-123", repo_path=tmp_dir)
            next(gen)  # Get snapshot
            gen.close()  # Close generator

            # Verify cleanup
            mock_manager.remove_client.assert_called_once_with("test-123", mock_q)

    def test_snapshot_includes_visualization(self):
        """Test that snapshot event contains DAG visualization."""
        pipeline = create_test_pipeline()
        tmp_dir = Path("/tmp/test-sse-snapshot")

        with patch("sse.get_sse_manager") as mock_mgr, \
             patch("sse.get_state_store") as mock_store, \
             patch("sse.render_pipeline_dag") as mock_dag, \
             patch("sse.generate_status_report") as mock_report:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance
            mock_dag.return_value = "PIPELINE DAG RENDERING"
            mock_report.return_value = {
                "pipeline_id": "test-123",
                "status": "running",
                "current_phase": "implement",
            }

            gen = create_sse_stream("test-123", repo_path=tmp_dir)
            snapshot = next(gen)
            gen.close()

            assert "event: snapshot" in snapshot
            assert "PIPELINE DAG RENDERING" in snapshot
            assert "retry: 5000" in snapshot

    def test_terminal_event_ends_stream(self):
        """Test that receiving a terminal event ends the stream."""
        pipeline = create_test_pipeline()
        tmp_dir = Path("/tmp/test-sse-terminal")

        with patch("sse.get_sse_manager") as mock_mgr, \
             patch("sse.get_state_store") as mock_store, \
             patch("sse.render_pipeline_dag") as mock_dag, \
             patch("sse.generate_status_report") as mock_report:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            store_instance = MagicMock()
            store_instance.load_pipeline.return_value = pipeline
            mock_store.return_value = store_instance
            mock_dag.return_value = "DAG"
            mock_report.return_value = {"pipeline_id": "test-123", "status": "running"}

            # Pre-load terminal event into the queue
            terminal_payload = {
                "event_type": "pipeline.completed",
                "pipeline_id": "test-123",
                "status": "complete",
            }
            mock_q.put(("event", terminal_payload, True))

            gen = create_sse_stream("test-123", repo_path=tmp_dir)
            events = list(gen)

            # snapshot + terminal event + done
            assert len(events) == 3
            assert "event: snapshot" in events[0]
            assert "pipeline.completed" in events[1]
            assert "event: done" in events[2]
            assert "completed" in events[2]

    def test_no_initial_snapshot(self):
        """Test stream without initial snapshot."""
        with patch("sse.get_sse_manager") as mock_mgr:

            mock_q = Queue()
            mock_manager = MagicMock()
            mock_manager.add_client.return_value = mock_q
            mock_mgr.return_value = mock_manager

            # Pre-load terminal event
            terminal_payload = {
                "event_type": "pipeline.completed",
                "pipeline_id": "test-123",
            }
            mock_q.put(("event", terminal_payload, True))

            gen = create_sse_stream(
                "test-123",
                repo_path=Path("/tmp"),
                include_initial=False,
            )
            events = list(gen)

            # Should have event + done (no snapshot)
            assert len(events) == 2
            assert "event: snapshot" not in events[0]
            assert "pipeline.completed" in events[0]


class TestGetSSEManager:
    """Tests for get_sse_manager singleton."""

    def test_returns_manager(self):
        """Test singleton returns a manager instance."""
        with patch("sse.SSEClientManager") as MockManager:
            instance = MagicMock()
            MockManager.return_value = instance
            manager = get_sse_manager()
            assert manager is instance
            instance.subscribe_to_events.assert_called_once()

    def test_singleton_reuse(self):
        """Test that subsequent calls return the same instance."""
        with patch("sse.SSEClientManager") as MockManager:
            instance = MagicMock()
            MockManager.return_value = instance

            m1 = get_sse_manager()
            m2 = get_sse_manager()
            assert m1 is m2
            MockManager.assert_called_once()
