"""Tests for multi-agent orchestration.

Tests the orchestration logic for coordinating specialized agents
(coder, tester, documenter, integrator) during the implement phase.
"""

from egg_contracts import (
    AgentExecutionModel,
    AgentExecutionStatus,
    AgentRole,
    AgentRoleType,
    Contract,
    IssueInfo,
    MultiAgentConfig,
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
    DispatchDecision,
    create_orchestrator,
    get_dispatch_for_contract,
)


class TestDependencyGraph:
    """Tests for dependency graph construction and analysis."""

    def test_build_graph_all_roles(self):
        """Build graph includes all agent roles."""
        graph = build_dependency_graph()
        assert len(graph.nodes) == 14
        assert AgentRole.CODER in graph.nodes
        assert AgentRole.TESTER in graph.nodes
        assert AgentRole.DOCUMENTER in graph.nodes
        assert AgentRole.INTEGRATOR in graph.nodes
        assert AgentRole.ARCHITECT in graph.nodes
        assert AgentRole.TASK_PLANNER in graph.nodes
        assert AgentRole.RISK_ANALYST in graph.nodes
        assert AgentRole.REFINER in graph.nodes

    def test_build_graph_implement_roles(self):
        """Build graph with implement-phase roles only."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement")
        graph = build_dependency_graph(roles)
        assert len(graph.nodes) == 4
        assert AgentRole.CODER in graph.nodes
        assert AgentRole.TESTER in graph.nodes
        assert AgentRole.DOCUMENTER in graph.nodes
        assert AgentRole.INTEGRATOR in graph.nodes

    def test_build_graph_subset(self):
        """Build graph with subset of roles."""
        graph = build_dependency_graph([AgentRole.CODER, AgentRole.TESTER])
        assert len(graph.nodes) == 2
        assert AgentRole.CODER in graph.nodes
        assert AgentRole.TESTER in graph.nodes
        assert AgentRole.DOCUMENTER not in graph.nodes

    def test_no_cycles(self):
        """Default configuration has no cycles."""
        graph = build_dependency_graph()
        assert not graph.has_cycle()

    def test_topological_sort(self):
        """Topological sort respects dependencies."""
        graph = build_dependency_graph()
        sorted_roles = graph.topological_sort()

        # Coder must come before tester and documenter
        coder_idx = sorted_roles.index(AgentRole.CODER)
        tester_idx = sorted_roles.index(AgentRole.TESTER)
        documenter_idx = sorted_roles.index(AgentRole.DOCUMENTER)
        integrator_idx = sorted_roles.index(AgentRole.INTEGRATOR)

        assert coder_idx < tester_idx
        assert coder_idx < documenter_idx
        assert tester_idx < integrator_idx

    def test_compute_waves(self):
        """Compute waves groups implement-phase agents correctly."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement")
        graph = build_dependency_graph(roles)
        waves = graph.compute_waves()

        # Wave 1: coder
        assert AgentRole.CODER in waves[0]

        # Wave 2: tester and documenter (parallel)
        assert len(waves[1]) == 2
        assert AgentRole.TESTER in waves[1]
        assert AgentRole.DOCUMENTER in waves[1]

        # Wave 3: integrator
        assert AgentRole.INTEGRATOR in waves[2]

    def test_execution_plan(self):
        """Execution plan has correct structure for implement phase."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement")
        plan = compute_execution_plan(roles)

        assert len(plan) == 3
        assert plan.total_agents == 4

        wave1 = plan.get_wave(1)
        assert wave1 is not None
        assert not wave1.is_parallel()
        assert AgentRole.CODER in wave1.agents

        wave2 = plan.get_wave(2)
        assert wave2 is not None
        assert wave2.is_parallel()

        wave3 = plan.get_wave(3)
        assert wave3 is not None
        assert not wave3.is_parallel()


class TestOrchestrationState:
    """Tests for orchestration state management."""

    def _create_test_contract(self) -> Contract:
        """Create a test contract."""
        return Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            )
        )

    def test_initialize_orchestration(self):
        """Initialize creates pending executions for default implement-phase roles."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        assert len(state.executions) == 4
        for role in [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR]:
            assert role in state.executions
            assert state.executions[role].status == AgentExecutionStatus.PENDING

    def test_initialize_with_enabled_roles(self):
        """Initialize respects enabled roles configuration."""
        contract = self._create_test_contract()
        contract.multi_agent_config = MultiAgentConfig(
            roles_enabled=[AgentRoleType.CODER, AgentRoleType.TESTER]
        )
        state = initialize_orchestration(contract)

        assert len(state.executions) == 2
        assert AgentRole.CODER in state.executions
        assert AgentRole.TESTER in state.executions
        assert AgentRole.DOCUMENTER not in state.executions

    def test_mark_running(self):
        """Mark agent as running."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        execution = state.mark_running(AgentRole.CODER)

        assert execution.status == AgentExecutionStatus.RUNNING
        assert execution.started_at is not None

    def test_mark_complete(self):
        """Mark agent as complete with commit."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_running(AgentRole.CODER)

        execution = state.mark_complete(
            AgentRole.CODER,
            commit="abc1234",
            outputs={"changed_files": ["foo.py"]},
        )

        assert execution.status == AgentExecutionStatus.COMPLETE
        assert execution.commit == "abc1234"
        assert execution.outputs == {"changed_files": ["foo.py"]}
        assert execution.completed_at is not None

    def test_mark_failed(self):
        """Mark agent as failed with error."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_running(AgentRole.CODER)

        execution = state.mark_failed(AgentRole.CODER, "Test error")

        assert execution.status == AgentExecutionStatus.FAILED
        assert execution.error == "Test error"

    def test_get_pending_roles(self):
        """Get pending roles."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_running(AgentRole.CODER)
        state.mark_complete(AgentRole.CODER)

        pending = state.get_pending_roles()

        assert AgentRole.CODER not in pending
        assert AgentRole.TESTER in pending
        assert AgentRole.DOCUMENTER in pending
        assert AgentRole.INTEGRATOR in pending

    def test_all_complete(self):
        """Check all complete."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        assert not state.all_complete()

        # Mark only registered roles (implement-phase defaults) as complete
        for role in list(state.executions.keys()):
            state.mark_complete(role)

        assert state.all_complete()

    def test_any_failed(self):
        """Check any failed."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        assert not state.any_failed()

        state.mark_failed(AgentRole.CODER, "Error")

        assert state.any_failed()


