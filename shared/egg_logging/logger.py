"""
EggLogger - Structured logging for egg components.

Provides a unified logging interface with JSON output, context propagation,
and GCP Cloud Logging compatibility.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .context import get_current_context
from .formatters import ConsoleFormatter, JsonFormatter


class EggLogger:
    """Structured logger for egg components.

    Provides a logging interface that:
    - Outputs structured JSON for production/file logging
    - Outputs human-readable format for console/development
    - Propagates context (trace_id, task_id, etc.) to all logs
    - Is compatible with GCP Cloud Logging

    Usage:
        from egg_logging import get_logger

        logger = get_logger("github-watcher")
        logger.info("Processing PR", pr_number=123, repository="owner/repo")
    """

    def __init__(
        self,
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ):
        """Initialize the logger.

        Args:
            name: Logger name (typically service name)
            level: Log level (default INFO)
            component: Optional component within the service
        """
        self.name = name
        self.component = component
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level if isinstance(level, int) else getattr(logging, level.upper()))
        self._logger.propagate = False  # Don't propagate to root logger

        # Environment detection
        self._environment = self._detect_environment()

    def _detect_environment(self) -> str:
        """Detect the running environment."""
        # Check for GCP Cloud Run
        if os.environ.get("K_SERVICE"):
            return "gcp"

        # Check for container
        if Path("/.dockerenv").exists() or os.environ.get("EGG_CONTAINER"):
            return "container"

        return "host"

    def _ensure_handlers(self) -> None:
        """Ensure handlers are configured (lazy initialization)."""
        if self._logger.handlers:
            return

        # Console handler for development
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)

        # In GCP, use JSON for console (goes to Cloud Logging)
        if self._environment == "gcp":
            console_handler.setFormatter(
                JsonFormatter(
                    service=self.name,
                    component=self.component,
                    environment=self._environment,
                )
            )
        else:
            console_handler.setFormatter(
                ConsoleFormatter(
                    service=self.name,
                    use_colors=None,  # Auto-detect
                )
            )

        self._logger.addHandler(console_handler)

    def _get_extra(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Get extra fields including context."""
        result: dict[str, Any] = {}

        # Add context from current scope
        ctx = get_current_context()
        if ctx:
            if ctx.trace_id:
                result["trace_id"] = ctx.trace_id
            if ctx.span_id:
                result["span_id"] = ctx.span_id
            if ctx.trace_flags:
                result["trace_flags"] = ctx.trace_flags
            if ctx.task_id:
                result["task_id"] = ctx.task_id
            if ctx.repository:
                result["repository"] = ctx.repository
            if ctx.pr_number:
                result["pr_number"] = ctx.pr_number
            if ctx.extra:
                result.update(ctx.extra)

        # Add extra fields from call
        if extra:
            result.update(extra)

        return result

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        exc_info: Any = None,
        stacklevel: int = 1,
        **kwargs: Any,
    ) -> None:
        """Internal logging method."""
        self._ensure_handlers()

        extra = self._get_extra(kwargs)

        # stacklevel adjusts where Python looks for caller info:
        # +1 for this method, +1 for the public method (info, warning, etc.)
        # Additional levels can be added by callers (e.g., BoundLogger adds +1)
        self._logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            extra=extra,
            stacklevel=stacklevel + 2,
        )

    def debug(self, msg: str, *args: Any, _stacklevel: int = 1, **kwargs: Any) -> None:
        """Log a debug message."""
        self._log(logging.DEBUG, msg, *args, stacklevel=_stacklevel, **kwargs)

    def info(self, msg: str, *args: Any, _stacklevel: int = 1, **kwargs: Any) -> None:
        """Log an info message."""
        self._log(logging.INFO, msg, *args, stacklevel=_stacklevel, **kwargs)

    def warning(self, msg: str, *args: Any, _stacklevel: int = 1, **kwargs: Any) -> None:
        """Log a warning message."""
        self._log(logging.WARNING, msg, *args, stacklevel=_stacklevel, **kwargs)

    def error(
        self, msg: str, *args: Any, exc_info: Any = None, _stacklevel: int = 1, **kwargs: Any
    ) -> None:
        """Log an error message."""
        self._log(logging.ERROR, msg, *args, exc_info=exc_info, stacklevel=_stacklevel, **kwargs)

    def critical(
        self, msg: str, *args: Any, exc_info: Any = None, _stacklevel: int = 1, **kwargs: Any
    ) -> None:
        """Log a critical message."""
        self._log(logging.CRITICAL, msg, *args, exc_info=exc_info, stacklevel=_stacklevel, **kwargs)

    def exception(self, msg: str, *args: Any, _stacklevel: int = 1, **kwargs: Any) -> None:
        """Log an exception (includes stack trace)."""
        self._log(logging.ERROR, msg, *args, exc_info=True, stacklevel=_stacklevel, **kwargs)

    def add_file_handler(
        self,
        log_file: str | Path,
        level: int = logging.DEBUG,
        max_bytes: int = 10 * 1024 * 1024,  # 10 MB
        backup_count: int = 5,
    ) -> None:
        """Add a file handler with JSON formatting.

        Args:
            log_file: Path to the log file
            level: Log level for file handler
            max_bytes: Max file size before rotation
            backup_count: Number of backup files to keep
        """
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(
            JsonFormatter(
                service=self.name,
                component=self.component,
                environment=self._environment,
            )
        )

        self._logger.addHandler(file_handler)

    def with_context(self, **kwargs: Any) -> "BoundLogger":
        """Create a bound logger with additional context.

        Usage:
            bound = logger.with_context(task_id="bd-abc123")
            bound.info("Processing")  # Includes task_id in all logs
        """
        return BoundLogger(self, kwargs)


