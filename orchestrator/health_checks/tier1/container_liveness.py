"""
ContainerLivenessCheck — adapter wrapping ContainerMonitor (DD-3).

Inspects cached container states from the ContainerMonitor and checks
whether containers that the pipeline considers RUNNING are actually alive
in Docker.  Does *not* modify ContainerMonitor code.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add shared directory to path
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
from models import ContainerStatus, PipelineStatus

logger = get_logger("orchestrator.health_checks.container_liveness")


class ContainerLivenessCheck:
    """Check that containers the pipeline considers RUNNING are alive in Docker.

    This adapter delegates the actual container inspection to
    ``PipelineHealthContext.live_container_ids`` (which in turn uses the
    DockerClient) without touching ContainerMonitor internals.
    """

    name: str = "container_liveness"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset({
        HealthTrigger.STARTUP,
        HealthTrigger.RUNTIME_TICK,
        HealthTrigger.WAVE_COMPLETE,
        HealthTrigger.PHASE_COMPLETE,
        HealthTrigger.ON_DEMAND,
    })

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check container liveness for the current pipeline."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Pipeline is not running; liveness check skipped.",
            )

        # Collect container IDs that the pipeline considers RUNNING
        expected_running: list[str] = []
        for phase_exec in pipeline.phases.values():
            for ci in phase_exec.containers:
                if ci.status == ContainerStatus.RUNNING:
                    expected_running.append(ci.container_id)

        if not expected_running:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="No containers expected to be running.",
            )

        # Compare against Docker reality
        live_ids = context.live_container_ids
        missing = [cid for cid in expected_running if cid not in live_ids]

        if not missing:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning=f"All {len(expected_running)} expected containers are alive.",
            )

        # Missing containers → FAILED (infrastructure problem)
        return HealthResult(
            status=HealthStatus.FAILED,
            check_name=self.name,
            tier=self.tier,
            reasoning=(
                f"{len(missing)} of {len(expected_running)} expected containers "
                f"are missing from Docker."
            ),
            action=HealthAction.FAIL_PIPELINE,
            details={
                "missing_container_ids": missing,
                "expected_running_count": len(expected_running),
            },
        )
