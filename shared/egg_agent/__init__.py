"""Shared Claude Agent SDK wrapper for egg.

Provides a unified interface for running Claude agents, used both inside
sandbox containers (in-process via the SDK) and from the orchestrator
(as container commands).
"""

from egg_agent.command import build_agent_command
from egg_agent.result import AgentResult

__all__ = [
    "AgentResult",
    "build_agent_command",
]
