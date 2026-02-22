"""Integration tests for health check lifecycle hooks.

Tests the health check integration points in:
- routes/phases.py: PHASE_COMPLETE blocking (409), force flag bypass
- Container monitor: RUNTIME_TICK dispatch patterns
- Multi-agent: WAVE_COMPLETE with real runner behavior
- routes/health.py: aggregate status with mixed results, empty results
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from flask import Flask
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
)
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from routes.health import health_bp

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


def _make_result(
    status: HealthStatus = HealthStatus.HEALTHY,
    action: HealthAction = HealthAction.CONTINUE,
    name: str = "test-check",
) -> HealthResult:
    return HealthResult(
        status=status,
        check_name=name,
        tier=HealthTier.PROGRAMMATIC,
        reasoning="test",
        action=action,
    )


# ---------------------------------------------------------------------------
# Flask test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(health_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


# ===========================================================================
# Pipeline Health Endpoint — aggregate status edge cases
# ===========================================================================


class TestPipelineHealthEndpointAggregation:
    """Test aggregate status calculation in /pipelines/<id>/health."""

    @patch("state_store.get_state_store")
    def test_mixed_failed_and_degraded_returns_failed(self, mock_get_store, app, client):
        """When both FAILED and DEGRADED results exist, aggregate is 'failed'."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.DEGRADED, HealthAction.ALERT, "check-1"),
            _make_result(HealthStatus.FAILED, HealthAction.FAIL_PIPELINE, "check-2"),
            _make_result(HealthStatus.HEALTHY, HealthAction.CONTINUE, "check-3"),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["status"] == "failed"
        assert len(data["results"]) == 3

    @patch("state_store.get_state_store")
    def test_degraded_only_returns_degraded(self, mock_get_store, app, client):
        """When only DEGRADED (no FAILED), aggregate is 'degraded'."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.HEALTHY, name="check-1"),
            _make_result(HealthStatus.DEGRADED, HealthAction.ALERT, "check-2"),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        data = json.loads(resp.data)
        assert data["status"] == "degraded"

    @patch("state_store.get_state_store")
    def test_all_healthy_returns_healthy(self, mock_get_store, app, client):
        """When all results are HEALTHY, aggregate is 'healthy'."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.HEALTHY, name="check-1"),
            _make_result(HealthStatus.HEALTHY, name="check-2"),
            _make_result(HealthStatus.HEALTHY, name="check-3"),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        data = json.loads(resp.data)
        assert data["status"] == "healthy"
        assert len(data["results"]) == 3

    @patch("state_store.get_state_store")
    def test_empty_results_returns_healthy(self, mock_get_store, app, client):
        """Empty results list returns 'healthy' aggregate."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        data = json.loads(resp.data)
        assert data["status"] == "healthy"
        assert data["results"] == []

    @patch("state_store.get_state_store")
    def test_results_serialized_correctly(self, mock_get_store, app, client):
        """Each result is serialized via to_dict()."""
        result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="phase_output_presence",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="No commits found",
            action=HealthAction.ALERT,
            details={"completed_agent_count": 2},
        )
        mock_runner = MagicMock()
        mock_runner.run.return_value = [result]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        data = json.loads(resp.data)
        r = data["results"][0]
        assert r["check_name"] == "phase_output_presence"
        assert r["status"] == "degraded"
        assert r["action"] == "alert"
        assert r["details"]["completed_agent_count"] == 2
        assert r["timestamp"].endswith("Z")


# ===========================================================================
# Pipeline Health Endpoint — error handling
# ===========================================================================


class TestPipelineHealthEndpointErrors:
    """Test error scenarios for the health endpoint."""

    def test_runner_not_initialized(self, app, client):
        """503 when HEALTH_CHECK_RUNNER not in config."""
        app.config.pop("HEALTH_CHECK_RUNNER", None)
        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 503
        data = json.loads(resp.data)
        assert data["status"] == "unknown"

    @patch("state_store.get_state_store")
    def test_pipeline_not_found(self, mock_get_store, app, client):
        """404 when pipeline doesn't exist."""
        from state_store import PipelineNotFoundError

        mock_runner = MagicMock()
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("issue-99")
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 404

    @patch("state_store.get_state_store")
    def test_store_load_generic_error(self, mock_get_store, app, client):
        """500 when store.load_pipeline raises a generic exception."""
        mock_runner = MagicMock()
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = RuntimeError("Disk error")
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 500
        assert "Disk error" in json.loads(resp.data)["error"]

    @patch("state_store.get_state_store")
    def test_runner_execution_error(self, mock_get_store, app, client):
        """500 when runner.run() raises an exception."""
        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Check crashed")
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = _make_pipeline()
        mock_get_store.return_value = mock_store

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 500
        assert "Check crashed" in json.loads(resp.data)["error"]


