"""Local container log collector.

This module collects logs from local egg container runs using the
existing container_logging infrastructure.
"""

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .base import LogCollector, RunLog

# Type alias for status values
StatusType = Literal["success", "failure", "cancelled", "running"]

# Default logs directory (lazy-loaded to avoid import issues in GHA)
_default_logs_dir: Path | None = None


def _get_default_logs_dir() -> Path:
    """Get the default container logs directory.

    Lazily imports from container_logging to avoid issues when
    running in environments where the module may have side effects.
    """
    global _default_logs_dir
    if _default_logs_dir is None:
        try:
            from ...container_logging import CONTAINER_LOGS_DIR

            _default_logs_dir = CONTAINER_LOGS_DIR
        except ImportError:
            # Fall back to default path if container_logging unavailable
            _default_logs_dir = Path.home() / ".cache" / "egg" / "container-logs"
    return _default_logs_dir


class LocalLogCollector(LogCollector):
    """Collects logs from local container runs.

    Uses the log-index.json maintained by container_logging.py to find
    recent runs and their associated log files.
    """

    def __init__(self, logs_dir: Path | None = None) -> None:
        """Initialize the collector.

        Args:
            logs_dir: Directory containing container logs.
                     Defaults to ~/.cache/egg/container-logs/
        """
        self.logs_dir = logs_dir or _get_default_logs_dir()
        self.index_file = self.logs_dir / "log-index.json"

    def collect(self, since: datetime) -> list[RunLog]:
        """Collect logs from local container runs since the given time.

        Args:
            since: Only collect logs from runs that started after this time

        Returns:
            List of RunLog instances for matching runs
        """
        if not self.index_file.exists():
            return []

        # Ensure since has timezone info
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

        index = self._load_index()
        entries = index.get("entries", [])

        result = []
        for entry in entries:
            run_log = self._process_entry(entry, since)
            if run_log:
                result.append(run_log)

        return result

    def _load_index(self) -> dict[str, list[dict[str, str | None]]]:
        """Load the log index file.

        Returns:
            Parsed index dictionary, or empty dict if loading fails
        """
        try:
            content = self.index_file.read_text()
            index: dict[str, list[dict[str, str | None]]] = json.loads(content)
            return index
        except (OSError, json.JSONDecodeError):
            return {}

    def _process_entry(
        self,
        entry: dict[str, str | None],
        since: datetime,
    ) -> RunLog | None:
        """Process a single log index entry.

        Args:
            entry: Entry from the log index
            since: Only include entries after this time

        Returns:
            RunLog instance for this entry, or None if it should be skipped
        """
        timestamp_str = entry.get("timestamp")
        if not timestamp_str:
            return None

        # Parse the timestamp
        try:
            # Handle ISO format timestamps
            timestamp = datetime.fromisoformat(timestamp_str)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
        except ValueError:
            return None

        # Skip entries before since
        if timestamp <= since:
            return None

        container_id = entry.get("container_id")
        if not container_id:
            return None

        # Load log file content
        log_file_path = entry.get("log_file")
        logs = ""
        if log_file_path:
            log_path = Path(log_file_path)
            # Resolve relative paths against logs_dir
            if not log_path.is_absolute():
                log_path = self.logs_dir / log_path
            # Resolve to canonical path and validate it's within logs_dir
            # to prevent path traversal attacks via malicious index entries
            log_path = log_path.resolve()
            logs_dir_resolved = self.logs_dir.resolve()
            if log_path.is_relative_to(logs_dir_resolved) and log_path.exists():
                with contextlib.suppress(OSError):
                    logs = log_path.read_text(errors="replace")

        # Determine status from log content
        status = self._infer_status(logs)

        return RunLog(
            run_id=container_id,
            source="local",
            started_at=timestamp,
            completed_at=None,  # Local logs don't track completion time
            status=status,
            trigger="exec",  # Local runs are from egg --exec
            logs=logs,
            metadata={
                "task_id": entry.get("task_id"),
                "thread_ts": entry.get("thread_ts"),
                "log_file": log_file_path,
            },
        )

    def _infer_status(self, logs: str) -> StatusType:
        """Infer run status from log content.

        Uses line-start patterns to avoid false positives from phrases like
        "fixed the error" or "tests that previously failed".

        Args:
            logs: Log file content

        Returns:
            Status literal ("success" or "failure")
        """
        import re

        # Patterns that indicate success (check first, more specific)
        success_patterns = [
            "completed successfully",
            "egg finished successfully",
            "all tests passed",
        ]

        # Patterns that indicate failure - must be at line start to avoid
        # false positives from "fixed the error", "tests that failed now pass", etc.
        error_line_patterns = [
            r"^Error:",
            r"^ERROR[:\s]",
            r"^FAILED[:\s]",
            r"^FATAL[:\s]",
            r"^Exception:",
            r"^Traceback \(most recent call last\)",
        ]

        # Check for success patterns first (they're more specific)
        for pattern in success_patterns:
            if pattern in logs:
                return "success"

        # Check for error patterns at line starts
        for pattern in error_line_patterns:
            if re.search(pattern, logs, re.MULTILINE):
                return "failure"

        # Default to success if no clear indicators
        return "success"
