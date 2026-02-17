"""Tests for Orchestrator class with phase_id parameter (Tier 3).

Covers:
- Initialization with phase_id
- start_agent, complete_agent, fail_agent with phase_id
- get_next_dispatch with phase-scoped state
- Backward compatibility (phase_id=None)
- Independent tracking across phases
"""

from __future__ import annotations

from egg_contracts.agent_roles import AgentRole
from egg_contracts.models import (
    AgentExecutionStatus,
    AgentRoleType,
    Contract,
)
from egg_contracts.orchestrator import Orchestrator


def _make_contract(**kwargs) -> Contract:
    """Create a minimal Contract for testing."""
    defaults = {
        "schemaVersion": "1.0",
        "issue": {"number": 1, "title": "test", "url": "http://test"},
        "phases": [],
    }
    defaults.update(kwargs)
    return Contract(**defaults)


class TestOrchestratorInitWithPhaseId:
    """Tests for Orchestrator initialization with phase_id."""

    def test_init_without_phase_id(self):
        """Orchestrator initializes without phase_id (backward compat)."""
        contract = _make_contract()
        orch = Orchestrator(contract)
        assert orch.phase_id is None

    def test_init_with_phase_id(self):
        """Orchestrator stores phase_id."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")
        assert orch.phase_id == "phase-1"

    def test_init_with_phase_id_creates_phase_keyed_executions(self):
        """Orchestrator with phase_id creates phase-keyed executions."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        # All executions should have phase_id set
        for execution in orch.state.executions.values():
            assert execution.phase_id == "phase-1"

    def test_init_without_phase_id_creates_none_keyed_executions(self):
        """Orchestrator without phase_id creates None-keyed executions."""
        contract = _make_contract()
        orch = Orchestrator(contract)

        for execution in orch.state.executions.values():
            assert execution.phase_id is None


