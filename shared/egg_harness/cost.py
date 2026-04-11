"""Cost tracking for agent invocations.

Provides per-model token pricing and a stateful tracker that accumulates
usage across multiple API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rates are expressed in USD **per token** (i.e. the published per-million
# rate divided by 1_000_000).  Keeping them in per-token form avoids a
# division on every cost calculation.
TOKEN_RATES: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {  # noqa: EGG201 - canonical model ID as dict key
        "input": 15.00 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "cache_read": 1.50 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
    },
    "claude-sonnet-4-5-20250514": {  # noqa: EGG201 - canonical model ID as dict key
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
    "claude-haiku-4-5": {  # noqa: EGG201 - canonical model ID as dict key
        "input": 0.80 / 1_000_000,
        "output": 4.00 / 1_000_000,
        "cache_read": 0.08 / 1_000_000,
        "cache_write": 1.00 / 1_000_000,
    },
}


@dataclass
class CostTracker:
    """Accumulates token usage and cost across multiple API calls.  # noqa: EGG201

    Example::

        tracker = CostTracker()
        cost = tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-5-20250514",
        )
        print(f"This call: ${cost:.4f}, running total: ${tracker.total_cost_usd:.4f}")
    """

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cost_usd: float = 0.0

    def add_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        model: str = "",
    ) -> float:
        """Record token usage and return the incremental cost in USD.

        Args:
            input_tokens: Number of input (prompt) tokens consumed.
            output_tokens: Number of output (completion) tokens generated.
            cache_read_tokens: Number of tokens served from prompt cache.
            cache_write_tokens: Number of tokens written into prompt cache.
            model: Model identifier used for the request.  If the model is
                not present in :data:`TOKEN_RATES`, token counts are still
                accumulated but the returned cost is ``0.0``.

        Returns:
            The cost in USD attributable to this single usage entry.
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens

        rates = TOKEN_RATES.get(model)
        if rates is None:
            logger.warning("Unknown model %r — cost will be reported as $0.00", model)
            return 0.0

        cost = (
            input_tokens * rates["input"]
            + output_tokens * rates["output"]
            + cache_read_tokens * rates["cache_read"]
            + cache_write_tokens * rates["cache_write"]
        )
        self.total_cost_usd += cost
        return cost
