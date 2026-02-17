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

from .agent_roles import AgentRole, get_role_definition
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


def _composite_key(role: AgentRole, phase_id: str | None = None) -> tuple[str | None, AgentRole]:
    """Create a composite key for execution tracking.

    Args:
        role: The agent role
        phase_id: Optional plan phase ID (e.g., 'phase-1')

    Returns:
        Tuple of (phase_id, role) for use as dict key
    """
    return (phase_id, role)


@dataclass
class OrchestrationState:
    """Complete state of multi-agent orchestration.

    Tracks all agent executions, their status, and coordination data.
    This state is persisted in the contract and used by the orchestrator
    to determine which agents to run next.

    Supports two keying modes:
    - Role-only (Tier 2): executions keyed by (None, role) for backward compatibility
    - Composite (Tier 3): executions keyed by (phase_id, role) for phase-level dispatch
    """

    executions: dict[AgentRole, AgentExecutionModel] = field(default_factory=dict)
    # Composite key executions: (phase_id, role) -> AgentExecutionModel
    phase_executions: dict[tuple[str | None, AgentRole], AgentExecutionModel] = field(
        default_factory=dict
    )
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
                # Also populate phase_executions for composite key support
                key = _composite_key(role, execution.phase_id)
                state.phase_executions[key] = execution
            except ValueError:
                # Skip unknown roles
                pass

        return state

    def to_execution_list(self) -> list[AgentExecutionModel]:
        """Convert executions dict back to a list for the contract.

        Returns:
            List of AgentExecutionModel objects
        """
        # If phase_executions has entries that aren't in executions,
        # include them too (Tier 3 mode)
        seen = set()
        result = []
        for execution in self.executions.values():
            key = (execution.phase_id, AgentRole(execution.role.value))
            if key not in seen:
                seen.add(key)
                result.append(execution)

        for key, execution in self.phase_executions.items():
            if key not in seen:
                seen.add(key)
                result.append(execution)

        return result

    def get_execution(
        self, role: AgentRole, phase_id: str | None = None
    ) -> AgentExecutionModel | None:
        """Get the execution state for a role, optionally scoped to a phase.

        Args:
            role: The agent role
            phase_id: Optional plan phase ID for composite key lookup

        Returns:
            AgentExecutionModel or None
        """
        if phase_id is not None:
            key = _composite_key(role, phase_id)
            return self.phase_executions.get(key)
        return self.executions.get(role)

    def set_execution(
        self,
        role: AgentRole,
        status: AgentExecutionStatus,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        phase_id: str | None = None,
    ) -> AgentExecutionModel:
        """Set or update the execution state for a role.

        Args:
            role: The agent role
            status: New status
            commit: Git commit SHA if agent made changes
            outputs: Handoff data produced by agent
            error: Error message if failed
            phase_id: Optional plan phase ID for composite key

        Returns:
            The updated AgentExecutionModel
        """
        now = datetime.utcnow().isoformat() + "Z"

        key = _composite_key(role, phase_id)

        # For phase-scoped lookups
        if phase_id is not None:
            if key not in self.phase_executions:
                self.phase_executions[key] = AgentExecutionModel(
                    role=AgentRoleType(role.value),
                    phase_id=phase_id,
                    status=status,
                )
            execution = self.phase_executions[key]
        else:
            if role not in self.executions:
                self.executions[role] = AgentExecutionModel(
                    role=AgentRoleType(role.value),
                    status=status,
                )
            execution = self.executions[role]
            # Mirror to phase_executions
            self.phase_executions[key] = execution

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

    def mark_running(self, role: AgentRole, phase_id: str | None = None) -> AgentExecutionModel:
        """Mark an agent as running."""
        return self.set_execution(role, AgentExecutionStatus.RUNNING, phase_id=phase_id)

    def mark_complete(
        self,
        role: AgentRole,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
        phase_id: str | None = None,
    ) -> AgentExecutionModel:
        """Mark an agent as complete."""
        return self.set_execution(
            role,
            AgentExecutionStatus.COMPLETE,
            commit=commit,
            outputs=outputs,
            phase_id=phase_id,
        )

    def mark_failed(
        self,
        role: AgentRole,
        error: str,
        phase_id: str | None = None,
    ) -> AgentExecutionModel:
        """Mark an agent as failed."""
        return self.set_execution(
            role,
            AgentExecutionStatus.FAILED,
            error=error,
            phase_id=phase_id,
        )

    def mark_skipped(self, role: AgentRole, phase_id: str | None = None) -> AgentExecutionModel:
        """Mark an agent as skipped."""
        return self.set_execution(role, AgentExecutionStatus.SKIPPED, phase_id=phase_id)

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
        """Get all roles that are pending execution.

        Only returns roles that have been registered in this orchestration's
        executions, not all possible roles from the enum.
        """
        pending = []
        for role, execution in self.executions.items():
            if execution.status == AgentExecutionStatus.PENDING:
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
        """Check if all enabled agents have completed (successfully or skipped).

        Only checks roles that exist in self.executions, not all possible roles.
        This allows disabling roles via multi_agent_config.roles_enabled without
        blocking completion.
        """
        # If no executions configured, consider complete
        if not self.executions:
            return True

        for execution in self.executions.values():
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


