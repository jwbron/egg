"""
Log formatters for egg_logging.

Provides JSON and console formatters compatible with GCP Cloud Logging.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """JSON formatter compatible with GCP Cloud Logging.

    Produces structured JSON logs with fields that map directly to
    GCP Cloud Logging's structured log format.

    Output format:
        {
            "timestamp": "2025-11-28T12:34:56.789Z",
            "severity": "INFO",
            "message": "Human-readable message",
            "service": "github-watcher",
            "component": "pr_checker",
            ...
        }
    """

    # Map Python log levels to GCP severity levels
    SEVERITY_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def __init__(
        self,
        service: str = "egg",
        component: str | None = None,
        environment: str | None = None,
        include_extra: bool = True,
    ):
        """Initialize the JSON formatter.

        Args:
            service: Service name for all logs
            component: Optional component within the service
            environment: Environment name (e.g., "container", "host", "gcp")
            include_extra: Whether to include extra fields from log records
        """
        super().__init__()
        self.service = service
        self.component = component
        self.environment = environment
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON."""
        # Build the base log entry
        log_entry: dict[str, Any] = {
            "timestamp": self._format_timestamp(record),
            "severity": self.SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "service": self.service,
        }

        if self.component:
            log_entry["component"] = self.component

        if self.environment:
            log_entry["environment"] = self.environment

        # Add logger name if different from service
        if record.name and record.name != self.service:
            log_entry["logger"] = record.name

        # Add trace context if present
        if hasattr(record, "trace_id") and record.trace_id:
            log_entry["traceId"] = record.trace_id
        if hasattr(record, "span_id") and record.span_id:
            log_entry["spanId"] = record.span_id
        if hasattr(record, "trace_flags") and record.trace_flags:
            log_entry["traceFlags"] = record.trace_flags

        # Add context fields
        context_fields = {}
        for field in ["task_id", "repository", "pr_number"]:
            if hasattr(record, field) and getattr(record, field):
                context_fields[field] = getattr(record, field)

        if context_fields:
            log_entry["context"] = context_fields

        # Add extra fields from record
        if self.include_extra:
            extra = self._extract_extra(record)
            if extra:
                log_entry["extra"] = extra

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add source location for all logs
        log_entry["sourceLocation"] = {
            "file": record.pathname,
            "line": record.lineno,
            "function": record.funcName,
        }

        return json.dumps(log_entry, default=str, ensure_ascii=False)

    def _format_timestamp(self, record: logging.LogRecord) -> str:
        """Format timestamp in ISO 8601 format with UTC timezone."""
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"

    def _extract_extra(self, record: logging.LogRecord) -> dict[str, Any]:
        """Extract extra fields that were passed to the log call."""
        # Standard LogRecord attributes to exclude
        standard_attrs = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "message",
            "taskName",
            # Our custom context fields
            "trace_id",
            "span_id",
            "trace_flags",
            "task_id",
            "repository",
            "pr_number",
        }

        extra = {}
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                extra[key] = value

        return extra


class ConsoleFormatter(logging.Formatter):
    """Human-readable console formatter for development.

    Produces colored, formatted output suitable for terminal viewing.

    Output format:
        2025-11-28 12:34:56 [INFO] github-watcher: Processing PR #123
    """

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    # Standard LogRecord attributes to exclude from extra fields display
    STANDARD_ATTRS = {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "exc_info",
        "exc_text",
        "thread",
        "threadName",
        "message",
        "taskName",
        # Our custom context fields (rendered inline when show_context is enabled)
        "trace_id",
        "span_id",
        "trace_flags",
        "task_id",
        "repository",
        "pr_number",
        # Internal attrs
        "access_level",
    }

    def __init__(
        self,
        service: str = "egg",
        use_colors: bool | None = None,
        show_context: bool = True,
        show_source_location: bool = True,
        show_extra: bool = True,
    ):
        """Initialize the console formatter.

        Args:
            service: Service name for logs
            use_colors: Whether to use ANSI colors (auto-detected if None)
            show_context: Whether to show context fields
            show_source_location: Whether to show source file and line number
            show_extra: Whether to show extra fields passed to log calls
        """
        super().__init__()
        self.service = service
        self.use_colors = use_colors if use_colors is not None else self._detect_color_support()
        self.show_context = show_context
        self.show_source_location = show_source_location
        self.show_extra = show_extra

    def _detect_color_support(self) -> bool:
        """Detect if the terminal supports colors."""
        # Check if stdout is a TTY
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return False

        # Check for NO_COLOR environment variable
        import os

        return not os.environ.get("NO_COLOR")

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record for console output."""
        # Format timestamp
        dt = datetime.fromtimestamp(record.created)
        timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")

        # Format level with optional color
        level = record.levelname
        if self.use_colors:
            color = self.COLORS.get(level, "")
            level = f"{color}{level:8}{self.RESET}"
        else:
            level = f"{level:8}"

        # Build the message
        parts = [f"{timestamp} [{level}] {self.service}"]

        # Add logger name if it's a sub-logger
        if record.name and record.name != self.service and "." in record.name:
            parts.append(f".{record.name.split('.')[-1]}")

        parts.append(f": {record.getMessage()}")

        # Collect all structured fields inline as key=value pairs
        inline_pairs = []

        if self.show_context:
            if hasattr(record, "task_id") and record.task_id:
                inline_pairs.append(f"task_id={self._format_inline_value(record.task_id)}")
            if hasattr(record, "repository") and record.repository:
                inline_pairs.append(f"repository={self._format_inline_value(record.repository)}")
            if hasattr(record, "pr_number") and record.pr_number:
                inline_pairs.append(f"pr_number={self._format_inline_value(record.pr_number)}")

        if self.show_extra:
            extra = self._extract_extra(record)
            for key, value in extra.items():
                inline_pairs.append(f"{key}={self._format_inline_value(value)}")

        if inline_pairs:
            inline_str = " ".join(inline_pairs)
            if self.use_colors:
                inline_str = f"\033[90m{inline_str}\033[0m"
            parts.append(f" {inline_str}")

        # Add source location if enabled
        if self.show_source_location:
            location = f"{record.pathname}:{record.lineno}"
            if self.use_colors:
                location = f"\033[90m[{location}]\033[0m"
            else:
                location = f"[{location}]"
            parts.append(f" {location}")

        message = "".join(parts)

        # Add exception info if present (remains multi-line)
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        return message

    def _format_inline_value(self, value: Any) -> str:
        """Format a value for inline key=value display.

        Truncates values longer than 80 chars and quotes values containing spaces.
        Newlines and carriage returns are replaced with spaces to keep the output
        on a single line. Embedded double quotes are escaped.
        """
        value_str = str(value) if value is not None else ""
        # Replace newlines and carriage returns with spaces for inline display
        value_str = value_str.replace("\r", " ").replace("\n", " ")
        # Truncate long values
        if len(value_str) > 80:
            value_str = value_str[:77] + "..."
        # Quote values containing spaces
        if " " in value_str:
            value_str = f'"{value_str.replace(chr(34), chr(92) + chr(34))}"'
        return value_str

    def _extract_extra(self, record: logging.LogRecord) -> dict[str, Any]:
        """Extract extra fields that were passed to the log call."""
        extra = {}
        for key, value in record.__dict__.items():
            if key not in self.STANDARD_ATTRS and not key.startswith("_"):
                extra[key] = value
        return extra
