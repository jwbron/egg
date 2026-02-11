"""Tests for multi-agent orchestration.

Tests the orchestration logic for coordinating specialized agents
(coder, tester, documenter, integrator) during the implement phase.
"""

from egg_contracts import (
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
        """Compute waves groups agents correctly."""
        graph = build_dependency_graph()
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
        """Execution plan has correct structure."""
        plan = compute_execution_plan()

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
        """Initialize creates pending executions for all roles."""
        contract = self._create_test_contract()
        state = initialize_orchestration(contract)

        assert len(state.executions) == 4
        for role in AgentRole:
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

        for role in AgentRole:
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
