"""Integration tests for health check lifecycle hooks and route endpoints."""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    pipeline_id: str = "issue-99",
    status: PipelineStatus = PipelineStatus.RUNNING,
) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=status,
        current_phase=PipelinePhase.IMPLEMENT,
    )


def _healthy_result(check_name: str = "test_check") -> HealthResult:
    return HealthResult(
        status=HealthStatus.HEALTHY,
        check_name=check_name,
        tier=HealthTier.PROGRAMMATIC,
        reasoning="All good",
    )


def _failed_result(check_name: str = "test_check") -> HealthResult:
    return HealthResult(
        status=HealthStatus.FAILED,
        check_name=check_name,
        tier=HealthTier.PROGRAMMATIC,
        reasoning="Infrastructure down",
        action=HealthAction.FAIL_PIPELINE,
    )


def _degraded_result(check_name: str = "test_check") -> HealthResult:
    return HealthResult(
        status=HealthStatus.DEGRADED,
        check_name=check_name,
        tier=HealthTier.PROGRAMMATIC,
        reasoning="Partial failure",
        action=HealthAction.ALERT,
    )


# ===========================================================================
# Tests: Pipeline health route (/api/v1/pipelines/<id>/health)
# ===========================================================================


@pytest.fixture
def app():
    """Create a test Flask app with the health blueprint."""
    from flask import Flask
    from routes.health import health_bp

    app = Flask(__name__)
    app.register_blueprint(health_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


class TestPipelineHealthEndpoint:
    def test_returns_503_when_runner_not_initialized(self, client, app):
        """Should return 503 when HEALTH_CHECK_RUNNER is not in app config."""
        # Ensure no runner is set
        app.config.pop("HEALTH_CHECK_RUNNER", None)
        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["status"] == "unknown"
        assert "not initialized" in data["message"]

    @patch("state_store.get_state_store")
    def test_returns_404_when_pipeline_not_found(self, mock_get_store, client, app):
        """Should return 404 when pipeline doesn't exist."""
        from state_store import PipelineNotFoundError

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("not found")
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/nonexistent/health")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "not found" in data["error"].lower()

    @patch("state_store.get_state_store")
    def test_returns_200_healthy(self, mock_get_store, client, app):
        """Should return healthy status with results."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_healthy_result()]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pipeline_id"] == "issue-99"
        assert data["status"] == "healthy"
        assert len(data["results"]) == 1
        assert "timestamp" in data

    @patch("state_store.get_state_store")
    def test_returns_200_failed_aggregate(self, mock_get_store, client, app):
        """Aggregate status should reflect worst result."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_healthy_result("a"), _failed_result("b")]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "failed"

    @patch("state_store.get_state_store")
    def test_returns_200_degraded_aggregate(self, mock_get_store, client, app):
        """Degraded results should set aggregate to degraded."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_healthy_result("a"), _degraded_result("b")]
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "degraded"

    @patch("state_store.get_state_store")
    def test_returns_500_on_runner_exception(self, mock_get_store, client, app):
        """Health check runner exception should return 500."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Runner crashed")
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 500
        data = resp.get_json()
        assert "failed" in data["error"].lower()

    @patch("state_store.get_state_store")
    def test_returns_500_on_store_exception(self, mock_get_store, client, app):
        """State store general exception should return 500."""
        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = RuntimeError("Store broken")
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 500

    @patch("state_store.get_state_store")
    def test_empty_results_returns_healthy(self, mock_get_store, client, app):
        """No check results should yield healthy aggregate."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        app.config["HEALTH_CHECK_RUNNER"] = mock_runner

        resp = client.get("/api/v1/pipelines/issue-99/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["results"] == []


class TestBasicHealthEndpoints:
    def test_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["service"] == "egg-orchestrator"

    def test_ready(self, client):
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ready"] is True

    def test_live(self, client):
        resp = client.get("/api/v1/live")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["alive"] is True


# ===========================================================================
# Tests: ContainerMonitor RUNTIME_TICK integration
# ===========================================================================


class TestContainerMonitorHealthIntegration:
    def test_set_health_check_runner(self):
        """set_health_check_runner should store runner and repo_path."""
        from container_monitor import ContainerMonitor

        mock_docker = MagicMock()
        monitor = ContainerMonitor(docker_client=mock_docker)
        mock_runner = MagicMock()
        monitor.set_health_check_runner(mock_runner, "/tmp/repo")

        assert monitor._health_check_runner is mock_runner
        assert monitor._health_check_repo_path == "/tmp/repo"

    def test_run_health_checks_on_change_no_runner(self):
        """Should silently return when no runner is set."""
        from container_monitor import ContainerMonitor

        mock_docker = MagicMock()
        monitor = ContainerMonitor(docker_client=mock_docker)
        # No runner set — should not raise
        monitor._run_health_checks_on_change()

    @patch("state_store.get_state_store")
    def test_run_health_checks_on_change_with_runner(self, mock_get_store):
        """Should call runner.run for each running pipeline."""
        from container_monitor import ContainerMonitor

        mock_docker = MagicMock()
        monitor = ContainerMonitor(docker_client=mock_docker)

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        monitor.set_health_check_runner(mock_runner, "/tmp/repo")

        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.list_pipelines.return_value = ["issue-99"]
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        monitor._run_health_checks_on_change()
        mock_runner.run.assert_called_once()

    @patch("state_store.get_state_store")
    def test_run_health_checks_skips_non_running(self, mock_get_store):
        """Should skip pipelines that are not running."""
        from container_monitor import ContainerMonitor

        mock_docker = MagicMock()
        monitor = ContainerMonitor(docker_client=mock_docker)

        mock_runner = MagicMock()
        mock_runner.run.return_value = []
        monitor.set_health_check_runner(mock_runner, "/tmp/repo")

        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        mock_store = MagicMock()
        mock_store.list_pipelines.return_value = ["issue-99"]
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        monitor._run_health_checks_on_change()
        mock_runner.run.assert_not_called()

    @patch("state_store.get_state_store")
    def test_run_health_checks_exception_handled(self, mock_get_store):
        """Store exceptions should not crash the monitor."""
        from container_monitor import ContainerMonitor

        mock_docker = MagicMock()
        monitor = ContainerMonitor(docker_client=mock_docker)

        mock_runner = MagicMock()
        monitor.set_health_check_runner(mock_runner, "/tmp/repo")

        mock_get_store.side_effect = RuntimeError("Store unavailable")

        # Should not raise
        monitor._run_health_checks_on_change()


# ===========================================================================
# Tests: MultiAgent WAVE_COMPLETE integration
# ===========================================================================


class TestMultiAgentWaveHealthChecks:
    def test_run_wave_health_checks_returns_false_on_healthy(self):
        """Should return False when all checks are healthy."""
        from multi_agent import MultiAgentExecutor

        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.pipeline = pipeline
        mock_dispatcher.repo_path = Path("/tmp/repo")

        orch = MultiAgentExecutor.__new__(MultiAgentExecutor)
        orch.pipeline = pipeline
        orch.repo_path = Path("/tmp/repo")
        orch.dispatcher = mock_dispatcher

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_healthy_result()]

        result = orch._run_wave_health_checks(mock_runner, 1)
        assert result is False

    def test_run_wave_health_checks_returns_true_on_fail_pipeline(self):
        """Should return True when FAIL_PIPELINE action is present."""
        from multi_agent import MultiAgentExecutor

        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.pipeline = pipeline
        mock_dispatcher.repo_path = Path("/tmp/repo")

        orch = MultiAgentExecutor.__new__(MultiAgentExecutor)
        orch.pipeline = pipeline
        orch.repo_path = Path("/tmp/repo")
        orch.dispatcher = mock_dispatcher

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_failed_result()]

        result = orch._run_wave_health_checks(mock_runner, 1)
        assert result is True

    def test_run_wave_health_checks_exception_returns_false(self):
        """Runner exception should return False (don't fail pipeline on check error)."""
        from multi_agent import MultiAgentExecutor

        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.pipeline = pipeline
        mock_dispatcher.repo_path = Path("/tmp/repo")

        orch = MultiAgentExecutor.__new__(MultiAgentExecutor)
        orch.pipeline = pipeline
        orch.repo_path = Path("/tmp/repo")
        orch.dispatcher = mock_dispatcher

        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("Runner crashed")

        result = orch._run_wave_health_checks(mock_runner, 1)
        assert result is False

    def test_run_wave_health_checks_alert_returns_false(self):
        """ALERT action should not fail pipeline."""
        from multi_agent import MultiAgentExecutor

        pipeline = _make_pipeline()
        mock_dispatcher = MagicMock()
        mock_dispatcher.pipeline = pipeline
        mock_dispatcher.repo_path = Path("/tmp/repo")

        orch = MultiAgentExecutor.__new__(MultiAgentExecutor)
        orch.pipeline = pipeline
        orch.repo_path = Path("/tmp/repo")
        orch.dispatcher = mock_dispatcher

        mock_runner = MagicMock()
        mock_runner.run.return_value = [_degraded_result()]

        result = orch._run_wave_health_checks(mock_runner, 1)
        assert result is False


# ===========================================================================
# Tests: HealthResult serialization
# ===========================================================================


class TestHealthResultSerialization:
    def test_to_dict_all_fields(self):
        result = HealthResult(
            status=HealthStatus.FAILED,
            check_name="state_consistency",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="Agent missing container",
            action=HealthAction.FAIL_PIPELINE,
            details={"issues": ["Agent coder missing"], "count": 1},
        )
        d = result.to_dict()
        assert d["status"] == "failed"
        assert d["check_name"] == "state_consistency"
        assert d["tier"] == "tier1"
        assert d["reasoning"] == "Agent missing container"
        assert d["action"] == "fail_pipeline"
        assert d["details"]["count"] == 1
        assert d["timestamp"].endswith("Z")

    def test_to_dict_default_action(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.AGENT,
            reasoning="OK",
        )
        d = result.to_dict()
        assert d["action"] == "continue"

    def test_to_dict_json_serializable(self):
        """to_dict output should be JSON serializable."""
        result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="Some issue",
            details={"nested": {"key": "value"}, "list": [1, 2, 3]},
        )
        d = result.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["status"] == "degraded"


