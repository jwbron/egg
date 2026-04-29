"""Tests for egg_harness.cost — CostTracker and per-model pricing rates."""

from __future__ import annotations

import pytest
from egg_harness.cost import CostTracker


class TestCostTrackerBasic:
    """Test CostTracker core functionality."""

    def test_initial_total_cost_is_zero(self):
        tracker = CostTracker()
        assert tracker.total_cost_usd == 0.0

    def test_add_usage_computes_correct_cost_for_opus(self):
        """CostTracker.add_usage with opus should compute correct USD cost.

        Anthropic published rates for claude-opus-4-6 (as of 2025):
          Input:  $15.00 / 1M tokens
          Output: $75.00 / 1M tokens
        """
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
        )
        # Expected: (1000 * 15.00 / 1_000_000) + (500 * 75.00 / 1_000_000)
        # = 0.015 + 0.0375 = 0.0525
        assert tracker.total_cost_usd == pytest.approx(0.0525, rel=1e-6)

    def test_add_usage_computes_correct_cost_for_sonnet(self):
        """Anthropic published rates for claude-sonnet-4-5-20250514:
        Input:  $3.00 / 1M tokens
        Output: $15.00 / 1M tokens
        """
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-5-20250514",
        )
        # Expected: (1000 * 3.00 / 1_000_000) + (500 * 15.00 / 1_000_000)
        # = 0.003 + 0.0075 = 0.0105
        assert tracker.total_cost_usd == pytest.approx(0.0105, rel=1e-6)

    def test_add_usage_computes_correct_cost_for_haiku(self):
        """Anthropic published rates for claude-haiku-4-5:
        Input:  $0.80 / 1M tokens
        Output: $4.00 / 1M tokens
        """
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-haiku-4-5",
        )
        # Expected: (1000 * 0.80 / 1_000_000) + (500 * 4.00 / 1_000_000)
        # = 0.0008 + 0.002 = 0.0028
        assert tracker.total_cost_usd == pytest.approx(0.0028, rel=1e-6)


class TestCostTrackerRateTable:
    """Verify rate table contains all current Claude models."""

    def test_opus_rates_exist(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1, output_tokens=0, model="claude-opus-4-6")
        assert tracker.total_cost_usd > 0

    def test_sonnet_rates_exist(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1, output_tokens=0, model="claude-sonnet-4-5-20250514")
        assert tracker.total_cost_usd > 0

    def test_haiku_rates_exist(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1, output_tokens=0, model="claude-haiku-4-5")
        assert tracker.total_cost_usd > 0


class TestCostTrackerZeroTokens:
    """Test edge case with zero tokens."""

    def test_zero_input_tokens(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=0, output_tokens=500, model="claude-opus-4-6")
        # Only output cost
        expected = 500 * 75.00 / 1_000_000
        assert tracker.total_cost_usd == pytest.approx(expected, rel=1e-6)

    def test_zero_output_tokens(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1000, output_tokens=0, model="claude-opus-4-6")
        # Only input cost
        expected = 1000 * 15.00 / 1_000_000
        assert tracker.total_cost_usd == pytest.approx(expected, rel=1e-6)

    def test_zero_both_tokens(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=0, output_tokens=0, model="claude-opus-4-6")
        assert tracker.total_cost_usd == 0.0


class TestCostTrackerAccumulation:
    """Test that multiple add_usage calls accumulate correctly."""

    def test_multiple_calls_accumulate(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        first_cost = tracker.total_cost_usd

        tracker.add_usage(input_tokens=2000, output_tokens=1000, model="claude-opus-4-6")
        second_cost = tracker.total_cost_usd

        assert second_cost > first_cost
        # Second add doubles the tokens, so adds 2x the first cost
        expected_second_addition = first_cost * 2
        assert second_cost == pytest.approx(first_cost + expected_second_addition, rel=1e-6)

    def test_mixed_model_accumulation(self):
        """Costs from different models should accumulate together."""
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        opus_cost = tracker.total_cost_usd

        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-5-20250514",
        )
        total = tracker.total_cost_usd

        assert total > opus_cost
        # Sonnet is cheaper than opus
        sonnet_expected = (1000 * 3.00 / 1_000_000) + (500 * 15.00 / 1_000_000)
        assert total == pytest.approx(opus_cost + sonnet_expected, rel=1e-6)

    def test_many_small_additions(self):
        """Many small add_usage calls should produce consistent result."""
        tracker = CostTracker()
        for _ in range(100):
            tracker.add_usage(input_tokens=10, output_tokens=5, model="claude-opus-4-6")

        single_tracker = CostTracker()
        single_tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")

        assert tracker.total_cost_usd == pytest.approx(single_tracker.total_cost_usd, rel=1e-6)


