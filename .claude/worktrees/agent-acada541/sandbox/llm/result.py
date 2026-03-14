"""Result type for Claude Code agent invocations."""

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentResult:
    """Result of a Claude Code agent invocation.

    Attributes:
        success: True if agent completed successfully
        stdout: Standard output / response text
        stderr: Error output (if any)
        returncode: Exit code (0 = success)
        error: Human-readable error message if something went wrong
        metadata: Optional dict with provider-specific info (e.g., model used)
    """

    success: bool
    stdout: str
    stderr: str
    returncode: int
    error: str | None = None
    metadata: dict[str, Any] | None = None


# Backward compatibility alias
ClaudeResult = AgentResult
