"""Tests for egg_logging formatters."""

import json
import logging

import pytest
from egg_logging import ConsoleFormatter, JsonFormatter


class TestJsonFormatter:
    """Tests for JsonFormatter."""

    @pytest.fixture
    def formatter(self):
        """Create a basic JSON formatter."""
        return JsonFormatter(service="test-service", component="test-component")

    @pytest.fixture
    def log_record(self):
        """Create a basic log record."""
        record = logging.LogRecord(
            name="test-logger",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        return record

    def test_formats_as_valid_json(self, formatter, log_record):
        """Test that output is valid JSON."""
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_includes_timestamp(self, formatter, log_record):
        """Test that timestamp is included in ISO 8601 format."""
        output = formatter.format(log_record)
        parsed = json.loads(output)

        assert "timestamp" in parsed
        assert "T" in parsed["timestamp"]
        assert parsed["timestamp"].endswith("Z")

    def test_maps_severity_levels(self, formatter):
        """Test that Python log levels map to GCP severity."""
        levels = [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]

        for py_level, expected_severity in levels:
            record = logging.LogRecord(
                name="test",
                level=py_level,
                pathname="",
                lineno=0,
                msg="Test",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["severity"] == expected_severity

    def test_includes_message(self, formatter, log_record):
        """Test that message is included."""
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["message"] == "Test message"

    def test_includes_service_and_component(self, formatter, log_record):
        """Test that service and component are included."""
        output = formatter.format(log_record)
        parsed = json.loads(output)

        assert parsed["service"] == "test-service"
        assert parsed["component"] == "test-component"

    def test_includes_trace_context(self, formatter):
        """Test that trace context is included when present."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.trace_id = "abc123"
        record.span_id = "def456"
        record.trace_flags = "01"

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["traceId"] == "abc123"
        assert parsed["spanId"] == "def456"
        assert parsed["traceFlags"] == "01"

    def test_includes_context_fields(self, formatter):
        """Test that context fields are nested under 'context'."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.task_id = "bd-abc123"
        record.repository = "owner/repo"
        record.pr_number = 42

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "context" in parsed
        assert parsed["context"]["task_id"] == "bd-abc123"
        assert parsed["context"]["repository"] == "owner/repo"
        assert parsed["context"]["pr_number"] == 42

    def test_includes_extra_fields(self, formatter):
        """Test that extra fields are included."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.custom_field = "custom_value"

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "extra" in parsed
        assert parsed["extra"]["custom_field"] == "custom_value"

    def test_includes_source_location_for_all_levels(self, formatter):
        """Test that source location is included for all log levels."""
        for level in [
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ]:
            record = logging.LogRecord(
                name="test",
                level=level,
                pathname="/path/to/file.py",
                lineno=42,
                msg="Test",
                args=(),
                exc_info=None,
                func="test_function",
            )

            output = formatter.format(record)
            parsed = json.loads(output)

            assert "sourceLocation" in parsed
            assert parsed["sourceLocation"]["file"] == "/path/to/file.py"
            assert parsed["sourceLocation"]["line"] == 42
            assert parsed["sourceLocation"]["function"] == "test_function"

    def test_includes_exception_info(self, formatter):
        """Test that exception info is included."""
        try:
            raise ValueError("Test error")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="Error occurred",
                args=(),
                exc_info=sys.exc_info(),
            )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "exception" in parsed
        assert "ValueError: Test error" in parsed["exception"]


class TestConsoleFormatter:
    """Tests for ConsoleFormatter."""

    @pytest.fixture
    def formatter(self):
        """Create a console formatter without colors."""
        return ConsoleFormatter(service="test-service", use_colors=False)

    @pytest.fixture
    def log_record(self):
        """Create a basic log record."""
        record = logging.LogRecord(
            name="test-logger",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        return record

    def test_includes_timestamp(self, formatter, log_record):
        """Test that output includes timestamp."""
        output = formatter.format(log_record)
        # Should have YYYY-MM-DD HH:MM:SS format
        assert len(output.split()[0]) == 10  # YYYY-MM-DD
        assert ":" in output.split()[1]  # HH:MM:SS

    def test_includes_level(self, formatter, log_record):
        """Test that output includes log level."""
        output = formatter.format(log_record)
        assert "[INFO" in output

    def test_includes_service(self, formatter, log_record):
        """Test that output includes service name."""
        output = formatter.format(log_record)
        assert "test-service" in output

    def test_includes_message(self, formatter, log_record):
        """Test that output includes the message."""
        output = formatter.format(log_record)
        assert "Test message" in output

    def test_shows_context_when_enabled(self, formatter):
        """Test that context is shown when enabled."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.task_id = "bd-abc"
        record.repository = "owner/repo"
        record.pr_number = 42

        output = formatter.format(record)

        assert "task=bd-abc" in output
        assert "repo=owner/repo" in output
        assert "pr=#42" in output

    def test_hides_context_when_disabled(self):
        """Test that context is hidden when disabled."""
        formatter = ConsoleFormatter(service="test", use_colors=False, show_context=False)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.task_id = "bd-abc"

        output = formatter.format(record)

        assert "task=" not in output

    def test_color_detection_respects_no_color_env(self, monkeypatch):
        """Test that NO_COLOR environment variable disables colors."""
        monkeypatch.setenv("NO_COLOR", "1")
        formatter = ConsoleFormatter(service="test", use_colors=None)
        assert formatter.use_colors is False

    def test_formats_all_levels(self, formatter):
        """Test that all log levels are formatted."""
        levels = [
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ]

        for level in levels:
            record = logging.LogRecord(
                name="test",
                level=level,
                pathname="",
                lineno=0,
                msg="Test",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            assert logging.getLevelName(level) in output

    def test_shows_source_location_when_enabled(self, formatter):
        """Test that source location is shown when enabled (default)."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)

        assert "[/path/to/file.py:42]" in output

    def test_hides_source_location_when_disabled(self):
        """Test that source location is hidden when disabled."""
        formatter = ConsoleFormatter(service="test", use_colors=False, show_source_location=False)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)

        assert "/path/to/file.py" not in output
        # Check that source location format (file:lineno) is not in output
        # Note: We can't just check for ":42" as timestamps may contain that
        assert "[/path/to/file.py:42]" not in output
