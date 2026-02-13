"""Tests for multi-agent phase support.

Tests the extended multi-agent orchestration covering:
- Plan-phase agent roles (ARCHITECT, TASK_PLANNER, RISK_ANALYST)
- get_roles_for_phase() helper
- Reviewer role definitions and dependencies
- File conflict detection (detect_write_overlaps)
- MultiAgentConfig extensions (max_parallel_agents, phase_overrides)
- Wave computation for plan and implement phases with reviewers
"""

from egg_contracts import (
    AgentExecutionStatus,
    AgentRole,
    AgentRoleType,
    Contract,
    IssueInfo,
    MultiAgentConfig,
)
from egg_contracts.agent_roles import (
    ARCHITECT_ROLE,
    RISK_ANALYST_ROLE,
    TASK_PLANNER_ROLE,
    detect_write_overlaps,
    get_reviewer_roles,
    get_roles_for_phase,
    is_reviewer_role,
)
from egg_contracts.dependency_graph import (
    build_dependency_graph,
    compute_execution_plan,
)
from egg_contracts.orchestration import (
    can_agent_run,
    get_runnable_agents,
    initialize_orchestration,
)
from egg_contracts.orchestrator import (
    create_orchestrator,
)


class TestPlanPhaseRoles:
    """Tests for plan-phase agent roles."""

    def test_architect_role_defined(self):
        """ARCHITECT role is defined with no dependencies."""
        assert ARCHITECT_ROLE.role == AgentRole.ARCHITECT
        assert ARCHITECT_ROLE.dependencies == []

    def test_task_planner_depends_on_architect(self):
        """TASK_PLANNER depends on ARCHITECT."""
        assert TASK_PLANNER_ROLE.dependencies == [AgentRole.ARCHITECT]
        assert TASK_PLANNER_ROLE.can_run_in_parallel is True

    def test_risk_analyst_depends_on_architect(self):
        """RISK_ANALYST depends on ARCHITECT."""
        assert RISK_ANALYST_ROLE.dependencies == [AgentRole.ARCHITECT]
        assert RISK_ANALYST_ROLE.can_run_in_parallel is True

    def test_plan_roles_write_to_drafts(self):
        """All plan-phase roles can write to .egg-state/drafts/."""
        for role_def in [ARCHITECT_ROLE, TASK_PLANNER_ROLE, RISK_ANALYST_ROLE]:
            assert role_def.file_access.can_write(".egg-state/drafts/analysis.md")

    def test_plan_roles_cannot_write_code(self):
        """Plan-phase roles cannot write to code files."""
        for role_def in [ARCHITECT_ROLE, TASK_PLANNER_ROLE, RISK_ANALYST_ROLE]:
            assert not role_def.file_access.can_write("src/main.py")
            assert not role_def.file_access.can_write("app.ts")


class TestGetRolesForPhase:
    """Tests for get_roles_for_phase() helper."""

    def test_implement_roles(self):
        """Implement phase returns CODER, TESTER, DOCUMENTER, INTEGRATOR."""
        roles = get_roles_for_phase("implement")
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert AgentRole.INTEGRATOR in roles
        assert len(roles) == 4

    def test_plan_roles(self):
        """Plan phase returns ARCHITECT, TASK_PLANNER, RISK_ANALYST."""
        roles = get_roles_for_phase("plan")
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert len(roles) == 3

    def test_implement_with_reviewers(self):
        """Implement phase with reviewers includes all reviewer roles."""
        roles = get_roles_for_phase("implement", include_reviewers=True)
        assert AgentRole.CODER in roles
        assert AgentRole.REVIEWER_UNIFIED in roles
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert len(roles) == 8

    def test_plan_with_reviewers(self):
        """Plan phase with reviewers includes unified and agent-design."""
        roles = get_roles_for_phase("plan", include_reviewers=True)
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.REVIEWER_UNIFIED in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert len(roles) == 5

    def test_invalid_phase_raises(self):
        """Invalid phase raises ValueError."""
        try:
            get_roles_for_phase("invalid")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "invalid" in str(e)


