"""
Tests for status reporter module.
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from events import Event, EventBus, EventType
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from status_reporter import (
    StatusReporter,
    StatusUpdate,
    create_console_handler,
    create_file_handler,
    get_status_reporter,
    report_pipeline_status,
)


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


class TestStatusUpdate:
    """Tests for StatusUpdate class."""

    def test_create_basic(self):
        """Test creating a basic status update."""
        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.started",
            status="running",
            current_phase="implement",
        )
        assert update.pipeline_id == "test-123"
        assert update.event_type == "phase.started"
        assert update.status == "running"
        assert update.current_phase == "implement"
        assert update.timestamp is not None

    def test_create_with_all_fields(self):
        """Test creating status update with all fields."""
        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.completed",
            status="running",
            current_phase="plan",
            message="Phase completed successfully",
            visualization={"dag": "...", "compact": "..."},
            data={"review_cycles": 1},
        )
        assert update.message == "Phase completed successfully"
        assert "dag" in update.visualization
        assert update.data["review_cycles"] == 1

    def test_to_dict(self):
        """Test converting to dictionary."""
        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.started",
            status="running",
            current_phase="implement",
            message="Test message",
        )
        result = update.to_dict()

        assert result["pipeline_id"] == "test-123"
        assert result["event_type"] == "phase.started"
        assert result["status"] == "running"
        assert result["current_phase"] == "implement"
        assert result["message"] == "Test message"
        assert "timestamp" in result

    def test_to_json(self):
        """Test converting to JSON string."""
        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.started",
            status="running",
            current_phase="implement",
        )
        result = update.to_json()

        # Should be valid JSON
        parsed = json.loads(result)
        assert parsed["pipeline_id"] == "test-123"


class TestStatusReporter:
    """Tests for StatusReporter class."""

    def test_create_reporter(self):
        """Test creating a status reporter."""
        event_bus = EventBus()
        reporter = StatusReporter(event_bus=event_bus)

        assert reporter.event_bus is event_bus
        assert reporter.use_ascii is False

    def test_add_remove_handler(self):
        """Test adding and removing handlers."""
        reporter = StatusReporter(event_bus=EventBus())

        handler = MagicMock()
        reporter.add_handler(handler)
        assert handler in reporter._handlers

        reporter.remove_handler(handler)
        assert handler not in reporter._handlers

    def test_update_pipeline_cache(self):
        """Test updating pipeline cache."""
        reporter = StatusReporter(event_bus=EventBus())
        pipeline = create_test_pipeline()

        reporter.update_pipeline_cache(pipeline)

        cached = reporter._get_cached_pipeline(pipeline.id)
        assert cached is pipeline

    def test_report_status_dispatches_to_handlers(self):
        """Test that report_status dispatches to handlers."""
        reporter = StatusReporter(event_bus=EventBus())
        handler = MagicMock()
        reporter.add_handler(handler)

        pipeline = create_test_pipeline()
        reporter.report_status(pipeline, "test.event", "Test message")

        handler.assert_called_once()
        call_args = handler.call_args[0][0]
        assert isinstance(call_args, StatusUpdate)
        assert call_args.pipeline_id == pipeline.id
        assert call_args.event_type == "test.event"
        assert call_args.message == "Test message"

    def test_report_status_includes_visualization(self):
        """Test that report_status includes visualization data."""
        reporter = StatusReporter(event_bus=EventBus())
        handler = MagicMock()
        reporter.add_handler(handler)

        pipeline = create_test_pipeline()
        reporter.report_status(pipeline)

        call_args = handler.call_args[0][0]
        assert "dag" in call_args.visualization
        assert "compact" in call_args.visualization
        assert "progress" in call_args.visualization

    def test_subscribe_to_event_bus(self):
        """Test subscribing to event bus."""
        event_bus = EventBus()
        reporter = StatusReporter(event_bus=event_bus)

        reporter.subscribe()
        assert reporter._subscribed is True

        # Publishing an event should be handled
        handler = MagicMock()
        reporter.add_handler(handler)

        event = Event(
            event_type=EventType.PHASE_STARTED,
            pipeline_id="test-123",
            data={"phase": "implement", "status": "running"},
        )
        event_bus.publish(event)

        # Handler should have been called
        handler.assert_called_once()

    def test_unsubscribe_from_event_bus(self):
        """Test unsubscribing from event bus."""
        event_bus = EventBus()
        reporter = StatusReporter(event_bus=event_bus)

        reporter.subscribe()
        reporter.unsubscribe()

        assert reporter._subscribed is False

    def test_ascii_mode_propagates(self):
        """Test that ASCII mode is used in visualizations."""
        reporter = StatusReporter(event_bus=EventBus(), use_ascii=True)
        handler = MagicMock()
        reporter.add_handler(handler)

        pipeline = create_test_pipeline()
        reporter.report_status(pipeline)

        call_args = handler.call_args[0][0]
        # ASCII mode should use --> arrows
        assert "-->" in call_args.visualization["compact"]


class TestFileHandler:
    """Tests for file-based status handler."""

    def test_writes_status_file(self):
        """Test that status is written to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            handler = create_file_handler(output_dir)

            update = StatusUpdate(
                pipeline_id="test-123",
                event_type="phase.started",
                status="running",
                current_phase="implement",
            )
            handler(update)

            status_file = output_dir / "test-123-status.json"
            assert status_file.exists()

            content = json.loads(status_file.read_text())
            assert content["pipeline_id"] == "test-123"

    def test_appends_to_history_file(self):
        """Test that history is appended to JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            handler = create_file_handler(output_dir)

            # Send multiple updates
            for i in range(3):
                update = StatusUpdate(
                    pipeline_id="test-123",
                    event_type=f"event.{i}",
                    status="running",
                    current_phase="implement",
                )
                handler(update)

            history_file = output_dir / "test-123-history.jsonl"
            assert history_file.exists()

            lines = history_file.read_text().strip().split("\n")
            assert len(lines) == 3

    def test_creates_output_directory(self):
        """Test that output directory is created if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "nested" / "path"
            handler = create_file_handler(output_dir)

            update = StatusUpdate(
                pipeline_id="test-123",
                event_type="test",
                status="running",
                current_phase="implement",
            )
            handler(update)

            assert output_dir.exists()


