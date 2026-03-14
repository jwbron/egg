"""
Tests for coordinator event types (Phase 1, TASK-1-3).

The coordinator feature requires four new event types:
- COORDINATOR_DECISION
- COORDINATOR_SPAWN
- COORDINATOR_ESCALATION
- COORDINATOR_LOOPBACK
"""

from events import Event, EventBus, EventType


class TestCoordinatorEventTypes:
    """Tests for coordinator event types in the EventType enum."""

    def test_coordinator_decision_event_exists(self):
        """COORDINATOR_DECISION event type must exist."""
        assert hasattr(EventType, "COORDINATOR_DECISION")
        assert "coordinator" in EventType.COORDINATOR_DECISION.value.lower()

    def test_coordinator_spawn_event_exists(self):
        """COORDINATOR_SPAWN event type must exist."""
        assert hasattr(EventType, "COORDINATOR_SPAWN")
        assert "coordinator" in EventType.COORDINATOR_SPAWN.value.lower()

    def test_coordinator_escalation_event_exists(self):
        """COORDINATOR_ESCALATION event type must exist."""
        assert hasattr(EventType, "COORDINATOR_ESCALATION")
        assert "coordinator" in EventType.COORDINATOR_ESCALATION.value.lower()

    def test_coordinator_loopback_event_exists(self):
        """COORDINATOR_LOOPBACK event type must exist."""
        assert hasattr(EventType, "COORDINATOR_LOOPBACK")
        assert "coordinator" in EventType.COORDINATOR_LOOPBACK.value.lower()


class TestCoordinatorEventEmission:
    """Tests for emitting coordinator events through the EventBus."""

    def test_emit_coordinator_decision(self):
        """Can emit a COORDINATOR_DECISION event."""
        bus = EventBus(async_delivery=False)
        received = []
        bus.subscribe(EventType.COORDINATOR_DECISION, received.append)
        bus.emit(
            EventType.COORDINATOR_DECISION,
            "issue-1028",
            data={
                "action": "skip_phase",
                "phase": "refine",
                "reason": "Simple bug fix",
            },
        )
        assert len(received) == 1
        assert received[0].data["action"] == "skip_phase"

    def test_emit_coordinator_spawn(self):
        """Can emit a COORDINATOR_SPAWN event."""
        bus = EventBus(async_delivery=False)
        received = []
        bus.subscribe(EventType.COORDINATOR_SPAWN, received.append)
        bus.emit(
            EventType.COORDINATOR_SPAWN,
            "issue-1028",
            data={
                "agent_role": "coder",
                "task_context": "Fix authentication bug",
            },
        )
        assert len(received) == 1
        assert received[0].data["agent_role"] == "coder"

    def test_emit_coordinator_escalation(self):
        """Can emit a COORDINATOR_ESCALATION event."""
        bus = EventBus(async_delivery=False)
        received = []
        bus.subscribe(EventType.COORDINATOR_ESCALATION, received.append)
        bus.emit(
            EventType.COORDINATOR_ESCALATION,
            "issue-1028",
            data={
                "question": "Which database?",
                "escalation_type": "choice",
            },
        )
        assert len(received) == 1
        assert received[0].data["question"] == "Which database?"

    def test_emit_coordinator_loopback(self):
        """Can emit a COORDINATOR_LOOPBACK event."""
        bus = EventBus(async_delivery=False)
        received = []
        bus.subscribe(EventType.COORDINATOR_LOOPBACK, received.append)
        bus.emit(
            EventType.COORDINATOR_LOOPBACK,
            "issue-1028",
            data={
                "from_phase": "test",
                "to_phase": "implement",
                "reason": "Edge case found",
            },
        )
        assert len(received) == 1
        assert received[0].data["reason"] == "Edge case found"

    def test_coordinator_events_appear_in_history(self):
        """Coordinator events are tracked in event history."""
        bus = EventBus(async_delivery=False)
        bus.emit(EventType.COORDINATOR_SPAWN, "issue-1028")
        bus.emit(EventType.COORDINATOR_DECISION, "issue-1028")

        history = bus.get_history(pipeline_id="issue-1028")
        event_types = [e.event_type for e in history]
        assert EventType.COORDINATOR_SPAWN in event_types
        assert EventType.COORDINATOR_DECISION in event_types

    def test_coordinator_events_serialization(self):
        """Coordinator events serialize correctly."""
        event = Event(
            event_type=EventType.COORDINATOR_DECISION,
            pipeline_id="issue-1028",
            data={"action": "advance"},
        )
        d = event.to_dict()
        assert "coordinator" in d["event_type"]
        assert d["pipeline_id"] == "issue-1028"

    def test_wildcard_handler_receives_coordinator_events(self):
        """Wildcard handler receives coordinator events."""
        bus = EventBus(async_delivery=False)
        received = []
        bus.subscribe(None, received.append)  # wildcard

        bus.emit(EventType.COORDINATOR_SPAWN, "issue-1028")
        bus.emit(EventType.COORDINATOR_DECISION, "issue-1028")
        bus.emit(EventType.COORDINATOR_ESCALATION, "issue-1028")
        bus.emit(EventType.COORDINATOR_LOOPBACK, "issue-1028")

        assert len(received) == 4