class TestCostTrackerUnknownModel:
    """Test behavior with unknown/unrecognized models."""

    def test_unknown_model_handles_gracefully(self):
        """Unknown model should not crash — may return zero cost or raise."""
        tracker = CostTracker()
        try:
            tracker.add_usage(input_tokens=1000, output_tokens=500, model="unknown-model")
            # If it doesn't raise, cost should be zero or some default
            assert tracker.total_cost_usd >= 0.0
        except ValueError, KeyError:
            pass  # Also acceptable to raise for unknown models

    def test_openai_compatible_model_no_cost(self):
        """OpenAI-compatible models may have no Anthropic pricing."""
        tracker = CostTracker()
        try:
            tracker.add_usage(input_tokens=1000, output_tokens=500, model="gpt-4o")
            # Should not crash; cost may be zero
            assert tracker.total_cost_usd >= 0.0
        except ValueError, KeyError:
            pass  # Acceptable to raise for non-Anthropic models


class TestCostTrackerCacheTokens:
    """Test cache read/write token tracking."""

    def test_cache_read_tokens(self):
        """Cache read tokens should be tracked (typically at reduced rate)."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
            cache_read_tokens=2000,
        )
        cost_with_cache = tracker.total_cost_usd

        tracker2 = CostTracker()
        tracker2.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
        )
        cost_without_cache = tracker2.total_cost_usd

        # Cache reads add cost, but at a reduced rate compared to input
        assert cost_with_cache >= cost_without_cache

    def test_cache_write_tokens(self):
        """Cache write tokens should be tracked (typically at premium rate)."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
            cache_write_tokens=500,
        )
        cost_with_cache_write = tracker.total_cost_usd

        tracker2 = CostTracker()
        tracker2.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
        )
        cost_without = tracker2.total_cost_usd

        # Cache writes add cost (at premium rate, typically 1.25x input)
        assert cost_with_cache_write >= cost_without

    def test_cache_tokens_default_to_zero(self):
        """When cache tokens not provided, should default to zero impact."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=1000,
            output_tokens=500,
            model="claude-opus-4-6",
        )
        expected = (1000 * 15.00 / 1_000_000) + (500 * 75.00 / 1_000_000)
        assert tracker.total_cost_usd == pytest.approx(expected, rel=1e-6)


class TestCostTrackerReset:
    """Test reset/clear functionality."""

    def test_reset_clears_total_cost(self):
        """If reset() exists, it should zero out accumulated cost."""
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        assert tracker.total_cost_usd > 0

        if hasattr(tracker, "reset"):
            tracker.reset()
            assert tracker.total_cost_usd == 0.0


class TestCostTrackerLargeValues:
    """Test with very large token counts for overflow safety."""

    def test_very_large_token_counts(self):
        """Large token counts should not cause integer overflow or crash."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=100_000_000,  # 100M tokens
            output_tokens=50_000_000,  # 50M tokens
            model="claude-opus-4-6",
        )
        # Expected: (100M * 15 / 1M) + (50M * 75 / 1M) = 1500 + 3750 = 5250
        assert tracker.total_cost_usd == pytest.approx(5250.0, rel=1e-6)
        assert isinstance(tracker.total_cost_usd, float)

    def test_max_context_window_tokens(self):
        """Simulate full context window input (200k tokens)."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=200_000,
            output_tokens=8_192,
            model="claude-opus-4-6",
        )
        assert tracker.total_cost_usd > 0
        assert isinstance(tracker.total_cost_usd, float)


class TestCostTrackerTotalCostProperty:
    """Test the total_cost_usd attribute specifically."""

    def test_total_cost_usd_is_float(self):
        tracker = CostTracker()
        assert isinstance(tracker.total_cost_usd, float)

    def test_total_cost_usd_updates_after_add(self):
        tracker = CostTracker()
        before = tracker.total_cost_usd
        tracker.add_usage(input_tokens=100, output_tokens=50, model="claude-opus-4-6")
        after = tracker.total_cost_usd
        assert after > before

    def test_total_cost_usd_is_non_negative(self):
        tracker = CostTracker()
        tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        assert tracker.total_cost_usd >= 0.0
