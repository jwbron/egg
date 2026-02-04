"""
Claude Code interface for jib container.

This module provides an interface for running Claude Code agents,
supporting both API key and OAuth authentication.

Usage:
    from llm import run_agent, run_interactive, ClaudeConfig

    # Programmatic mode
    result = run_agent(
        prompt="Analyze this code",
        cwd="/path/to/repo",
        timeout=300,
    )

    if result.success:
        print(result.stdout)
    else:
        print(f"Error: {result.error}")

    # Interactive mode - launches Claude CLI
    run_interactive()

Authentication:
    Claude Code supports two authentication methods:
    - API Key: Set ANTHROPIC_API_KEY environment variable
    - OAuth: Set ANTHROPIC_AUTH_METHOD=oauth (uses Claude's built-in OAuth)
"""

from llm.claude.config import ClaudeConfig
from llm.claude.runner import run_agent, run_agent_async
from llm.result import AgentResult, ClaudeResult
from llm.runner import run_interactive

__all__ = [
    "AgentResult",
    "ClaudeConfig",
    "ClaudeResult",
    "run_agent",
    "run_agent_async",
    "run_interactive",
]

# Backward compatibility aliases
LLMConfig = ClaudeConfig
BaseConfig = ClaudeConfig
