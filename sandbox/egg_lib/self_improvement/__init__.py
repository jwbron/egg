"""Self-improvement analysis for egg.

This module handles:
- Collecting run metadata from GitHub Actions and local container runs
- Generating summary reports for analysis

Following agent-mode design principles, the actual analysis and issue
creation is handled by egg itself, not by a rigid detection pipeline.
The module provides run metadata that egg can use to fetch logs and
reason about problems intelligently.
"""

from .collectors.base import LogCollector as LogCollector
from .collectors.base import RunLog as RunLog

__all__ = [
    "LogCollector",
    "RunLog",
]
