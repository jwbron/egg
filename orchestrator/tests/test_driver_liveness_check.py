"""Tests for the driver heartbeat registry + DriverLivenessCheck (#3540).

Covers:
* ``driver_heartbeat`` — stamp/age/clear semantics.
* ``DriverLivenessCheck`` — the three degraded modes (driver_dead,
  driver_hung, driver_no_progress), every healthy exemption (not running,
  pending HITL, live containers, young phase, recent spawn/tick), and the
  first-observed grace clocks.
* ``KubernetesMonitor._handle_driver_liveness_results`` — escalation
  (error log + OVERSEER_ALERT + persisted HITL decision), the re-alert
  throttle, and the pending-decision dedup.
* ``_maybe_dispatch_driver_liveness_resolution`` — "Retry phase" routes to
  the shared restart-phase executor; dismiss is record-only; foreign
  contexts pass through.
"""

import sys
from datetime import UTC, datetime, timedelta
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

import driver_heartbeat
from health_checks.context import PipelineHealthContext
from health_checks.tier1.driver_liveness import (
    DRIVER_LIVENESS_DISMISS_OPTION,
    DRIVER_LIVENESS_HITL_CONTEXT,
    DRIVER_LIVENESS_RETRY_OPTION,
    DriverLivenessCheck,
)
from health_checks.types import HealthAction, HealthStatus
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

PID = "issue-3540"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    status: PipelineStatus = PipelineStatus.RUNNING,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
    phase_running: bool = True,
    phase_started_seconds_ago: float = 7200,
) -> Pipeline:
    pipeline = Pipeline(
        id=PID,
        issue_number=3540,
        repo="owner/repo",
        branch="egg/issue-3540",
        mode="issue",
        status=status,
        current_phase=phase,
    )
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = PipelineStatus.RUNNING if phase_running else PipelineStatus.PENDING
    phase_exec.started_at = datetime.now(UTC) - timedelta(seconds=phase_started_seconds_ago)
    return pipeline


def _make_context(pipeline: Pipeline) -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=Path("/tmp/test-repo"),
        trigger="runtime_tick",
        docker_client=MagicMock(),
    )


def _fresh_registry():
    driver_heartbeat.clear(PID)


# ---------------------------------------------------------------------------
# driver_heartbeat registry
# ---------------------------------------------------------------------------


