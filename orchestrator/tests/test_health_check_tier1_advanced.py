"""Advanced tests for Tier 1 health checks: edge cases and attribute validation."""

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


def _add_agent_and_container(
    pipeline: Pipeline,
    phase: PipelinePhase,
    role: AgentRole,
    agent_status: AgentExecutionStatus,
    container_status: ContainerStatus,
    container_id: str,
    commit: str | None = None,
) -> None:
    """Add an agent and container to a pipeline phase execution."""
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = PipelineStatus.RUNNING
    if not phase_exec.started_at:
        phase_exec.started_at = datetime.utcnow()
    phase_exec.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name=f"egg-{role.value}-{pipeline.id}",
            status=container_status,
        )
    )
    phase_exec.agents.append(
        AgentExecution(
            role=role,
            status=agent_status,
            container_id=container_id,
            started_at=datetime.utcnow(),
            commit=commit,
        )
    )


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


def _mock_docker_with_ids(*container_ids: str) -> MagicMock:
    mock = MagicMock()
    containers = []
    for cid in container_ids:
        c = MagicMock()
        c.container_id = cid
        containers.append(c)
    mock.list_containers.return_value = containers
    return mock


# ===========================================================================
# ContainerLivenessCheck: attribute validation and edge cases
# ===========================================================================


class TestContainerLivenessAttributes:
    def test_name(self):
        assert ContainerLivenessCheck().name == "container_liveness"

    def test_tier(self):
        assert ContainerLivenessCheck().tier == HealthTier.PROGRAMMATIC

    def test_triggers_include_all_lifecycle_events(self):
        triggers = ContainerLivenessCheck().triggers
        assert HealthTrigger.STARTUP in triggers
        assert HealthTrigger.RUNTIME_TICK in triggers
        assert HealthTrigger.WAVE_COMPLETE in triggers
        assert HealthTrigger.PHASE_COMPLETE in triggers
        assert HealthTrigger.ON_DEMAND in triggers


