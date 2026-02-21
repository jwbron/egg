"""Tests for Tier 1 health checks."""

import json
import sys
import tempfile
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
from health_checks.tier1.container_liveness import ContainerLivenessCheck
from health_checks.tier1.phase_output import PhaseOutputPresenceCheck
from health_checks.tier1.startup_state import StartupStateCheck
from health_checks.tier1.state_consistency import StateConsistencyCheck
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
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


def _make_pipeline_with_running_agent(container_id: str = "abc123") -> Pipeline:
    """Return a RUNNING pipeline with one RUNNING coder agent."""
    pipeline = _make_pipeline()
    phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    phase.status = PipelineStatus.RUNNING
    phase.started_at = datetime.utcnow()

    phase.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name="egg-coder-issue-99",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
    )
    phase.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=datetime.utcnow(),
        )
    )
    return pipeline


def _make_pipeline_with_completed_agent(
    commit: str | None = None,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    """Return a pipeline with a completed coder agent."""
    pipeline = _make_pipeline(phase=phase)
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = PipelineStatus.RUNNING
    phase_exec.started_at = datetime.utcnow()
    phase_exec.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.COMPLETE,
            container_id="coder-123",
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            commit=commit,
        )
    )
    return pipeline


def _make_context(
    pipeline: Pipeline,
    docker_client: MagicMock | None = None,
    trigger: str = "on_demand",
) -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=Path("/tmp/test-repo"),
        trigger=trigger,
        docker_client=docker_client,
    )


# ===========================================================================
# Tests: ContainerLivenessCheck
# ===========================================================================


class TestContainerLivenessCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(ContainerLivenessCheck(), HealthCheck)

    def test_healthy_when_not_running(self):
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_no_containers(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_all_alive(self):
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.container_id = "abc123"
        mock_docker.list_containers.return_value = [mock_container]

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_failed_when_container_missing(self):
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []  # No live containers

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE
        assert "missing" in result.reasoning.lower()
        assert "abc123" in result.details["missing_container_ids"]


# ===========================================================================
# Tests: StartupStateCheck
# ===========================================================================


class TestStartupStateCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(StartupStateCheck(), HealthCheck)

    def test_healthy_when_not_running(self):
        pipeline = _make_pipeline(status=PipelineStatus.PENDING)
        ctx = _make_context(pipeline)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_all_alive(self):
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.container_id = "abc123"
        mock_docker.list_containers.return_value = [mock_container]

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_failed_when_stale_container(self):
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE
        assert "stale" in result.reasoning.lower()

    def test_failed_when_stale_agent(self):
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        # Both stale containers and stale agents should be reported
        assert result.details["stale_containers"] == ["abc123"]
        assert len(result.details["stale_agents"]) == 1


# ===========================================================================
# Tests: PhaseOutputPresenceCheck
# ===========================================================================


class TestPhaseOutputPresenceCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(PhaseOutputPresenceCheck(), HealthCheck)

    def test_healthy_when_pending(self):
        pipeline = _make_pipeline()
        # No phase execution initialized yet
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_no_agents_complete(self):
        pipeline = _make_pipeline_with_running_agent()
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "no agents have completed" in result.reasoning.lower()

    def test_healthy_when_agent_reported_commit(self):
        pipeline = _make_pipeline_with_completed_agent(commit="abc123")
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "reported commits" in result.reasoning.lower()

    def test_degraded_when_no_commits(self):
        """Agents completed but no commits — the issue-835 scenario."""
        pipeline = _make_pipeline_with_completed_agent(commit=None)
        ctx = _make_context(pipeline)
        with patch("subprocess.run") as mock_run:
            # Simulate no new commits on the branch
            mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert "no commits" in result.reasoning.lower()

    def test_healthy_when_git_has_commits(self):
        """Even if agent didn't report commit, git log shows new commits."""
        pipeline = _make_pipeline_with_completed_agent(commit=None)
        ctx = _make_context(pipeline)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="3\n", returncode=0)
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_plan_phase_healthy_when_plan_exists(self):
        pipeline = _make_pipeline_with_completed_agent(
            commit=None, phase=PipelinePhase.PLAN,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a plan artifact
            state_dir = Path(tmpdir) / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "99-plan.md").write_text("# Plan\nSome plan content")

            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_plan_phase_degraded_when_no_plan(self):
        pipeline = _make_pipeline_with_completed_agent(
            commit=None, phase=PipelinePhase.PLAN,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            # No .egg-state directory
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED

    def test_refine_phase_always_healthy(self):
        """REFINE phase has no artifact requirements."""
        pipeline = _make_pipeline_with_completed_agent(
            commit=None, phase=PipelinePhase.REFINE,
        )
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY


# ===========================================================================
# Tests: StateConsistencyCheck
# ===========================================================================


class TestStateConsistencyCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(StateConsistencyCheck(), HealthCheck)

    def test_healthy_when_not_running(self):
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_consistent(self):
        """Pipeline RUNNING, agents RUNNING, containers alive."""
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.container_id = "abc123"
        mock_docker.list_containers.return_value = [mock_container]

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_failed_when_running_agent_missing_container(self):
        """Agent is RUNNING but container is not in Docker."""
        pipeline = _make_pipeline_with_running_agent("abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE

    def test_failed_when_container_failed_but_agent_running(self):
        """Container has FAILED status but agent is still RUNNING."""
        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING

        container_id = "abc123"
        phase.containers.append(
            ContainerInfo(
                container_id=container_id,
                container_name="egg-coder-issue-99",
                status=ContainerStatus.FAILED,
                exit_code=1,
            )
        )
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=container_id,
            )
        )

        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.FAILED

    def test_degraded_when_complete_agents_but_pending_tasks(self):
        """Agent COMPLETE but contract has pending tasks."""
        pipeline = _make_pipeline_with_completed_agent(commit="abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        # Create a context with contract data showing pending tasks
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test-repo"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        # Inject contract into agent_outputs cache
        contract_data = {
            "tasks": [
                {"id": "task-1", "status": "pending"},
                {"id": "task-2", "status": "complete"},
            ]
        }
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}

        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert "pending" in result.reasoning.lower()

    def test_healthy_when_no_contract(self):
        """No contract data — should not report issues."""
        pipeline = _make_pipeline_with_completed_agent(commit="abc123")
        mock_docker = MagicMock()
        mock_docker.list_containers.return_value = []

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test-repo"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        # No contract in agent_outputs
        ctx._agent_outputs = {}

        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