class TestDriverHeartbeatRegistry:
    def test_unknown_pipeline_has_no_ages(self):
        assert driver_heartbeat.tick_age_seconds("never-stamped") is None
        assert driver_heartbeat.spawn_age_seconds("never-stamped") is None

    def test_tick_and_spawn_ages_are_fresh_after_stamp(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        driver_heartbeat.record_spawn(PID)
        assert driver_heartbeat.tick_age_seconds(PID) < 5
        assert driver_heartbeat.spawn_age_seconds(PID) < 5

    def test_clear_drops_both_stamps(self):
        driver_heartbeat.record_tick(PID)
        driver_heartbeat.record_spawn(PID)
        driver_heartbeat.clear(PID)
        assert driver_heartbeat.tick_age_seconds(PID) is None
        assert driver_heartbeat.spawn_age_seconds(PID) is None


# ---------------------------------------------------------------------------
# DriverLivenessCheck — healthy exemptions
# ---------------------------------------------------------------------------


class TestDriverLivenessHealthyPaths:
    def test_non_running_pipeline_skipped(self):
        check = DriverLivenessCheck()
        pipeline = _make_pipeline(status=PipelineStatus.AWAITING_HUMAN)
        result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_non_running_phase_skipped(self):
        check = DriverLivenessCheck()
        pipeline = _make_pipeline(phase_running=False)
        result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_pending_decisions_exempt(self):
        _fresh_registry()
        check = DriverLivenessCheck(dead_grace_seconds=0, stall_grace_seconds=0)
        pipeline = _make_pipeline()
        pipeline.add_decision(question="q?", options=["a"], phase=PipelinePhase.IMPLEMENT)
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=True):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY
        assert "Pending HITL" in result.reasoning

    def test_live_containers_exempt(self):
        """A stale spawn stamp must not fire while agent containers run."""
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        check = DriverLivenessCheck(stall_grace_seconds=100)
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.containers.append(
            ContainerInfo(
                container_id="c1",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        with (
            patch("routes.pipelines.has_live_pipeline_driver", return_value=True),
            patch("driver_heartbeat.spawn_age_seconds", return_value=9999.0),
        ):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_live_persisted_agents_exempt(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        check = DriverLivenessCheck(stall_grace_seconds=100)
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="c1",
                started_at=datetime.now(UTC),
            )
        )
        with (
            patch("routes.pipelines.has_live_pipeline_driver", return_value=True),
            patch("driver_heartbeat.spawn_age_seconds", return_value=9999.0),
        ):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_young_phase_exempt_from_no_progress(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        check = DriverLivenessCheck(stall_grace_seconds=600)
        pipeline = _make_pipeline(phase_started_seconds_ago=30)
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=True):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_recent_spawn_exempt(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        driver_heartbeat.record_spawn(PID)
        check = DriverLivenessCheck(stall_grace_seconds=600)
        pipeline = _make_pipeline()
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=True):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# DriverLivenessCheck — degraded modes
# ---------------------------------------------------------------------------


class TestDriverLivenessDegradedModes:
    def test_driver_dead_fires_after_grace(self):
        _fresh_registry()
        check = DriverLivenessCheck(dead_grace_seconds=0)
        pipeline = _make_pipeline()
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=False):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert result.details["mode"] == "driver_dead"
        assert result.details["pipeline_id"] == PID

    def test_driver_dead_within_grace_is_healthy(self):
        _fresh_registry()
        check = DriverLivenessCheck(dead_grace_seconds=300)
        pipeline = _make_pipeline()
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=False):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_driver_dead_clock_resets_when_thread_returns(self):
        _fresh_registry()
        check = DriverLivenessCheck(dead_grace_seconds=300, stall_grace_seconds=10**6)
        pipeline = _make_pipeline()
        driver_heartbeat.record_tick(PID)
        driver_heartbeat.record_spawn(PID)
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=False):
            check.run(_make_context(pipeline))
        assert (PID, "driver_dead") in check._first_observed
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=True):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY
        assert (PID, "driver_dead") not in check._first_observed

    def test_driver_hung_fires_on_stale_tick(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        check = DriverLivenessCheck(stall_grace_seconds=0)
        pipeline = _make_pipeline()
        with (
            patch("routes.pipelines.has_live_pipeline_driver", return_value=True),
            patch("driver_heartbeat.tick_age_seconds", return_value=9999.0),
        ):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.DEGRADED
        assert result.details["mode"] == "driver_hung"

    def test_no_tick_stamp_uses_observation_clock_not_immediate_fire(self):
        """After an orchestrator restart the registry is empty; the check
        must start a grace window, not fire on the first sweep."""
        _fresh_registry()
        check = DriverLivenessCheck(stall_grace_seconds=600)
        pipeline = _make_pipeline()
        with patch("routes.pipelines.has_live_pipeline_driver", return_value=True):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.HEALTHY

    def test_driver_no_progress_fires_when_spawn_stale(self):
        _fresh_registry()
        driver_heartbeat.record_tick(PID)
        check = DriverLivenessCheck(stall_grace_seconds=100)
        pipeline = _make_pipeline()
        with (
            patch("routes.pipelines.has_live_pipeline_driver", return_value=True),
            patch("driver_heartbeat.spawn_age_seconds", return_value=9999.0),
        ):
            result = check.run(_make_context(pipeline))
        assert result.status == HealthStatus.DEGRADED
        assert result.details["mode"] == "driver_no_progress"
        assert result.details["spawn_age_s"] == 9999.0

    def test_driver_no_progress_without_any_spawn_stamp_uses_observation_clock(self):
        _fresh_registry()
        check = DriverLivenessCheck(stall_grace_seconds=0)
        pipeline = _make_pipeline()
        with (
            patch("routes.pipelines.has_live_pipeline_driver", return_value=True),
            patch("driver_heartbeat.tick_age_seconds", return_value=0.0),
            patch("driver_heartbeat.spawn_age_seconds", return_value=None),
        ):
            first = check.run(_make_context(pipeline))
            second = check.run(_make_context(pipeline))
        # grace 0: the first observation already exceeds it on the second run
        assert second.status == HealthStatus.DEGRADED
        assert second.details["mode"] == "driver_no_progress"
        # first run establishes the clock; with grace 0 it may fire
        # immediately too, but must never crash
        assert first.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)


# ---------------------------------------------------------------------------
# KubernetesMonitor._handle_driver_liveness_results
# ---------------------------------------------------------------------------


def _degraded_result(mode: str = "driver_no_progress"):
    from health_checks.types import HealthResult, HealthTier

    return HealthResult(
        status=HealthStatus.DEGRADED,
        check_name="driver_liveness",
        tier=HealthTier.PROGRAMMATIC,
        reasoning="wedged",
        action=HealthAction.ALERT,
        details={"pipeline_id": PID, "phase": "implement", "mode": mode},
    )


