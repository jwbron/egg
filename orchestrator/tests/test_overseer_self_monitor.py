"""Tests for overseer self-monitoring (Phase 4).

Validates that the OverseerSelfMonitor correctly tracks poll cycle
timing, message volume, LLM call costs, and reports health status.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional import
# ---------------------------------------------------------------------------

try:
    from overseer.self_monitor import OverseerSelfMonitor
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.self_monitor not available yet: {exc}",
        allow_module_level=True,
    )


# ===================================================================
# test_record_poll_cycle
# ===================================================================


class TestRecordPollCycle:
    """Test poll cycle recording."""

    def test_record_poll_cycle(self) -> None:
        """Recording poll cycles updates metrics."""
        monitor = OverseerSelfMonitor()

        monitor.record_poll_cycle(5.0)
        monitor.record_poll_cycle(10.0)
        monitor.record_poll_cycle(3.0)

        health = monitor.check_health()
        metrics = health["metrics"]

        assert metrics["cycle_count"] == 3
        assert metrics["avg_poll_duration_seconds"] == 6.0
        assert metrics["max_poll_duration_seconds"] == 10.0

    def test_record_poll_cycle_resets_message_counter(self) -> None:
        """Recording a poll cycle resets the per-cycle message counter."""
        monitor = OverseerSelfMonitor()

        monitor.record_message_sent()
        monitor.record_message_sent()
        assert monitor._messages_this_cycle == 2

        monitor.record_poll_cycle(1.0)
        assert monitor._messages_this_cycle == 0


# ===================================================================
# test_healthy_when_within_limits
# ===================================================================


class TestHealthyWhenWithinLimits:
    """Test that monitor reports healthy when within limits."""

    def test_healthy_when_within_limits(self) -> None:
        """All metrics within limits should report healthy."""
        monitor = OverseerSelfMonitor(
            max_poll_delay_seconds=60.0,
            max_messages_per_cycle=10,
            max_llm_cost_per_hour=5.0,
        )

        # Record normal metrics
        monitor.record_poll_cycle(5.0)
        monitor.record_message_sent()
        monitor.record_llm_call("haiku", 100, 0.001)

        health = monitor.check_health()

        assert health["healthy"] is True
        assert health["concerns"] == []

    def test_healthy_with_no_data(self) -> None:
        """No recorded data should be healthy (nothing to complain about)."""
        monitor = OverseerSelfMonitor()
        health = monitor.check_health()

        assert health["healthy"] is True
        assert health["concerns"] == []


# ===================================================================
# test_unhealthy_when_poll_delay_exceeded
# ===================================================================


class TestUnhealthyWhenPollDelayExceeded:
    """Test that exceeding poll delay triggers unhealthy status."""

    def test_unhealthy_when_poll_delay_exceeded(self) -> None:
        """Poll cycle exceeding max_poll_delay_seconds should be unhealthy."""
        monitor = OverseerSelfMonitor(max_poll_delay_seconds=10.0)

        monitor.record_poll_cycle(5.0)  # OK
        monitor.record_poll_cycle(15.0)  # exceeds limit

        health = monitor.check_health()

        assert health["healthy"] is False
        assert len(health["concerns"]) >= 1
        assert any("poll cycle" in c.lower() for c in health["concerns"])

    def test_unhealthy_when_messages_exceeded(self) -> None:
        """Exceeding max messages per cycle should be unhealthy."""
        monitor = OverseerSelfMonitor(max_messages_per_cycle=3)

        for _ in range(5):
            monitor.record_message_sent()

        health = monitor.check_health()

        assert health["healthy"] is False
        assert any("message volume" in c.lower() for c in health["concerns"])

    def test_unhealthy_when_llm_cost_exceeded(self) -> None:
        """Exceeding max LLM cost per hour should be unhealthy."""
        monitor = OverseerSelfMonitor(max_llm_cost_per_hour=1.0)

        # Record calls totaling > $1.00 in the last hour
        for _ in range(20):
            monitor.record_llm_call("sonnet", 1000, 0.10)

        health = monitor.check_health()

        assert health["healthy"] is False
        assert any("llm cost" in c.lower() for c in health["concerns"])

    def test_should_self_report_when_unhealthy(self) -> None:
        """should_self_report returns True when unhealthy."""
        monitor = OverseerSelfMonitor(max_poll_delay_seconds=5.0)
        monitor.record_poll_cycle(10.0)

        assert monitor.should_self_report() is True

    def test_should_not_self_report_when_healthy(self) -> None:
        """should_self_report returns False when healthy."""
        monitor = OverseerSelfMonitor(max_poll_delay_seconds=60.0)
        monitor.record_poll_cycle(5.0)

        assert monitor.should_self_report() is False


# ===================================================================
# test_record_llm_call
# ===================================================================


class TestRecordLLMCall:
    """Test LLM call recording."""

    def test_record_llm_call(self) -> None:
        """LLM calls are recorded and reflected in metrics."""
        monitor = OverseerSelfMonitor()

        monitor.record_llm_call("haiku", 500, 0.005)
        monitor.record_llm_call("sonnet", 1000, 0.05)

        health = monitor.check_health()
        metrics = health["metrics"]

        assert metrics["total_llm_calls"] == 2
        assert metrics["total_llm_tokens"] == 1500
        assert metrics["total_llm_cost_usd"] == pytest.approx(0.055, abs=0.001)

    def test_hourly_cost_excludes_old_calls(self) -> None:
        """LLM calls older than 1 hour should not count toward hourly cost."""
        monitor = OverseerSelfMonitor(max_llm_cost_per_hour=1.0)

        # Record an old call by manipulating the timestamp
        monitor.record_llm_call("sonnet", 1000, 0.50)
        # Make the call appear old
        monitor._llm_calls[-1].timestamp = time.time() - 7200  # 2 hours ago

        # Record a recent call
        monitor.record_llm_call("haiku", 100, 0.01)

        health = monitor.check_health()
        metrics = health["metrics"]

        # Hourly cost should only include the recent call
        assert metrics["hourly_llm_cost_usd"] == pytest.approx(0.01, abs=0.001)
        # Total cost includes everything
        assert metrics["total_llm_cost_usd"] == pytest.approx(0.51, abs=0.001)
