"""
Claude Agent SDK utilities for egg container.

Provides an interface for running Claude via the Agent SDK,
with streaming output by default for visibility into long-running tasks.

Note: This module is only available inside the egg container, since Claude
Agent SDK is only installed there. Do not import this from shared/ or host code.

Usage:
    from llm.claude import run_agent

    result = run_agent(
        prompt="Analyze this code and suggest improvements",
        timeout=300,
        cwd="/path/to/repo",
    )

    if result.success:
        print(result.stdout)
    else:
        print(f"Error: {result.error}")
"""

from llm.claude.config import ClaudeConfig
from llm.claude.runner import run_agent, run_agent_async

# Re-export AgentResult from llm.result
from llm.result import AgentResult, ClaudeResult

__all__ = [
    "AgentResult",
    "ClaudeConfig",
    "ClaudeResult",
    "run_agent",
    "run_agent_async",
]
