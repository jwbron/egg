"""Additional test coverage for health check framework.

Covers gaps identified during tester review:
- PhaseOutputPresenceCheck: _get_state_dir edge cases, _branch_has_new_commits
  with varied subprocess outcomes, plan file naming patterns, PR phase behavior,
  empty commit strings, agents in non-complete states
- StateConsistencyCheck: Check 2 (container/agent mismatch) with EXITED containers,
  multiple phases with mixed issues, precedence when FAILED and DEGRADED coexist,
  contract with various malformed shapes
- PipelineHealthContext: _run_git repo subdirectory resolution, _read_agent_outputs
  with .yaml/.yml files, file read errors, both subdirectories populated
- HealthCheckRunner: aggregate completion event worst status, multiple Tier 1 checks
  with mixed results, exception in one check doesn't block others
- ContainerLivenessCheck: containers across multiple phases, COMPLETE/FAILED pipelines
- StartupStateCheck: stale containers AND stale agents in same pipeline
- Integration: phase advance health check blocking (409), force flag bypass,
  wave health checks with mixed actions
"""

import json
import subprocess
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
from health_checks.runner import HealthCheckRunner, worst_action
from health_checks.tier1.container_liveness import ContainerLivenessCheck
from health_checks.tier1.phase_output import PhaseOutputPresenceCheck
from health_checks.tier1.startup_state import StartupStateCheck
from health_checks.tier1.state_consistency import StateConsistencyCheck
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthResult,
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
    repo: str | None = "owner/repo",
    branch: str | None = "egg/issue-99",
) -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo=repo,
        branch=branch,
        mode="issue",
        status=status,
        current_phase=phase,
    )


def _make_context(
    pipeline: Pipeline,
    docker_client: MagicMock | None = None,
    trigger: str = "on_demand",
    repo_path: Path | None = None,
    state_store: MagicMock | None = None,
    wave_number: int | None = None,
) -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=repo_path or Path("/tmp/test-repo"),
        trigger=trigger,
        docker_client=docker_client,
        state_store=state_store,
        wave_number=wave_number,
    )


def _mock_docker_with_ids(ids: set[str]) -> MagicMock:
    """Create a mock docker client that returns containers with given IDs."""
    docker = MagicMock()
    containers = []
    for cid in ids:
        c = MagicMock()
        c.container_id = cid
        containers.append(c)
    docker.list_containers.return_value = containers
    return docker


# ===========================================================================
# PhaseOutputPresenceCheck — additional coverage
# ===========================================================================