class TestRunnableAgents:
    """Tests for determining which agents can run."""

    def _create_test_contract(self) -> Contract:
        """Create a test contract."""
        return Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            )
        )

    def test_initial_runnable(self):
        """Initially only coder can run."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        runnable = get_runnable_agents(state)

        assert runnable == [AgentRole.CODER]

    def test_after_coder_complete(self):
        """After coder completes, tester and documenter can run."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_complete(AgentRole.CODER)

        runnable = get_runnable_agents(state)

        assert AgentRole.TESTER in runnable
        assert AgentRole.DOCUMENTER in runnable
        assert AgentRole.INTEGRATOR not in runnable

    def test_after_tester_complete(self):
        """After tester completes, integrator can run."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_complete(AgentRole.CODER)
        state.mark_complete(AgentRole.TESTER)

        runnable = get_runnable_agents(state)

        assert AgentRole.INTEGRATOR in runnable

    def test_no_runnable_when_running(self):
        """Running agent is not in runnable list."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)
        state.mark_running(AgentRole.CODER)

        runnable = get_runnable_agents(state)

        assert AgentRole.CODER not in runnable


class TestOrchestrator:
    """Tests for the main Orchestrator class."""

    def _create_test_contract(self) -> Contract:
        """Create a test contract."""
        return Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            )
        )

    def test_create_orchestrator(self):
        """Create orchestrator initializes state."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)

        assert orch.contract is contract
        assert len(orch.state.executions) == 4

    def test_first_dispatch(self):
        """First dispatch selects coder."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)

        decision = orch.get_next_dispatch()

        assert decision.agents_to_run == [AgentRole.CODER]
        assert decision.wave_number == 1
        assert not decision.is_parallel
        assert not decision.all_complete

    def test_second_dispatch_parallel(self):
        """Second wave dispatches tester and documenter in parallel."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)
        orch.complete_agent(AgentRole.CODER)

        decision = orch.get_next_dispatch()

        assert len(decision.agents_to_run) == 2
        assert AgentRole.TESTER in decision.agents_to_run
        assert AgentRole.DOCUMENTER in decision.agents_to_run
        assert decision.wave_number == 2
        assert decision.is_parallel

    def test_final_dispatch_complete(self):
        """After all agents complete, returns all_complete."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)

        for role in AgentRole:
            orch.complete_agent(role)

        decision = orch.get_next_dispatch()

        assert decision.all_complete
        assert decision.agents_to_run == []

    def test_dispatch_with_failure(self):
        """Failure stops dispatch."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)
        orch.fail_agent(AgentRole.CODER, "Test error")

        decision = orch.get_next_dispatch()

        assert decision.has_failures
        assert decision.agents_to_run == []

    def test_status_summary(self):
        """Get status summary."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)
        orch.complete_agent(AgentRole.CODER, commit="abc1234")

        summary = orch.get_status_summary()

        assert summary["total_agents"] == 4
        assert summary["completed"] == 1
        assert summary["pending"] == 3
        assert not summary["all_complete"]
        assert not summary["any_failed"]

    def test_apply_to_contract(self):
        """Apply state back to contract."""
        contract = self._create_test_contract()
        orch = create_orchestrator(contract)
        orch.complete_agent(AgentRole.CODER, commit="abc1234")

        updated = orch.apply_to_contract()

        assert len(updated.agent_executions) == 4
        coder_ex = next(ex for ex in updated.agent_executions if ex.role == AgentRoleType.CODER)
        assert coder_ex.status == AgentExecutionStatus.COMPLETE
        assert coder_ex.commit == "abc1234"


