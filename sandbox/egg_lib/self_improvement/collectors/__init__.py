"""Log collectors for self-improvement analysis.

This package provides collectors for different log sources:
- GHALogCollector: Collects logs from GitHub Actions runs
- LocalLogCollector: Collects logs from local container runs
"""

from .base import LogCollector as LogCollector
from .base import RunLog as RunLog

__all__ = [
    "LogCollector",
    "RunLog",
]
