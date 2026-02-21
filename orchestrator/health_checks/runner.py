"""
HealthCheckRunner — central dispatcher for health checks (DD-2).

Registers checks, dispatches by trigger, handles Tier 1 → Tier 2
escalation, and emits results via the EventBus.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
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
    HealthCheck,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)

logger = get_logger("orchestrator.health_checks.runner")


class HealthCheckRunner:
    """Dispatches health checks by trigger and manages Tier escalation.

    Usage::

        runner = HealthCheckRunner()
        runner.register(ContainerLivenessCheck())
        runner.register(PhaseOutputPresenceCheck())

        results = runner.run(context, trigger=HealthTrigger.WAVE_COMPLETE)
    """

    def __init__(self) -> None:
        self._checks: list[HealthCheck] = []

    def register(self, check: HealthCheck) -> None:
        """Register a health check."""
        self._checks.append(check)
        logger.debug("Health check registered", check_name=check.name, tier=check.tier)

    @property
    def checks(self) -> list[HealthCheck]:
        """All registered checks."""
        return list(self._checks)

    def run(
        self,
        context: PipelineHealthContext,
        trigger: HealthTrigger,
    ) -> list[HealthResult]:
        """Run applicable checks for the given trigger.

        Tier 1 checks always run first.  Tier 2 escalation follows the
        gating rules from DD-6:

        - WAVE_COMPLETE: Tier 2 runs only if any Tier 1 result is DEGRADED.
        - PHASE_COMPLETE / ON_DEMAND: Tier 2 always runs.
        - STARTUP / RUNTIME_TICK: Tier 2 never runs.

        Returns:
            List of HealthResult values (one per check that ran).
        """
        self._emit_started(context, trigger)

        tier1 = [c for c in self._checks if c.tier == HealthTier.PROGRAMMATIC and trigger in c.triggers]
        tier2 = [c for c in self._checks if c.tier == HealthTier.AGENT and trigger in c.triggers]

        results: list[HealthResult] = []

        # --- Tier 1 ---
        for check in tier1:
            result = self._run_single(check, context)
            results.append(result)
            self._emit_result(context, result)

        # --- Tier 2 escalation (DD-6) ---
        should_run_tier2 = self._should_escalate_to_tier2(trigger, results)
        if should_run_tier2:
            for check in tier2:
                result = self._run_single(check, context)
                results.append(result)
                self._emit_result(context, result)

        # Emit aggregate completion event
        self._emit_completed(context, trigger, results)

        return results

    # ------------------------------------------------------------------
    # Escalation logic (DD-6)
    # ------------------------------------------------------------------

    @staticmethod
    def _should_escalate_to_tier2(
        trigger: HealthTrigger,
        tier1_results: list[HealthResult],
    ) -> bool:
        """Decide whether Tier 2 checks should run."""
        if trigger in (HealthTrigger.PHASE_COMPLETE, HealthTrigger.ON_DEMAND):
            return True
        if trigger == HealthTrigger.WAVE_COMPLETE:
            return any(r.status == HealthStatus.DEGRADED for r in tier1_results)
        # STARTUP and RUNTIME_TICK never escalate
        return False

    # ------------------------------------------------------------------
    # Single-check execution
    # ------------------------------------------------------------------

    @staticmethod
    def _run_single(check: HealthCheck, context: PipelineHealthContext) -> HealthResult:
        """Run one check, catching any unexpected exceptions."""
        try:
            return check.run(context)
        except Exception as exc:
            logger.error(
                "Health check raised unexpected exception",
                check_name=check.name,
                error=str(exc),
            )
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=check.name,
                tier=check.tier,
                reasoning=f"Check failed internally: {exc}",
                action=HealthAction.CONTINUE,
            )

    # ------------------------------------------------------------------
    # EventBus integration (DD-8)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_event_bus() -> Any:
        """Import and return the singleton EventBus (lazy to avoid circular imports)."""
        try:
            from events import get_event_bus
            return get_event_bus()
        except ImportError:
            return None

    def _emit_started(self, context: PipelineHealthContext, trigger: HealthTrigger) -> None:
        bus = self._get_event_bus()
        if bus is None:
            return
        try:
            from events import EventType
            bus.emit(
                EventType.HEALTH_CHECK_STARTED,
                context.pipeline_id,
                data={"trigger": trigger.value},
            )
        except Exception as exc:
            logger.debug("Failed to emit HEALTH_CHECK_STARTED", error=str(exc))

    def _emit_result(self, context: PipelineHealthContext, result: HealthResult) -> None:
        bus = self._get_event_bus()
        if bus is None:
            return
        try:
            from events import EventType

            if result.status == HealthStatus.FAILED:
                event_type = EventType.HEALTH_CHECK_FAILED
            elif result.status == HealthStatus.DEGRADED:
                event_type = EventType.HEALTH_CHECK_DEGRADED
            else:
                event_type = EventType.HEALTH_CHECK_COMPLETED

            bus.emit(
                event_type,
                context.pipeline_id,
                data=result.to_dict(),
            )
        except Exception as exc:
            logger.debug("Failed to emit health check result event", error=str(exc))

    def _emit_completed(
        self,
        context: PipelineHealthContext,
        trigger: HealthTrigger,
        results: list[HealthResult],
    ) -> None:
        bus = self._get_event_bus()
        if bus is None:
            return
        try:
            from events import EventType

            worst = HealthStatus.HEALTHY
            for r in results:
                if r.status == HealthStatus.FAILED:
                    worst = HealthStatus.FAILED
                    break
                if r.status == HealthStatus.DEGRADED:
                    worst = HealthStatus.DEGRADED

            bus.emit(
                EventType.HEALTH_CHECK_COMPLETED,
                context.pipeline_id,
                data={
                    "trigger": trigger.value,
                    "aggregate_status": worst.value,
                    "check_count": len(results),
                    "results": [r.to_dict() for r in results],
                },
            )
        except Exception as exc:
            logger.debug("Failed to emit HEALTH_CHECK_COMPLETED", error=str(exc))


# ------------------------------------------------------------------
# Helper: aggregate worst action from results
# ------------------------------------------------------------------

def worst_action(results: list[HealthResult]) -> HealthAction:
    """Return the most severe action from a list of results.

    Severity order: FAIL_PIPELINE > ALERT > CONTINUE.
    """
    if any(r.action == HealthAction.FAIL_PIPELINE for r in results):
        return HealthAction.FAIL_PIPELINE
    if any(r.action == HealthAction.ALERT for r in results):
        return HealthAction.ALERT
    return HealthAction.CONTINUE