class TestDispatchDecision:
    """Tests for DispatchDecision helper methods."""

    def test_decision_none(self):
        """Create decision with no agents."""
        decision = DispatchDecision.none("Waiting for dependencies")

        assert decision.agents_to_run == []
        assert "Waiting" in decision.reason
        assert not decision.all_complete

    def test_decision_complete(self):
        """Create complete decision."""
        decision = DispatchDecision.complete()

        assert decision.all_complete
        assert decision.agents_to_run == []

    def test_decision_failed(self):
        """Create failed decision."""
        decision = DispatchDecision.failed([AgentRole.CODER])

        assert decision.has_failures
        assert "coder" in decision.reason.lower()


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_dispatch_for_contract(self):
        """Get dispatch directly from contract."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            )
        )

        decision = get_dispatch_for_contract(contract)

        assert decision.agents_to_run == [AgentRole.CODER]

    def test_can_agent_run(self):
        """Check if specific agent can run."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/test/repo/issues/123",
            )
        )
        state = initialize_orchestration(contract)

        assert can_agent_run(AgentRole.CODER, state)
        assert not can_agent_run(AgentRole.TESTER, state)

        state.mark_complete(AgentRole.CODER)

        assert can_agent_run(AgentRole.TESTER, state)
        assert can_agent_run(AgentRole.DOCUMENTER, state)
        assert not can_agent_run(AgentRole.INTEGRATOR, state)


