"""
Agent result collection and handoff data management.

Handles collecting outputs from agents and passing handoff data
to dependent agents.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for logging and contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from egg_contracts.agent_roles import (
    AgentRole as ContractAgentRole,
    get_role_definition,
)
from egg_contracts.orchestrator import (
    collect_handoff_data as contract_collect_handoff,
    load_agent_output,
    save_agent_output as contract_save_output,
)

from models import AgentExecution, AgentExecutionStatus, AgentRole

logger = get_logger("orchestrator.handoffs")


# Mapping between orchestrator and contract agent roles
ROLE_MAP = {
    AgentRole.CODER: ContractAgentRole.CODER,
    AgentRole.TESTER: ContractAgentRole.TESTER,
    AgentRole.DOCUMENTER: ContractAgentRole.DOCUMENTER,
    AgentRole.INTEGRATOR: ContractAgentRole.INTEGRATOR,
}

REVERSE_ROLE_MAP = {v: k for k, v in ROLE_MAP.items()}


class HandoffData:
    """Container for handoff data between agents."""

    def __init__(
        self,
        source_role: AgentRole,
        data: dict[str, Any],
        timestamp: datetime | None = None,
    ):
        """Initialize handoff data.

        Args:
            source_role: Agent that produced the data
            data: Handoff data dictionary
            timestamp: When data was produced
        """
        self.source_role = source_role
        self.data = data
        self.timestamp = timestamp or datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "source_role": self.source_role.value,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HandoffData":
        """Create from dictionary representation."""
        return cls(
            source_role=AgentRole(d["source_role"]),
            data=d["data"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
        )


class AgentOutput:
    """Output produced by an agent execution."""

    def __init__(
        self,
        role: AgentRole,
        commit: str | None = None,
        files_changed: list[str] | None = None,
        handoff_data: dict[str, Any] | None = None,
        logs: str | None = None,
        metrics: dict[str, Any] | None = None,
    ):
        """Initialize agent output.

        Args:
            role: Agent role
            commit: Git commit SHA if changes made
            files_changed: List of changed files
            handoff_data: Data for dependent agents
            logs: Execution logs
            metrics: Performance metrics
        """
        self.role = role
        self.commit = commit
        self.files_changed = files_changed or []
        self.handoff_data = handoff_data or {}
        self.logs = logs
        self.metrics = metrics or {}
        self.timestamp = datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "role": self.role.value,
            "commit": self.commit,
            "files_changed": self.files_changed,
            "handoff_data": self.handoff_data,
            "logs": self.logs,
            "metrics": self.metrics,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentOutput":
        """Create from dictionary representation."""
        output = cls(
            role=AgentRole(d["role"]),
            commit=d.get("commit"),
            files_changed=d.get("files_changed", []),
            handoff_data=d.get("handoff_data", {}),
            logs=d.get("logs"),
            metrics=d.get("metrics", {}),
        )
        if d.get("timestamp"):
            output.timestamp = datetime.fromisoformat(d["timestamp"])
        return output


def save_agent_output(
    repo_path: Path,
    output: AgentOutput,
) -> Path:
    """Save agent output to disk.

    Args:
        repo_path: Path to repository
        output: Agent output to save

    Returns:
        Path to saved file
    """
    contract_role = ROLE_MAP[output.role]
    output_data = output.to_dict()

    return contract_save_output(repo_path, contract_role, output_data)


def load_agent_output_data(
    repo_path: Path,
    role: AgentRole,
) -> AgentOutput | None:
    """Load agent output from disk.

    Args:
        repo_path: Path to repository
        role: Agent role

    Returns:
        AgentOutput or None if not found
    """
    contract_role = ROLE_MAP[role]
    data = load_agent_output(repo_path, contract_role)

    if not data:
        return None

    return AgentOutput.from_dict(data)


def collect_handoff_data(
    repo_path: Path,
    target_role: AgentRole,
) -> dict[str, HandoffData]:
    """Collect handoff data for a target agent.

    Gathers outputs from all agents that the target depends on.

    Args:
        repo_path: Path to repository
        target_role: Target agent role

    Returns:
        Dictionary mapping source role to handoff data
    """
    contract_role = ROLE_MAP[target_role]
    role_def = get_role_definition(contract_role)

    handoffs = {}

    for dep_role in role_def.dependencies:
        orch_role = REVERSE_ROLE_MAP[dep_role]
        output = load_agent_output_data(repo_path, orch_role)

        if output and output.handoff_data:
            handoffs[orch_role.value] = HandoffData(
                source_role=orch_role,
                data=output.handoff_data,
                timestamp=output.timestamp,
            )

    return handoffs


def get_handoff_env_var(
    repo_path: Path,
    target_role: AgentRole,
) -> str:
    """Get handoff data as a JSON string for environment variable.

    Args:
        repo_path: Path to repository
        target_role: Target agent role

    Returns:
        JSON string of handoff data
    """
    handoffs = collect_handoff_data(repo_path, target_role)

    data = {
        role: handoff.data
        for role, handoff in handoffs.items()
    }

    return json.dumps(data)


def get_agent_dependencies(role: AgentRole) -> list[AgentRole]:
    """Get the dependencies for an agent role.

    Args:
        role: Agent role

    Returns:
        List of roles this agent depends on
    """
    contract_role = ROLE_MAP[role]
    role_def = get_role_definition(contract_role)

    return [
        REVERSE_ROLE_MAP[dep_role]
        for dep_role in role_def.dependencies
    ]


def get_agent_dependents(role: AgentRole) -> list[AgentRole]:
    """Get agents that depend on this role.

    Args:
        role: Agent role

    Returns:
        List of roles that depend on this agent
    """
    dependents = []

    for orch_role in AgentRole:
        deps = get_agent_dependencies(orch_role)
        if role in deps:
            dependents.append(orch_role)

    return dependents


def format_handoff_for_prompt(
    handoff_data: dict[str, HandoffData],
) -> str:
    """Format handoff data for inclusion in agent prompt.

    Args:
        handoff_data: Handoff data dictionary

    Returns:
        Formatted string for prompt
    """
    if not handoff_data:
        return "No handoff data from previous agents."

    lines = ["## Handoff Data from Previous Agents\n"]

    for role, handoff in handoff_data.items():
        lines.append(f"### From {role.upper()}")
        lines.append(f"*Produced at: {handoff.timestamp.isoformat()}*\n")

        for key, value in handoff.data.items():
            if isinstance(value, list):
                lines.append(f"**{key}:**")
                for item in value:
                    lines.append(f"  - {item}")
            elif isinstance(value, dict):
                lines.append(f"**{key}:**")
                lines.append(f"```json\n{json.dumps(value, indent=2)}\n```")
            else:
                lines.append(f"**{key}:** {value}")

        lines.append("")

    return "\n".join(lines)


class ResultCollector:
    """Collects and aggregates results from multiple agents."""

    def __init__(self):
        """Initialize collector."""
        self.results: dict[AgentRole, AgentExecution] = {}
        self.outputs: dict[AgentRole, AgentOutput] = {}

    def add_result(
        self,
        execution: AgentExecution,
        output: AgentOutput | None = None,
    ) -> None:
        """Add an agent result.

        Args:
            execution: Agent execution result
            output: Optional agent output
        """
        self.results[execution.role] = execution
        if output:
            self.outputs[execution.role] = output

    def get_successful(self) -> list[AgentExecution]:
        """Get all successful executions."""
        return [
            ex for ex in self.results.values()
            if ex.status == AgentExecutionStatus.COMPLETE
        ]

    def get_failed(self) -> list[AgentExecution]:
        """Get all failed executions."""
        return [
            ex for ex in self.results.values()
            if ex.status == AgentExecutionStatus.FAILED
        ]

    def all_succeeded(self) -> bool:
        """Check if all agents succeeded."""
        return len(self.get_failed()) == 0 and len(self.results) > 0

    def get_commits(self) -> list[str]:
        """Get all commit SHAs from successful agents."""
        return [
            ex.commit
            for ex in self.get_successful()
            if ex.commit
        ]

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all results."""
        return {
            "total": len(self.results),
            "successful": len(self.get_successful()),
            "failed": len(self.get_failed()),
            "commits": self.get_commits(),
            "by_role": {
                role.value: {
                    "status": ex.status.value,
                    "commit": ex.commit,
                    "error": ex.error,
                }
                for role, ex in self.results.items()
            },
        }
