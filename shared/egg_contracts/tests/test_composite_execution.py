"""Tests for composite (phase_id, role) execution tracking.

Covers:
- Creation and lookup with composite keys
- Backward compatibility with None phase_id
- Serialization to/from contract
- Phase-scoped can_agent_run and get_runnable_agents
"""

from __future__ import annotations

from egg_contracts.agent_roles import AgentRole
from egg_contracts.models import (
    AgentExecutionModel,
    AgentExecutionStatus,
    AgentRoleType,
    Contract,
)
from egg_contracts.orchestration import (
    OrchestrationState,
    can_agent_run,
    get_runnable_agents,
    initialize_orchestration,
)


class TestCompositeKeyCreation:
    """Tests for creating executions with composite keys."""

    def test_set_execution_with_phase_id(self):
        """Setting execution with phase_id stores in phase_executions."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER,
            AgentExecutionStatus.PENDING,
            phase_id="phase-1",
        )

        assert (
            "phase-1",
            AgentRole.CODER,
        ) in state.phase_executions
        execution = state.phase_executions[("phase-1", AgentRole.CODER)]
        assert execution.phase_id == "phase-1"
        assert execution.role == AgentRoleType.CODER

    def test_set_execution_without_phase_id(self):
        """Setting execution without phase_id stores in both dicts."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER,
            AgentExecutionStatus.PENDING,
        )

        assert AgentRole.CODER in state.executions
        assert (None, AgentRole.CODER) in state.phase_executions
        assert state.executions[AgentRole.CODER].phase_id is None

    def test_multiple_phases_same_role(self):
        """Same role can have different executions in different phases."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER,
            AgentExecutionStatus.PENDING,
            phase_id="phase-1",
        )
        state.set_execution(
            AgentRole.CODER,
            AgentExecutionStatus.PENDING,
            phase_id="phase-2",
        )

        assert ("phase-1", AgentRole.CODER) in state.phase_executions
        assert ("phase-2", AgentRole.CODER) in state.phase_executions
        # They should be different execution objects
        ex1 = state.phase_executions[("phase-1", AgentRole.CODER)]
        ex2 = state.phase_executions[("phase-2", AgentRole.CODER)]
        assert ex1 is not ex2
        assert ex1.phase_id == "phase-1"
        assert ex2.phase_id == "phase-2"


class TestCompositeKeyLookup:
    """Tests for looking up executions with composite keys."""

    def test_get_execution_with_phase_id(self):
        """get_execution returns phase-scoped execution."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        result = state.get_execution(AgentRole.CODER, phase_id="phase-1")
        assert result is not None
        assert result.phase_id == "phase-1"

    def test_get_execution_wrong_phase_returns_none(self):
        """get_execution returns None for wrong phase_id."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        result = state.get_execution(AgentRole.CODER, phase_id="phase-2")
        assert result is None

    def test_get_execution_none_phase_fallback(self):
        """get_execution with phase_id=None falls back to role-only."""
        state = OrchestrationState()
        state.set_execution(AgentRole.CODER, AgentExecutionStatus.PENDING)

        result = state.get_execution(AgentRole.CODER)
        assert result is not None
        assert result.phase_id is None


class TestCompositeKeyMarking:
    """Tests for mark_running/complete/failed with phase_id."""

    def test_mark_running_with_phase_id(self):
        """mark_running sets status for phase-scoped execution."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        state.mark_running(AgentRole.CODER, phase_id="phase-1")
        ex = state.get_execution(AgentRole.CODER, phase_id="phase-1")
        assert ex is not None
        assert ex.status == AgentExecutionStatus.RUNNING

    def test_mark_complete_with_phase_id(self):
        """mark_complete sets status and commit for phase-scoped execution."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.RUNNING, phase_id="phase-1"
        )

        state.mark_complete(AgentRole.CODER, commit="abc123", phase_id="phase-1")
        ex = state.get_execution(AgentRole.CODER, phase_id="phase-1")
        assert ex is not None
        assert ex.status == AgentExecutionStatus.COMPLETE
        assert ex.commit == "abc123"

    def test_mark_failed_with_phase_id(self):
        """mark_failed sets status and error for phase-scoped execution."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.RUNNING, phase_id="phase-1"
        )

        state.mark_failed(AgentRole.CODER, error="test error", phase_id="phase-1")
        ex = state.get_execution(AgentRole.CODER, phase_id="phase-1")
        assert ex is not None
        assert ex.status == AgentExecutionStatus.FAILED
        assert ex.error == "test error"