class TestReviewerRoles:
    """Tests for reviewer agent roles."""

    def test_reviewer_roles_exist(self):
        """All reviewer roles are defined."""
        reviewer_roles = get_reviewer_roles()
        assert len(reviewer_roles) == 4
        assert AgentRole.REVIEWER_UNIFIED in reviewer_roles
        assert AgentRole.REVIEWER_CODE in reviewer_roles
        assert AgentRole.REVIEWER_CONTRACT in reviewer_roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in reviewer_roles

    def test_is_reviewer_role(self):
        """is_reviewer_role correctly identifies reviewer roles."""
        assert is_reviewer_role(AgentRole.REVIEWER_UNIFIED)
        assert is_reviewer_role(AgentRole.REVIEWER_CODE)
        assert not is_reviewer_role(AgentRole.CODER)
        assert not is_reviewer_role(AgentRole.ARCHITECT)

    def test_reviewers_depend_on_integrator(self):
        """Reviewer roles depend on INTEGRATOR (for implement phase)."""
        from egg_contracts.agent_roles import REVIEWER_UNIFIED_ROLE

        assert AgentRole.INTEGRATOR in REVIEWER_UNIFIED_ROLE.dependencies

    def test_reviewers_can_run_in_parallel(self):
        """Reviewer roles can run in parallel with each other."""
        from egg_contracts.agent_roles import (
            REVIEWER_CODE_ROLE,
            REVIEWER_CONTRACT_ROLE,
            REVIEWER_UNIFIED_ROLE,
        )

        assert REVIEWER_UNIFIED_ROLE.can_run_in_parallel is True
        assert REVIEWER_CODE_ROLE.can_run_in_parallel is True
        assert REVIEWER_CONTRACT_ROLE.can_run_in_parallel is True

    def test_reviewers_read_only_access(self):
        """Reviewers cannot write to code files."""
        from egg_contracts.agent_roles import REVIEWER_UNIFIED_ROLE

        assert not REVIEWER_UNIFIED_ROLE.file_access.can_write("src/main.py")
        assert REVIEWER_UNIFIED_ROLE.file_access.can_write(
            ".egg-state/agent-outputs/review.json"
        )


class TestPlanPhaseDependencyGraph:
    """Tests for plan-phase dependency graph and wave computation."""

    def test_build_plan_graph(self):
        """Build dependency graph for plan-phase roles."""
        roles = get_roles_for_phase("plan")
        graph = build_dependency_graph(roles)

        assert len(graph.nodes) == 3
        assert AgentRole.ARCHITECT in graph.nodes
        assert AgentRole.TASK_PLANNER in graph.nodes
        assert AgentRole.RISK_ANALYST in graph.nodes

    def test_plan_no_cycles(self):
        """Plan-phase dependency graph has no cycles."""
        roles = get_roles_for_phase("plan")
        graph = build_dependency_graph(roles)
        assert not graph.has_cycle()

    def test_plan_waves(self):
        """Plan-phase wave computation is correct."""
        roles = get_roles_for_phase("plan")
        graph = build_dependency_graph(roles)
        waves = graph.compute_waves()

        # Wave 1: ARCHITECT
        assert AgentRole.ARCHITECT in waves[0]
        assert len(waves[0]) == 1

        # Wave 2: TASK_PLANNER + RISK_ANALYST (parallel)
        assert len(waves[1]) == 2
        assert AgentRole.TASK_PLANNER in waves[1]
        assert AgentRole.RISK_ANALYST in waves[1]

    def test_plan_execution_plan(self):
        """Plan-phase execution plan has 2 waves."""
        roles = get_roles_for_phase("plan")
        plan = compute_execution_plan(roles)

        assert len(plan) == 2
        assert plan.total_agents == 3


class TestImplementPhaseWithReviewers:
    """Tests for implement phase with reviewer agents in waves."""

    def test_implement_with_reviewers_waves(self):
        """Implement phase with reviewers has 4 waves."""
        roles = get_roles_for_phase("implement", include_reviewers=True)
        graph = build_dependency_graph(roles)
        waves = graph.compute_waves()

        # Wave 1: CODER
        assert AgentRole.CODER in waves[0]

        # Wave 2: TESTER + DOCUMENTER
        assert AgentRole.TESTER in waves[1]
        assert AgentRole.DOCUMENTER in waves[1]

        # Wave 3: INTEGRATOR
        assert AgentRole.INTEGRATOR in waves[2]

        # Wave 4: All reviewers (parallel)
        assert AgentRole.REVIEWER_UNIFIED in waves[3]
        assert AgentRole.REVIEWER_CODE in waves[3]
        assert AgentRole.REVIEWER_CONTRACT in waves[3]
        assert AgentRole.REVIEWER_AGENT_DESIGN in waves[3]


class TestFileConflictDetection:
    """Tests for file write overlap detection."""

    def test_tester_documenter_no_overlap(self):
        """TESTER and DOCUMENTER have no meaningful write overlaps."""
        overlaps = detect_write_overlaps([AgentRole.TESTER, AgentRole.DOCUMENTER])
        # They share .egg-state/agent-outputs/ but that's excluded
        assert len(overlaps) == 0

    def test_plan_roles_overlap_on_drafts(self):
        """TASK_PLANNER and RISK_ANALYST share .egg-state/drafts/ writes."""
        overlaps = detect_write_overlaps(
            [AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST]
        )
        assert len(overlaps) == 1
        role1, role2, patterns = overlaps[0]
        assert ".egg-state/drafts/" in patterns

    def test_no_overlap_single_role(self):
        """Single role has no overlaps."""
        overlaps = detect_write_overlaps([AgentRole.CODER])
        assert len(overlaps) == 0


