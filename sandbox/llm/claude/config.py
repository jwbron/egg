"""
Configuration for the Claude agent runner.

Claude Code supports two authentication methods:
- API Key: Set ANTHROPIC_API_KEY environment variable
- OAuth: Set ANTHROPIC_AUTH_METHOD=oauth (uses Claude's built-in OAuth flow)
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClaudeConfig:
    """Configuration for Claude agent execution.

    Attributes:
        cwd: Working directory for the agent
        timeout: Maximum execution time in seconds (default: 2 hours)
        allowed_tools: List of allowed tool names. Empty = all tools allowed.
            Available: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Task
    """

    cwd: Path | str | None = None
    timeout: int = 7200  # 2 hours

    # Tools: empty list = all tools allowed (with dangerouslySkipPermissions)
    # Available: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Task
    allowed_tools: list[str] = field(default_factory=list)


# Backward compatibility alias
AgentConfig = ClaudeConfig
