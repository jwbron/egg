"""
StateConsistencyCheck — cross-reference orchestrator state vs Docker vs contract.

Detects inconsistencies between:
- Pipeline state (agents marked RUNNING) vs Docker reality (container missing)
- Pipeline state (agents marked COMPLETE) vs contract (tasks still PENDING)
"""

from __future__ import annotations

import sys
from pathlib import Path

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from health_checks.context import PipelineHealthContext
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import AgentExecutionStatus, ContainerStatus, PipelineStatus

logger = get_logger("orchestrator.health_checks.state_consistency")


class StateConsistencyCheck:
    """Cross-reference orchestrator state vs Docker reality vs contract."""

    name: str = "state_consistency"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.RUNTIME_TICK,
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.ON_DEMAND,
        }
    )

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Run state consistency checks."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Pipeline is not running; consistency check skipped.",
            )

        issues: list[str] = []
        severity = HealthStatus.HEALTHY

        # --- Check 1: RUNNING agents with missing containers ---
        live_ids = context.live_container_ids
        for phase_exec in pipeline.phases.values():
            for agent in phase_exec.agents:
                if (
                    agent.status == AgentExecutionStatus.RUNNING
                    and agent.container_id
                    and agent.container_id not in live_ids
                ):
                    issues.append(
                        f"Agent {agent.role} is RUNNING but container "
                        f"{agent.container_id[:12]} is missing from Docker."
                    )
                    severity = HealthStatus.FAILED

        # --- Check 2: Container status vs agent status mismatch ---
        for phase_exec in pipeline.phases.values():
            container_map = {ci.container_id: ci for ci in phase_exec.containers}
            for agent in phase_exec.agents:
                if not agent.container_id or agent.container_id not in container_map:
                    continue
                ci = container_map[agent.container_id]
                # Container FAILED but agent still RUNNING
                if (
                    ci.status in (ContainerStatus.FAILED, ContainerStatus.EXITED)
                    and agent.status == AgentExecutionStatus.RUNNING
                ):
                    issues.append(
                        f"Agent {agent.role} is RUNNING but its container is {ci.status.value}."
                    )
                    severity = HealthStatus.FAILED

        # --- Check 3: COMPLETE agents with PENDING contract tasks ---
        complete_agents = []
        for phase_exec in pipeline.phases.values():
            for agent in phase_exec.agents:
                if agent.status == AgentExecutionStatus.COMPLETE:
                    complete_agents.append(agent)

        if complete_agents:
            contract_issue = self._check_contract_consistency(context, complete_agents)
            if contract_issue:
                issues.append(contract_issue)
                if severity == HealthStatus.HEALTHY:
                    severity = HealthStatus.DEGRADED

        if not issues:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Pipeline state is consistent with Docker and contract.",
            )

        action = (
            HealthAction.FAIL_PIPELINE if severity == HealthStatus.FAILED else HealthAction.ALERT
        )
        return HealthResult(
            status=severity,
            check_name=self.name,
            tier=self.tier,
            reasoning=f"Found {len(issues)} consistency issue(s): {'; '.join(issues)}",
            action=action,
            details={"issues": issues},
        )

    @staticmethod
    def _check_contract_consistency(
        context: PipelineHealthContext,
        complete_agents: list,
    ) -> str | None:
        """Check if contract tasks are still PENDING when agents are COMPLETE.

        Returns a description of the issue, or None if consistent.
        """
        # Read contract from agent_outputs (lazy property)
        outputs = context.agent_outputs
        contract_content = None
        for name, content in outputs.items():
            if "contract" in name.lower():
                contract_content = content
                break

        if contract_content is None:
            return None

        # Simple heuristic: check for "pending" task entries in contract
        # A proper implementation would parse the contract JSON, but for
        # a health check a heuristic suffices.
        import json

        try:
            contract = json.loads(contract_content)
        except (json.JSONDecodeError, TypeError):
            return None

        # Look for tasks with status=pending in the contract
        tasks = contract.get("tasks", [])
        if isinstance(tasks, list):
            pending_tasks = [
                t for t in tasks if isinstance(t, dict) and t.get("status") == "pending"
            ]
            if pending_tasks and len(complete_agents) > 0:
                return (
                    f"{len(complete_agents)} agent(s) marked COMPLETE but "
                    f"{len(pending_tasks)} contract task(s) are still pending."
                )

        return None
