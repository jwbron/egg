"""Tests for health check core types, runner, and context."""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from health_checks.context import PipelineHealthContext
from health_checks.runner import HealthCheckRunner, worst_action
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    status: PipelineStatus = PipelineStatus.RUNNING,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=status,
        current_phase=phase,
    )


def _make_context(
    pipeline: Pipeline | None = None,
    trigger: str = "on_demand",
) -> PipelineHealthContext:
    p = pipeline or _make_pipeline()
    return PipelineHealthContext(
        pipeline=p,
        repo_path=Path("/tmp/test-repo"),
        trigger=trigger,
    )


class _AlwaysHealthyCheck:
    """Test check that always returns HEALTHY."""

    name = "always_healthy"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({HealthTrigger.ON_DEMAND, HealthTrigger.WAVE_COMPLETE, HealthTrigger.PHASE_COMPLETE})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="All good.",
        )


class _AlwaysDegradedCheck:
    """Test check that always returns DEGRADED."""

    name = "always_degraded"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({HealthTrigger.ON_DEMAND, HealthTrigger.WAVE_COMPLETE, HealthTrigger.PHASE_COMPLETE})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Something is off.",
            action=HealthAction.ALERT,
        )


class _AlwaysFailedCheck:
    """Test check that always returns FAILED."""

    name = "always_failed"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({HealthTrigger.ON_DEMAND, HealthTrigger.WAVE_COMPLETE, HealthTrigger.PHASE_COMPLETE})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        return HealthResult(
            status=HealthStatus.FAILED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Infrastructure down.",
            action=HealthAction.FAIL_PIPELINE,
        )


class _ExplodingCheck:
    """Test check that raises an exception."""

    name = "exploding"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({HealthTrigger.ON_DEMAND})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        raise RuntimeError("Kaboom!")


class _Tier2Check:
    """Test Tier 2 check."""

    name = "mock_tier2"
    tier = HealthTier.AGENT
    triggers = frozenset({HealthTrigger.WAVE_COMPLETE, HealthTrigger.PHASE_COMPLETE, HealthTrigger.ON_DEMAND})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Agent found semantic issues.",
            action=HealthAction.ALERT,
        )


# ===========================================================================
# Tests: HealthResult
# ===========================================================================


class TestHealthResult:
    def test_creation_with_defaults(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="OK",
        )
        assert result.status == HealthStatus.HEALTHY
        assert result.action == HealthAction.CONTINUE
        assert result.details == {}
        assert isinstance(result.timestamp, datetime)

    def test_to_dict(self):
        result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="test_check",
            tier=HealthTier.AGENT,
            reasoning="Partial failure",
            action=HealthAction.ALERT,
            details={"missing": ["file.txt"]},
        )
        d = result.to_dict()
        assert d["status"] == "degraded"
        assert d["check_name"] == "test_check"
        assert d["tier"] == "tier2"
        assert d["action"] == "alert"
        assert d["details"] == {"missing": ["file.txt"]}
        assert d["timestamp"].endswith("Z")

    def test_frozen(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="OK",
        )
        try:
            result.status = HealthStatus.FAILED  # type: ignore[misc]
            assert False, "Should have raised"
        except AttributeError:
            pass


# ===========================================================================
# Tests: HealthCheck Protocol
# ===========================================================================


class TestHealthCheckProtocol:
    def test_structural_subtyping(self):
        """Any object with the right attributes satisfies the protocol."""
        check = _AlwaysHealthyCheck()
        assert isinstance(check, HealthCheck)

    def test_tier2_satisfies_protocol(self):
        check = _Tier2Check()
        assert isinstance(check, HealthCheck)


# ===========================================================================
# Tests: Enums
# ===========================================================================


class TestEnums:
    def test_health_status_values(self):
        assert HealthStatus.HEALTHY == "healthy"
        assert HealthStatus.DEGRADED == "degraded"
        assert HealthStatus.FAILED == "failed"

    def test_health_tier_values(self):
        assert HealthTier.PROGRAMMATIC == "tier1"
        assert HealthTier.AGENT == "tier2"

    def test_trigger_values(self):
        assert HealthTrigger.STARTUP == "startup"
        assert HealthTrigger.RUNTIME_TICK == "runtime_tick"
        assert HealthTrigger.WAVE_COMPLETE == "wave_complete"
        assert HealthTrigger.PHASE_COMPLETE == "phase_complete"
        assert HealthTrigger.ON_DEMAND == "on_demand"

    def test_action_values(self):
        assert HealthAction.CONTINUE == "continue"
        assert HealthAction.FAIL_PIPELINE == "fail_pipeline"
        assert HealthAction.ALERT == "alert"


# ===========================================================================
# Tests: PipelineHealthContext
# ===========================================================================