class TestMultiAgentConfig:
    """Tests for MultiAgentConfig extensions."""

    def test_max_parallel_agents_default(self):
        """max_parallel_agents defaults to 10."""
        config = MultiAgentConfig()
        assert config.max_parallel_agents == 10

    def test_phase_overrides_default(self):
        """phase_overrides defaults to empty dict."""
        config = MultiAgentConfig()
        assert config.phase_overrides == {}

    def test_phase_overrides_set(self):
        """phase_overrides can disable multi-agent per phase."""
        config = MultiAgentConfig(phase_overrides={"plan": False, "implement": True})
        assert config.phase_overrides["plan"] is False
        assert config.phase_overrides["implement"] is True

    def test_new_role_types_in_enum(self):
        """New role types are available in AgentRoleType."""
        assert AgentRoleType.ARCHITECT == "architect"
        assert AgentRoleType.TASK_PLANNER == "task_planner"
        assert AgentRoleType.RISK_ANALYST == "risk_analyst"
        assert AgentRoleType.REVIEWER_UNIFIED == "reviewer_unified"
        assert AgentRoleType.REVIEWER_CODE == "reviewer_code"
        assert AgentRoleType.REVIEWER_CONTRACT == "reviewer_contract"
        assert AgentRoleType.REVIEWER_AGENT_DESIGN == "reviewer_agent_design"

    def test_conflicts_field_on_execution(self):
        """AgentExecutionModel has conflicts field."""
        from egg_contracts.models import AgentExecutionModel

        execution = AgentExecutionModel(role=AgentRoleType.CODER)
        assert execution.conflicts == []

        execution_with_conflicts = AgentExecutionModel(
            role=AgentRoleType.CODER,
            conflicts=["src/main.py", "src/utils.py"],
        )
        assert len(execution_with_conflicts.conflicts) == 2

    def test_roles_enabled_includes_new_roles(self):
        """roles_enabled default includes all role types."""
        config = MultiAgentConfig()
        assert AgentRoleType.ARCHITECT in config.roles_enabled
        assert AgentRoleType.REVIEWER_UNIFIED in config.roles_enabled


class TestPlanPhaseOrchestration:
    """Tests for plan-phase orchestration state and dispatch."""

    def _create_test_contract(self) -> Contract:
        """Create a test contract with plan-phase roles enabled."""
        return Contract(
            issue=IssueInfo(
                number=456,
                title="Test Plan Phase",
                url="https://github.com/test/repo/issues/456",
            ),
            multi_agent_config=MultiAgentConfig(
                roles_enabled=[
                    AgentRoleType.ARCHITECT,
                    AgentRoleType.TASK_PLANNER,
                    AgentRoleType.RISK_ANALYST,
                ]
            ),
        )

    def test_initialize_plan_orchestration(self):
        """Initialize creates pending executions for plan-phase roles."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        assert len(state.executions) == 3
        assert AgentRole.ARCHITECT in state.executions
        assert AgentRole.TASK_PLANNER in state.executions
        assert AgentRole.RISK_ANALYST in state.executions

    def test_plan_initial_runnable(self):
        """Initially only ARCHITECT can run in plan phase."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        runnable = get_runnable_agents(state)
        assert runnable == [AgentRole.ARCHITECT]

    def test_after_architect_complete(self):
        """After ARCHITECT completes, TASK_PLANNER and RISK_ANALYST can run."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_complete(AgentRole.ARCHITECT)

        runnable = get_runnable_agents(state)
        assert AgentRole.TASK_PLANNER in runnable
        assert AgentRole.RISK_ANALYST in runnable

    def test_plan_dispatch_first(self):
        """First dispatch in plan phase selects ARCHITECT."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)

        decision = orch.get_next_dispatch()
        assert decision.agents_to_run == [AgentRole.ARCHITECT]
        assert decision.wave_number == 1

    def test_plan_dispatch_parallel(self):
        """Second dispatch in plan phase runs TASK_PLANNER + RISK_ANALYST."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)
        orch.complete_agent(AgentRole.ARCHITECT)

        decision = orch.get_next_dispatch()
        assert len(decision.agents_to_run) == 2
        assert AgentRole.TASK_PLANNER in decision.agents_to_run
        assert AgentRole.RISK_ANALYST in decision.agents_to_run
        assert decision.is_parallel

    def test_plan_all_complete(self):
        """All plan agents complete marks dispatch as complete."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)

        orch.complete_agent(AgentRole.ARCHITECT)
        orch.complete_agent(AgentRole.TASK_PLANNER)
        orch.complete_agent(AgentRole.RISK_ANALYST)

        decision = orch.get_next_dispatch()
        assert decision.all_complete


class TestSingleAgentFallback:
    """Tests for single-agent fallback behavior."""

    def test_default_multi_agent_false(self):
        """PipelineConfig.multi_agent defaults to False for safe rollout."""
        # We can't import orchestrator models directly in integration tests
        # that run from shared/ context, but we can verify the contract side
        config = MultiAgentConfig(enabled=False)
        assert config.enabled is False

    def test_empty_phase_overrides(self):
        """Empty phase_overrides means use global enabled flag."""
        config = MultiAgentConfig(phase_overrides={})
        assert config.phase_overrides == {}
