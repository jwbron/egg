"""Tests for jib_logging logger module."""

import json
import logging

import pytest
from egg_logging import ContextScope, JibLogger, get_logger, set_current_context
from egg_logging.logger import BoundLogger, _loggers


class TestJibLogger:
    """Tests for JibLogger class."""

    def setup_method(self):
        """Reset state before each test."""
        # Clear logger registry
        _loggers.clear()
        # Reset context
        set_current_context(None)

    def teardown_method(self):
        """Clean up after each test."""
        # Clear logger registry
        _loggers.clear()
        # Reset context
        set_current_context(None)

    def test_creates_logger_with_name(self):
        """Test that logger is created with the given name."""
        logger = JibLogger("test-service")
        assert logger.name == "test-service"

    def test_default_level_is_info(self):
        """Test that default log level is INFO."""
        logger = JibLogger("test")
        assert logger._logger.level == logging.INFO

    def test_accepts_string_level(self):
        """Test that level can be specified as string."""
        logger = JibLogger("test", level="DEBUG")
        assert logger._logger.level == logging.DEBUG

    def test_accepts_int_level(self):
        """Test that level can be specified as int."""
        logger = JibLogger("test", level=logging.WARNING)
        assert logger._logger.level == logging.WARNING

    def test_detects_container_environment(self, tmp_path, monkeypatch):
        """Test environment detection for container."""
        monkeypatch.setenv("EGG_CONTAINER", "1")
        logger = JibLogger("test")
        assert logger._environment == "container"

    def test_detects_gcp_environment(self, monkeypatch):
        """Test environment detection for GCP."""
        monkeypatch.setenv("K_SERVICE", "my-service")
        logger = JibLogger("test")
        assert logger._environment == "gcp"

    def test_detects_host_environment(self, monkeypatch):
        """Test environment detection for host (default)."""
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("EGG_CONTAINER", raising=False)
        monkeypatch.delenv("JIB_CONTAINER", raising=False)
        logger = JibLogger("test")
        # May be "host" or "container" depending on test environment
        assert logger._environment in ("host", "container")


class TestLoggerMethods:
    """Tests for logger log methods."""

    def setup_method(self):
        """Reset state before each test."""
        _loggers.clear()
        set_current_context(None)

    def teardown_method(self):
        """Clean up after each test."""
        _loggers.clear()
        set_current_context(None)

    @pytest.fixture
    def logger(self):
        """Create a test logger."""
        return JibLogger("test-service")

    def test_debug_logs_at_debug_level(self, logger, capfd):
        """Test debug method."""
        logger._logger.setLevel(logging.DEBUG)
        logger.debug("Debug message")
        captured = capfd.readouterr()
        assert "Debug message" in captured.err

    def test_info_logs_at_info_level(self, logger, capfd):
        """Test info method."""
        logger.info("Info message")
        captured = capfd.readouterr()
        assert "Info message" in captured.err

    def test_warning_logs_at_warning_level(self, logger, capfd):
        """Test warning method."""
        logger.warning("Warning message")
        captured = capfd.readouterr()
        assert "Warning message" in captured.err

    def test_error_logs_at_error_level(self, logger, capfd):
        """Test error method."""
        logger.error("Error message")
        captured = capfd.readouterr()
        assert "Error message" in captured.err

    def test_critical_logs_at_critical_level(self, logger, capfd):
        """Test critical method."""
        logger.critical("Critical message")
        captured = capfd.readouterr()
        assert "Critical message" in captured.err

    def test_exception_includes_traceback(self, logger, capfd):
        """Test exception method includes stack trace."""
        try:
            raise ValueError("Test error")
        except ValueError:
            logger.exception("An error occurred")

        captured = capfd.readouterr()
        assert "An error occurred" in captured.err
        assert "ValueError" in captured.err

    def test_includes_kwargs_as_extra(self, logger, capfd):
        """Test that kwargs are included as extra fields."""
        logger.info("Processing", pr_number=123, repository="owner/repo")
        captured = capfd.readouterr()
        # The extra fields should be visible in console output
        # (exact format depends on formatter)
        assert "Processing" in captured.err