# ===========================================================================
# Basic health/ready/live endpoints
# ===========================================================================


class TestBasicEndpoints:
    """Test /health, /ready, /live endpoints."""

    def test_health_returns_service_info(self, app, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "healthy"
        assert data["service"] == "egg-orchestrator"
        assert "timestamp" in data
        assert "components" in data

    def test_ready_returns_true(self, app, client):
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 200
        assert json.loads(resp.data)["ready"] is True

    def test_live_returns_true(self, app, client):
        resp = client.get("/api/v1/live")
        assert resp.status_code == 200
        assert json.loads(resp.data)["alive"] is True


# ===========================================================================
# Container Monitor Health Integration
# ===========================================================================


class TestContainerMonitorHealthIntegrationExtra:
    """Additional tests for container monitor health check integration."""

    @patch("state_store.get_state_store")
    def test_multiple_running_pipelines_all_checked(self, mock_get_store):
        """Health checks run for each running pipeline."""
        from container_monitor import ContainerMonitor

        monitor = ContainerMonitor.__new__(ContainerMonitor)
        monitor.docker_client = MagicMock()
        monitor._health_check_runner = MagicMock()
        monitor._health_check_repo_path = "/tmp/repo"

        p1 = _make_pipeline(status=PipelineStatus.RUNNING)
        p1.id = "pipeline-1"
        p2 = _make_pipeline(status=PipelineStatus.RUNNING)
        p2.id = "pipeline-2"

        mock_store = MagicMock()
        mock_store.list_pipelines.return_value = ["pipeline-1", "pipeline-2"]
        mock_store.load_pipeline.side_effect = lambda pid: p1 if pid == "pipeline-1" else p2
        mock_get_store.return_value = mock_store

        monitor._run_health_checks_on_change()
        assert monitor._health_check_runner.run.call_count == 2

    @patch("state_store.get_state_store")
    def test_empty_pipeline_list_no_checks(self, mock_get_store):
        """No health checks run when no pipelines exist."""
        from container_monitor import ContainerMonitor

        monitor = ContainerMonitor.__new__(ContainerMonitor)
        monitor.docker_client = MagicMock()
        monitor._health_check_runner = MagicMock()
        monitor._health_check_repo_path = "/tmp/repo"

        mock_store = MagicMock()
        mock_store.list_pipelines.return_value = []
        mock_get_store.return_value = mock_store

        monitor._run_health_checks_on_change()
        monitor._health_check_runner.run.assert_not_called()

    @patch("state_store.get_state_store")
    def test_state_store_exception_handled(self, mock_get_store):
        """Exceptions from get_state_store don't crash monitor."""
        from container_monitor import ContainerMonitor

        monitor = ContainerMonitor.__new__(ContainerMonitor)
        monitor.docker_client = MagicMock()
        monitor._health_check_runner = MagicMock()
        monitor._health_check_repo_path = "/tmp/repo"

        mock_get_store.side_effect = RuntimeError("Store unavailable")
        # Should not raise
        monitor._run_health_checks_on_change()

    @patch("state_store.get_state_store")
    def test_per_pipeline_error_doesnt_stop_iteration(self, mock_get_store):
        """If one pipeline fails, others still get checked."""
        from container_monitor import ContainerMonitor

        monitor = ContainerMonitor.__new__(ContainerMonitor)
        monitor.docker_client = MagicMock()
        monitor._health_check_runner = MagicMock()
        monitor._health_check_repo_path = "/tmp/repo"

        mock_store = MagicMock()
        mock_store.list_pipelines.return_value = ["pipeline-1", "pipeline-2"]

        def load_side_effect(pid):
            if pid == "pipeline-1":
                raise RuntimeError("Corrupt pipeline")
            return _make_pipeline(status=PipelineStatus.RUNNING)

        mock_store.load_pipeline.side_effect = load_side_effect
        mock_get_store.return_value = mock_store

        monitor._run_health_checks_on_change()
        # Pipeline-2 should still get checked
        assert monitor._health_check_runner.run.call_count == 1


# ===========================================================================
# Multi-agent WAVE_COMPLETE health checks
# ===========================================================================


class TestMultiAgentWaveHealthExtra:
    """Additional wave health check integration tests."""

    def test_alert_action_does_not_terminate_waves(self):
        """ALERT action from health checks should NOT cause wave termination."""
        from multi_agent import MultiAgentExecutor

        executor = MultiAgentExecutor.__new__(MultiAgentExecutor)
        executor.pipeline = _make_pipeline()
        executor.repo_path = Path("/tmp/repo")
        executor.docker_client = None

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.DEGRADED, HealthAction.ALERT),
        ]

        result = executor._run_wave_health_checks(mock_runner, wave_number=1)
        assert result is False  # False = don't terminate

    def test_fail_pipeline_action_terminates_waves(self):
        """FAIL_PIPELINE action should cause wave termination."""
        from multi_agent import MultiAgentExecutor

        executor = MultiAgentExecutor.__new__(MultiAgentExecutor)
        executor.pipeline = _make_pipeline()
        executor.repo_path = Path("/tmp/repo")
        executor.docker_client = None

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.FAILED, HealthAction.FAIL_PIPELINE),
        ]

        result = executor._run_wave_health_checks(mock_runner, wave_number=1)
        assert result is True  # True = terminate waves

    def test_mixed_continue_and_alert_does_not_terminate(self):
        """Mix of CONTINUE and ALERT should not terminate."""
        from multi_agent import MultiAgentExecutor

        executor = MultiAgentExecutor.__new__(MultiAgentExecutor)
        executor.pipeline = _make_pipeline()
        executor.repo_path = Path("/tmp/repo")
        executor.docker_client = None

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.HEALTHY, HealthAction.CONTINUE, "check-1"),
            _make_result(HealthStatus.DEGRADED, HealthAction.ALERT, "check-2"),
        ]

        result = executor._run_wave_health_checks(mock_runner, wave_number=1)
        assert result is False

    def test_exception_in_health_check_returns_false(self):
        """Exception during health check should not terminate waves."""
        from multi_agent import MultiAgentExecutor

        executor = MultiAgentExecutor.__new__(MultiAgentExecutor)
        executor.pipeline = _make_pipeline()
        executor.repo_path = Path("/tmp/repo")
        executor.docker_client = None

        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Check crashed")

        result = executor._run_wave_health_checks(mock_runner, wave_number=1)
        assert result is False

    def test_empty_results_does_not_terminate(self):
        """No health check results should not terminate waves."""
        from multi_agent import MultiAgentExecutor

        executor = MultiAgentExecutor.__new__(MultiAgentExecutor)
        executor.pipeline = _make_pipeline()
        executor.repo_path = Path("/tmp/repo")
        executor.docker_client = None

        mock_runner = MagicMock()
        mock_runner.run.return_value = []

        result = executor._run_wave_health_checks(mock_runner, wave_number=1)
        assert result is False


