"""
Dispatch logic integration with egg_contracts.Orchestrator.

Bridges the orchestrator container service with the existing
dispatch logic from egg_contracts.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from egg_contracts import load_contract, save_contract
from egg_contracts.agent_roles import AgentRole as ContractAgentRole
from egg_contracts.orchestrator import (
    DispatchDecision,
    collect_handoff_data,
    create_orchestrator,
    format_dispatch_for_workflow,
    save_agent_output,
)
from egg_contracts.orchestrator import (
    Orchestrator as ContractOrchestrator,
)
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    Pipeline,
)
from state_store import get_state_store

logger = get_logger("orchestrator.dispatch")


def map_contract_role_to_agent_role(contract_role: ContractAgentRole) -> AgentRole:
    """Map egg_contracts AgentRole to orchestrator AgentRole.

    Args:
        contract_role: Role from egg_contracts

    Returns:
        Corresponding orchestrator AgentRole
    """
    mapping = {
        ContractAgentRole.CODER: AgentRole.CODER,
        ContractAgentRole.TESTER: AgentRole.TESTER,
        ContractAgentRole.DOCUMENTER: AgentRole.DOCUMENTER,
        ContractAgentRole.INTEGRATOR: AgentRole.INTEGRATOR,
        ContractAgentRole.ARCHITECT: AgentRole.ARCHITECT,
        ContractAgentRole.TASK_PLANNER: AgentRole.TASK_PLANNER,
        ContractAgentRole.RISK_ANALYST: AgentRole.RISK_ANALYST,
    }
    return mapping[contract_role]


def map_agent_role_to_contract_role(agent_role: AgentRole) -> ContractAgentRole | None:
    """Map orchestrator AgentRole to egg_contracts AgentRole.

    Args:
        agent_role: Role from orchestrator

    Returns:
        Corresponding egg_contracts AgentRole, or None if no mapping exists
        (e.g. REVIEWER, CHECKER roles don't interact with contracts)
    """
    mapping = {
        AgentRole.CODER: ContractAgentRole.CODER,
        AgentRole.TESTER: ContractAgentRole.TESTER,
        AgentRole.DOCUMENTER: ContractAgentRole.DOCUMENTER,
        AgentRole.INTEGRATOR: ContractAgentRole.INTEGRATOR,
        AgentRole.ARCHITECT: ContractAgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER: ContractAgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST: ContractAgentRole.RISK_ANALYST,
    }
    return mapping.get(agent_role)


class PipelineDispatcher:
    """Manages dispatch decisions for pipeline execution.

    Wraps the egg_contracts.Orchestrator to integrate with
    the orchestrator container service.
    """

    def __init__(self, pipeline: Pipeline, repo_path: Path):
        """Initialize dispatcher.

        Args:
            pipeline: Pipeline to dispatch for
            repo_path: Path to repository
        """
        self.pipeline = pipeline
        self.repo_path = repo_path
        self._contract_orchestrator: ContractOrchestrator | None = None

    @property
    def contract_orchestrator(self) -> ContractOrchestrator:
        """Get or create the contract orchestrator."""
        if self._contract_orchestrator is None:
            contract = load_contract(self.pipeline.issue_number, self.repo_path)
            self._contract_orchestrator = create_orchestrator(contract)
        return self._contract_orchestrator

    def get_next_dispatch(self) -> DispatchDecision:
        """Get the next dispatch decision.

        Returns:
            DispatchDecision from egg_contracts
        """
        return self.contract_orchestrator.get_next_dispatch()

    def get_agents_to_run(self) -> list[AgentRole]:
        """Get list of agents to run next.

        Returns:
            List of AgentRole values
        """
        decision = self.get_next_dispatch()
        return [map_contract_role_to_agent_role(role) for role in decision.agents_to_run]

    def is_complete(self) -> bool:
        """Check if all agents have completed.

        Returns:
            True if implementation is complete
        """
        decision = self.get_next_dispatch()
        return decision.all_complete

    def has_failures(self) -> bool:
        """Check if any agents have failed.

        Returns:
            True if any agent failed
        """
        decision = self.get_next_dispatch()
        return decision.has_failures

    def start_agent(self, role: AgentRole) -> AgentExecution:
        """Mark an agent as started.

        Args:
            role: Agent role to start

        Returns:
            AgentExecution with updated state
        """
        contract_role = map_agent_role_to_contract_role(role)
        if contract_role is not None:
            self.contract_orchestrator.start_agent(contract_role)

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

    def complete_agent(
        self,
        role: AgentRole,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> AgentExecution:
        """Mark an agent as complete.

        Args:
            role: Agent role
            commit: Git commit SHA if changes made
            outputs: Handoff data for dependent agents

        Returns:
            AgentExecution with updated state
        """
        contract_role = map_agent_role_to_contract_role(role)
        if contract_role is not None:
            self.contract_orchestrator.complete_agent(
                contract_role,
                commit=commit,
                outputs=outputs,
            )

            # Save outputs if provided — only for contract-mapped roles (CODER,
            # TESTER, DOCUMENTER, INTEGRATOR). REVIEWER and CHECKER roles don't
            # have contract counterparts; their verdicts are stored in the
            # AgentExecution record returned below, not as contract outputs.
            if outputs:
                save_agent_output(self.repo_path, contract_role, outputs)

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.COMPLETE,
            commit=commit,
            outputs=outputs or {},
            completed_at=datetime.utcnow(),
        )

    def fail_agent(self, role: AgentRole, error: str) -> AgentExecution:
        """Mark an agent as failed.

        Args:
            role: Agent role
            error: Error message

        Returns:
            AgentExecution with updated state
        """
        contract_role = map_agent_role_to_contract_role(role)
        if contract_role is not None:
            self.contract_orchestrator.fail_agent(contract_role, error)

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.FAILED,
            error=error,
            completed_at=datetime.utcnow(),
        )

    def get_handoff_data(self, role: AgentRole) -> dict[str, Any]:
        """Get handoff data for an agent.

        Collects outputs from all agents that the target depends on.

        Args:
            role: Target agent role

        Returns:
            Combined handoff data
        """
        contract_role = map_agent_role_to_contract_role(role)
        if contract_role is None:
            return {}
        return collect_handoff_data(self.repo_path, contract_role)

    def save_contract(self) -> None:
        """Save updated contract to disk."""
        updated_contract = self.contract_orchestrator.apply_to_contract()
        save_contract(updated_contract, self.repo_path)

    def get_status_summary(self) -> dict[str, Any]:
        """Get orchestration status summary.

        Returns:
            Status dictionary
        """
        return self.contract_orchestrator.get_status_summary()

    def get_workflow_output(self) -> dict[str, Any]:
        """Get dispatch decision formatted for workflow output.

        Returns:
            Workflow-compatible output dictionary
        """
        decision = self.get_next_dispatch()
        return format_dispatch_for_workflow(decision)

    def aggregate_reviewer_verdicts(
        self, reviewer_results: dict[str, dict[str, Any]]
    ) -> tuple[str, str]:
        """Aggregate verdicts from multiple reviewer agents.

        Implements the aggregation logic: any reviewer verdict of
        'needs_revision' triggers a re-run of worker agents.

        Args:
            reviewer_results: Dict mapping reviewer role -> outputs dict
                Each outputs dict should contain 'verdict' and optionally 'feedback'

        Returns:
            (overall_verdict, combined_feedback) tuple
        """
        needs_revision = False
        feedback_parts = []

        for role, outputs in reviewer_results.items():
            verdict = outputs.get("verdict", "approved")
            feedback = outputs.get("feedback", "")

            if verdict == "needs_revision":
                needs_revision = True
                if feedback:
                    feedback_parts.append(f"[{role}] {feedback}")

        if needs_revision:
            return "needs_revision", "\n\n".join(feedback_parts)
        return "approved", ""

    def is_multi_agent_enabled_for_phase(self, phase: str) -> bool:
        """Check if multi-agent is enabled for a specific phase.

        Considers per-phase overrides from the contract's MultiAgentConfig.

        Args:
            phase: Phase name

        Returns:
            True if multi-agent is enabled for this phase
        """
        contract = load_contract(self.pipeline.issue_number, self.repo_path)
        if contract.multi_agent_config is None:
            return True
        config = contract.multi_agent_config
        if not config.enabled:
            return False
        # Check per-phase override
        override = config.phase_overrides.get(phase)
        if override is not None:
            return override
        return True


def create_dispatcher(pipeline: Pipeline, repo_path: Path | str) -> PipelineDispatcher:
    """Create a dispatcher for a pipeline.

    Args:
        pipeline: Pipeline to dispatch for
        repo_path: Path to repository

    Returns:
        PipelineDispatcher instance
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)
    return PipelineDispatcher(pipeline, repo_path)


def get_next_agents_for_pipeline(
    pipeline_id: str,
    repo_path: Path | str,
) -> list[AgentRole]:
    """Get the next agents to run for a pipeline.

    Convenience function that loads pipeline and returns agents.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository

    Returns:
        List of AgentRole values to run next
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)

    store = get_state_store(repo_path)
    pipeline = store.load_pipeline(pipeline_id)

    dispatcher = create_dispatcher(pipeline, repo_path)
    return dispatcher.get_agents_to_run()