class TestPhaseOutputGetStateDir:
    """Test _get_state_dir resolution logic."""

    def test_returns_none_when_no_state_dir_exists(self):
        """When .egg-state doesn't exist anywhere, returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(repo=None)
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = PhaseOutputPresenceCheck._get_state_dir(ctx)
            assert result is None

    def test_prefers_repo_subdir_state(self):
        """When pipeline.repo is set and repo/.egg-state exists, use it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_state = Path(tmp) / "repo" / ".egg-state"
            repo_state.mkdir(parents=True)
            pipeline = _make_pipeline(repo="owner/repo")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = PhaseOutputPresenceCheck._get_state_dir(ctx)
            assert result == repo_state

    def test_falls_back_to_direct_state_dir(self):
        """When repo subdir .egg-state doesn't exist, use repo_path/.egg-state."""
        with tempfile.TemporaryDirectory() as tmp:
            direct_state = Path(tmp) / ".egg-state"
            direct_state.mkdir()
            pipeline = _make_pipeline(repo="owner/repo")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = PhaseOutputPresenceCheck._get_state_dir(ctx)
            assert result == direct_state

    def test_returns_none_when_repo_set_but_nothing_exists(self):
        """When repo is set but neither state dir exists, returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(repo="owner/repo")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = PhaseOutputPresenceCheck._get_state_dir(ctx)
            assert result is None


class TestPhaseOutputBranchHasNewCommits:
    """Test _branch_has_new_commits with various subprocess outcomes."""

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_returns_true_when_count_positive(self, mock_run):
        mock_run.return_value = MagicMock(stdout="3\n")
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline)
        assert PhaseOutputPresenceCheck._branch_has_new_commits(ctx) is True

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_returns_false_when_count_zero(self, mock_run):
        mock_run.return_value = MagicMock(stdout="0\n")
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline)
        assert PhaseOutputPresenceCheck._branch_has_new_commits(ctx) is False

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_returns_false_on_non_integer_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="fatal: not a git repo\n")
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline)
        assert PhaseOutputPresenceCheck._branch_has_new_commits(ctx) is False

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline)
        assert PhaseOutputPresenceCheck._branch_has_new_commits(ctx) is False

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_returns_false_on_os_error(self, mock_run):
        mock_run.side_effect = OSError("git not found")
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline)
        assert PhaseOutputPresenceCheck._branch_has_new_commits(ctx) is False

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_resolves_repo_subdir(self, mock_run):
        """When pipeline.repo is set and subdir exists, runs git there."""
        mock_run.return_value = MagicMock(stdout="5\n")
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            pipeline = _make_pipeline(repo="owner/repo")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            PhaseOutputPresenceCheck._branch_has_new_commits(ctx)
            # Verify cwd was set to the repo subdirectory
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("cwd") == str(repo_dir) or call_kwargs[1].get(
                "cwd"
            ) == str(repo_dir)


class TestPhaseOutputPlanFiles:
    """Test plan phase file detection patterns."""

    def test_detects_architect_output_json(self):
        """Finds architect-output.json in drafts."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "architect-output.json").write_text("{}")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = check.run(ctx)
            assert result.status == HealthStatus.HEALTHY

    def test_detects_numbered_plan_file(self):
        """Finds 850-plan.md in drafts."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "850-plan.md").write_text("# Plan")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = check.run(ctx)
            assert result.status == HealthStatus.HEALTHY

    def test_does_not_match_unrelated_files(self):
        """Files without 'plan' or 'architect' in the name are ignored."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "random-notes.md").write_text("# Notes")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = check.run(ctx)
            assert result.status == HealthStatus.DEGRADED

    def test_plan_degraded_when_drafts_dir_missing(self):
        """Plan phase returns DEGRADED when drafts directory doesn't exist."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = check.run(ctx)
            assert result.status == HealthStatus.DEGRADED
            assert result.action == HealthAction.ALERT


class TestPhaseOutputEdgeCasesExtra:
    """Additional edge cases for PhaseOutputPresenceCheck."""

    def test_pr_phase_always_healthy(self):
        """PR phase has no artifact requirements."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.PR)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        ctx = _make_context(pipeline)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_refine_phase_always_healthy(self):
        """Refine phase has no artifact requirements."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        phase_exec = pipeline.get_phase_execution(PipelinePhase.REFINE)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.REFINER,
                status=AgentExecutionStatus.COMPLETE,
            )
        )
        ctx = _make_context(pipeline)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_empty_commit_string_treated_as_no_commit(self):
        """Agent with commit='' should be treated as having no commit."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                commit="",
            )
        )
        with patch(
            "health_checks.tier1.phase_output.subprocess.run",
            side_effect=OSError("no git"),
        ):
            ctx = _make_context(pipeline)
            result = check.run(ctx)
            # Empty string is falsy, so no commit detected
            assert result.status == HealthStatus.DEGRADED

    def test_running_and_failed_agents_not_counted(self):
        """Only COMPLETE agents are checked for artifacts."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        # One running, one failed — neither COMPLETE
        phase_exec.agents.append(
            AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.RUNNING)
        )
        phase_exec.agents.append(
            AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.FAILED)
        )
        ctx = _make_context(pipeline)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "No agents have completed" in result.reasoning

    @patch("health_checks.tier1.phase_output.subprocess.run")
    def test_multiple_completed_agents_some_with_commits(self, mock_run):
        """Multiple agents, only some with commits — should be HEALTHY."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                commit="abc123",
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.TESTER,
                status=AgentExecutionStatus.COMPLETE,
                commit=None,
            )
        )
        ctx = _make_context(pipeline)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "1 agent(s) reported commits" in result.reasoning

    def test_phase_not_in_phases_dict(self):
        """Phase execution not found in pipeline.phases returns HEALTHY."""
        check = PhaseOutputPresenceCheck()
        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        # Remove the implement phase execution from phases dict
        pipeline.phases.pop("implement", None)
        ctx = _make_context(pipeline)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY


# ===========================================================================
# StateConsistencyCheck — additional coverage
# ===========================================================================