class TestBackwardCompatibility:
    """Tests for backward compatibility with None phase_id."""

    def test_none_phase_id_default(self):
        """AgentExecutionModel defaults phase_id to None."""
        execution = AgentExecutionModel(
            role=AgentRoleType.CODER,
            status=AgentExecutionStatus.PENDING,
        )
        assert execution.phase_id is None

    def test_initialize_orchestration_no_phase_id(self):
        """initialize_orchestration without phase_id creates None-keyed executions."""
        contract = Contract(
            schemaVersion="1.0",
            issue={"number": 1, "title": "test", "url": "http://test"},
            phases=[],
        )
        state = initialize_orchestration(contract)

        # All executions should have phase_id=None
        for execution in state.executions.values():
            assert execution.phase_id is None

    def test_initialize_orchestration_with_phase_id(self):
        """initialize_orchestration with phase_id creates phase-keyed executions."""
        contract = Contract(
            schemaVersion="1.0",
            issue={"number": 1, "title": "test", "url": "http://test"},
            phases=[],
        )
        state = initialize_orchestration(contract, phase_id="phase-1")

        for execution in state.executions.values():
            assert execution.phase_id == "phase-1"

    def test_from_contract_preserves_phase_id(self):
        """from_contract preserves phase_id from contract executions."""
        contract = Contract(
            schemaVersion="1.0",
            issue={"number": 1, "title": "test", "url": "http://test"},
            phases=[],
            agent_executions=[
                AgentExecutionModel(
                    role=AgentRoleType.CODER,
                    phase_id="phase-1",
                    status=AgentExecutionStatus.COMPLETE,
                ),
                AgentExecutionModel(
                    role=AgentRoleType.TESTER,
                    status=AgentExecutionStatus.PENDING,
                ),
            ],
        )
        state = OrchestrationState.from_contract(contract)

        # Phase-keyed execution
        coder = state.get_execution(AgentRole.CODER, phase_id="phase-1")
        assert coder is not None
        assert coder.phase_id == "phase-1"

        # Role-only execution (None phase_id)
        tester = state.get_execution(AgentRole.TESTER)
        assert tester is not None
        assert tester.phase_id is None


class TestToExecutionList:
    """Tests for serialization back to list."""

    def test_to_execution_list_includes_phase_executions(self):
        """to_execution_list includes both role-only and phase-scoped executions."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-2"
        )
        state.set_execution(
            AgentRole.TESTER, AgentExecutionStatus.PENDING,
        )

        result = state.to_execution_list()
        assert len(result) == 3

    def test_to_execution_list_no_duplicates(self):
        """to_execution_list does not produce duplicate entries."""
        state = OrchestrationState()
        # Setting without phase_id populates both executions and phase_executions
        state.set_execution(AgentRole.CODER, AgentExecutionStatus.PENDING)

        result = state.to_execution_list()
        # Should only have 1 entry even though it's in both dicts
        assert len(result) == 1


class TestPhaseScopedDispatch:
    """Tests for phase-scoped can_agent_run and get_runnable_agents."""

    def test_can_agent_run_phase_scoped(self):
        """can_agent_run checks phase-scoped status."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        assert can_agent_run(AgentRole.CODER, state, phase_id="phase-1")

    def test_can_agent_run_wrong_phase(self):
        """can_agent_run returns False for non-existent phase."""
        state = OrchestrationState()
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        assert not can_agent_run(AgentRole.CODER, state, phase_id="phase-2")

    def test_get_runnable_agents_phase_scoped(self):
        """get_runnable_agents filters by phase_id."""
        state = OrchestrationState()
        # Phase 1: CODER pending
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )
        # Phase 2: CODER also pending
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-2"
        )

        # Only phase-1 agents should be returned when scoped to phase-1
        runnable = get_runnable_agents(state, phase_id="phase-1")
        assert AgentRole.CODER in runnable

    def test_phase_scoped_dependencies(self):
        """Dependencies are checked within phase scope."""
        state = OrchestrationState()
        # Phase 1: CODER complete, TESTER pending
        state.set_execution(
            AgentRole.CODER, AgentExecutionStatus.COMPLETE, phase_id="phase-1"
        )
        state.set_execution(
            AgentRole.TESTER, AgentExecutionStatus.PENDING, phase_id="phase-1"
        )

        # TESTER depends on CODER, which is complete in phase-1
        assert can_agent_run(AgentRole.TESTER, state, phase_id="phase-1")
