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


# ===================================================================
# Slice-8 (task-8-4): self-monitor alert EMISSION — the "emit-vs-log"
# nuance resolved. The monitor must produce structured alert payloads
# (ready for the OVERSEER_ALERT path) when unhealthy, not merely log.
# ===================================================================


def _build_alerts(monitor):
    """Return the monitor's structured alerts, or skip if emission isn't wired yet.

    Slice-8 adds ``build_alerts() -> list[dict]`` to ``OverseerSelfMonitor`` to
    resolve the emit-vs-log nuance (§5 self-health, wired into the alert path).
    On the tester branch alone the method is absent, so this skips and ``make
    test`` stays green; once the coder lands it the assertions run strict.
    """
    if not hasattr(monitor, "build_alerts"):
        pytest.skip("OverseerSelfMonitor.build_alerts() not wired yet (slice-8 coder task-8-3)")
    return monitor.build_alerts()


class TestSelfMonitorAlertEmission:
    """task-8-4: the self-monitor emits structured alerts when unhealthy."""

    def test_no_alerts_emitted_when_healthy(self) -> None:
        """A healthy monitor emits an empty alert list — never cries wolf."""
        monitor = OverseerSelfMonitor(max_poll_delay_seconds=60.0)
        monitor.record_poll_cycle(5.0)

        alerts = _build_alerts(monitor)
        assert alerts == []

    def test_alert_emitted_when_unhealthy(self) -> None:
        """An unhealthy monitor emits at least one structured alert payload.

        Each alert is shaped for the OVERSEER_ALERT path: it carries an
        ``anomaly`` tag, a ``priority``, and a human-readable ``summary``.
        """
        monitor = OverseerSelfMonitor(max_llm_cost_per_hour=1.0)
        for _ in range(20):
            monitor.record_llm_call("sonnet", 1000, 0.10)  # > $1.00/hr

        assert monitor.should_self_report() is True
        alerts = _build_alerts(monitor)

        assert alerts, "an unhealthy monitor must emit at least one alert"
        for alert in alerts:
            assert alert.get("anomaly"), f"alert missing 'anomaly': {alert!r}"
            assert alert.get("priority") in {"low", "medium", "high"}, (
                f"alert priority must be a valid severity: {alert!r}"
            )
            assert alert.get("summary"), f"alert missing 'summary': {alert!r}"

    def test_alert_count_matches_concerns(self) -> None:
        """Every distinct health concern surfaces as its own alert (no silent drop)."""
        monitor = OverseerSelfMonitor(
            max_poll_delay_seconds=5.0,
            max_messages_per_cycle=2,
            max_llm_cost_per_hour=1.0,
        )
        # Trip all three limits.
        monitor.record_poll_cycle(10.0)  # poll-delay breach
        for _ in range(5):
            monitor.record_message_sent()  # message-volume breach
        for _ in range(20):
            monitor.record_llm_call("sonnet", 1000, 0.10)  # cost breach

        health = monitor.check_health()
        alerts = _build_alerts(monitor)
        assert len(alerts) == len(health["concerns"]), (
            "each concern must map to exactly one emitted alert"
        )


# ===================================================================
# Slice-8 (task-8-4): cost-tracking fix. The pre-fix monitor summed cost
# over a bounded deque (maxlen=500), so "total" silently undercounted once
# more than 500 calls were recorded, and there was no per-model breakdown.
# The fix tracks a lifetime accumulator plus a per-model breakdown.
# ===================================================================


def _metrics_with_cost_fix(monitor):
    """Return metrics, or skip if the cost-tracking fix (cost_by_model) isn't landed."""
    metrics = monitor.check_health()["metrics"]
    if "cost_by_model" not in metrics:
        pytest.skip("self_monitor cost-tracking fix (cost_by_model) not landed yet (slice-8 task-8-3)")
    return metrics


class TestSelfMonitorCostTrackingFix:
    """task-8-4: cost tracking is complete — lifetime-accurate + per-model."""

    def test_cost_by_model_breakdown(self) -> None:
        """Cost is attributed per model in the metrics."""
        monitor = OverseerSelfMonitor()
        monitor.record_llm_call("haiku", 100, 0.01)
        monitor.record_llm_call("haiku", 100, 0.02)
        monitor.record_llm_call("opus", 500, 0.50)

        metrics = _metrics_with_cost_fix(monitor)
        by_model = metrics["cost_by_model"]
        assert by_model["haiku"] == pytest.approx(0.03, abs=0.001)
        assert by_model["opus"] == pytest.approx(0.50, abs=0.001)

    def test_total_cost_not_undercounted_past_deque_window(self) -> None:
        """Lifetime total cost survives more calls than the bounded recent-window.

        The pre-fix bug: ``total_llm_cost_usd`` summed a ``deque(maxlen=500)`` so
        call #501 evicted call #1 and the "total" drifted below the true sum. The
        fix keeps a lifetime accumulator, so the reported total equals every
        recorded call regardless of how many there have been.
        """
        monitor = OverseerSelfMonitor()
        n = 600  # exceeds the recent-window deque bound
        for _ in range(n):
            monitor.record_llm_call("haiku", 10, 0.01)

        metrics = _metrics_with_cost_fix(monitor)
        # All 600 calls counted: 600 * 0.01 = 6.00, not the last-500 undercount of 5.00.
        assert metrics["total_llm_cost_usd"] == pytest.approx(n * 0.01, abs=0.01)
        assert metrics["total_llm_tokens"] == n * 10
