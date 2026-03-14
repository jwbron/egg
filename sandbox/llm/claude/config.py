"""
Configuration for the Claude agent runner.

Claude Code supports two authentication methods:
- API Key: Set ANTHROPIC_API_KEY environment variable
- OAuth: Set ANTHROPIC_AUTH_METHOD=oauth (uses Claude's built-in OAuth flow)
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClaudeConfig:
    """Configuration for Claude agent execution.

    Attributes:
        cwd: Working directory for the agent
        timeout: Maximum execution time in seconds (default: 2 hours)
    """

    cwd: Path | str | None = None
    timeout: int = 7200  # 2 hours


# Backward compatibility alias
AgentConfig = ClaudeConfig
