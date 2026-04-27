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
        assert r["timestamp"].endswith("+00:00")


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
        # Mock the state-store probe (independently covered in
        # test_state_store_wedge_propagation.py) so the test exercises
        # the route response shape, not the live probe behavior — the
        # probe would fail in test envs without /home/egg/.egg-state.
        with patch("routes.health._probe_state_store", return_value=(True, "ok")):
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "healthy"
        assert data["service"] == "egg-orchestrator"
        assert "timestamp" in data
        assert "components" in data
        # Readiness history fields (issue #1855) — let operators see when
        # the service actually came up vs. a point-in-time snapshot.
        assert data["process_start_time"] is not None
        # First observation is healthy, so healthy_since == process_start_time.
        assert data["healthy_since"] == data["process_start_time"]
        assert data["last_unhealthy_at"] is None
        assert isinstance(data["recent_transitions"], list)

    def test_health_recent_transitions_accumulate(self, app, client):
        # Two successive hits should not double-record a transition —
        # the service has been healthy the whole time.
        with patch("routes.health._probe_state_store", return_value=(True, "ok")):
            client.get("/api/v1/health")
            resp = client.get("/api/v1/health")
        data = json.loads(resp.data)
        # Still exactly one transition (the initial healthy one).
        healthy_transitions = [t for t in data["recent_transitions"] if t["state"] == "healthy"]
        assert len(healthy_transitions) == 1

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
    """Additional tests for container monitor health check integration.

    KubernetesMonitor fires RUNTIME_TICK checks via _run_runtime_tick_checks
    when pod state transitions are detected.  The underlying health-check
    runner logic is tested in test_health_checks.py and the
    KubernetesMonitor's check_container_health is tested in
    test_kubernetes_monitor.py.
    """

    pass


# ===========================================================================
# Phase advance health check integration
# ===========================================================================


class TestPhaseAdvanceHealthCheck:
    """Test PHASE_COMPLETE health check integration in routes/phases.py."""

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_fail_pipeline_blocks_phase_advance(self, mock_get_store_for_pipeline, app):
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
        mock_store.repo_path = Path("/tmp/repo")
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code == 409
            data = json.loads(resp.data)
            assert "health" in data.get("message", "").lower()

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_flag_bypasses_health_checks(
        self, mock_get_store_for_pipeline, mock_get_lock, mock_thread_cls, app
    ):
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
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement", "force": True},
            )
            # Force should bypass health checks
            assert resp.status_code != 409
            mock_runner.run.assert_not_called()

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_healthy_results_allow_phase_advance(
        self, mock_get_store_for_pipeline, mock_get_lock, mock_thread_cls, app
    ):
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
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_health_check_exception_does_not_block_advance(
        self, mock_get_store_for_pipeline, mock_get_lock, mock_thread_cls, app
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
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            # Should proceed despite exception
            assert resp.status_code != 409

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_no_runner_allows_advance(
        self, mock_get_store_for_pipeline, mock_get_lock, mock_thread_cls, app
    ):
        """When HEALTH_CHECK_RUNNER is not set, advance proceeds normally."""
        from routes.phases import phases_bp

        app.register_blueprint(phases_bp)

        # Don't set HEALTH_CHECK_RUNNER
        app.config.pop("HEALTH_CHECK_RUNNER", None)

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE

        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409

    @patch("routes.phases.threading.Thread")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_alert_action_allows_advance(
        self, mock_get_store_for_pipeline, mock_get_lock, mock_thread_cls, app
    ):
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
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store_for_pipeline.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        with app.test_client() as client:
            resp = client.post(
                "/api/v1/pipelines/issue-99/phase",
                json={"target_phase": "implement"},
            )
            assert resp.status_code != 409
