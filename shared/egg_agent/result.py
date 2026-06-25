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
        window_occupancy: Cumulative context-window occupancy for the final
            turn, defined as ``cache_read_input_tokens +
            cache_creation_input_tokens + input_tokens`` from the SDK usage
            block. This is the load-bearing field for the threshold-reseed
            decision (#3200): it measures how much of the real backend window
            the resumed session is consuming. It is NOT the billed/effective
            input — billing excludes cache reads and discounts cache writes,
            so occupancy is typically much larger than the billed input. None
            when the SDK reports no usage (e.g. non-Claude/LiteLLM routes with
            partial or absent usage), in which case callers must bias to a
            safe reseed rather than a lossy resume.
        token_usage: Optional raw component counts (input/cache_read/
            cache_creation/output) preserved for downstream breakout and
            measurement surfaces (#3200 phase 10). The single
            ``window_occupancy`` total is the load-bearing field; this dict is
            purely informational and may be None.
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
    window_occupancy: int | None = None
    token_usage: dict[str, int] | None = None