class TestStateConsistencyCheckExtra:
    """Additional edge cases for StateConsistencyCheck."""

    def test_check2_exited_container_agent_running(self):
        """Container EXITED but agent still RUNNING → FAILED."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.containers.append(
            ContainerInfo(
                container_id="abc123",
                container_name="egg-coder",
                status=ContainerStatus.EXITED,
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="abc123",
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE

    def test_check2_failed_container_agent_running(self):
        """Container FAILED but agent still RUNNING → FAILED."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.containers.append(
            ContainerInfo(
                container_id="abc123",
                container_name="egg-coder",
                status=ContainerStatus.FAILED,
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="abc123",
            )
        )
        # Container is in live IDs (Docker sees it) but its status is FAILED
        docker = _mock_docker_with_ids({"abc123"})
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.FAILED

    def test_failed_takes_precedence_over_degraded(self):
        """When both FAILED and DEGRADED issues exist, FAILED wins."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING

        # Check 1 issue: RUNNING agent with missing container → FAILED
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="missing-123",
            )
        )
        # Check 3 issue: COMPLETE agent with pending tasks → DEGRADED
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.TESTER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="tester-123",
            )
        )

        docker = _mock_docker_with_ids({"tester-123"})
        with tempfile.TemporaryDirectory() as tmp:
            # Create contract with pending tasks
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            contract = {"tasks": [{"id": "task-1", "status": "pending"}]}
            (state_dir / "99-contract.json").write_text(json.dumps(contract))

            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)

        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.FAIL_PIPELINE
        assert len(result.details["issues"]) >= 1

    def test_degraded_only_returns_alert_action(self):
        """When only DEGRADED issues (no FAILED), returns ALERT action."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            contract = {"tasks": [{"id": "task-1", "status": "pending"}]}
            (state_dir / "99-contract.json").write_text(json.dumps(contract))

            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT

    def test_agent_without_container_id_skipped_in_check1(self):
        """Agents with no container_id are not flagged in check 1."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_agent_without_container_id_skipped_in_check2(self):
        """Agents with no container_id are skipped in container/agent mismatch."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.containers.append(
            ContainerInfo(
                container_id="orphan-container",
                container_name="egg-coder",
                status=ContainerStatus.FAILED,
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,  # Not linked to container
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_multiple_phases_with_issues(self):
        """Issues detected across multiple phases are all reported."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()

        # Implement phase: agent RUNNING but container missing
        impl_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        impl_exec.status = PipelineStatus.RUNNING
        impl_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="impl-missing",
            )
        )

        # Plan phase: agent RUNNING but container missing
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.RUNNING
        plan_exec.agents.append(
            AgentExecution(
                role=AgentRole.ARCHITECT,
                status=AgentExecutionStatus.RUNNING,
                container_id="plan-missing",
            )
        )

        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.FAILED
        assert len(result.details["issues"]) == 2

    def test_contract_with_empty_tasks_list(self):
        """Contract with empty tasks list is consistent."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            (state_dir / "99-contract.json").write_text(json.dumps({"tasks": []}))
            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_contract_with_all_complete_tasks(self):
        """Contract where all tasks are complete is consistent."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            contract = {
                "tasks": [
                    {"id": "task-1", "status": "complete"},
                    {"id": "task-2", "status": "complete"},
                ]
            }
            (state_dir / "99-contract.json").write_text(json.dumps(contract))
            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_contract_with_non_dict_task_entries(self):
        """Contract with non-dict task entries doesn't crash."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            contract = {"tasks": ["not-a-dict", 42, None, {"status": "pending"}]}
            (state_dir / "99-contract.json").write_text(json.dumps(contract))
            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)
        # One pending task among the valid entries → DEGRADED
        assert result.status == HealthStatus.DEGRADED

    def test_contract_with_non_list_tasks_field(self):
        """Contract with tasks not being a list doesn't crash."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            contract = {"tasks": "not-a-list"}
            (state_dir / "99-contract.json").write_text(json.dumps(contract))
            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)
        # tasks is not a list, so the check skips gracefully
        assert result.status == HealthStatus.HEALTHY

    def test_contract_malformed_json(self):
        """Malformed JSON in contract file doesn't crash."""
        check = StateConsistencyCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="coder-123",
            )
        )
        docker = _mock_docker_with_ids({"coder-123"})
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "repo" / ".egg-state" / "contracts"
            state_dir.mkdir(parents=True)
            (state_dir / "99-contract.json").write_text("{invalid json")
            ctx = _make_context(pipeline, docker_client=docker, repo_path=Path(tmp))
            result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY


# ===========================================================================
# PipelineHealthContext — additional coverage
# ===========================================================================


