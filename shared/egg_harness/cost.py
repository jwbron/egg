"""Token cost tracking with hardcoded per-model rates."""

from __future__ import annotations

from dataclasses import dataclass, field

# Per-token pricing in USD (input / output / cache_read / cache_write)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-20250514": {
        "input": 15.0 / 1_000_000,
        "output": 75.0 / 1_000_000,
        "cache_read": 1.5 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
    },
    "claude-sonnet-4-20250514": {
        "input": 3.0 / 1_000_000,
        "output": 15.0 / 1_000_000,
        "cache_read": 0.3 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
    "claude-haiku-4-5-20250414": {
        "input": 0.80 / 1_000_000,
        "output": 4.0 / 1_000_000,
        "cache_read": 0.08 / 1_000_000,
        "cache_write": 1.0 / 1_000_000,
    },
}

# Fallback pricing for unknown models
_DEFAULT_PRICING = {
    "input": 3.0 / 1_000_000,
    "output": 15.0 / 1_000_000,
    "cache_read": 0.3 / 1_000_000,
    "cache_write": 3.75 / 1_000_000,
}


@dataclass
class UsageAccumulator:
    """Accumulates token usage and cost across turns."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    _turn_costs: list[float] = field(default_factory=list)

    def add_turn(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Add a turn's token usage and return the cost for this turn."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens

        pricing = MODEL_PRICING.get(model, _DEFAULT_PRICING)
        cost = (
            input_tokens * pricing["input"]
            + output_tokens * pricing["output"]
            + cache_read_tokens * pricing["cache_read"]
            + cache_write_tokens * pricing["cache_write"]
        )
        self.total_cost_usd += cost
        self._turn_costs.append(cost)
        return cost

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def num_turns(self) -> int:
        return len(self._turn_costs)
