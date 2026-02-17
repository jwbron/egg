"""
Orchestrator dispatch logic for multi-agent workflows.

This module provides the main orchestration logic for coordinating
agent execution during the implement phase. It reads the contract,
determines which agents to run next, and manages state transitions.

Key concepts:
- Dispatch: Select and start agents for execution
- Wave: A group of agents running in parallel
- Handoff: Transfer of data between agents
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent_roles import AgentRole, get_role_definition
from .dependency_graph import compute_execution_plan
from .models import (
    AgentExecutionModel,
    AgentExecutionStatus,
    Contract,
)
from .orchestration import (
    OrchestrationState,
    get_runnable_agents,
    initialize_orchestration,
    update_contract_orchestration,
)

if TYPE_CHECKING:
    pass


@dataclass
class DispatchDecision:
    """Decision about which agents to dispatch next.

    Contains the list of agents to run and metadata about the decision.
    """

    agents_to_run: list[AgentRole]
    wave_number: int
    reason: str
    is_parallel: bool = False
    all_complete: bool = False
    has_failures: bool = False

    @classmethod
    def none(cls, reason: str) -> DispatchDecision:
        """Create a decision with no agents to run."""
        return cls(
            agents_to_run=[],
            wave_number=0,
            reason=reason,
        )

    @classmethod
    def complete(cls) -> DispatchDecision:
        """Create a decision indicating all agents are complete."""
        return cls(
            agents_to_run=[],
            wave_number=0,
            reason="All agents have completed",
            all_complete=True,
        )

    @classmethod
    def failed(cls, failed_agents: list[AgentRole]) -> DispatchDecision:
        """Create a decision indicating failure."""
        return cls(
            agents_to_run=[],
            wave_number=0,
            reason=f"Agents failed: {', '.join(r.value for r in failed_agents)}",
            has_failures=True,
        )


@dataclass
class AgentResult:
    """Result of an agent execution."""

    role: AgentRole
    success: bool
    commit: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Orchestrator:
    """Main orchestration controller for multi-agent workflows.

    Manages the lifecycle of agent executions, including:
    - Initializing orchestration state
    - Determining which agents to run next
    - Recording agent results
    - Managing handoffs between agents

    Supports two modes:
    - Role-only (Tier 2): Default mode, dispatch by role
    - Phase-scoped (Tier 3): Dispatch by (phase_id, role) composite key
    """

    def __init__(self, contract: Contract, phase_id: str | None = None):
        """Initialize the orchestrator with a contract.

        Args:
            contract: The contract to orchestrate
            phase_id: Optional plan phase ID for Tier 3 phase-scoped dispatch
        """
        self.contract = contract
        self.phase_id = phase_id
        self.state = OrchestrationState.from_contract(contract)

        # If no executions exist, initialize them
        if not self.state.executions:
            self.state = initialize_orchestration(contract, phase_id=phase_id)

    def get_next_dispatch(self) -> DispatchDecision:
        """Determine which agents to dispatch next.

        Returns:
            DispatchDecision with agents to run and metadata
        """
        # Check for failures first
        failed = self.state.get_failed_roles()
        if failed:
            return DispatchDecision.failed(failed)

        # Check if all complete
        if self.state.all_complete():
            return DispatchDecision.complete()

        # Get runnable agents (phase-scoped if phase_id is set)
        runnable = get_runnable_agents(self.state, phase_id=self.phase_id)

        if not runnable:
            # No agents can run - check why
            pending = self.state.get_pending_roles()
            running = [
                role
                for role, ex in self.state.executions.items()
                if ex.status == AgentExecutionStatus.RUNNING
            ]

            if running:
                return DispatchDecision.none(
                    f"Waiting for running agents: {', '.join(r.value for r in running)}"
                )
            elif pending:
                return DispatchDecision.none("Agents are pending but dependencies not met")
            else:
                return DispatchDecision.complete()

        # Determine wave number
        wave_number = self._compute_wave_number(runnable)

        return DispatchDecision(
            agents_to_run=runnable,
            wave_number=wave_number,
            reason=f"Wave {wave_number}: {', '.join(r.value for r in runnable)}",
            is_parallel=len(runnable) > 1,
        )

    def _compute_wave_number(self, runnable: list[AgentRole]) -> int:
        """Compute the wave number for the given runnable agents.

        Args:
            runnable: List of agents that can run

        Returns:
            Wave number (1-indexed)
        """
        plan = compute_execution_plan()

        for wave in plan:
            # Check if any runnable agent is in this wave
            for agent in runnable:
                if agent in wave.agents:
                    return wave.wave_number

        return 1  # Default to wave 1

    def start_agent(
        self, role: AgentRole, phase_id: str | None = None
    ) -> AgentExecutionModel:
        """Mark an agent as started.

        Args:
            role: The agent role to start
            phase_id: Optional phase ID override (uses self.phase_id if not set)

        Returns:
            Updated AgentExecutionModel
        """
        pid = phase_id if phase_id is not None else self.phase_id
        return self.state.mark_running(role, phase_id=pid)

    def complete_agent(
        self,
        role: AgentRole,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
        phase_id: str | None = None,
    ) -> AgentExecutionModel:
        """Mark an agent as complete.

        Args:
            role: The agent role
            commit: Git commit SHA if agent made changes
            outputs: Handoff data produced by agent
            phase_id: Optional phase ID override

        Returns:
            Updated AgentExecutionModel
        """
        pid = phase_id if phase_id is not None else self.phase_id
        return self.state.mark_complete(role, commit=commit, outputs=outputs, phase_id=pid)

    def fail_agent(
        self, role: AgentRole, error: str, phase_id: str | None = None
    ) -> AgentExecutionModel:
        """Mark an agent as failed.

        Args:
            role: The agent role
            error: Error message
            phase_id: Optional phase ID override

        Returns:
            Updated AgentExecutionModel
        """
        pid = phase_id if phase_id is not None else self.phase_id
        return self.state.mark_failed(role, error, phase_id=pid)

    def record_result(self, result: AgentResult) -> AgentExecutionModel:
        """Record the result of an agent execution.

        Args:
            result: The agent result

        Returns:
            Updated AgentExecutionModel
        """
        if result.success:
            return self.complete_agent(
                result.role,
                commit=result.commit,
                outputs=result.outputs,
            )
        else:
            return self.fail_agent(result.role, result.error or "Unknown error")

    def apply_to_contract(self) -> Contract:
        """Apply orchestration state back to the contract.

        Returns:
            Updated contract
        """
        return update_contract_orchestration(self.contract, self.state)

    def get_status_summary(self) -> dict[str, Any]:
        """Get a summary of orchestration status.

        Returns:
            Dictionary with status information
        """
        return {
            "total_agents": len(self.state.executions),
            "pending": len(self.state.get_pending_roles()),
            "running": len(
                [
                    r
                    for r, ex in self.state.executions.items()
                    if ex.status == AgentExecutionStatus.RUNNING
                ]
            ),
            "completed": len(self.state.get_completed_roles()),
            "failed": len(self.state.get_failed_roles()),
            "all_complete": self.state.all_complete(),
            "any_failed": self.state.any_failed(),
            "executions": {
                role.value: {
                    "status": ex.status.value,
                    "commit": ex.commit,
                    "error": ex.error,
                }
                for role, ex in self.state.executions.items()
            },
        }


def create_orchestrator(contract: Contract) -> Orchestrator:
    """Create an orchestrator for a contract.

    Args:
        contract: The contract to orchestrate

    Returns:
        Configured Orchestrator
    """
    return Orchestrator(contract)


def get_dispatch_for_contract(contract: Contract) -> DispatchDecision:
    """Get the next dispatch decision for a contract.

    Convenience function that creates an orchestrator and returns
    the next dispatch decision.

    Args:
        contract: The contract to check

    Returns:
        DispatchDecision with agents to run
    """
    orchestrator = create_orchestrator(contract)
    return orchestrator.get_next_dispatch()


def load_agent_output(repo_path: Path, role: AgentRole) -> dict[str, Any]:
    """Load the handoff output from an agent.

    Args:
        repo_path: Path to the repository
        role: The agent role

    Returns:
        Parsed output data (empty dict if not found)
    """
    output_file = repo_path / ".egg-state" / "agent-outputs" / f"{role.value}-output.json"

    if not output_file.exists():
        return {}

    try:
        with output_file.open() as f:
            result: dict[str, Any] = json.load(f)
            return result
    except (json.JSONDecodeError, OSError):
        return {}


def save_agent_output(
    repo_path: Path,
    role: AgentRole,
    outputs: dict[str, Any],
) -> Path:
    """Save handoff output for an agent.

    Args:
        repo_path: Path to the repository
        role: The agent role
        outputs: The output data to save

    Returns:
        Path to the saved file
    """
    output_dir = repo_path / ".egg-state" / "agent-outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"{role.value}-output.json"

    with output_file.open("w") as f:
        json.dump(outputs, f, indent=2)

    return output_file


def collect_handoff_data(
    repo_path: Path,
    target_role: AgentRole,
) -> dict[str, Any]:
    """Collect all handoff data for a target agent.

    Reads outputs from all agents that the target depends on and
    combines them into a single data structure.

    Args:
        repo_path: Path to the repository
        target_role: The agent role to collect data for

    Returns:
        Combined handoff data from dependencies
    """
    role_def = get_role_definition(target_role)
    handoff = {}

    for dep_role in role_def.dependencies:
        dep_output = load_agent_output(repo_path, dep_role)
        if dep_output:
            handoff[dep_role.value] = dep_output

    return handoff


def format_dispatch_for_workflow(decision: DispatchDecision) -> dict[str, Any]:
    """Format a dispatch decision for workflow output.

    Creates a structure suitable for GitHub Actions workflow output.

    Args:
        decision: The dispatch decision

    Returns:
        Dictionary for workflow output
    """
    return {
        "agents": [r.value for r in decision.agents_to_run],
        "wave": decision.wave_number,
        "parallel": decision.is_parallel,
        "complete": decision.all_complete,
        "failed": decision.has_failures,
        "reason": decision.reason,
    }