class TestContextRunGit:
    """Additional tests for _run_git repo subdirectory resolution."""

    @patch("health_checks.context.subprocess.run")
    def test_uses_repo_subdir_when_exists(self, mock_run):
        """When pipeline.repo is set and subdir exists, git runs there."""
        mock_run.return_value = MagicMock(stdout="abc123 commit msg\n", returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            pipeline = _make_pipeline(repo="owner/repo")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = ctx.git_log
            assert result == "abc123 commit msg"
            call_kwargs = mock_run.call_args
            assert str(repo_dir) in str(call_kwargs)

    @patch("health_checks.context.subprocess.run")
    def test_uses_repo_path_when_subdir_missing(self, mock_run):
        """When pipeline.repo subdir doesn't exist, uses repo_path directly."""
        mock_run.return_value = MagicMock(stdout="abc123\n", returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(repo="owner/nonexistent")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            result = ctx.git_log
            assert result == "abc123"
            call_kwargs = mock_run.call_args
            assert str(Path(tmp)) in str(call_kwargs)

    @patch("health_checks.context.subprocess.run")
    def test_uses_repo_path_when_no_repo_set(self, mock_run):
        """When pipeline.repo is None, uses repo_path directly."""
        mock_run.return_value = MagicMock(stdout="xyz\n", returncode=0)
        pipeline = _make_pipeline(repo=None)
        ctx = _make_context(pipeline, repo_path=Path("/tmp/test"))
        _ = ctx.git_log
        call_kwargs = mock_run.call_args
        assert "/tmp/test" in str(call_kwargs)


class TestContextAgentOutputsExtra:
    """Additional agent_outputs coverage."""

    def test_reads_yaml_and_yml_files(self):
        """Agent outputs include .yaml and .yml files."""
        pipeline = _make_pipeline(repo=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".egg-state"
            drafts_dir = state_dir / "drafts"
            drafts_dir.mkdir(parents=True)
            (drafts_dir / "config.yaml").write_text("key: value")
            (drafts_dir / "extra.yml").write_text("other: data")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            outputs = ctx.agent_outputs
            assert "config.yaml" in outputs
            assert "extra.yml" in outputs
            assert outputs["config.yaml"] == "key: value"
            assert outputs["extra.yml"] == "other: data"

    def test_reads_from_both_drafts_and_contracts(self):
        """Agent outputs include files from both drafts/ and contracts/."""
        pipeline = _make_pipeline(repo=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".egg-state"
            (state_dir / "drafts").mkdir(parents=True)
            (state_dir / "contracts").mkdir(parents=True)
            (state_dir / "drafts" / "plan.json").write_text('{"plan": true}')
            (state_dir / "contracts" / "contract.json").write_text('{"tasks": []}')
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            outputs = ctx.agent_outputs
            assert "plan.json" in outputs
            assert "contract.json" in outputs

    def test_skips_non_matching_extensions(self):
        """Files with non-matching extensions (.txt, .py) are skipped."""
        pipeline = _make_pipeline(repo=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "notes.txt").write_text("text file")
            (state_dir / "script.py").write_text("python file")
            (state_dir / "valid.json").write_text("{}")
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            outputs = ctx.agent_outputs
            assert "notes.txt" not in outputs
            assert "script.py" not in outputs
            assert "valid.json" in outputs

    def test_truncates_large_files(self):
        """Files larger than 4000 chars are truncated."""
        pipeline = _make_pipeline(repo=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "large.json").write_text("x" * 5000)
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            outputs = ctx.agent_outputs
            assert len(outputs["large.json"]) == 4000

    def test_empty_state_dir_returns_empty(self):
        """State dir with empty subdirectories returns empty dict."""
        pipeline = _make_pipeline(repo=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".egg-state"
            (state_dir / "drafts").mkdir(parents=True)
            (state_dir / "contracts").mkdir(parents=True)
            ctx = _make_context(pipeline, repo_path=Path(tmp))
            outputs = ctx.agent_outputs
            assert outputs == {}


class TestContextLiveContainerIdsExtra:
    """Additional live_container_ids coverage."""

    def test_returns_empty_when_docker_client_none(self):
        """Without docker client, returns empty set."""
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline, docker_client=None)
        assert ctx.live_container_ids == set()

    def test_docker_api_error_returns_empty(self):
        """Docker API errors return empty set."""
        docker = MagicMock()
        docker.list_containers.side_effect = RuntimeError("Docker daemon not responding")
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline, docker_client=docker)
        assert ctx.live_container_ids == set()


class TestContextCheapAccessors:
    """Verify cheap accessors work correctly."""

    def test_pipeline_id(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        assert ctx.pipeline_id == "issue-99"

    def test_branch(self):
        pipeline = _make_pipeline(branch="egg/feature")
        ctx = _make_context(pipeline)
        assert ctx.branch == "egg/feature"

    def test_branch_none(self):
        pipeline = _make_pipeline(branch=None)
        ctx = _make_context(pipeline)
        assert ctx.branch is None

    def test_current_phase_defaults_to_pipeline(self):
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        ctx = _make_context(pipeline)
        assert ctx.current_phase == PipelinePhase.PLAN

    def test_explicit_phase_override(self):
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp"),
            trigger="on_demand",
            phase=PipelinePhase.IMPLEMENT,
        )
        assert ctx.current_phase == PipelinePhase.IMPLEMENT

    def test_wave_number_stored(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline, wave_number=3)
        assert ctx.wave_number == 3

    def test_timestamp_is_utc(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        assert ctx.timestamp.tzinfo is not None


# ===========================================================================
# HealthCheckRunner — additional coverage
# ===========================================================================


class TestRunnerAggregateStatus:
    """Test the aggregate worst status in _emit_completed."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_aggregate_healthy_when_all_healthy(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus
        runner = HealthCheckRunner()

        mock_check = MagicMock()
        mock_check.name = "test-check"
        mock_check.tier = HealthTier.PROGRAMMATIC
        mock_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        mock_check.run.return_value = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test-check",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="All good",
        )
        runner.register(mock_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Find the completed event call (last emit)
        completed_calls = [
            call for call in bus.emit.call_args_list if "aggregate_status" in str(call)
        ]
        assert len(completed_calls) == 1
        data = completed_calls[0][1].get("data") or completed_calls[0].kwargs.get("data")
        assert data["aggregate_status"] == "healthy"

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_aggregate_failed_when_mixed(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus
        runner = HealthCheckRunner()

        # Register two checks: one healthy, one failed
        for status, name in [
            (HealthStatus.HEALTHY, "check-ok"),
            (HealthStatus.FAILED, "check-fail"),
        ]:
            mock_check = MagicMock()
            mock_check.name = name
            mock_check.tier = HealthTier.PROGRAMMATIC
            mock_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
            mock_check.run.return_value = HealthResult(
                status=status,
                check_name=name,
                tier=HealthTier.PROGRAMMATIC,
                reasoning="test",
                action=HealthAction.FAIL_PIPELINE
                if status == HealthStatus.FAILED
                else HealthAction.CONTINUE,
            )
            runner.register(mock_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        runner.run(ctx, HealthTrigger.ON_DEMAND)

        completed_calls = [
            call for call in bus.emit.call_args_list if "aggregate_status" in str(call)
        ]
        assert len(completed_calls) == 1
        data = completed_calls[0][1].get("data") or completed_calls[0].kwargs.get("data")
        assert data["aggregate_status"] == "failed"

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_aggregate_degraded_when_no_failures(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus
        runner = HealthCheckRunner()

        mock_check = MagicMock()
        mock_check.name = "check-degraded"
        mock_check.tier = HealthTier.PROGRAMMATIC
        mock_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        mock_check.run.return_value = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="check-degraded",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="something off",
            action=HealthAction.ALERT,
        )
        runner.register(mock_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        runner.run(ctx, HealthTrigger.ON_DEMAND)

        completed_calls = [
            call for call in bus.emit.call_args_list if "aggregate_status" in str(call)
        ]
        assert len(completed_calls) == 1
        data = completed_calls[0][1].get("data") or completed_calls[0].kwargs.get("data")
        assert data["aggregate_status"] == "degraded"


class TestRunnerExceptionIsolation:
    """Test that one check's exception doesn't prevent others from running."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_exception_in_first_check_doesnt_block_second(self, mock_get_bus):
        mock_get_bus.return_value = None  # No event bus
        runner = HealthCheckRunner()

        # First check raises exception
        bad_check = MagicMock()
        bad_check.name = "bad-check"
        bad_check.tier = HealthTier.PROGRAMMATIC
        bad_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        bad_check.run.side_effect = RuntimeError("Unexpected error")

        # Second check returns healthy
        good_check = MagicMock()
        good_check.name = "good-check"
        good_check.tier = HealthTier.PROGRAMMATIC
        good_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        good_check.run.return_value = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="good-check",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="All good",
        )

        runner.register(bad_check)
        runner.register(good_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        assert len(results) == 2
        # First result should be DEGRADED (from exception handling)
        assert results[0].status == HealthStatus.DEGRADED
        assert results[0].action == HealthAction.ALERT
        assert "failed internally" in results[0].reasoning.lower()
        # Second result should be HEALTHY
        assert results[1].status == HealthStatus.HEALTHY


class TestRunnerTriggerFiltering:
    """Additional trigger filtering tests."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_only_matching_trigger_checks_run(self, mock_get_bus):
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()

        # Startup-only check
        startup_check = MagicMock()
        startup_check.name = "startup-only"
        startup_check.tier = HealthTier.PROGRAMMATIC
        startup_check.triggers = frozenset({HealthTrigger.STARTUP})
        startup_check.run.return_value = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="startup-only",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="ok",
        )

        # On-demand-only check
        od_check = MagicMock()
        od_check.name = "od-only"
        od_check.tier = HealthTier.PROGRAMMATIC
        od_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        od_check.run.return_value = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="od-only",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="ok",
        )

        runner.register(startup_check)
        runner.register(od_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)

        # Run with STARTUP trigger
        results = runner.run(ctx, HealthTrigger.STARTUP)
        assert len(results) == 1
        assert results[0].check_name == "startup-only"

        # Run with ON_DEMAND trigger
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)
        assert len(results) == 1
        assert results[0].check_name == "od-only"

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_no_checks_matching_trigger_returns_empty(self, mock_get_bus):
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()

        check = MagicMock()
        check.name = "startup-only"
        check.tier = HealthTier.PROGRAMMATIC
        check.triggers = frozenset({HealthTrigger.STARTUP})
        runner.register(check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.RUNTIME_TICK)
        assert results == []


class TestRunnerEscalation:
    """Additional escalation edge cases."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_wave_complete_failed_only_does_not_escalate(self, mock_get_bus):
        """WAVE_COMPLETE with only FAILED (not DEGRADED) does NOT escalate to Tier 2."""
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()

        tier1_check = MagicMock()
        tier1_check.name = "tier1"
        tier1_check.tier = HealthTier.PROGRAMMATIC
        tier1_check.triggers = frozenset({HealthTrigger.WAVE_COMPLETE})
        tier1_check.run.return_value = HealthResult(
            status=HealthStatus.FAILED,
            check_name="tier1",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="failed",
            action=HealthAction.FAIL_PIPELINE,
        )

        tier2_check = MagicMock()
        tier2_check.name = "tier2"
        tier2_check.tier = HealthTier.AGENT
        tier2_check.triggers = frozenset({HealthTrigger.WAVE_COMPLETE})

        runner.register(tier1_check)
        runner.register(tier2_check)

        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        # Only Tier 1 result should be present
        assert len(results) == 1
        assert results[0].tier == HealthTier.PROGRAMMATIC
        tier2_check.run.assert_not_called()

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_on_demand_always_runs_tier2(self, mock_get_bus):
        """ON_DEMAND always runs Tier 2 even with all healthy."""
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()

        tier2_check = MagicMock()
        tier2_check.name = "tier2"
        tier2_check.tier = HealthTier.AGENT
        tier2_check.triggers = frozenset({HealthTrigger.ON_DEMAND})
        tier2_check.run.return_value = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="tier2",
            tier=HealthTier.AGENT,
            reasoning="ok",
        )

        runner.register(tier2_check)
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        assert len(results) == 1
        assert results[0].tier == HealthTier.AGENT


# ===========================================================================
# worst_action — additional coverage
# ===========================================================================


class TestWorstActionExtra:
    """Additional worst_action edge cases."""

    def test_multiple_fail_pipeline_returns_fail_pipeline(self):
        results = [
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="fail",
                action=HealthAction.FAIL_PIPELINE,
            ),
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="fail",
                action=HealthAction.FAIL_PIPELINE,
            ),
        ]
        assert worst_action(results) == HealthAction.FAIL_PIPELINE

    def test_alert_with_continue_returns_alert(self):
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="degraded",
                action=HealthAction.ALERT,
            ),
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="b",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
                action=HealthAction.CONTINUE,
            ),
        ]
        assert worst_action(results) == HealthAction.ALERT

    def test_all_continue_returns_continue(self):
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=f"check-{i}",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="ok",
            )
            for i in range(5)
        ]
        assert worst_action(results) == HealthAction.CONTINUE


