"""Detection module for self-improvement analysis.

This module provides:
- Detection: dataclass for detected issues
- Severity: enum for severity levels
- DetectionEngine: pattern matching engine for log analysis
"""

from .engine import Detection as Detection
from .engine import DetectionEngine as DetectionEngine
from .engine import Severity as Severity

__all__ = [
    "Detection",
    "DetectionEngine",
    "Severity",
]
