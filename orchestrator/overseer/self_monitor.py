"""Overseer self-monitoring for tracking its own health metrics.

Tracks poll cycle timing, message volume, and LLM call costs to
ensure the overseer itself is operating within acceptable bounds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _LLMCallRecord:
    """Record of a single LLM call."""

    model: str
    tokens: int
    cost: float
    timestamp: float


class OverseerSelfMonitor:
    """Tracks the overseer's own health metrics.

    Args:
        max_poll_delay_seconds: Maximum acceptable poll cycle duration.
        max_messages_per_cycle: Maximum messages to send in a single cycle.
        max_llm_cost_per_hour: Maximum LLM cost (USD) per hour.
    """

    def __init__(
        self,
        max_poll_delay_seconds: float = 60.0,
        max_messages_per_cycle: int = 10,
        max_llm_cost_per_hour: float = 5.0,
    ) -> None:
        self.max_poll_delay_seconds = max_poll_delay_seconds
        self.max_messages_per_cycle = max_messages_per_cycle
        self.max_llm_cost_per_hour = max_llm_cost_per_hour

        # Metrics storage
        self._poll_durations: list[float] = []
        self._messages_this_cycle: int = 0
        self._total_messages: int = 0
        self._llm_calls: list[_LLMCallRecord] = []
        self._cycle_count: int = 0

    # -----------------------------------------------------------------
    # Recording methods
    # -----------------------------------------------------------------

    def record_poll_cycle(self, duration_seconds: float) -> None:
        """Record the duration of a completed poll cycle.

        Args:
            duration_seconds: How long the cycle took.
        """
        self._poll_durations.append(duration_seconds)
        self._cycle_count += 1
        # Reset per-cycle message counter
        self._messages_this_cycle = 0

    def record_message_sent(self) -> None:
        """Record that a message was sent during the current cycle."""
        self._messages_this_cycle += 1
        self._total_messages += 1

    def record_llm_call(self, model: str, tokens: int, cost: float) -> None:
        """Record an LLM call with its cost.

        Args:
            model: The model used (e.g. ``"haiku"``, ``"sonnet"``).
            tokens: Total tokens consumed.
            cost: Cost in USD.
        """
        self._llm_calls.append(
            _LLMCallRecord(
                model=model,
                tokens=tokens,
                cost=cost,
                timestamp=time.time(),
            )
        )

    # -----------------------------------------------------------------
    # Health checks
    # -----------------------------------------------------------------

    def check_health(self) -> dict:
        """Evaluate the overseer's own health.

        Returns:
            A dict with keys:
                healthy: bool -- True if all metrics are within limits
                concerns: list[str] -- descriptions of any issues
                metrics: dict -- current metric values
        """
        concerns: list[str] = []

        # Check poll cycle duration
        avg_poll = 0.0
        max_poll = 0.0
        if self._poll_durations:
            avg_poll = sum(self._poll_durations) / len(self._poll_durations)
            max_poll = max(self._poll_durations)
            if max_poll > self.max_poll_delay_seconds:
                concerns.append(
                    f"Poll cycle exceeded limit: {max_poll:.1f}s > "
                    f"{self.max_poll_delay_seconds:.1f}s max"
                )

        # Check message volume (current cycle)
        if self._messages_this_cycle > self.max_messages_per_cycle:
            concerns.append(
                f"Message volume exceeded: {self._messages_this_cycle} > "
                f"{self.max_messages_per_cycle} max per cycle"
            )

        # Check LLM cost (last hour)
        hourly_cost = self._get_hourly_llm_cost()
        if hourly_cost > self.max_llm_cost_per_hour:
            concerns.append(
                f"LLM cost exceeded: ${hourly_cost:.2f}/hr > "
                f"${self.max_llm_cost_per_hour:.2f}/hr max"
            )

        total_tokens = sum(c.tokens for c in self._llm_calls)
        total_cost = sum(c.cost for c in self._llm_calls)

        metrics = {
            "cycle_count": self._cycle_count,
            "avg_poll_duration_seconds": round(avg_poll, 2),
            "max_poll_duration_seconds": round(max_poll, 2),
            "messages_this_cycle": self._messages_this_cycle,
            "total_messages": self._total_messages,
            "total_llm_calls": len(self._llm_calls),
            "total_llm_tokens": total_tokens,
            "total_llm_cost_usd": round(total_cost, 4),
            "hourly_llm_cost_usd": round(hourly_cost, 4),
        }

        return {
            "healthy": len(concerns) == 0,
            "concerns": concerns,
            "metrics": metrics,
        }

    def should_self_report(self) -> bool:
        """Whether the overseer should include a self-report in its output.

        Returns True if any health concern has been detected.
        """
        health = self.check_health()
        return not health["healthy"]

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _get_hourly_llm_cost(self) -> float:
        """Sum LLM costs from the last hour."""
        cutoff = time.time() - 3600
        return sum(c.cost for c in self._llm_calls if c.timestamp >= cutoff)
