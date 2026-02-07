"""Local container log collector.

This module collects logs from local egg container runs using the
existing container_logging infrastructure.
"""

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ...container_logging import CONTAINER_LOGS_DIR
from .base import LogCollector, RunLog

# Type alias for status values
StatusType = Literal["success", "failure", "cancelled", "running"]


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
        self.logs_dir = logs_dir or CONTAINER_LOGS_DIR
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
            if log_path.exists():
                with contextlib.suppress(OSError):
                    logs = log_path.read_text(errors="replace")

        # Determine status from log content
        status = self._infer_status(logs)

        return RunLog(
            run_id=container_id,
            source="local",
            started_at=timestamp,
            completed_at=timestamp,  # Local logs don't track completion separately
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

        Args:
            logs: Log file content

        Returns:
            Status literal ("success" or "failure")
        """
        # Look for common error patterns
        error_patterns = [
            "error:",
            "Error:",
            "ERROR",
            "failed",
            "Failed",
            "FAILED",
            "exception",
            "Exception",
            "Traceback",
        ]

        success_patterns = [
            "completed successfully",
            "egg finished successfully",
            "success",
        ]

        # Check for success patterns first (they're more specific)
        for pattern in success_patterns:
            if pattern in logs:
                return "success"

        # Then check for error patterns
        for pattern in error_patterns:
            if pattern in logs:
                return "failure"

        # Default to success if no clear indicators
        return "success"
