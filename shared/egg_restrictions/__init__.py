"""
Shared agent file restriction patterns and checking logic.

This package provides role-based file access patterns and validation
functions used by the gateway and other components to enforce agent
file access boundaries.

The package's public API is re-exported lazily via :pep:`562`
``__getattr__`` because :mod:`egg_restrictions.patterns` depends on
:mod:`egg_contracts.agent_roles`, which in turn depends on the
cycle-free :mod:`egg_restrictions.matchers` (#2356). Eager re-exports
from ``.patterns`` would re-enter ``egg_contracts`` mid-import and
raise ``ImportError: partially initialized module``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .checker import (
        AgentRestrictionResult,
        check_agent_file_access,
        get_agent_pattern,
        validate_agent_push,
    )
    from .matchers import match_pattern
    from .patterns import (
        AGENT_PATTERNS,
        AgentFilePattern,
        AgentRole,
    )

__all__ = [
    "AgentFilePattern",
    "AgentRestrictionResult",
    "AgentRole",
    "AGENT_PATTERNS",
    "check_agent_file_access",
    "get_agent_pattern",
    "match_pattern",
    "validate_agent_push",
]

_CHECKER_NAMES = frozenset(
    {
        "AgentRestrictionResult",
        "check_agent_file_access",
        "get_agent_pattern",
        "validate_agent_push",
    }
)
_PATTERNS_NAMES = frozenset({"AGENT_PATTERNS", "AgentFilePattern", "AgentRole"})
_MATCHERS_NAMES = frozenset({"match_pattern"})


def __getattr__(name: str) -> Any:
    if name in _CHECKER_NAMES:
        from . import checker

        return getattr(checker, name)
    if name in _PATTERNS_NAMES:
        from . import patterns

        return getattr(patterns, name)
    if name in _MATCHERS_NAMES:
        from . import matchers

        return getattr(matchers, name)
    raise AttributeError(f"module 'egg_restrictions' has no attribute {name!r}")
