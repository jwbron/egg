"""
Git utilities for egg.

Provides common git operations that can be reused across processors and tasks.
"""

from .cross_process_lock import bare_repo_lock, lock_path_for_repo
from .default_branch import get_default_branch

__all__ = [
    "bare_repo_lock",
    "get_default_branch",
    "lock_path_for_repo",
]