def initialize_orchestration(
    contract: Contract,
    roles: list[AgentRole] | None = None,
    phase_id: str | None = None,
) -> OrchestrationState:
    """Initialize orchestration state for a contract.

    Creates pending executions for the specified agent roles (or defaults
    based on contract configuration).

    Args:
        contract: The contract to initialize orchestration for
        roles: Specific roles to use. If None, uses the contract's
            multi_agent_config.roles_enabled or defaults to the 4
            implement-phase roles for backward compatibility.
        phase_id: Optional plan phase ID for Tier 3 composite keying.
            When set, executions are keyed by (phase_id, role).

    Returns:
        Initialized OrchestrationState
    """
    state = OrchestrationState()
    state.started_at = datetime.utcnow().isoformat() + "Z"

    if roles is not None:
        enabled_roles = roles
    elif contract.multi_agent_config is not None:
        enabled_roles = [AgentRole(r.value) for r in contract.multi_agent_config.roles_enabled]
    else:
        # Default: implement-phase roles for backward compatibility
        enabled_roles = [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.INTEGRATOR,
        ]

    # Create pending execution for each enabled role
    for role in enabled_roles:
        execution = AgentExecutionModel(
            role=AgentRoleType(role.value),
            phase_id=phase_id,
            status=AgentExecutionStatus.PENDING,
        )
        state.executions[role] = execution
        key = _composite_key(role, phase_id)
        state.phase_executions[key] = execution

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


def can_agent_run(
    role: AgentRole,
    state: OrchestrationState,
    phase_id: str | None = None,
) -> bool:
    """Check if an agent can run based on its dependencies.

    An agent can run if:
    1. It is pending execution
    2. All its dependencies have completed successfully

    Args:
        role: The agent role to check
        state: Current orchestration state
        phase_id: Optional plan phase ID for phase-scoped check

    Returns:
        True if the agent can run
    """
    execution = state.get_execution(role, phase_id=phase_id)

    # Can't run if not registered or not pending
    if execution is None:
        return False
    if execution.status != AgentExecutionStatus.PENDING:
        return False

    # Check dependencies
    role_def = get_role_definition(role)
    for dep in role_def.dependencies:
        dep_execution = state.get_execution(dep, phase_id=phase_id)
        if dep_execution is None or dep_execution.status != AgentExecutionStatus.COMPLETE:
            return False

    return True


def get_runnable_agents(
    state: OrchestrationState,
    phase_id: str | None = None,
) -> list[AgentRole]:
    """Get all agents that can currently run.

    Returns agents that are pending and have all dependencies satisfied.
    These agents can be run in parallel. Only considers roles registered
    in the orchestration state, not all possible roles.

    Args:
        state: Current orchestration state
        phase_id: Optional plan phase ID for phase-scoped check

    Returns:
        List of roles that can run now
    """
    runnable = []
    if phase_id is not None:
        # Phase-scoped: only consider executions for this phase
        for key, _execution in state.phase_executions.items():
            key_phase_id, key_role = key
            if key_phase_id == phase_id and can_agent_run(key_role, state, phase_id=phase_id):
                runnable.append(key_role)
    else:
        for role in state.executions:
            if can_agent_run(role, state):
                runnable.append(role)
    return runnable


def get_next_wave(
    state: OrchestrationState,
    phase_id: str | None = None,
) -> list[AgentRole]:
    """Get the next wave of agents to run.

    A wave is a set of agents that can run in parallel. This function
    returns all agents that are ready to run, considering dependencies.

    Args:
        state: Current orchestration state
        phase_id: Optional plan phase ID for phase-scoped check

    Returns:
        List of roles in the next wave
    """
    return get_runnable_agents(state, phase_id=phase_id)
