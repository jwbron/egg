"""
StartupStateCheck — adapter wrapping reconcile_stale_containers (DD-3).

Verifies that after startup reconciliation has run, there are no
pipelines stuck in RUNNING with containers that no longer exist.
This is a belt-and-suspenders check on top of the existing
startup_reconciliation module.
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

logger = get_logger("orchestrator.health_checks.startup_state")


class StartupStateCheck:
    """Verify post-startup pipeline state consistency.

    Checks that no RUNNING pipeline has agents/containers whose
    container IDs are absent from Docker — the same condition that
    ``reconcile_stale_containers`` fixes, recast as a health check
    for ongoing monitoring.
    """

    name: str = "startup_state"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset({
        HealthTrigger.STARTUP,
        HealthTrigger.ON_DEMAND,
    })

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check for stale containers in the pipeline."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Pipeline is not running; startup state check skipped.",
            )

        live_ids = context.live_container_ids
        stale_containers: list[str] = []
        stale_agents: list[str] = []

        for phase_exec in pipeline.phases.values():
            for ci in phase_exec.containers:
                if ci.status == ContainerStatus.RUNNING and ci.container_id not in live_ids:
                    stale_containers.append(ci.container_id)

            for agent in phase_exec.agents:
                if (
                    agent.status == AgentExecutionStatus.RUNNING
                    and agent.container_id
                    and agent.container_id not in live_ids
                ):
                    stale_agents.append(f"{agent.role}:{agent.container_id}")

        if not stale_containers and not stale_agents:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="No stale containers or agents detected.",
            )

        return HealthResult(
            status=HealthStatus.FAILED,
            check_name=self.name,
            tier=self.tier,
            reasoning=(
                f"Found {len(stale_containers)} stale container(s) and "
                f"{len(stale_agents)} stale agent(s) with missing Docker containers."
            ),
            action=HealthAction.FAIL_PIPELINE,
            details={
                "stale_containers": stale_containers,
                "stale_agents": stale_agents,
            },
        )