class TestPlanPhaseRoles:
    """Tests for plan-phase agent roles."""

    def test_get_roles_for_plan_phase(self):
        """Get roles for plan phase returns architect, task_planner, risk_analyst."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("plan")
        assert len(roles) == 3
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles

    def test_get_roles_for_implement_phase(self):
        """Get roles for implement phase returns coder, tester, documenter, integrator."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement")
        assert len(roles) == 4
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert AgentRole.INTEGRATOR in roles

    def test_get_roles_for_unknown_phase_raises(self):
        """Unknown phase raises ValueError."""
        import pytest
        from egg_contracts.agent_roles import get_roles_for_phase

        with pytest.raises(ValueError, match="No agent roles defined"):
            get_roles_for_phase("unknown")

    def test_plan_phase_dependency_graph(self):
        """Plan-phase roles have correct dependency structure."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("plan")
        graph = build_dependency_graph(roles)

        assert len(graph.nodes) == 3
        waves = graph.compute_waves()

        # Wave 1: architect (no dependencies)
        assert AgentRole.ARCHITECT in waves[0]

        # Wave 2: task_planner and risk_analyst (both depend on architect)
        assert len(waves[1]) == 2
        assert AgentRole.TASK_PLANNER in waves[1]
        assert AgentRole.RISK_ANALYST in waves[1]

    def test_plan_phase_orchestration(self):
        """Plan-phase orchestration follows architect -> planner + analyst."""
        from egg_contracts.agent_roles import get_roles_for_phase

        contract = Contract(
            issue=IssueInfo(
                number=456,
                title="Plan Test",
                url="https://github.com/test/repo/issues/456",
            )
        )

        roles = get_roles_for_phase("plan")
        state = initialize_orchestration(contract, roles=roles)

        assert len(state.executions) == 3
        runnable = get_runnable_agents(state)
        assert runnable == [AgentRole.ARCHITECT]

        # After architect completes, planner and analyst can run
        state.mark_complete(AgentRole.ARCHITECT)
        runnable = get_runnable_agents(state)
        assert AgentRole.TASK_PLANNER in runnable
        assert AgentRole.RISK_ANALYST in runnable

    def test_get_roles_with_reviewers(self):
        """Get roles with reviewers included."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement", include_reviewers=True)
        assert len(roles) == 8  # 4 implement + 4 reviewers
        assert AgentRole.REVIEWER_UNIFIED in roles
        assert AgentRole.REVIEWER_CODE in roles

    def test_get_plan_roles_with_reviewers(self):
        """Get plan roles with reviewers included."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("plan", include_reviewers=True)
        assert len(roles) == 6  # 3 plan + 3 reviewers
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert AgentRole.REVIEWER_UNIFIED in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert AgentRole.REVIEWER_PLAN in roles

    def test_reviewer_plan_role_definition(self):
        """Reviewer plan role has correct properties."""
        from egg_contracts.agent_roles import get_role_definition

        role_def = get_role_definition(AgentRole.REVIEWER_PLAN)
        assert role_def.role == AgentRole.REVIEWER_PLAN
        assert AgentRole.TASK_PLANNER in role_def.dependencies
        assert AgentRole.RISK_ANALYST in role_def.dependencies
        assert ".egg-state/reviews/" in role_def.file_access.allowed_write
        assert role_def.file_access.can_write(".egg-state/reviews/verdict.json")
        assert not role_def.file_access.can_write("src/main.py")


class TestRefinePhaseRoles:
    """Tests for refine-phase agent roles."""

    def test_get_roles_for_refine_phase(self):
        """Get roles for refine phase returns refiner."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("refine")
        assert len(roles) == 1
        assert AgentRole.REFINER in roles

    def test_get_refine_roles_with_reviewers(self):
        """Get refine roles with reviewers included."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("refine", include_reviewers=True)
        assert len(roles) == 3  # 1 refiner + 2 reviewers
        assert AgentRole.REFINER in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles

    def test_refine_phase_dependency_graph(self):
        """Refine-phase has simple single-agent dependency structure."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("refine")
        graph = build_dependency_graph(roles)

        assert len(graph.nodes) == 1
        waves = graph.compute_waves()

        # Wave 1: refiner (no dependencies)
        assert AgentRole.REFINER in waves[0]

    def test_refiner_role_definition(self):
        """Refiner role has correct properties."""
        from egg_contracts.agent_roles import get_role_definition

        role_def = get_role_definition(AgentRole.REFINER)
        assert role_def.role == AgentRole.REFINER
        assert role_def.dependencies == []
        assert ".egg-state/drafts/" in role_def.file_access.allowed_write
        assert role_def.file_access.can_write(".egg-state/drafts/123-analysis.md")
        assert not role_def.file_access.can_write("src/main.py")

    def test_reviewer_refine_role_definition(self):
        """Reviewer refine role has correct properties."""
        from egg_contracts.agent_roles import get_role_definition

        role_def = get_role_definition(AgentRole.REVIEWER_REFINE)
        assert role_def.role == AgentRole.REVIEWER_REFINE
        assert AgentRole.REFINER in role_def.dependencies
        assert ".egg-state/reviews/" in role_def.file_access.allowed_write
        assert role_def.file_access.can_write(".egg-state/reviews/verdict.json")
        assert not role_def.file_access.can_write("src/main.py")


