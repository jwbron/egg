"""
ConsensusStallCheck — detect BRC consensus complete but phase not advancing.

Fires on RUNTIME_TICK.  When the peer consensus tracker (or message-based
fallback) indicates all agents have confirmed, but the phase execution is
still RUNNING past a grace period, this check reports DEGRADED so the
container monitor recovery handler can drive the transition.

This check is purely diagnostic — it does not mutate global state.
Recovery actions are driven by the caller (container_monitor).
"""

from __future__ import annotations

import sys
import time
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
from models import Pipeline, PipelineStatus

logger = get_logger("orchestrator.health_checks.consensus_stall")

# How long after phase start we wait before flagging a stall.  Gives the
# polling loop time to pick up consensus naturally.
DEFAULT_CONSENSUS_STALL_GRACE_SECONDS = 60


class ConsensusStallCheck:
    """Detect consensus-complete-but-phase-stuck conditions."""

    name: str = "consensus_stall"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.RUNTIME_TICK,
            HealthTrigger.ON_DEMAND,
        }
    )

    def __init__(self, consensus_stall_grace_seconds: int = DEFAULT_CONSENSUS_STALL_GRACE_SECONDS):
        self._grace_seconds = consensus_stall_grace_seconds

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check for stuck consensus."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            return self._healthy("Pipeline is not running; consensus stall check skipped.")

        # Only relevant for concurrent execution phases
        try:
            from concurrent_executor import is_concurrent_execution
        except ImportError:
            return self._healthy("Concurrent executor not available; check skipped.")

        current_phase = pipeline.current_phase
        if not is_concurrent_execution(pipeline, current_phase):
            return self._healthy("Phase is not using concurrent execution; check skipped.")

        # Check grace period — phase must have been running long enough
        phase_exec = pipeline.phases.get(current_phase.value)
        if phase_exec is None or phase_exec.status != PipelineStatus.RUNNING:
            return self._healthy("Phase execution is not running; check skipped.")

        if phase_exec.started_at is not None:
            elapsed = time.time() - phase_exec.started_at.timestamp()
            if elapsed < self._grace_seconds:
                return self._healthy(
                    f"Phase started {elapsed:.0f}s ago, within grace period "
                    f"({self._grace_seconds}s); check skipped."
                )

        # Evaluate consensus state
        consensus_complete = self._check_consensus_complete(pipeline.id, pipeline)
        if not consensus_complete:
            return self._healthy("Consensus is not complete; no stall detected.")

        # Consensus IS complete but phase is still RUNNING — stall detected.
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="BRC consensus is complete but phase execution has not advanced.",
            action=HealthAction.ALERT,
            details={
                "recovery_action": "drive_phase_transition",
                "pipeline_id": pipeline.id,
                "phase": current_phase.value,
            },
        )

    # ------------------------------------------------------------------
    # Consensus detection — try tracker first, then message fallback
    # ------------------------------------------------------------------

    def _check_consensus_complete(self, pipeline_id: str, pipeline: Pipeline) -> bool:
        """Return True if BRC consensus is complete (all agents confirmed).

        Tries the in-memory tracker first.  If the tracker exists but says
        consensus is incomplete, falls through to the message-based check as
        a second opinion — the tracker's in-memory state can become stale
        after withdraw→re-propose cycles (#1471).
        """
        # Strategy 1: check the in-memory tracker
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker is not None:
                result = tracker.evaluate()
                if result.get("is_complete", False):
                    return True
                # Tracker says not complete — fall through to message check.
                # The tracker state may be stale after re-review cycles.
                logger.debug(
                    "Tracker says consensus incomplete, trying message fallback",
                    pipeline_id=pipeline_id,
                    blocking_agents=result.get("blocking_agents", []),
                )
        except Exception:
            logger.debug("Tracker evaluate failed", pipeline_id=pipeline_id, exc_info=True)

        # Strategy 2: scan messages for CONSENSUS_CONFIRMED from all roles
        return self._check_consensus_from_messages(pipeline_id, pipeline)

    def _check_consensus_from_messages(self, pipeline_id: str, pipeline: Pipeline) -> bool:
        """Fallback: check if all expected roles sent CONSENSUS_CONFIRMED."""
        try:
            from message_store import get_message_store
            from review_graph import get_review_graph_for_phase

            phase_value = pipeline.current_phase.value
            graph = get_review_graph_for_phase(phase_value, repo=pipeline.repo)
            expected_roles = graph.all_roles()
            if not expected_roles:
                return False

            store = get_message_store()
            messages = store.get_messages(pipeline_id, limit=10000)
            confirmed_roles = {
                m.from_role for m in messages if m.message_type == "CONSENSUS_CONFIRMED"
            }
            return expected_roles.issubset(confirmed_roles)
        except Exception:
            logger.debug(
                "Message-based consensus check failed",
                pipeline_id=pipeline_id,
                exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _healthy(self, reasoning: str) -> HealthResult:
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning=reasoning,
        )
