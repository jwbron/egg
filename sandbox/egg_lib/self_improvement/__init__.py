"""Self-improvement analysis for egg.

This module handles:
- Collecting logs from GitHub Actions and local container runs
- Analyzing runs for errors, inefficiencies, and behavior issues
- Tracking metrics and improvement velocity
"""

from .collectors.base import LogCollector as LogCollector
from .collectors.base import RunLog as RunLog

__all__ = [
    "LogCollector",
    "RunLog",
]
