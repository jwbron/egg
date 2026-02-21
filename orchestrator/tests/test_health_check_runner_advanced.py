"""Advanced tests for HealthCheckRunner: escalation, event emission, edge cases."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from health_checks.context import PipelineHealthContext
from health_checks.runner import HealthCheckRunner, worst_action
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import Pipeline, PipelinePhase, PipelineStatus


def _make_pipeline() -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )


def _make_context() -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=_make_pipeline(),
        repo_path=Path("/tmp/test"),
        trigger="on_demand",
    )


# ---------------------------------------------------------------------------
# Test check classes
# ---------------------------------------------------------------------------


class _HealthyCheck:
    name = "healthy"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset(
        {
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.STARTUP,
            HealthTrigger.RUNTIME_TICK,
        }
    )

    def run(self, ctx):
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="OK",
        )


class _DegradedCheck:
    name = "degraded"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset(
        {
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
        }
    )

    def run(self, ctx):
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Degraded",
            action=HealthAction.ALERT,
        )


class _FailedCheck:
    name = "failed"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset(
        {
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
        }
    )

    def run(self, ctx):
        return HealthResult(
            status=HealthStatus.FAILED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Failed",
            action=HealthAction.FAIL_PIPELINE,
        )


class _Tier2HealthyCheck:
    name = "tier2_healthy"
    tier = HealthTier.AGENT
    triggers = frozenset(
        {
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.STARTUP,
        }
    )

    def run(self, ctx):
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="Agent check OK",
        )


class _Tier2DegradedCheck:
    name = "tier2_degraded"
    tier = HealthTier.AGENT
    triggers = frozenset(
        {
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
        }
    )

    def run(self, ctx):
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Agent found issues",
            action=HealthAction.ALERT,
        )


# ===========================================================================
# Tests: Runner registration
# ===========================================================================


class TestRunnerRegistration:
    def test_checks_returns_copy(self):
        """Modifying the returned list should not affect internal state."""
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        checks = runner.checks
        checks.clear()
        assert len(runner.checks) == 1

    def test_multiple_registrations(self):
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        runner.register(_DegradedCheck())
        runner.register(_FailedCheck())
        assert len(runner.checks) == 3

    def test_register_tier2_check(self):
        runner = HealthCheckRunner()
        runner.register(_Tier2HealthyCheck())
        assert runner.checks[0].tier == HealthTier.AGENT


# ===========================================================================
# Tests: Escalation logic (_should_escalate_to_tier2)
# ===========================================================================


class TestEscalationLogic:
    def test_phase_complete_always_escalates(self):
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.PHASE_COMPLETE, []) is True

    def test_on_demand_always_escalates(self):
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.ON_DEMAND, []) is True

    def test_wave_complete_escalates_on_degraded(self):
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="x",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
            )
        ]
        assert (
            HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.WAVE_COMPLETE, results)
            is True
        )

    def test_wave_complete_no_escalation_on_healthy(self):
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="x",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            )
        ]
        assert (
            HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.WAVE_COMPLETE, results)
            is False
        )

    def test_wave_complete_no_escalation_on_failed_only(self):
        """FAILED without DEGRADED should NOT escalate on WAVE_COMPLETE."""
        results = [
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="x",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="failed",
            )
        ]
        assert (
            HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.WAVE_COMPLETE, results)
            is False
        )

    def test_startup_never_escalates(self):
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="x",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
            )
        ]
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.STARTUP, results) is False

    def test_runtime_tick_never_escalates(self):
        results = [
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="x",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="failed",
            )
        ]
        assert (
            HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.RUNTIME_TICK, results)
            is False
        )

    def test_wave_complete_escalates_with_mixed_results(self):
        """DEGRADED among other results should trigger escalation."""
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            ),
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
            ),
        ]
        assert (
            HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.WAVE_COMPLETE, results)
            is True
        )

    def test_phase_complete_escalates_with_empty_results(self):
        """PHASE_COMPLETE escalates even with no Tier 1 results."""
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.PHASE_COMPLETE, []) is True


# ===========================================================================
# Tests: Runner execution
# ===========================================================================


class TestRunnerExecution:
    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_no_checks_returns_empty(self, _):
        """Runner with no registered checks returns empty results."""
        runner = HealthCheckRunner()
        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert results == []

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_trigger_filtering_mixed(self, _):
        """Only checks matching the trigger should execute."""
        runner = HealthCheckRunner()

        class RuntimeTickOnly:
            name = "runtime_only"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.RUNTIME_TICK})

            def run(self, ctx):
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="ok",
                )

        runner.register(RuntimeTickOnly())
        runner.register(_HealthyCheck())

        # STARTUP: only _HealthyCheck matches (both have STARTUP in triggers)
        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.STARTUP)
        names = [r.check_name for r in results]
        assert "runtime_only" not in names
        assert "healthy" in names

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier1_before_tier2(self, _):
        """Tier 1 results should appear before Tier 2 in results list."""
        runner = HealthCheckRunner()
        runner.register(_DegradedCheck())
        runner.register(_Tier2DegradedCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 2
        assert results[0].tier == HealthTier.PROGRAMMATIC
        assert results[1].tier == HealthTier.AGENT

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_runtime_tick_no_tier2(self, _):
        """RUNTIME_TICK should never run Tier 2 checks."""
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        runner.register(_Tier2HealthyCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.RUNTIME_TICK)
        assert len(results) == 1
        assert all(r.tier == HealthTier.PROGRAMMATIC for r in results)

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_on_demand_runs_tier2(self, _):
        """ON_DEMAND should always run Tier 2."""
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        runner.register(_Tier2HealthyCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        tiers = {r.tier for r in results}
        assert HealthTier.PROGRAMMATIC in tiers
        assert HealthTier.AGENT in tiers

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_exception_in_check_returns_healthy(self, _):
        """A check that raises should produce HEALTHY with error reasoning."""

        class Exploding:
            name = "exploding"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.ON_DEMAND})

            def run(self, ctx):
                raise ValueError("Unexpected error")

        runner = HealthCheckRunner()
        runner.register(Exploding())
        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 1
        assert results[0].status == HealthStatus.HEALTHY
        assert results[0].action == HealthAction.CONTINUE
        assert "Unexpected error" in results[0].reasoning

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_multiple_tier1_all_run(self, _):
        """All matching Tier 1 checks should execute."""
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        runner.register(_DegradedCheck())
        runner.register(_FailedCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        names = [r.check_name for r in results if r.tier == HealthTier.PROGRAMMATIC]
        assert "healthy" in names
        assert "degraded" in names
        assert "failed" in names


# ===========================================================================
# Tests: Event emission
# ===========================================================================


class TestEventEmission:
    def test_started_event_emitted(self):
        """HEALTH_CHECK_STARTED should be emitted at the beginning."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        # First emit should be HEALTH_CHECK_STARTED
        first_call = mock_bus.emit.call_args_list[0]
        from events import EventType

        assert first_call.args[0] == EventType.HEALTH_CHECK_STARTED

    def test_completed_event_emitted(self):
        """HEALTH_CHECK_COMPLETED should be emitted at the end."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Last emit should be HEALTH_CHECK_COMPLETED with aggregate data
        last_call = mock_bus.emit.call_args_list[-1]
        from events import EventType

        assert last_call.args[0] == EventType.HEALTH_CHECK_COMPLETED
        data = (
            last_call.kwargs.get("data") or last_call.args[2]
            if len(last_call.args) > 2
            else last_call.kwargs.get("data")
        )
        assert "aggregate_status" in data
        assert "check_count" in data

    def test_failed_result_emits_failed_event(self):
        """FAILED result should emit HEALTH_CHECK_FAILED event."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_FailedCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        from events import EventType

        emitted_types = [c.args[0] for c in mock_bus.emit.call_args_list]
        assert EventType.HEALTH_CHECK_FAILED in emitted_types

    def test_degraded_result_emits_degraded_event(self):
        """DEGRADED result should emit HEALTH_CHECK_DEGRADED event."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_DegradedCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        from events import EventType

        emitted_types = [c.args[0] for c in mock_bus.emit.call_args_list]
        assert EventType.HEALTH_CHECK_DEGRADED in emitted_types

    def test_healthy_result_emits_completed_event(self):
        """HEALTHY result should emit HEALTH_CHECK_COMPLETED event (not DEGRADED or FAILED)."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        from events import EventType

        emitted_types = [c.args[0] for c in mock_bus.emit.call_args_list]
        assert EventType.HEALTH_CHECK_DEGRADED not in emitted_types
        assert EventType.HEALTH_CHECK_FAILED not in emitted_types

    def test_event_bus_failure_does_not_crash(self):
        """EventBus emission failure should not crash the runner."""
        mock_bus = MagicMock()
        mock_bus.emit.side_effect = RuntimeError("Event bus broken")
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Results should still be returned despite event bus failure
        assert len(results) == 1
        assert results[0].status == HealthStatus.HEALTHY

    def test_aggregate_status_reflects_worst(self):
        """Completed event aggregate_status should reflect worst result."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())
        runner.register(_FailedCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Find the completion event
        from events import EventType

        completion_calls = [
            c for c in mock_bus.emit.call_args_list if c.args[0] == EventType.HEALTH_CHECK_COMPLETED
        ]
        assert len(completion_calls) >= 1
        last = completion_calls[-1]
        data = (
            last.kwargs.get("data") or last.args[2]
            if len(last.args) > 2
            else last.kwargs.get("data")
        )
        assert data["aggregate_status"] == "failed"

    def test_no_event_bus_graceful(self):
        """Runner should work without event bus."""
        runner = HealthCheckRunner()
        runner.register(_HealthyCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None):
            ctx = _make_context()
            results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        assert len(results) == 1


# ===========================================================================
# Tests: worst_action helper edge cases
# ===========================================================================


class TestWorstActionAdvanced:
    def test_single_continue(self):
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            )
        ]
        assert worst_action(results) == HealthAction.CONTINUE

    def test_fail_pipeline_overrides_alert(self):
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
                action=HealthAction.ALERT,
            ),
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="failed",
                action=HealthAction.FAIL_PIPELINE,
            ),
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="c",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            ),
        ]
        assert worst_action(results) == HealthAction.FAIL_PIPELINE

    def test_mixed_continue_and_alert(self):
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            ),
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
                action=HealthAction.ALERT,
            ),
        ]
        assert worst_action(results) == HealthAction.ALERT
