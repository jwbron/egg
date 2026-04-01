"""
Shared agent file restriction patterns and checking logic.

This package provides role-based file access patterns and validation
functions used by the gateway and other components to enforce agent
file access boundaries.
"""

from .checker import (
    AgentRestrictionResult,
    check_agent_file_access,
    get_agent_pattern,
    validate_agent_push,
)
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
    "validate_agent_push",
]
