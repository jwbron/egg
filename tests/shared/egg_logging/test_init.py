"""Tests for egg_logging module initialization and exports."""

import egg_logging
from egg_logging import (
    BoundLogger,
    ConsoleFormatter,
    ContextScope,
    EggLogger,
    JsonFormatter,
    LogContext,
    configure_root_logging,
    context_from_env,
    get_current_context,
    get_logger,
    get_or_create_context,
    set_current_context,
)


class TestModuleExports:
    """Test that all expected items are exported from the module."""

    def test_exports_get_logger(self):
        """Test that get_logger is exported."""
        assert callable(get_logger)

    def test_exports_context_scope(self):
        """Test that ContextScope is exported."""
        assert ContextScope is not None
        # Should be usable as context manager
        with ContextScope():
            pass

    def test_exports_egg_logger(self):
        """Test that EggLogger is exported."""
        assert EggLogger is not None
        logger = EggLogger("test")
        assert hasattr(logger, "info")

    def test_exports_bound_logger(self):
        """Test that BoundLogger is exported."""
        assert BoundLogger is not None

    def test_exports_log_context(self):
        """Test that LogContext is exported."""
        assert LogContext is not None
        ctx = LogContext()
        assert hasattr(ctx, "trace_id")

    def test_exports_context_functions(self):
        """Test that context functions are exported."""
        assert callable(get_current_context)
        assert callable(set_current_context)
        assert callable(get_or_create_context)
        assert callable(context_from_env)

    def test_exports_formatters(self):
        """Test that formatters are exported."""
        assert JsonFormatter is not None
        assert ConsoleFormatter is not None

    def test_exports_configure_root_logging(self):
        """Test that configure_root_logging is exported."""
        assert callable(configure_root_logging)


class TestModuleVersion:
    """Test module version."""

    def test_has_version(self):
        """Test that module has __version__ attribute."""

        assert hasattr(egg_logging, "__version__")
        assert isinstance(egg_logging.__version__, str)


class TestBasicUsage:
    """Test basic usage patterns from docstring."""

    def test_simple_logging(self, capfd):
        """Test simple logging usage."""
        logger = get_logger("test-service")
        logger.info("Processing PR", pr_number=123)

        captured = capfd.readouterr()
        assert "Processing PR" in captured.err

    def test_context_scope_usage(self, capfd):
        """Test ContextScope usage."""
        logger = get_logger("test-service")

        with ContextScope(task_id="bd-abc123"):
            logger.info("Starting task")

        captured = capfd.readouterr()
        assert "Starting task" in captured.err

    def test_bound_logger_usage(self, capfd):
        """Test bound logger usage."""
        logger = get_logger("test-service")
        bound = logger.with_context(task_id="bd-abc123")

        bound.info("Processing step 1")
        bound.info("Processing step 2")

        captured = capfd.readouterr()
        assert "Processing step 1" in captured.err
        assert "Processing step 2" in captured.err