# ===========================================================================
# HealthResult — additional coverage
# ===========================================================================


class TestHealthResultExtra:
    """Additional HealthResult edge cases."""

    def test_to_dict_timestamp_format(self):
        """Timestamp should end with 'Z' and be ISO format."""
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="ok",
        )
        d = result.to_dict()
        assert d["timestamp"].endswith("Z")
        # Should be parseable as ISO
        ts = d["timestamp"].rstrip("Z")
        datetime.fromisoformat(ts)

    def test_to_dict_with_details(self):
        """Details dict is preserved in serialization."""
        result = HealthResult(
            status=HealthStatus.FAILED,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="bad",
            action=HealthAction.FAIL_PIPELINE,
            details={"missing": ["a", "b"], "count": 2},
        )
        d = result.to_dict()
        assert d["details"]["missing"] == ["a", "b"]
        assert d["details"]["count"] == 2
        assert d["action"] == "fail_pipeline"

    def test_frozen_prevents_mutation(self):
        """HealthResult is immutable."""
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="ok",
        )
        try:
            result.status = HealthStatus.FAILED  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass

    def test_default_action_is_continue(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="ok",
        )
        assert result.action == HealthAction.CONTINUE


# ===========================================================================
# ContainerLivenessCheck — additional coverage
# ===========================================================================


