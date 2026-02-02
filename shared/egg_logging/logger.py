"""Structured logging for egg.

Provides JSON-formatted logging with context propagation.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_loggers: dict[str, "EggLogger"] = {}


class JsonFormatter(logging.Formatter):
    """JSON log formatter for production use."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        time_str = datetime.now().strftime("%H:%M:%S")
        msg = f"{color}{time_str} [{record.levelname:8}]{self.RESET} {record.getMessage()}"

        # Add extra fields
        if hasattr(record, "extra_fields") and record.extra_fields:
            extras = " ".join(f"{k}={v}" for k, v in record.extra_fields.items())
            msg += f" | {extras}"

        return msg


class EggLogger:
    """Structured logger with extra field support."""

    def __init__(self, name: str, level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._context: dict[str, Any] = {}

        # Only add handler if none exist
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            # Use JSON in production, console in development
            if sys.stderr.isatty():
                handler.setFormatter(ConsoleFormatter())
            else:
                handler.setFormatter(JsonFormatter())
            self._logger.addHandler(handler)

    def _log(
        self, level: int, msg: str, *args: Any, exc_info: bool = False, **kwargs: Any
    ) -> None:
        """Internal log method with extra field support."""
        # Merge context with kwargs
        extra_fields = {**self._context, **kwargs}
        record = self._logger.makeRecord(
            self._logger.name,
            level,
            "",
            0,
            msg,
            args,
            exc_info=exc_info if exc_info else None,
        )
        record.extra_fields = extra_fields  # type: ignore
        self._logger.handle(record)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at DEBUG level."""
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at INFO level."""
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at WARNING level."""
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, exc_info: bool = False, **kwargs: Any) -> None:
        """Log at ERROR level."""
        self._log(logging.ERROR, msg, *args, exc_info=exc_info, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR level with exception info."""
        self._log(logging.ERROR, msg, *args, exc_info=True, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at CRITICAL level."""
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def with_context(self, **kwargs: Any) -> "EggLogger":
        """Create a new logger with additional context fields.

        Example:
            log = get_logger("gateway")
            request_log = log.with_context(request_id="abc123")
            request_log.info("Processing request")  # Includes request_id
        """
        new_logger = EggLogger.__new__(EggLogger)
        new_logger._logger = self._logger
        new_logger._context = {**self._context, **kwargs}
        return new_logger


def get_logger(name: str) -> EggLogger:
    """Get or create a logger by name.

    Args:
        name: Logger name (usually module or component name)

    Returns:
        EggLogger instance
    """
    if name not in _loggers:
        _loggers[name] = EggLogger(f"egg.{name}")
    return _loggers[name]


def configure_logging(level: str = "INFO", format: str = "auto") -> None:
    """Configure logging globally.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format: Log format ("json", "console", or "auto")
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger("egg").setLevel(log_level)

    # Configure root logger for third-party libraries
    logging.basicConfig(level=log_level)