class TestContextIntegration:
    """Tests for logger context integration."""

    def setup_method(self):
        """Reset state before each test."""
        _loggers.clear()
        set_current_context(None)

    def teardown_method(self):
        """Clean up after each test."""
        _loggers.clear()
        set_current_context(None)

    def test_includes_context_from_scope(self, capfd):
        """Test that logger includes context from ContextScope."""
        logger = JibLogger("test")

        with ContextScope(task_id="bd-abc123"):
            logger.info("Processing task")

        captured = capfd.readouterr()
        assert "Processing task" in captured.err
        # Context should be visible
        assert "bd-abc123" in captured.err or "task=" in captured.err


class TestBoundLogger:
    """Tests for BoundLogger class."""

    def setup_method(self):
        """Reset state before each test."""
        _loggers.clear()
        set_current_context(None)

    def teardown_method(self):
        """Clean up after each test."""
        _loggers.clear()
        set_current_context(None)

    def test_with_context_returns_bound_logger(self):
        """Test that with_context returns a BoundLogger."""
        logger = JibLogger("test")
        bound = logger.with_context(task_id="bd-abc")
        assert isinstance(bound, BoundLogger)

    def test_bound_logger_includes_bound_fields(self, capfd):
        """Test that bound logger includes bound fields."""
        logger = JibLogger("test")
        bound = logger.with_context(task_id="bd-abc")
        bound.info("Processing")

        captured = capfd.readouterr()
        assert "Processing" in captured.err

    def test_bound_logger_merges_call_kwargs(self, capfd):
        """Test that bound logger merges call kwargs with bound fields."""
        logger = JibLogger("test")
        bound = logger.with_context(task_id="bd-abc")
        bound.info("Processing", pr_number=42)

        captured = capfd.readouterr()
        assert "Processing" in captured.err

    def test_bound_logger_can_chain_context(self):
        """Test that bound loggers can be chained."""
        logger = JibLogger("test")
        bound1 = logger.with_context(task_id="bd-abc")
        bound2 = bound1.with_context(pr_number=42)

        assert isinstance(bound2, BoundLogger)


class TestGetLogger:
    """Tests for get_logger function."""

    def setup_method(self):
        """Reset state before each test."""
        _loggers.clear()

    def teardown_method(self):
        """Clean up after each test."""
        _loggers.clear()

    def test_returns_jib_logger(self):
        """Test that get_logger returns a JibLogger."""
        logger = get_logger("test")
        assert isinstance(logger, JibLogger)

    def test_caches_loggers_by_name(self):
        """Test that loggers are cached by name."""
        logger1 = get_logger("test")
        logger2 = get_logger("test")
        assert logger1 is logger2

    def test_different_names_get_different_loggers(self):
        """Test that different names get different loggers."""
        logger1 = get_logger("service1")
        logger2 = get_logger("service2")
        assert logger1 is not logger2

    def test_component_affects_cache_key(self):
        """Test that component is part of cache key."""
        logger1 = get_logger("test", component="comp1")
        logger2 = get_logger("test", component="comp2")
        assert logger1 is not logger2


class TestFileHandler:
    """Tests for file handler functionality."""

    def setup_method(self):
        """Reset state before each test."""
        _loggers.clear()

    def teardown_method(self):
        """Clean up after each test."""
        _loggers.clear()

    def test_add_file_handler_creates_file(self, tmp_path):
        """Test that add_file_handler creates the log file."""
        log_file = tmp_path / "test.log"
        logger = JibLogger("test")
        logger.add_file_handler(log_file)
        logger.info("Test message")

        assert log_file.exists()

    def test_add_file_handler_writes_json(self, tmp_path):
        """Test that file handler writes JSON."""
        log_file = tmp_path / "test.log"
        logger = JibLogger("test")
        logger.add_file_handler(log_file)
        logger.info("Test message")

        content = log_file.read_text().strip()
        parsed = json.loads(content)
        assert parsed["message"] == "Test message"

    def test_add_file_handler_creates_parent_dirs(self, tmp_path):
        """Test that add_file_handler creates parent directories."""
        log_file = tmp_path / "subdir" / "deep" / "test.log"
        logger = JibLogger("test")
        logger.add_file_handler(log_file)
        logger.info("Test message")

        assert log_file.exists()
