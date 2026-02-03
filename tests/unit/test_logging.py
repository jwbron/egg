"""Tests for shared/egg_logging module."""

import json
import logging
import sys
from unittest.mock import patch

import pytest

from shared.egg_logging import get_logger
from shared.egg_logging.logger import (
    ConsoleFormatter,
    EggLogger,
    JsonFormatter,
    configure_logging,
)


class TestJsonFormatter:
    """Tests for JsonFormatter."""

    def test_format_basic_message(self):
        """Test formatting a basic log message."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        result = formatter.format(record)
        data = json.loads(result)

        assert data["level"] == "INFO"
        assert data["message"] == "Test message"
        assert "timestamp" in data

    def test_format_with_extra_fields(self):
        """Test formatting with extra fields."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.extra_fields = {"user_id": 123, "action": "test"}

        result = formatter.format(record)
        data = json.loads(result)

        assert data["user_id"] == 123
        assert data["action"] == "test"

    def test_format_with_exception(self):
        """Test formatting with exception info."""
        formatter = JsonFormatter()

        try:
            raise ValueError("Test error")
        except ValueError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="Error occurred",
                args=(),
                exc_info=sys.exc_info(),
            )

        result = formatter.format(record)
        data = json.loads(result)

        assert data["level"] == "ERROR"
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestConsoleFormatter:
    """Tests for ConsoleFormatter."""

    def test_format_basic_message(self):
        """Test formatting a basic log message."""
        formatter = ConsoleFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        result = formatter.format(record)

        assert "INFO" in result
        assert "Test message" in result

    def test_format_with_colors(self):
        """Test that colors are applied."""
        formatter = ConsoleFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Error",
            args=(),
            exc_info=None,
        )

        result = formatter.format(record)

        # Should contain ANSI color codes
        assert "\033[31m" in result  # Red for ERROR
        assert "\033[0m" in result  # Reset

    def test_format_with_extra_fields(self):
        """Test formatting with extra fields."""
        formatter = ConsoleFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.extra_fields = {"user_id": 123}

        result = formatter.format(record)

        assert "user_id=123" in result

    def test_color_mapping(self):
        """Test that all log levels have colors."""
        formatter = ConsoleFormatter()

        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            assert level in formatter.COLORS


class TestEggLogger:
    """Tests for EggLogger class."""

    @pytest.fixture
    def logger(self):
        """Create a fresh logger for testing."""
        # Create a unique name to avoid conflicts
        import uuid

        name = f"test-{uuid.uuid4().hex[:8]}"
        return EggLogger(name)

    def test_uses_json_formatter_when_not_tty(self):
        """Test that JSON formatter is used when stderr is not a tty."""
        import uuid

        with patch.object(sys.stderr, "isatty", return_value=False):
            name = f"json-test-{uuid.uuid4().hex[:8]}"
            logger = EggLogger(name)
            handler = logger._logger.handlers[0]
            assert isinstance(handler.formatter, JsonFormatter)

    def test_debug_logging(self, logger):
        """Test debug level logging."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.debug("Debug message", key="value")
            mock_handle.assert_called_once()

    def test_info_logging(self, logger):
        """Test info level logging."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.info("Info message")
            mock_handle.assert_called_once()

    def test_warning_logging(self, logger):
        """Test warning level logging."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.warning("Warning message")
            mock_handle.assert_called_once()

    def test_error_logging(self, logger):
        """Test error level logging."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.error("Error message")
            mock_handle.assert_called_once()

    def test_critical_logging(self, logger):
        """Test critical level logging."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.critical("Critical message")
            mock_handle.assert_called_once()

    def test_exception_logging(self, logger):
        """Test exception logging includes exc_info."""
        with patch.object(logger._logger, "handle") as mock_handle:
            try:
                raise ValueError("Test")
            except ValueError:
                logger.exception("Exception occurred")

            mock_handle.assert_called_once()
            record = mock_handle.call_args[0][0]
            assert record.exc_info is not None

    def test_with_context(self, logger):
        """Test creating logger with context."""
        ctx_logger = logger.with_context(request_id="abc123", user="test")

        assert ctx_logger._context["request_id"] == "abc123"
        assert ctx_logger._context["user"] == "test"

    def test_with_context_inherits_original(self, logger):
        """Test that with_context inherits original context."""
        logger._context = {"original": "value"}
        ctx_logger = logger.with_context(new_key="new_value")

        assert ctx_logger._context["original"] == "value"
        assert ctx_logger._context["new_key"] == "new_value"

    def test_extra_fields_in_log(self, logger):
        """Test that extra fields are included in log record."""
        with patch.object(logger._logger, "handle") as mock_handle:
            logger.info("Message", user_id=123)

            record = mock_handle.call_args[0][0]
            assert record.extra_fields["user_id"] == 123


class TestGetLogger:
    """Tests for get_logger function."""

    def test_returns_egg_logger(self):
        """Test that get_logger returns EggLogger instance."""
        logger = get_logger("test-module")
        assert isinstance(logger, EggLogger)

    def test_caches_loggers(self):
        """Test that loggers are cached."""
        logger1 = get_logger("cached-module")
        logger2 = get_logger("cached-module")
        assert logger1 is logger2

    def test_different_names_different_loggers(self):
        """Test that different names get different loggers."""
        logger1 = get_logger("module-a")
        logger2 = get_logger("module-b")
        assert logger1 is not logger2


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_with_debug_level(self):
        """Test configuring with DEBUG level."""
        configure_logging(level="DEBUG")
        # Should not raise

    def test_configure_with_info_level(self):
        """Test configuring with INFO level."""
        configure_logging(level="INFO")
        # Should not raise

    def test_configure_with_warning_level(self):
        """Test configuring with WARNING level."""
        configure_logging(level="WARNING")
        # Should not raise

    def test_configure_with_invalid_level(self):
        """Test configuring with invalid level defaults to INFO."""
        configure_logging(level="INVALID")
        # Should not raise, defaults to INFO
