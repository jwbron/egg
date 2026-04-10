"""Result type for harness agent invocations.

Mirrors :class:`egg_agent.result.AgentResult` with additional harness-specific
fields (e.g. ``compaction_count``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentResult:
    """Result of a harness agent invocation.

    Attributes:
        success: True if the agent completed successfully.
        stdout: Standard output / response text.
        stderr: Error output (if any).
        returncode: Exit code (0 = success).
        error: Human-readable error message if something went wrong.
        metadata: Optional dict with provider-specific info (e.g. model used).
        cost_usd: Total cost in USD.
        num_turns: Number of conversation turns.
        duration_ms: Total duration in milliseconds.
        session_id: Session identifier.
        compaction_count: Number of context compactions that occurred during
            the agent run.
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
    compaction_count: int | None = None