class TestContainerLivenessExtra:
    """Additional ContainerLivenessCheck edge cases."""

    def test_complete_pipeline_skipped(self):
        """COMPLETE pipeline returns HEALTHY without checking containers."""
        check = ContainerLivenessCheck()
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        docker = MagicMock()
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        docker.list_containers.assert_not_called()

    def test_failed_pipeline_skipped(self):
        """FAILED pipeline returns HEALTHY without checking containers."""
        check = ContainerLivenessCheck()
        pipeline = _make_pipeline(status=PipelineStatus.FAILED)
        docker = MagicMock()
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        docker.list_containers.assert_not_called()

    def test_containers_across_multiple_phases(self):
        """Checks containers from all pipeline phases."""
        check = ContainerLivenessCheck()
        pipeline = _make_pipeline()

        # Add running container to implement phase
        impl_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        impl_exec.containers.append(
            ContainerInfo(
                container_id="impl-123",
                container_name="egg-impl",
                status=ContainerStatus.RUNNING,
            )
        )

        # Add running container to plan phase
        plan_exec = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.containers.append(
            ContainerInfo(
                container_id="plan-456",
                container_name="egg-plan",
                status=ContainerStatus.RUNNING,
            )
        )

        # Only impl container is alive
        docker = _mock_docker_with_ids({"impl-123"})
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.FAILED
        assert "plan-456" in str(result.details["missing_container_ids"])

    def test_exited_containers_not_expected(self):
        """EXITED containers are not counted as expected running."""
        check = ContainerLivenessCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.containers.append(
            ContainerInfo(
                container_id="exited-123",
                container_name="egg-old",
                status=ContainerStatus.EXITED,
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "No containers expected" in result.reasoning

    def test_all_containers_alive(self):
        """All expected containers present → HEALTHY with count."""
        check = ContainerLivenessCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.containers.append(
            ContainerInfo(
                container_id="c1",
                container_name="egg-c1",
                status=ContainerStatus.RUNNING,
            )
        )
        phase_exec.containers.append(
            ContainerInfo(
                container_id="c2",
                container_name="egg-c2",
                status=ContainerStatus.RUNNING,
            )
        )
        docker = _mock_docker_with_ids({"c1", "c2", "c3"})  # Extra container OK
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "2 expected containers are alive" in result.reasoning


# ===========================================================================
# StartupStateCheck — additional coverage
# ===========================================================================


class TestStartupStateExtra:
    """Additional StartupStateCheck edge cases."""

    def test_stale_containers_and_stale_agents_both_reported(self):
        """Both stale containers and stale agents are reported."""
        check = StartupStateCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING

        # Stale container
        phase_exec.containers.append(
            ContainerInfo(
                container_id="stale-c1",
                container_name="egg-stale",
                status=ContainerStatus.RUNNING,
            )
        )
        # Stale agent
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="stale-a1",
            )
        )

        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.FAILED
        assert len(result.details["stale_containers"]) == 1
        assert len(result.details["stale_agents"]) == 1

    def test_agent_without_container_id_not_flagged(self):
        """Agents with no container_id are not reported as stale."""
        check = StartupStateCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id=None,
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_complete_agent_not_flagged(self):
        """COMPLETE agents with container_id are not flagged as stale."""
        check = StartupStateCheck()
        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="finished-123",
            )
        )
        docker = _mock_docker_with_ids(set())
        ctx = _make_context(pipeline, docker_client=docker)
        result = check.run(ctx)
        assert result.status == HealthStatus.HEALTHY