class TestContainerLivenessEdgeCases:
    def test_multiple_containers_partial_missing(self):
        """Two containers expected, one alive, one missing."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c2",
        )
        mock_docker = _mock_docker_with_ids("c1")  # c2 missing
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = ContainerLivenessCheck().run(ctx)

        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE
        assert "c2" in result.details["missing_container_ids"]
        assert "c1" not in result.details["missing_container_ids"]
        assert result.details["expected_running_count"] == 2

    def test_exited_container_not_expected(self):
        """Container with EXITED status should NOT be in expected_running."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()  # No live containers
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = ContainerLivenessCheck().run(ctx)
        # EXITED containers should not be in expected_running
        assert result.status == HealthStatus.HEALTHY

    def test_containers_across_multiple_phases(self):
        """Containers from multiple phases should all be checked."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.PLAN,
            AgentRole.ARCHITECT,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c2",
        )
        mock_docker = _mock_docker_with_ids("c1", "c2")
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "2" in result.reasoning  # "All 2 expected containers are alive"

    def test_pipeline_complete_skips(self):
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "not running" in result.reasoning.lower()

    def test_pipeline_failed_skips(self):
        pipeline = _make_pipeline(status=PipelineStatus.FAILED)
        ctx = _make_context(pipeline)
        result = ContainerLivenessCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY


# ===========================================================================
# StartupStateCheck: edge cases
# ===========================================================================


class TestStartupStateAttributes:
    def test_name(self):
        assert StartupStateCheck().name == "startup_state"

    def test_tier(self):
        assert StartupStateCheck().tier == HealthTier.PROGRAMMATIC

    def test_triggers(self):
        triggers = StartupStateCheck().triggers
        assert HealthTrigger.STARTUP in triggers
        assert HealthTrigger.ON_DEMAND in triggers
        assert HealthTrigger.RUNTIME_TICK not in triggers
        assert HealthTrigger.WAVE_COMPLETE not in triggers


class TestStartupStateEdgeCases:
    def test_agent_without_container_id_skipped(self):
        """Agent with no container_id should be skipped for stale check."""
        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,
            )
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        # Agent without container_id should not be flagged
        assert result.status == HealthStatus.HEALTHY

    def test_container_exited_not_flagged(self):
        """EXITED containers should not be flagged as stale."""
        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.containers.append(
            ContainerInfo(
                container_id="c1",
                container_name="egg-coder",
                status=ContainerStatus.EXITED,
            )
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_stale_containers_and_agents_both_reported(self):
        """Both stale containers and stale agents should be in details."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c2",
        )
        mock_docker = _mock_docker_with_ids()  # None alive
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert len(result.details["stale_containers"]) == 2
        assert len(result.details["stale_agents"]) == 2

    def test_multiple_phases_checked(self):
        """Stale items from multiple phases are detected."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.PLAN,
            AgentRole.ARCHITECT,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c2",
        )
        mock_docker = _mock_docker_with_ids()  # None alive
        ctx = _make_context(pipeline, docker_client=mock_docker)
        result = StartupStateCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert len(result.details["stale_containers"]) == 2


# ===========================================================================
# PhaseOutputPresenceCheck: edge cases
# ===========================================================================


class TestPhaseOutputAttributes:
    def test_name(self):
        assert PhaseOutputPresenceCheck().name == "phase_output_presence"

    def test_tier(self):
        assert PhaseOutputPresenceCheck().tier == HealthTier.PROGRAMMATIC

    def test_triggers(self):
        triggers = PhaseOutputPresenceCheck().triggers
        assert HealthTrigger.WAVE_COMPLETE in triggers
        assert HealthTrigger.PHASE_COMPLETE in triggers
        assert HealthTrigger.ON_DEMAND in triggers
        assert HealthTrigger.STARTUP not in triggers
        assert HealthTrigger.RUNTIME_TICK not in triggers


class TestPhaseOutputEdgeCases:
    def test_multiple_agents_mixed_commits(self):
        """Some agents with commits, some without — should be HEALTHY."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
            commit="abc123",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c2",
            commit=None,
        )
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "1 agent(s) reported commits" in result.reasoning

    def test_agent_with_empty_commit_string(self):
        """Empty string commit should be treated as no commit."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
            commit="",
        )
        ctx = _make_context(pipeline)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED

    def test_git_subprocess_error_returns_degraded(self):
        """If git rev-list fails, should still report DEGRADED."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
            commit=None,
        )
        ctx = _make_context(pipeline)
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT

    def test_plan_phase_architect_output(self):
        """Plan phase should find architect-named files."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.utcnow()
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "architect-output.json").write_text('{"plan": "test"}')

            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "architect" in result.reasoning.lower()

    def test_plan_phase_repo_subdir_state(self):
        """Plan artifacts in repo subdirectory should be found."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.utcnow()
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            # Put state in repo subdirectory
            state_dir = Path(tmpdir) / "repo" / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "99-plan.md").write_text("# Plan")

            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_pr_phase_always_healthy(self):
        """PR phase has no artifact requirements."""
        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PR)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.utcnow()
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
            )
        )
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_implement_degraded_details(self):
        """DEGRADED result should include completed_agent_count and agents_with_commits."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
            commit=None,
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c2",
            commit=None,
        )
        ctx = _make_context(pipeline)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
            result = PhaseOutputPresenceCheck().run(ctx)
        assert result.details["completed_agent_count"] == 2
        assert result.details["agents_with_commits"] == 0

    def test_running_agents_not_counted(self):
        """Only COMPLETE agents should trigger artifact check."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c2",
        )
        ctx = _make_context(pipeline)
        result = PhaseOutputPresenceCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "no agents have completed" in result.reasoning.lower()


# ===========================================================================
# StateConsistencyCheck: edge cases
# ===========================================================================


class TestStateConsistencyAttributes:
    def test_name(self):
        assert StateConsistencyCheck().name == "state_consistency"

    def test_tier(self):
        assert StateConsistencyCheck().tier == HealthTier.PROGRAMMATIC

    def test_triggers(self):
        triggers = StateConsistencyCheck().triggers
        assert HealthTrigger.RUNTIME_TICK in triggers
        assert HealthTrigger.WAVE_COMPLETE in triggers
        assert HealthTrigger.PHASE_COMPLETE in triggers
        assert HealthTrigger.ON_DEMAND in triggers
        assert HealthTrigger.STARTUP not in triggers


