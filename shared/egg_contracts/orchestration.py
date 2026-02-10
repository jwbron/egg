"""
Agent orchestration state management.

This module provides state management for multi-agent orchestration,
tracking agent executions, managing handoffs between agents, and
coordinating parallel execution where dependencies allow.

Key concepts:
- Execution: An individual agent run with status, outputs, and timing
- Orchestration State: The complete state of all agent executions
- Handoff: Data passed between agents (changed files, test results, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .agent_roles import AgentRole, AgentStatus, get_role_definition
from .models import AgentExecutionModel, AgentExecutionStatus, AgentRoleType

if TYPE_CHECKING:
    from .models import Contract


@dataclass
class AgentHandoff:
    """Data passed from one agent to another.

    Agents produce handoff data that downstream agents consume.
    This enables coordination without tight coupling.
    """

    source_agent: AgentRole
    target_agents: list[AgentRole]
    data: dict[str, Any]
    timestamp: str  # ISO format

    def has_key(self, key: str) -> bool:
        """Check if the handoff contains a specific key."""
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from the handoff data."""
        return self.data.get(key, default)


@dataclass
class OrchestrationState:
    """Complete state of multi-agent orchestration.

    Tracks all agent executions, their status, and coordination data.
    This state is persisted in the contract and used by the orchestrator
    to determine which agents to run next.
    """

    executions: dict[AgentRole, AgentExecutionModel] = field(default_factory=dict)
    handoffs: list[AgentHandoff] = field(default_factory=list)
    started_at: str | None = None  # ISO format
    completed_at: str | None = None  # ISO format
    current_wave: int = 0  # Current parallel execution wave

    @classmethod
    def from_contract(cls, contract: Contract) -> OrchestrationState:
        """Create OrchestrationState from a contract.

        Args:
            contract: The Contract to extract state from

        Returns:
            OrchestrationState with current execution data
        """
        state = cls()

        # Convert agent_executions list to dict keyed by role
        for execution in contract.agent_executions:
            try:
                role = AgentRole(execution.role.value)
                state.executions[role] = execution
            except ValueError:
                # Skip unknown roles
                pass

        return state

    def to_execution_list(self) -> list[AgentExecutionModel]:
        """Convert executions dict back to a list for the contract.

        Returns:
            List of AgentExecutionModel objects
        """
        return list(self.executions.values())

    def get_execution(self, role: AgentRole) -> AgentExecutionModel | None:
        """Get the execution state for a role."""
        return self.executions.get(role)

    def set_execution(
        self,
        role: AgentRole,
        status: AgentExecutionStatus,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentExecutionModel:
        """Set or update the execution state for a role.

        Args:
            role: The agent role
            status: New status
            commit: Git commit SHA if agent made changes
            outputs: Handoff data produced by agent
            error: Error message if failed

        Returns:
            The updated AgentExecutionModel
        """
        now = datetime.utcnow().isoformat() + "Z"

        if role not in self.executions:
            self.executions[role] = AgentExecutionModel(
                role=AgentRoleType(role.value),
                status=status,
            )

        execution = self.executions[role]
        execution.status = status

        if status == AgentExecutionStatus.RUNNING and execution.started_at is None:
            execution.started_at = datetime.fromisoformat(now.replace("Z", "+00:00"))

        if status in (AgentExecutionStatus.COMPLETE, AgentExecutionStatus.FAILED):
            execution.completed_at = datetime.fromisoformat(now.replace("Z", "+00:00"))

        if commit is not None:
            execution.commit = commit

        if outputs is not None:
            execution.outputs = outputs

        if error is not None:
            execution.error = error
            execution.status = AgentExecutionStatus.FAILED

        return execution

    def mark_running(self, role: AgentRole) -> AgentExecutionModel:
        """Mark an agent as running."""
        return self.set_execution(role, AgentExecutionStatus.RUNNING)

    def mark_complete(
        self,
        role: AgentRole,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> AgentExecutionModel:
        """Mark an agent as complete."""
        return self.set_execution(
            role,
            AgentExecutionStatus.COMPLETE,
            commit=commit,
            outputs=outputs,
        )

    def mark_failed(
        self,
        role: AgentRole,
        error: str,
    ) -> AgentExecutionModel:
        """Mark an agent as failed."""
        return self.set_execution(
            role,
            AgentExecutionStatus.FAILED,
            error=error,
        )

    def mark_skipped(self, role: AgentRole) -> AgentExecutionModel:
        """Mark an agent as skipped."""
        return self.set_execution(role, AgentExecutionStatus.SKIPPED)

    def add_handoff(
        self,
        source: AgentRole,
        targets: list[AgentRole],
        data: dict[str, Any],
    ) -> AgentHandoff:
        """Add a handoff from one agent to others.

        Args:
            source: The agent producing the handoff
            targets: The agents that will consume it
            data: The handoff data

        Returns:
            The created AgentHandoff
        """
        handoff = AgentHandoff(
            source_agent=source,
            target_agents=targets,
            data=data,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )
        self.handoffs.append(handoff)
        return handoff

    def get_handoffs_for(self, role: AgentRole) -> list[AgentHandoff]:
        """Get all handoffs targeted at a specific agent.

        Args:
            role: The agent role to get handoffs for

        Returns:
            List of handoffs for this agent
        """
        return [h for h in self.handoffs if role in h.target_agents]

    def get_pending_roles(self) -> list[AgentRole]:
        """Get all roles that are pending execution."""
        pending = []
        for role in AgentRole:
            execution = self.executions.get(role)
            if execution is None or execution.status == AgentExecutionStatus.PENDING:
                pending.append(role)
        return pending

    def get_completed_roles(self) -> list[AgentRole]:
        """Get all roles that have completed successfully."""
        completed = []
        for role, execution in self.executions.items():
            if execution.status == AgentExecutionStatus.COMPLETE:
                completed.append(role)
        return completed

    def get_failed_roles(self) -> list[AgentRole]:
        """Get all roles that have failed."""
        failed = []
        for role, execution in self.executions.items():
            if execution.status == AgentExecutionStatus.FAILED:
                failed.append(role)
        return failed

    def all_complete(self) -> bool:
        """Check if all agents have completed (successfully or skipped)."""
        for role in AgentRole:
            execution = self.executions.get(role)
            if execution is None:
                return False
            if execution.status not in (
                AgentExecutionStatus.COMPLETE,
                AgentExecutionStatus.SKIPPED,
            ):
                return False
        return True

    def any_failed(self) -> bool:
        """Check if any agents have failed."""
        for execution in self.executions.values():
            if execution.status == AgentExecutionStatus.FAILED:
                return True
        return False


def initialize_orchestration(contract: Contract) -> OrchestrationState:
    """Initialize orchestration state for a contract.

    Creates pending executions for all agent roles based on the
    contract configuration.

    Args:
        contract: The contract to initialize orchestration for

    Returns:
        Initialized OrchestrationState
    """
    state = OrchestrationState()
    state.started_at = datetime.utcnow().isoformat() + "Z"

    # Check which roles are enabled
    enabled_roles = list(AgentRole)  # Default: all roles
    if contract.multi_agent_config is not None:
        enabled_roles = [
            AgentRole(r.value)
            for r in contract.multi_agent_config.roles_enabled
        ]

    # Create pending execution for each enabled role
    for role in enabled_roles:
        state.executions[role] = AgentExecutionModel(
            role=AgentRoleType(role.value),
            status=AgentExecutionStatus.PENDING,
        )

    return state


def update_contract_orchestration(
    contract: Contract,
    state: OrchestrationState,
) -> Contract:
    """Update a contract with orchestration state.

    Args:
        contract: The contract to update
        state: The orchestration state to apply

    Returns:
        The updated contract
    """
    contract.agent_executions = state.to_execution_list()
    return contract


def can_agent_run(role: AgentRole, state: OrchestrationState) -> bool:
    """Check if an agent can run based on its dependencies.

    An agent can run if:
    1. It is pending execution
    2. All its dependencies have completed successfully

    Args:
        role: The agent role to check
        state: Current orchestration state

    Returns:
        True if the agent can run
    """
    execution = state.executions.get(role)

    # Can't run if not pending
    if execution is not None and execution.status != AgentExecutionStatus.PENDING:
        return False

    # Check dependencies
    role_def = get_role_definition(role)
    for dep in role_def.dependencies:
        dep_execution = state.executions.get(dep)
        if dep_execution is None or dep_execution.status != AgentExecutionStatus.COMPLETE:
            return False

    return True


def get_runnable_agents(state: OrchestrationState) -> list[AgentRole]:
    """Get all agents that can currently run.

    Returns agents that are pending and have all dependencies satisfied.
    These agents can be run in parallel.

    Args:
        state: Current orchestration state

    Returns:
        List of roles that can run now
    """
    runnable = []
    for role in AgentRole:
        if can_agent_run(role, state):
            runnable.append(role)
    return runnable


def get_next_wave(state: OrchestrationState) -> list[AgentRole]:
    """Get the next wave of agents to run.

    A wave is a set of agents that can run in parallel. This function
    returns all agents that are ready to run, considering dependencies.

    Args:
        state: Current orchestration state

    Returns:
        List of roles in the next wave
    """
    return get_runnable_agents(state)
