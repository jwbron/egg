"""Result type for Claude agent invocations."""

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentResult:
    """Result of a Claude agent invocation.

    Attributes:
        success: True if agent completed successfully
        stdout: Standard output / response text
        stderr: Error output (if any)
        returncode: Exit code (0 = success)
        error: Human-readable error message if something went wrong
        metadata: Optional dict with provider-specific info (e.g., model used)
        cost_usd: Total cost in USD (from SDK ResultMessage)
        num_turns: Number of conversation turns
        duration_ms: Total duration in milliseconds
        session_id: Claude session ID
    """

    success: bool
    stdout: str
    stderr: str
    returncode: int
    error: str | None = None
    metadata: dict[str, Any] | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    session_id: str | None = None
