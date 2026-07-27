"""
Configuration for the Claude agent runner.

Claude Code supports two authentication methods:
- API Key: Set ANTHROPIC_API_KEY environment variable
- OAuth: Set ANTHROPIC_AUTH_METHOD=oauth (uses Claude's built-in OAuth flow)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClaudeConfig:
    """Configuration for Claude agent execution.

    Attributes:
        cwd: Working directory for the agent
        timeout: Maximum execution time in seconds (default: 2 hours)
    """

    cwd: Path | str | None = None
    timeout: int = field(
        default_factory=lambda: int(os.environ.get("EGG_AGENT_TIMEOUT_SECONDS", "7200"))
    )


# Backward compatibility alias
AgentConfig = ClaudeConfig
