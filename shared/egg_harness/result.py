"""Result type for egg harness agent invocations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentResult:
    """Result of an agent invocation via the egg harness.

    Compatible with egg_agent.result.AgentResult for drop-in replacement.

    Attributes:
        success: True if agent completed successfully
        stdout: Standard output / response text
        stderr: Error output (if any)
        returncode: Exit code (0 = success)
        error: Human-readable error message if something went wrong
        metadata: Optional dict with provider-specific info
        cost_usd: Total cost in USD
        num_turns: Number of conversation turns
        duration_ms: Total duration in milliseconds
        session_id: Session identifier
        compaction_count: Number of times context was compacted
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
    compaction_count: int = 0
