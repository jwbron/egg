"""Base classes for log collection.

This module defines the abstract base class for log collectors and the
unified RunLog dataclass used across all collectors.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class RunLog:
    """Unified log format for analysis.

    All collectors produce RunLog instances with a consistent structure,
    regardless of whether logs come from GitHub Actions or local containers.

    Attributes:
        run_id: Unique identifier (GHA run ID or container ID)
        source: Where the logs came from ("gha" or "local")
        started_at: When the run started
        completed_at: When the run finished (None if still running)
        status: Final status of the run
        trigger: What triggered the run (e.g., "issue_comment", "pull_request", "exec")
        logs: Raw log content
        metadata: Additional context (workflow name, PR number, task_id, etc.)
    """

    run_id: str
    source: Literal["gha", "local"]
    started_at: datetime
    completed_at: datetime | None
    status: Literal["success", "failure", "cancelled", "running"]
    trigger: str
    logs: str
    metadata: dict[str, str | int | None] = field(default_factory=dict)


class LogCollector(ABC):
    """Abstract base class for log collectors.

    Subclasses must implement the collect() method to fetch logs from
    their specific source (GitHub Actions, local containers, etc.).
    """

    @abstractmethod
    def collect(self, since: datetime) -> list[RunLog]:
        """Collect logs since the given timestamp.

        Args:
            since: Only collect logs from runs that started after this time

        Returns:
            List of RunLog instances representing collected runs
        """
        pass
