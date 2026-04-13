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
        """Test that context fields are rendered inline as key=value pairs."""
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

        # Context fields should appear inline with full key names
        assert "task_id=bd-abc" in output
        assert "repository=owner/repo" in output
        assert "pr_number=42" in output
        # Should be on the same line as the message (no newline before context)
        assert "Test" in output.split("\n")[0]
        assert "task_id=bd-abc" in output.split("\n")[0]

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

        assert "task_id=" not in output

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

    def test_extra_fields_rendered_inline(self, formatter):
        """Test that extra fields appear inline on the same line as the message."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Processing request",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"
        record.total_phases = 2

        output = formatter.format(record)

        # Extra fields should be on the same line as the message
        first_line = output.split("\n")[0]
        assert "Processing request" in first_line
        assert "pipeline_id=issue-1702" in first_line
        assert "total_phases=2" in first_line

    def test_extra_fields_no_multiline(self, formatter):
        """Test that extra fields do NOT produce multi-line output."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"
        record.agent_role = "coder"
        record.phase = "implement"

        output = formatter.format(record)

        # Output should be a single line (no extras on separate lines)
        assert "\n" not in output

    def test_context_and_extra_combined_inline(self, formatter):
        """Test that both context fields and extra fields appear inline together."""
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
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        first_line = output.split("\n")[0]
        assert "task_id=bd-abc" in first_line
        assert "pipeline_id=issue-1702" in first_line

    def test_value_truncation_over_80_chars(self, formatter):
        """Test that values longer than 80 chars are truncated with '...'."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        # Create a value that's exactly 81 chars
        long_value = "a" * 81
        record.long_field = long_value

        output = formatter.format(record)

        # Should be truncated to 77 chars + "..."
        assert "long_field=" + "a" * 77 + "..." in output
        # The full value should NOT appear
        assert long_value not in output

    def test_value_exactly_80_chars_not_truncated(self, formatter):
        """Test that values of exactly 80 chars are NOT truncated."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        exact_value = "b" * 80
        record.exact_field = exact_value

        output = formatter.format(record)

        assert f"exact_field={exact_value}" in output
        assert "..." not in output.split("exact_field=")[1].split(" ")[0]

    def test_value_with_spaces_quoted(self, formatter):
        """Test that values containing spaces are double-quoted."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.description = "hello world"

        output = formatter.format(record)

        assert 'description="hello world"' in output

    def test_value_without_spaces_not_quoted(self, formatter):
        """Test that values without spaces are NOT quoted."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        assert "pipeline_id=issue-1702" in output
        assert '"issue-1702"' not in output

    def test_value_with_newlines_replaced(self, formatter):
        """Test that newlines in values are replaced with spaces."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.multi_line = "line1\nline2\nline3"

        output = formatter.format(record)

        # Newlines replaced with spaces, then value gets quoted due to spaces
        assert 'multi_line="line1 line2 line3"' in output
        # The output itself should be single-line (no embedded newlines from this value)
        assert "\n" not in output

    def test_none_value_rendered_as_empty(self, formatter):
        """Test that None values are rendered as empty strings."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.empty_field = None

        output = formatter.format(record)

        assert "empty_field=" in output

    def test_show_extra_false_hides_extra_fields(self):
        """Test that show_extra=False suppresses extra field inline display."""
        formatter = ConsoleFormatter(service="test", use_colors=False, show_extra=False)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        assert "pipeline_id" not in output

    def test_show_extra_false_still_shows_context(self):
        """Test that show_extra=False does not suppress context fields."""
        formatter = ConsoleFormatter(
            service="test", use_colors=False, show_extra=False, show_context=True
        )

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

        assert "task_id=bd-abc" in output

    def test_exception_info_remains_multiline(self, formatter):
        """Test that exception tracebacks are still rendered on separate lines."""
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
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        # The output should have multiple lines (message + traceback)
        assert "\n" in output
        # pipeline_id should be on the first line
        assert "pipeline_id=issue-1702" in output.split("\n")[0]
        # Traceback should be on subsequent lines
        assert "ValueError: Test error" in output

    def test_inline_fields_before_source_location(self, formatter):
        """Test that inline fields appear before the source location bracket."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        # Find positions: inline fields should come before source location
        pipeline_pos = output.index("pipeline_id=issue-1702")
        location_pos = output.index("[/path/to/file.py:42]")
        assert pipeline_pos < location_pos

    def test_colored_inline_fields(self):
        """Test that inline fields get grey color when colors are enabled."""
        formatter = ConsoleFormatter(service="test", use_colors=True)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.pipeline_id = "issue-1702"

        output = formatter.format(record)

        # Inline fields should be wrapped in grey ANSI codes
        assert "\033[90mpipeline_id=issue-1702\033[0m" in output

    def test_truncation_with_spaces_produces_quoted_truncated_value(self, formatter):
        """Test truncation + quoting interaction: truncated values with spaces get quoted."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        # A long value with spaces that will be truncated
        record.desc = "word " * 20  # 100 chars, will be truncated

        output = formatter.format(record)

        # After truncation (77 chars + "..."), the result has spaces so gets quoted
        desc_part = (
            output.split("desc=")[1].split(" [")[0] if "[" in output else output.split("desc=")[1]
        )
        # The truncated value should start with a quote
        assert desc_part.startswith('"')

    def test_newline_replacement_then_truncation(self, formatter):
        """Test that newlines are replaced before truncation is applied."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        # Value with newlines that exceeds 80 chars after replacement
        record.big = "line\n" * 25  # 125 chars after newline replacement

        output = formatter.format(record)

        # No literal newlines from this field in the output
        first_line = output.split("\n")[0]
        assert "big=" in first_line
        # Should be truncated
        assert "..." in first_line

    def test_integer_value_rendered_without_quotes(self, formatter):
        """Test that integer values are rendered without quotes."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.count = 42

        output = formatter.format(record)

        assert "count=42" in output
        assert 'count="42"' not in output

    def test_list_value_rendered_inline(self, formatter):
        """Test that list values are rendered inline (may be quoted if spaces present)."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.phases = ["implement", "review"]

        output = formatter.format(record)

        # List str repr has spaces, so should be quoted
        assert "phases=" in output
        # Should still be on one line
        assert "\n" not in output

    def test_no_inline_fields_when_none_present(self, formatter):
        """Test that output is clean when no context or extra fields are present."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)

        # Should just be: timestamp [LEVEL] service: message [location]
        # No double spaces from empty inline section
        assert "Test message [" in output or "Test message  " not in output
