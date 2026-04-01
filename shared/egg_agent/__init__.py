"""Shared Claude Agent SDK wrapper for egg.

Provides a unified interface for running Claude agents, used both inside
sandbox containers (in-process via the SDK) and from the orchestrator
(as container commands).
"""

from egg_agent.command import build_agent_command
from egg_agent.result import AgentResult
from egg_agent.tool_interceptor import check_file_write_permission

__all__ = [
    "AgentResult",
    "build_agent_command",
    "check_file_write_permission",
]