class BoundLogger:
    """Logger bound to specific context fields.

    All log calls from a BoundLogger include the bound fields.
    """

    def __init__(self, parent: EggLogger, bound_fields: dict[str, Any]):
        self._parent = parent
        self._bound_fields = bound_fields

    def _merge_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Merge bound fields with call-specific kwargs."""
        result = dict(self._bound_fields)
        result.update(kwargs)
        return result

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._parent.debug(msg, *args, _stacklevel=2, **self._merge_kwargs(kwargs))

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._parent.info(msg, *args, _stacklevel=2, **self._merge_kwargs(kwargs))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._parent.warning(msg, *args, _stacklevel=2, **self._merge_kwargs(kwargs))

    def error(self, msg: str, *args: Any, exc_info: Any = None, **kwargs: Any) -> None:
        self._parent.error(
            msg, *args, exc_info=exc_info, _stacklevel=2, **self._merge_kwargs(kwargs)
        )

    def critical(self, msg: str, *args: Any, exc_info: Any = None, **kwargs: Any) -> None:
        self._parent.critical(
            msg, *args, exc_info=exc_info, _stacklevel=2, **self._merge_kwargs(kwargs)
        )

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._parent.exception(msg, *args, _stacklevel=2, **self._merge_kwargs(kwargs))

    def with_context(self, **kwargs: Any) -> "BoundLogger":
        """Create a new bound logger with additional context."""
        merged = dict(self._bound_fields)
        merged.update(kwargs)
        return BoundLogger(self._parent, merged)


# Logger registry for singleton behavior
_loggers: dict[str, EggLogger] = {}


def get_logger(
    name: str,
    level: int | str = logging.INFO,
    component: str | None = None,
) -> EggLogger:
    """Get or create a logger by name.

    This is the primary entry point for getting loggers. Loggers are
    cached by name, so calling get_logger with the same name returns
    the same logger instance.

    Args:
        name: Logger name (typically service name like "github-watcher")
        level: Log level (default INFO)
        component: Optional component within the service

    Returns:
        EggLogger instance
    """
    key = f"{name}:{component or ''}"

    if key not in _loggers:
        _loggers[key] = EggLogger(name, level, component)

    return _loggers[key]


def configure_root_logging(
    level: int | str = logging.WARNING,
    json_format: bool = False,
) -> None:
    """Configure the root logger for third-party libraries.

    This captures logs from libraries like requests, urllib3, etc.
    and formats them consistently.

    Args:
        level: Log level for root logger (default WARNING to reduce noise)
        json_format: Whether to use JSON format (default False)
    """
    root = logging.getLogger()
    root.setLevel(level if isinstance(level, int) else getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Add console handler
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JsonFormatter(service="root"))
    else:
        handler.setFormatter(ConsoleFormatter(service="root"))

    root.addHandler(handler)
