"""Self-improvement analysis for egg.

This module handles:
- Collecting logs from GitHub Actions and local container runs
- Analyzing runs for errors, inefficiencies, and behavior issues
- Detecting patterns and creating issues for problems
- Tracking metrics and improvement velocity
"""

from .collectors.base import LogCollector as LogCollector
from .collectors.base import RunLog as RunLog
from .detection.engine import Detection as Detection
from .detection.engine import DetectionEngine as DetectionEngine
from .detection.engine import Severity as Severity
from .output.issue_creator import IssueCreator as IssueCreator

__all__ = [
    "LogCollector",
    "RunLog",
    "Detection",
    "DetectionEngine",
    "Severity",
    "IssueCreator",
]