# ===========================================================================
# Phase advance health check integration
# ===========================================================================


class TestPhaseAdvanceHealthCheck:
    """Test PHASE_COMPLETE health check integration in routes/phases.py."""

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_fail_pipeline_blocks_phase_advance(self, mock_get_store, mock_repo_path, app):
        """When health checks return FAIL_PIPELINE, phase advance returns 409."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.FAILED, HealthAction.FAIL_PIPELINE, "check-1"),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code == 409
            data = json.loads(resp.data)
            assert "health" in data.get("message", "").lower()

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_force_flag_bypasses_health_checks(self, mock_get_store, mock_repo_path, app):
        """When force=true, health checks are skipped."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.FAILED, HealthAction.FAIL_PIPELINE),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement", "force": True},
            )
            # Force should bypass health checks
            assert resp.status_code != 409
            mock_runner.run.assert_not_called()

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_healthy_results_allow_phase_advance(self, mock_get_store, mock_repo_path, app):
        """When all health checks pass, phase advance proceeds normally."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.HEALTHY),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_health_check_exception_does_not_block_advance(
        self, mock_get_store, mock_repo_path, app
    ):
        """Exceptions in health checks should not block phase advance."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Check crashed")
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            # Should proceed despite exception
            assert resp.status_code != 409

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_no_runner_allows_advance(self, mock_get_store, mock_repo_path, app):
        """When HEALTH_CHECK_RUNNER is not set, advance proceeds normally."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        # Don't set HEALTH_CHECK_RUNNER
        app.config.pop("HEALTH_CHECK_RUNNER", None)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409

    @patch("routes.phases.get_repo_path", return_value=Path("/tmp/repo"))
    @patch("routes.phases.get_state_store")
    def test_alert_action_allows_advance(self, mock_get_store, mock_repo_path, app):
        """ALERT action should not block phase advance (only FAIL_PIPELINE blocks)."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        mock_runner = MagicMock()
        mock_runner.run.return_value = [
            _make_result(HealthStatus.DEGRADED, HealthAction.ALERT, "check-1"),
        ]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409