class TestPipelineHealthContext:
    def test_cheap_accessors(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        assert ctx.pipeline_id == "issue-99"
        assert ctx.branch == "egg/issue-99"
        assert ctx.current_phase == PipelinePhase.IMPLEMENT

    def test_lazy_git_log(self):
        """git_log is lazy — only computed on first access."""
        ctx = _make_context()
        # Accessing the internal cache directly
        assert ctx._git_log is None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123 commit msg\n", returncode=0)
            log = ctx.git_log
            assert "abc123" in log
            mock_run.assert_called_once()
            # Second access should be cached
            log2 = ctx.git_log
            assert log2 == log
            mock_run.assert_called_once()

    def test_lazy_git_diff_stat(self):
        ctx = _make_context()
        assert ctx._git_diff_stat is None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="3 files changed", returncode=0)
            stat = ctx.git_diff_stat
            assert "3 files" in stat

    def test_live_container_ids_no_docker(self):
        """Without a docker client, returns empty set."""
        ctx = _make_context()
        assert ctx.live_container_ids == set()

    def test_live_container_ids_with_docker(self):
        pipeline = _make_pipeline()
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.container_id = "abc123"
        mock_docker.list_containers.return_value = [mock_container]

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test-repo"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        assert ctx.live_container_ids == {"abc123"}


# ===========================================================================
# Tests: HealthCheckRunner
# ===========================================================================


class TestHealthCheckRunner:
    def test_register_and_list(self):
        runner = HealthCheckRunner()
        check = _AlwaysHealthyCheck()
        runner.register(check)
        assert len(runner.checks) == 1
        assert runner.checks[0].name == "always_healthy"

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_run_returns_results(self, _mock_bus):
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyCheck())
        runner.register(_AlwaysDegradedCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 2
        assert results[0].status == HealthStatus.HEALTHY
        assert results[1].status == HealthStatus.DEGRADED

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_trigger_filtering(self, _mock_bus):
        """Only checks matching the trigger should run."""
        runner = HealthCheckRunner()

        class StartupOnly:
            name = "startup_only"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.STARTUP})

            def run(self, ctx):
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="OK",
                )

        runner.register(StartupOnly())
        runner.register(_AlwaysHealthyCheck())

        # ON_DEMAND should only run the always_healthy check, not startup_only
        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 1
        assert results[0].check_name == "always_healthy"

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_exception_handling(self, _mock_bus):
        """A check that raises should not crash the runner."""
        runner = HealthCheckRunner()
        runner.register(_ExplodingCheck())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 1
        assert results[0].status == HealthStatus.HEALTHY
        assert "Kaboom" in results[0].reasoning

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier2_escalation_wave_complete_healthy(self, _mock_bus):
        """WAVE_COMPLETE: Tier 2 should NOT run if Tier 1 is all HEALTHY."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyCheck())
        runner.register(_Tier2Check())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)
        # Only Tier 1 should have run
        assert len(results) == 1
        assert results[0].tier == HealthTier.PROGRAMMATIC

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier2_escalation_wave_complete_degraded(self, _mock_bus):
        """WAVE_COMPLETE: Tier 2 should run if Tier 1 has DEGRADED."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysDegradedCheck())
        runner.register(_Tier2Check())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)
        # Both Tier 1 and Tier 2 should have run
        assert len(results) == 2
        assert results[0].tier == HealthTier.PROGRAMMATIC
        assert results[1].tier == HealthTier.AGENT

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier2_always_runs_phase_complete(self, _mock_bus):
        """PHASE_COMPLETE: Tier 2 should always run."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyCheck())
        runner.register(_Tier2Check())

        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)
        assert len(results) == 2
        tier_values = [r.tier for r in results]
        assert HealthTier.PROGRAMMATIC in tier_values
        assert HealthTier.AGENT in tier_values

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier2_never_runs_startup(self, _mock_bus):
        """STARTUP: Tier 2 should never run."""
        runner = HealthCheckRunner()

        class StartupTier2:
            name = "startup_tier2"
            tier = HealthTier.AGENT
            triggers = frozenset({HealthTrigger.STARTUP})

            def run(self, ctx):
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="OK",
                )

        runner.register(StartupTier2())
        ctx = _make_context()
        results = runner.run(ctx, HealthTrigger.STARTUP)
        assert len(results) == 0  # No Tier 1 checks registered with STARTUP

    def test_event_emission(self):
        """Runner should emit events via EventBus."""
        mock_bus = MagicMock()
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyCheck())

        with patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=mock_bus):
            ctx = _make_context()
            runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Should have emitted: STARTED, one result event, COMPLETED
        assert mock_bus.emit.call_count >= 2


# ===========================================================================
# Tests: worst_action helper
# ===========================================================================


class TestWorstAction:
    def test_all_continue(self):
        results = [
            HealthResult(status=HealthStatus.HEALTHY, check_name="a", tier=HealthTier.PROGRAMMATIC, reasoning="OK"),
            HealthResult(status=HealthStatus.HEALTHY, check_name="b", tier=HealthTier.PROGRAMMATIC, reasoning="OK"),
        ]
        assert worst_action(results) == HealthAction.CONTINUE

    def test_alert_present(self):
        results = [
            HealthResult(status=HealthStatus.HEALTHY, check_name="a", tier=HealthTier.PROGRAMMATIC, reasoning="OK"),
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="Degraded",
                action=HealthAction.ALERT,
            ),
        ]
        assert worst_action(results) == HealthAction.ALERT

    def test_fail_pipeline_present(self):
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="Degraded",
                action=HealthAction.ALERT,
            ),
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="Failed",
                action=HealthAction.FAIL_PIPELINE,
            ),
        ]
        assert worst_action(results) == HealthAction.FAIL_PIPELINE

    def test_empty_list(self):
        assert worst_action([]) == HealthAction.CONTINUE