class TestStateConsistencyEdgeCases:
    def test_agent_without_container_id_skipped_check1(self):
        """Agent without container_id should not trigger missing-container check."""
        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,
            )
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        ctx._agent_outputs = {}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_agent_without_container_id_skipped_check2(self):
        """Agent without container_id should skip container status mismatch."""
        pipeline = _make_pipeline()
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,
            )
        )
        phase.containers.append(
            ContainerInfo(
                container_id="c1",
                container_name="egg-coder",
                status=ContainerStatus.FAILED,
            )
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        ctx._agent_outputs = {}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_container_exited_agent_running_detected(self):
        """EXITED container with RUNNING agent should be FAILED."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        ctx._agent_outputs = {}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.FAILED

    def test_both_failed_and_degraded_issues(self):
        """When both FAILED and DEGRADED issues exist, severity should be FAILED."""
        pipeline = _make_pipeline()
        # Add RUNNING agent with missing container (FAILED)
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        # Add COMPLETE agent (for contract check — DEGRADED)
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.TESTER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c2",
        )
        mock_docker = _mock_docker_with_ids()  # c1 missing
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {"tasks": [{"id": "t1", "status": "pending"}]}
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE
        assert len(result.details["issues"]) >= 2

    def test_contract_all_complete_tasks(self):
        """All contract tasks complete should not trigger DEGRADED."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {
            "tasks": [
                {"id": "t1", "status": "complete"},
                {"id": "t2", "status": "complete"},
            ]
        }
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_contract_malformed_json(self):
        """Malformed JSON contract should not crash the check."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        ctx._agent_outputs = {"contract.json": "not valid json {{{"}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_contract_tasks_not_list(self):
        """Contract with non-list tasks should not crash."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {"tasks": "not a list"}
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_contract_tasks_with_non_dict_entries(self):
        """Contract with non-dict task entries should be handled."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {"tasks": ["string-entry", 42, {"id": "t1", "status": "pending"}]}
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED

    def test_contract_found_by_filename(self):
        """Contract should be found by filename containing 'contract'."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {"tasks": [{"id": "t1", "status": "pending"}]}
        # Different filename but contains "contract"
        ctx._agent_outputs = {"850-contract-v2.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED

    def test_degraded_only_issues(self):
        """When only DEGRADED issues exist (contract), action should be ALERT."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.COMPLETE,
            ContainerStatus.EXITED,
            "c1",
        )
        mock_docker = _mock_docker_with_ids()
        ctx = _make_context(pipeline, docker_client=mock_docker)
        contract_data = {"tasks": [{"id": "t1", "status": "pending"}]}
        ctx._agent_outputs = {"contract.json": json.dumps(contract_data)}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT

    def test_consistent_pipeline_all_matching(self):
        """Fully consistent pipeline with matching agents, containers, and contract."""
        pipeline = _make_pipeline()
        _add_agent_and_container(
            pipeline,
            PipelinePhase.IMPLEMENT,
            AgentRole.CODER,
            AgentExecutionStatus.RUNNING,
            ContainerStatus.RUNNING,
            "c1",
        )
        mock_docker = _mock_docker_with_ids("c1")
        ctx = _make_context(pipeline, docker_client=mock_docker)
        ctx._agent_outputs = {}
        result = StateConsistencyCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "consistent" in result.reasoning.lower()


# ===========================================================================
# All checks: protocol conformance
# ===========================================================================


class TestAllChecksProtocol:
    """Verify all Tier 1 checks conform to the HealthCheck protocol."""

    def test_container_liveness(self):
        assert isinstance(ContainerLivenessCheck(), HealthCheck)

    def test_startup_state(self):
        assert isinstance(StartupStateCheck(), HealthCheck)

    def test_phase_output(self):
        assert isinstance(PhaseOutputPresenceCheck(), HealthCheck)

    def test_state_consistency(self):
        assert isinstance(StateConsistencyCheck(), HealthCheck)