class TestPrPhaseRoles:
    """Tests for pr phase role mapping."""

    def test_get_roles_for_pr_phase(self):
        """Get roles for pr phase returns coder."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("pr")
        assert roles == [AgentRole.CODER]


class TestGetEffectiveRolesForPhase:
    """Tests for get_effective_roles_for_phase()."""

    def test_implement_multi_agent(self):
        """Multi-agent implement returns all 4 roles."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("implement", multi_agent=True)
        assert len(roles) == 4
        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert AgentRole.INTEGRATOR in roles

    def test_implement_single_agent(self):
        """Single-agent implement returns only CODER."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("implement", multi_agent=False)
        assert roles == [AgentRole.CODER]

    def test_plan_single_agent(self):
        """Single-agent plan returns CODER (matches legacy behaviour)."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("plan", multi_agent=False)
        assert roles == [AgentRole.CODER]

    def test_plan_multi_agent(self):
        """Multi-agent plan returns architect, task_planner, risk_analyst."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("plan", multi_agent=True)
        assert len(roles) == 3
        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles

    def test_refine_single_agent(self):
        """Single-agent refine returns REFINER."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("refine", multi_agent=False)
        assert roles == [AgentRole.REFINER]

    def test_refine_multi_agent_still_single(self):
        """Multi-agent flag has no effect on refine (not in implement/plan)."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("refine", multi_agent=True)
        assert roles == [AgentRole.REFINER]

    def test_pr_single_agent(self):
        """Single-agent pr returns CODER."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("pr", multi_agent=False)
        assert roles == [AgentRole.CODER]

    def test_pr_multi_agent_still_single(self):
        """Multi-agent flag has no effect on pr (not in implement/plan)."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase("pr", multi_agent=True)
        assert roles == [AgentRole.CODER]

    def test_unknown_phase_raises(self):
        """Unknown phase raises ValueError."""
        import pytest
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        with pytest.raises(ValueError, match="No agent roles defined"):
            get_effective_roles_for_phase("unknown", multi_agent=False)

    def test_with_reviewers(self):
        """include_reviewers appends reviewer roles."""
        from egg_contracts.agent_roles import get_effective_roles_for_phase

        roles = get_effective_roles_for_phase(
            "implement", multi_agent=False, include_reviewers=True
        )
        # Single CODER + 4 implement reviewers
        assert len(roles) == 5
        assert roles[0] == AgentRole.CODER
        assert AgentRole.REVIEWER_UNIFIED in roles


class TestReviewerRoles:
    """Tests for reviewer agent roles."""

    def test_reviewer_roles_in_graph(self):
        """Reviewer roles depend on integrator."""
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement", include_reviewers=True)
        graph = build_dependency_graph(roles)

        # Reviewers should be in a wave after integrator
        waves = graph.compute_waves()
        assert len(waves) == 4  # coder -> tester+doc -> integrator -> reviewers

        # Wave 4 should be all reviewers
        assert AgentRole.REVIEWER_UNIFIED in waves[3]
        assert AgentRole.REVIEWER_CODE in waves[3]
        assert AgentRole.REVIEWER_CONTRACT in waves[3]
        assert AgentRole.REVIEWER_AGENT_DESIGN in waves[3]

    def test_reviewer_roles_read_only(self):
        """Reviewer roles have read-only file access."""
        from egg_contracts.agent_roles import get_role_definition

        for role in [
            AgentRole.REVIEWER_UNIFIED,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_AGENT_DESIGN,
            AgentRole.REVIEWER_REFINE,
            AgentRole.REVIEWER_PLAN,
        ]:
            role_def = get_role_definition(role)
            # Reviewers can write to reviews and agent-outputs only
            assert ".egg-state/reviews/" in role_def.file_access.allowed_write
            assert ".egg-state/agent-outputs/" in role_def.file_access.allowed_write
            # Source directories blocked via directory-based patterns
            assert "src/" in role_def.file_access.blocked_write

            # Verify can_write() actually works for allowed paths
            assert role_def.file_access.can_write(".egg-state/reviews/verdict.json")
            assert role_def.file_access.can_write(".egg-state/agent-outputs/report.json")
            # Verify can_write() blocks source/production paths
            assert not role_def.file_access.can_write("src/main.py")
            assert not role_def.file_access.can_write("gateway/server.py")


class TestWriteOverlapDetection:
    """Tests for write overlap detection between parallel agents."""

    def test_overlaps_in_implement_phase(self):
        """Implement-phase parallel agents share agent-outputs directory."""
        from egg_contracts.agent_roles import detect_write_overlaps, get_roles_for_phase

        roles = get_roles_for_phase("implement")
        overlaps = detect_write_overlaps(roles)

        # Tester and documenter both write to .egg-state/agent-outputs/
        # This is expected since each agent writes to its own file within it
        assert len(overlaps) >= 1
        roles_in_overlap = {(o[0], o[1]) for o in overlaps}
        assert (AgentRole.TESTER, AgentRole.DOCUMENTER) in roles_in_overlap

    def test_overlaps_in_plan_phase(self):
        """Plan-phase parallel agents may share write patterns."""
        from egg_contracts.agent_roles import detect_write_overlaps, get_roles_for_phase

        roles = get_roles_for_phase("plan")
        overlaps = detect_write_overlaps(roles)

        # task_planner and risk_analyst both write to .egg-state/drafts/
        # and .egg-state/agent-outputs/
        assert len(overlaps) >= 1


class TestMultiAgentConfig:
    """Tests for MultiAgentConfig with phase overrides."""

    def test_default_multi_agent_config(self):
        """Default config enables all roles."""
        config = MultiAgentConfig()
        assert config.enabled is True
        assert config.parallel_execution is True
        assert len(config.roles_enabled) == 14  # All roles
        assert len(config.phase_overrides) == 0

    def test_phase_override(self):
        """Phase overrides customize per-phase behavior."""
        from egg_contracts.models import PhaseAgentConfig

        config = MultiAgentConfig(
            phase_overrides={
                "plan": PhaseAgentConfig(
                    enabled=True,
                    roles=[AgentRoleType.ARCHITECT, AgentRoleType.TASK_PLANNER],
                ),
            },
        )

        assert "plan" in config.phase_overrides
        plan_config = config.phase_overrides["plan"]
        assert len(plan_config.roles) == 2
        assert AgentRoleType.ARCHITECT in plan_config.roles

    def test_agent_execution_conflicts_field(self):
        """AgentExecutionModel has conflicts field."""
        model = AgentExecutionModel(
            role=AgentRoleType.CODER,
            status=AgentExecutionStatus.COMPLETE,
            conflicts=["src/file1.py", "src/file2.py"],
        )
        assert model.conflicts == ["src/file1.py", "src/file2.py"]

    def test_agent_execution_conflicts_default_empty(self):
        """AgentExecutionModel defaults to empty conflicts list."""
        model = AgentExecutionModel(
            role=AgentRoleType.CODER,
            status=AgentExecutionStatus.PENDING,
        )
        assert model.conflicts == []