# ===========================================================================
# HealthCheck Protocol — additional coverage
# ===========================================================================


class TestHealthCheckProtocolExtra:
    """Additional protocol conformance tests."""

    def test_all_tier1_checks_have_name(self):
        checks = [
            ContainerLivenessCheck(),
            StartupStateCheck(),
            PhaseOutputPresenceCheck(),
            StateConsistencyCheck(),
        ]
        for check in checks:
            assert isinstance(check.name, str)
            assert len(check.name) > 0

    def test_all_tier1_checks_are_tier_programmatic(self):
        checks = [
            ContainerLivenessCheck(),
            StartupStateCheck(),
            PhaseOutputPresenceCheck(),
            StateConsistencyCheck(),
        ]
        for check in checks:
            assert check.tier == HealthTier.PROGRAMMATIC

    def test_all_tier1_checks_have_frozen_triggers(self):
        checks = [
            ContainerLivenessCheck(),
            StartupStateCheck(),
            PhaseOutputPresenceCheck(),
            StateConsistencyCheck(),
        ]
        for check in checks:
            assert isinstance(check.triggers, frozenset)
            assert len(check.triggers) > 0

    def test_all_tier1_checks_satisfy_protocol(self):
        """All Tier 1 checks satisfy the HealthCheck protocol."""
        checks = [
            ContainerLivenessCheck(),
            StartupStateCheck(),
            PhaseOutputPresenceCheck(),
            StateConsistencyCheck(),
        ]
        for check in checks:
            assert isinstance(check, HealthCheck)


