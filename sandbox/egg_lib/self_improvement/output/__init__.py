"""Output module for self-improvement analysis.

This module provides:
- IssueCreator: creates GitHub issues for detected problems
- generate_fingerprint: creates unique fingerprints for deduplication
"""

from .issue_creator import IssueCreator as IssueCreator
from .issue_creator import generate_fingerprint as generate_fingerprint

__all__ = [
    "IssueCreator",
    "generate_fingerprint",
]
