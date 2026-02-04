"""
Git utilities for egg.

Provides common git operations that can be reused across processors and tasks.
"""

from .default_branch import get_default_branch

__all__ = [
    "get_default_branch",
]