class TestOrchestratorStartAgent:
    """Tests for Orchestrator.start_agent with phase_id."""

    def test_start_agent_uses_orchestrator_phase_id(self):
        """start_agent uses Orchestrator's phase_id by default."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        execution = orch.start_agent(AgentRole.CODER)
        assert execution.status == AgentExecutionStatus.RUNNING
        assert execution.started_at is not None

    def test_start_agent_with_explicit_phase_id(self):
        """start_agent with explicit phase_id overrides Orchestrator's."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        # Need to set up execution for phase-2 first
        orch.state.set_execution(AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-2")

        execution = orch.start_agent(AgentRole.CODER, phase_id="phase-2")
        assert execution.status == AgentExecutionStatus.RUNNING

    def test_start_agent_no_phase_id_backward_compat(self):
        """start_agent without phase_id works for Tier 2."""
        contract = _make_contract()
        orch = Orchestrator(contract)

        execution = orch.start_agent(AgentRole.CODER)
        assert execution.status == AgentExecutionStatus.RUNNING


class TestOrchestratorCompleteAgent:
    """Tests for Orchestrator.complete_agent with phase_id."""

    def test_complete_agent_uses_orchestrator_phase_id(self):
        """complete_agent uses Orchestrator's phase_id by default."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        execution = orch.complete_agent(AgentRole.CODER, commit="abc123")

        assert execution.status == AgentExecutionStatus.COMPLETE
        assert execution.commit == "abc123"

    def test_complete_agent_with_outputs(self):
        """complete_agent records outputs correctly."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        outputs = {"summary": "Implemented phase 1"}
        execution = orch.complete_agent(AgentRole.CODER, commit="abc123", outputs=outputs)

        assert execution.status == AgentExecutionStatus.COMPLETE
        assert execution.outputs == outputs

    def test_complete_agent_with_explicit_phase_id(self):
        """complete_agent with explicit phase_id overrides Orchestrator's."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        # Set up and start for phase-2
        orch.state.set_execution(AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-2")
        orch.state.mark_running(AgentRole.CODER, phase_id="phase-2")

        execution = orch.complete_agent(AgentRole.CODER, commit="def456", phase_id="phase-2")
        assert execution.status == AgentExecutionStatus.COMPLETE
        assert execution.commit == "def456"


class TestOrchestratorFailAgent:
    """Tests for Orchestrator.fail_agent with phase_id."""

    def test_fail_agent_uses_orchestrator_phase_id(self):
        """fail_agent uses Orchestrator's phase_id by default."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        execution = orch.fail_agent(AgentRole.CODER, error="test error")

        assert execution.status == AgentExecutionStatus.FAILED
        assert execution.error == "test error"

    def test_fail_agent_with_explicit_phase_id(self):
        """fail_agent with explicit phase_id overrides Orchestrator's."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        # Set up and start for phase-2
        orch.state.set_execution(AgentRole.CODER, AgentExecutionStatus.PENDING, phase_id="phase-2")
        orch.state.mark_running(AgentRole.CODER, phase_id="phase-2")

        execution = orch.fail_agent(AgentRole.CODER, error="phase-2 error", phase_id="phase-2")
        assert execution.status == AgentExecutionStatus.FAILED
        assert execution.error == "phase-2 error"


class TestOrchestratorGetNextDispatch:
    """Tests for get_next_dispatch with phase-scoped state."""

    def test_dispatch_returns_coder_first(self):
        """First dispatch should include CODER (no dependencies)."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        decision = orch.get_next_dispatch()
        assert AgentRole.CODER in decision.agents_to_run

    def test_dispatch_after_coder_complete(self):
        """After CODER completes, TESTER and DOCUMENTER become runnable."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        orch.complete_agent(AgentRole.CODER, commit="abc123")

        decision = orch.get_next_dispatch()
        assert AgentRole.TESTER in decision.agents_to_run

    def test_dispatch_waits_for_running_agents(self):
        """Dispatch returns waiting state when agents are running."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)

        decision = orch.get_next_dispatch()
        # CODER is running, so should indicate waiting
        assert decision.agents_to_run == [] or (AgentRole.CODER not in decision.agents_to_run)

    def test_dispatch_returns_failed_on_failure(self):
        """Dispatch returns failed decision when an agent fails."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        orch.fail_agent(AgentRole.CODER, error="boom")

        decision = orch.get_next_dispatch()
        assert decision.has_failures is True


class TestOrchestratorPhaseIndependence:
    """Tests for independent phase tracking."""

    def test_two_orchestrators_different_phases(self):
        """Two Orchestrators with different phase_ids track independently."""
        contract1 = _make_contract()
        contract2 = _make_contract()

        orch1 = Orchestrator(contract1, phase_id="phase-1")
        orch2 = Orchestrator(contract2, phase_id="phase-2")

        # Start and complete coder in phase-1
        orch1.start_agent(AgentRole.CODER)
        orch1.complete_agent(AgentRole.CODER, commit="abc123")

        # Phase-2 coder should still be pending
        coder_p2 = orch2.state.get_execution(AgentRole.CODER, phase_id="phase-2")
        assert coder_p2 is not None
        assert coder_p2.status == AgentExecutionStatus.PENDING

    def test_apply_to_contract_preserves_phase_id(self):
        """apply_to_contract preserves phase_id in execution models."""
        contract = _make_contract()
        orch = Orchestrator(contract, phase_id="phase-1")

        orch.start_agent(AgentRole.CODER)
        orch.complete_agent(AgentRole.CODER, commit="abc123")

        updated_contract = orch.apply_to_contract()
        # Find coder execution
        coder_execs = [
            ex for ex in updated_contract.agent_executions if ex.role == AgentRoleType.CODER
        ]
        assert len(coder_execs) > 0
        # At least one should have phase_id set
        phase_1_coders = [ex for ex in coder_execs if ex.phase_id == "phase-1"]
        assert len(phase_1_coders) > 0