# ===========================================================================
# Tests: EventType additions
# ===========================================================================


class TestHealthCheckEventTypes:
    def test_event_types_exist(self):
        """New EventType values should be defined."""
        from events import EventType

        assert hasattr(EventType, "HEALTH_CHECK")
        assert hasattr(EventType, "HEALTH_CHECK_STARTED")
        assert hasattr(EventType, "HEALTH_CHECK_COMPLETED")
        assert hasattr(EventType, "HEALTH_CHECK_DEGRADED")
        assert hasattr(EventType, "HEALTH_CHECK_FAILED")

    def test_event_type_values(self):
        from events import EventType

        assert EventType.HEALTH_CHECK == "system.health_check"
        assert EventType.HEALTH_CHECK_STARTED == "system.health_check.started"
        assert EventType.HEALTH_CHECK_COMPLETED == "system.health_check.completed"
        assert EventType.HEALTH_CHECK_DEGRADED == "system.health_check.degraded"
        assert EventType.HEALTH_CHECK_FAILED == "system.health_check.failed"


# ===========================================================================
# Tests: End-to-end runner with real Tier 1 checks
# ===========================================================================


class TestEndToEndRunnerWithTier1Checks:
    """Run the full runner with actual Tier 1 check classes."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_all_tier1_checks_healthy_pipeline(self, _):
        """All Tier 1 checks should pass for a well-formed healthy pipeline."""
        from health_checks.tier1.container_liveness import ContainerLivenessCheck
        from health_checks.tier1.startup_state import StartupStateCheck
        from health_checks.tier1.state_consistency import StateConsistencyCheck

        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())
        runner.register(StartupStateCheck())
        runner.register(StateConsistencyCheck())

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        # All checks should return HEALTHY for a COMPLETE pipeline
        assert all(r.status == HealthStatus.HEALTHY for r in results)

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus", return_value=None)
    def test_tier1_detects_missing_container(self, _):
        """ContainerLivenessCheck should detect missing container in full run."""
        from health_checks.tier1.container_liveness import ContainerLivenessCheck

        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        from models import AgentExecution, AgentExecutionStatus, AgentRole, ContainerInfo, ContainerStatus
        phase.containers.append(
            ContainerInfo(
                container_id="c1",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            )
        )
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="c1",
            )
        )

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 1
        assert results[0].status == HealthStatus.FAILED
        assert worst_action(results) == HealthAction.FAIL_PIPELINE