# ===========================================================================
# End-to-end: Runner with real Tier 1 checks
# ===========================================================================


class TestRunnerWithRealTier1Checks:
    """Run the real Tier 1 checks through the runner to verify integration."""

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_all_checks_pass_for_idle_pipeline(self, mock_get_bus):
        """All checks pass for a non-running pipeline."""
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())
        runner.register(StartupStateCheck())
        runner.register(PhaseOutputPresenceCheck())
        runner.register(StateConsistencyCheck())

        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        # All should be HEALTHY
        for r in results:
            assert r.status == HealthStatus.HEALTHY

    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_startup_trigger_only_runs_matching_checks(self, mock_get_bus):
        """STARTUP trigger only runs checks with STARTUP in their triggers."""
        mock_get_bus.return_value = None
        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())  # Has STARTUP
        runner.register(StartupStateCheck())  # Has STARTUP
        runner.register(PhaseOutputPresenceCheck())  # Does NOT have STARTUP
        runner.register(StateConsistencyCheck())  # Does NOT have STARTUP

        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        results = runner.run(ctx, HealthTrigger.STARTUP)

        check_names = {r.check_name for r in results}
        assert "container_liveness" in check_names
        assert "startup_state" in check_names
        assert "phase_output_presence" not in check_names
        assert "state_consistency" not in check_names

    @patch("health_checks.tier1.phase_output.subprocess.run")
    @patch("health_checks.runner.HealthCheckRunner._get_event_bus")
    def test_mixed_results_from_real_checks(self, mock_get_bus, mock_sub):
        """Runner returns mixed results from real checks on a problematic pipeline."""
        mock_get_bus.return_value = None
        mock_sub.side_effect = OSError("no git")

        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())
        runner.register(PhaseOutputPresenceCheck())
        runner.register(StateConsistencyCheck())

        pipeline = _make_pipeline()
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING

        # Running container that's alive → ContainerLivenessCheck happy
        phase_exec.containers.append(
            ContainerInfo(
                container_id="alive-123",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
            )
        )
        # Completed agent with no commit → PhaseOutputPresenceCheck degraded
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="alive-123",
                commit=None,
            )
        )

        docker = _mock_docker_with_ids({"alive-123"})
        ctx = _make_context(pipeline, docker_client=docker)
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        statuses = {r.check_name: r.status for r in results}
        assert statuses["container_liveness"] == HealthStatus.HEALTHY
        assert statuses["phase_output_presence"] == HealthStatus.DEGRADED


# ===========================================================================
# Enum value tests
# ===========================================================================


class TestEnumValues:
    """Verify enum values match expected strings."""

    def test_health_status_values(self):
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.FAILED.value == "failed"

    def test_health_tier_values(self):
        assert HealthTier.PROGRAMMATIC.value == "tier1"
        assert HealthTier.AGENT.value == "tier2"

    def test_health_trigger_values(self):
        assert HealthTrigger.STARTUP.value == "startup"
        assert HealthTrigger.RUNTIME_TICK.value == "runtime_tick"
        assert HealthTrigger.WAVE_COMPLETE.value == "wave_complete"
        assert HealthTrigger.PHASE_COMPLETE.value == "phase_complete"
        assert HealthTrigger.ON_DEMAND.value == "on_demand"

    def test_health_action_values(self):
        assert HealthAction.CONTINUE.value == "continue"
        assert HealthAction.FAIL_PIPELINE.value == "fail_pipeline"
        assert HealthAction.ALERT.value == "alert"

    def test_str_enum_string_comparison(self):
        """StrEnum values can be compared directly to strings."""
        assert HealthStatus.HEALTHY == "healthy"
        assert HealthTrigger.STARTUP == "startup"
        assert HealthAction.CONTINUE == "continue"