def _make_monitor():
    from kubernetes_monitor import KubernetesMonitor

    return KubernetesMonitor(k8s_client=MagicMock())


class TestDriverLivenessEscalation:
    def test_degraded_result_broadcasts_and_persists_decision(self):
        monitor = _make_monitor()
        pipeline = _make_pipeline()
        store = MagicMock()
        with (
            patch.object(monitor, "_broadcast_driver_liveness_alert") as mock_broadcast,
            patch("routes.pipelines._persist_hitl_decision") as mock_persist,
        ):
            monitor._handle_driver_liveness_results([_degraded_result()], pipeline, store)
        mock_broadcast.assert_called_once()
        mock_persist.assert_called_once()
        _, kwargs = mock_persist.call_args
        assert kwargs["context"] == DRIVER_LIVENESS_HITL_CONTEXT
        assert DRIVER_LIVENESS_RETRY_OPTION in kwargs["options"]

    def test_realert_throttled(self):
        monitor = _make_monitor()
        pipeline = _make_pipeline()
        store = MagicMock()
        with (
            patch.object(monitor, "_broadcast_driver_liveness_alert") as mock_broadcast,
            patch("routes.pipelines._persist_hitl_decision") as mock_persist,
        ):
            monitor._handle_driver_liveness_results([_degraded_result()], pipeline, store)
            monitor._handle_driver_liveness_results([_degraded_result()], pipeline, store)
        assert mock_broadcast.call_count == 1
        assert mock_persist.call_count == 1

    def test_pending_driver_liveness_decision_dedupes(self):
        monitor = _make_monitor()
        pipeline = _make_pipeline()
        decision = pipeline.add_decision(
            question="q?",
            options=[DRIVER_LIVENESS_RETRY_OPTION],
            phase=PipelinePhase.IMPLEMENT,
        )
        decision.context = DRIVER_LIVENESS_HITL_CONTEXT
        store = MagicMock()
        with (
            patch.object(monitor, "_broadcast_driver_liveness_alert") as mock_broadcast,
            patch("routes.pipelines._persist_hitl_decision") as mock_persist,
        ):
            monitor._handle_driver_liveness_results([_degraded_result()], pipeline, store)
        mock_broadcast.assert_not_called()
        mock_persist.assert_not_called()

    def test_healthy_results_ignored(self):
        from health_checks.types import HealthResult, HealthTier

        monitor = _make_monitor()
        pipeline = _make_pipeline()
        healthy = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="driver_liveness",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="fine",
        )
        with patch.object(monitor, "_broadcast_driver_liveness_alert") as mock_broadcast:
            monitor._handle_driver_liveness_results([healthy], pipeline, MagicMock())
        mock_broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Resolve-time dispatch
# ---------------------------------------------------------------------------


class TestDriverLivenessResolutionDispatch:
    def _decision(self, context: str = DRIVER_LIVENESS_HITL_CONTEXT):
        decision = MagicMock()
        decision.context = context
        decision.phase = PipelinePhase.IMPLEMENT
        decision.id = "decision-1"
        return decision

    def test_foreign_context_passes_through(self):
        from routes.decisions import _maybe_dispatch_driver_liveness_resolution

        result = _maybe_dispatch_driver_liveness_resolution(
            PID, self._decision(context="other"), DRIVER_LIVENESS_RETRY_OPTION
        )
        assert result is None

    def test_dismiss_is_record_only(self):
        from routes.decisions import _maybe_dispatch_driver_liveness_resolution

        result = _maybe_dispatch_driver_liveness_resolution(
            PID, self._decision(), DRIVER_LIVENESS_DISMISS_OPTION
        )
        assert result["action"] == "driver_liveness_dismiss"
        assert result["success"] is True

    def test_free_form_reply_passes_through(self):
        from routes.decisions import _maybe_dispatch_driver_liveness_resolution

        result = _maybe_dispatch_driver_liveness_resolution(PID, self._decision(), "some reply")
        assert result is None

    def test_retry_phase_routes_to_restart_executor(self):
        import routes.decisions._handlers as handlers_mod
        from routes.decisions import _maybe_dispatch_driver_liveness_resolution

        with patch.object(
            handlers_mod,
            "_execute_restart_phase",
            return_value={"action": "restart_phase", "success": True},
        ) as mock_exec:
            result = _maybe_dispatch_driver_liveness_resolution(
                PID, self._decision(), DRIVER_LIVENESS_RETRY_OPTION
            )
        mock_exec.assert_called_once()
        assert result["success"] is True
