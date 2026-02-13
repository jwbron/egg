"""
Tests for the event system.
"""

import time
from datetime import datetime

import pytest
from events import (
    Event,
    EventBus,
    EventType,
    emit_event,
    get_event_bus,
)


class TestEvent:
    """Tests for Event dataclass."""

    def test_create_event(self):
        """Test creating an event."""
        event = Event(
            event_type=EventType.PIPELINE_CREATED,
            pipeline_id="issue-123",
        )

        assert event.event_type == EventType.PIPELINE_CREATED
        assert event.pipeline_id == "issue-123"
        assert event.source == "orchestrator"
        assert isinstance(event.timestamp, datetime)

    def test_event_to_dict(self):
        """Test event serialization."""
        event = Event(
            event_type=EventType.AGENT_COMPLETED,
            pipeline_id="issue-456",
            data={"agent_role": "coder", "commit": "abc123"},
        )

        result = event.to_dict()

        assert result["event_type"] == "agent.completed"
        assert result["pipeline_id"] == "issue-456"
        assert result["data"]["agent_role"] == "coder"
        assert "timestamp" in result


class TestEventBus:
    """Tests for EventBus."""

    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus."""
        return EventBus(async_delivery=False)

    def test_subscribe_and_publish(self, event_bus):
        """Test basic subscribe and publish."""
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(EventType.PIPELINE_CREATED, handler)
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-123")

        assert len(received_events) == 1
        assert received_events[0].event_type == EventType.PIPELINE_CREATED
        assert received_events[0].pipeline_id == "issue-123"

    def test_wildcard_subscriber(self, event_bus):
        """Test subscribing to all events."""
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(None, handler)  # Wildcard

        event_bus.emit(EventType.PIPELINE_CREATED, "issue-1")
        event_bus.emit(EventType.AGENT_STARTED, "issue-2")
        event_bus.emit(EventType.DECISION_CREATED, "issue-3")

        assert len(received_events) == 3

    def test_multiple_handlers(self, event_bus):
        """Test multiple handlers for same event."""
        handler1_called = []
        handler2_called = []

        def handler1(event):
            handler1_called.append(event)

        def handler2(event):
            handler2_called.append(event)

        event_bus.subscribe(EventType.PIPELINE_COMPLETED, handler1)
        event_bus.subscribe(EventType.PIPELINE_COMPLETED, handler2)

        event_bus.emit(EventType.PIPELINE_COMPLETED, "issue-123")

        assert len(handler1_called) == 1
        assert len(handler2_called) == 1

    def test_unsubscribe(self, event_bus):
        """Test unsubscribing handlers."""
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(EventType.PIPELINE_FAILED, handler)
        event_bus.emit(EventType.PIPELINE_FAILED, "issue-1")

        assert len(received_events) == 1

        event_bus.unsubscribe(EventType.PIPELINE_FAILED, handler)
        event_bus.emit(EventType.PIPELINE_FAILED, "issue-2")

        assert len(received_events) == 1  # Still 1

    def test_handler_error_isolation(self, event_bus):
        """Test that handler errors don't affect other handlers."""
        received_events = []

        def bad_handler(event):
            raise ValueError("Handler error")

        def good_handler(event):
            received_events.append(event)

        event_bus.subscribe(EventType.AGENT_FAILED, bad_handler)
        event_bus.subscribe(EventType.AGENT_FAILED, good_handler)

        # Should not raise
        event_bus.emit(EventType.AGENT_FAILED, "issue-123")

        # Good handler should still have received the event
        assert len(received_events) == 1

    def test_event_history(self, event_bus):
        """Test event history."""
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-1")
        event_bus.emit(EventType.PIPELINE_STARTED, "issue-1")
        event_bus.emit(EventType.PIPELINE_COMPLETED, "issue-1")

        history = event_bus.get_history()
        assert len(history) == 3

        # Newest first
        assert history[0].event_type == EventType.PIPELINE_COMPLETED

    def test_event_history_filter_by_pipeline(self, event_bus):
        """Test filtering history by pipeline."""
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-1")
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-2")
        event_bus.emit(EventType.PIPELINE_STARTED, "issue-1")

        history = event_bus.get_history(pipeline_id="issue-1")
        assert len(history) == 2

    def test_event_history_filter_by_type(self, event_bus):
        """Test filtering history by event type."""
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-1")
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-2")
        event_bus.emit(EventType.PIPELINE_STARTED, "issue-1")

        history = event_bus.get_history(event_type=EventType.PIPELINE_CREATED)
        assert len(history) == 2

    def test_event_history_limit(self, event_bus):
        """Test history limit."""
        for i in range(10):
            event_bus.emit(EventType.AGENT_STARTED, f"issue-{i}")

        history = event_bus.get_history(limit=5)
        assert len(history) == 5

    def test_max_history_enforced(self):
        """Test that history doesn't exceed max."""
        event_bus = EventBus(max_history=5, async_delivery=False)

        for i in range(10):
            event_bus.emit(EventType.AGENT_COMPLETED, f"issue-{i}")

        history = event_bus.get_history()
        assert len(history) == 5

    def test_emit_with_data(self, event_bus):
        """Test emitting events with data."""
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(EventType.AGENT_COMPLETED, handler)
        event_bus.emit(
            EventType.AGENT_COMPLETED,
            "issue-123",
            data={"agent_role": "coder", "duration": 60.5},
        )

        assert received_events[0].data["agent_role"] == "coder"
        assert received_events[0].data["duration"] == 60.5


class TestAsyncEventBus:
    """Tests for async event delivery."""

    def test_async_delivery(self):
        """Test async event delivery."""
        event_bus = EventBus(async_delivery=True)
        received_events = []

        def handler(event):
            received_events.append(event)

        event_bus.subscribe(EventType.PIPELINE_CREATED, handler)
        event_bus.emit(EventType.PIPELINE_CREATED, "issue-123")

        # Give async worker time to process
        time.sleep(0.1)

        assert len(received_events) == 1

        event_bus.stop()


class TestEmitEventFunction:
    """Tests for the emit_event convenience function."""

    def test_emit_event(self):
        """Test emit_event function."""
        # Reset singleton
        import events
        events._event_bus = None

        received_events = []

        def handler(event):
            received_events.append(event)

        bus = get_event_bus()
        bus.subscribe(EventType.DECISION_RESOLVED, handler)

        event = emit_event(
            EventType.DECISION_RESOLVED,
            "issue-123",
            data={"decision_id": "decision-1"},
        )

        # Wait for async delivery
        time.sleep(0.1)

        assert len(received_events) == 1
        assert event.event_type == EventType.DECISION_RESOLVED

        bus.stop()