class TestConsoleHandler:
    """Tests for console status handler."""

    def test_handler_prints_output(self, capsys):
        """Test that console handler prints status."""
        handler = create_console_handler(show_dag=False)

        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.started",
            status="running",
            current_phase="implement",
            message="Phase started",
            visualization={"compact": "○Refine → ○Plan → [▶Implement] → ○PR"},
        )
        handler(update)

        captured = capsys.readouterr()
        assert "Pipeline: test-123" in captured.out
        assert "Event: phase.started" in captured.out
        assert "Phase started" in captured.out

    def test_handler_shows_dag_when_enabled(self, capsys):
        """Test that DAG is shown when enabled."""
        handler = create_console_handler(show_dag=True)

        update = StatusUpdate(
            pipeline_id="test-123",
            event_type="phase.started",
            status="running",
            current_phase="implement",
            visualization={
                "dag": "Test DAG visualization",
                "compact": "compact view",
            },
        )
        handler(update)

        captured = capsys.readouterr()
        assert "Test DAG visualization" in captured.out


class TestSingletonReporter:
    """Tests for singleton status reporter."""

    def test_get_status_reporter_returns_same_instance(self):
        """Test that get_status_reporter returns singleton."""
        # Reset the singleton first
        import status_reporter
        status_reporter._status_reporter = None

        reporter1 = get_status_reporter()
        reporter2 = get_status_reporter()

        assert reporter1 is reporter2

    def test_report_pipeline_status_uses_singleton(self):
        """Test that report_pipeline_status uses singleton reporter."""
        import status_reporter
        status_reporter._status_reporter = None

        pipeline = create_test_pipeline()
        update = report_pipeline_status(pipeline, "test.event", "Test message")

        assert isinstance(update, StatusUpdate)
        assert update.pipeline_id == pipeline.id
