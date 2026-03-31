"""
IncompleteConsensusStallCheck — detect BRC consensus stuck with blocking agents.

Fires on RUNTIME_TICK.  When the peer consensus tracker (or message-based
fallback) shows that one or more agents have NOT confirmed and the set of
blocking agents has not changed for several consecutive ticks, this check
reports DEGRADED so the overseer can nudge the blocking agents.

Unlike ConsensusStallCheck (which detects consensus-complete-but-phase-stuck),
this check detects consensus-*incomplete*-and-not-progressing — the scenario
where most agents have confirmed but one or more are stuck in a heartbeat
loop after a re-review cycle.

This check is purely diagnostic — it does not mutate global state.
Recovery actions are driven by the overseer (Tier 2).
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

logger = get_logger("orchestrator.health_checks.incomplete_consensus_stall")

# How long after phase start we wait before checking for incomplete consensus.
# Agents need time to complete work and enter the BRC protocol.
DEFAULT_GRACE_SECONDS = 300  # 5 minutes

# How many consecutive ticks the same blocking set must persist before alerting.
DEFAULT_STALL_TICK_THRESHOLD = 10


class IncompleteConsensusStallCheck:
    """Detect consensus-incomplete-and-not-progressing conditions."""

    name: str = "incomplete_consensus_stall"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.RUNTIME_TICK,
            HealthTrigger.ON_DEMAND,
        }
    )

    def __init__(
        self,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        stall_tick_threshold: int = DEFAULT_STALL_TICK_THRESHOLD,
    ):
        self._grace_seconds = grace_seconds
        self._stall_tick_threshold = stall_tick_threshold
        # Track consecutive ticks with the same blocking set, keyed by pipeline ID.
        # Health checks are singletons invoked across all pipelines, so flat
        # instance variables would corrupt across pipelines.
        self._prev_blocking: dict[str, frozenset[str]] = {}
        self._consecutive_ticks: dict[str, int] = {}

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check for stuck incomplete consensus."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            return self._healthy("Pipeline is not running; check skipped.")

        # Only relevant for concurrent execution phases
        try:
            from concurrent_executor import is_concurrent_execution
        except ImportError:
            return self._healthy("Concurrent executor not available; check skipped.")

        current_phase = pipeline.current_phase
        if not is_concurrent_execution(pipeline, current_phase):
            return self._healthy("Phase is not using concurrent execution; check skipped.")

        # Check grace period
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
        pipeline_id = pipeline.id
        blocking_agents = self._get_blocking_agents(pipeline_id, pipeline)
        if blocking_agents is None:
            # Could not determine consensus state
            return self._healthy("Could not determine consensus state; check skipped.")

        if not blocking_agents:
            # No blocking agents — consensus is complete (or nearly so)
            self._reset_tracking(pipeline_id)
            return self._healthy("No blocking agents; consensus progressing normally.")

        # Track consecutive ticks with the same blocking set
        current_blocking = frozenset(blocking_agents)
        prev = self._prev_blocking.get(pipeline_id)
        if current_blocking != prev:
            # Blocking set changed — reset counter
            self._prev_blocking[pipeline_id] = current_blocking
            self._consecutive_ticks[pipeline_id] = 1
            return self._healthy(
                f"Blocking agents changed to {sorted(blocking_agents)}; resetting stall counter."
            )

        self._consecutive_ticks[pipeline_id] = self._consecutive_ticks.get(pipeline_id, 0) + 1
        ticks = self._consecutive_ticks[pipeline_id]

        if ticks < self._stall_tick_threshold:
            return self._healthy(
                f"Blocking agents {sorted(blocking_agents)} unchanged for "
                f"{ticks}/{self._stall_tick_threshold} ticks; "
                f"not yet stalled."
            )

        # Stall detected — same agents blocking for too many consecutive ticks
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning=(
                f"BRC consensus incomplete: {sorted(blocking_agents)} have not "
                f"confirmed for {ticks} consecutive ticks. "
                f"These agents may be stuck in a heartbeat loop after a re-review cycle."
            ),
            action=HealthAction.ALERT,
            details={
                "recovery_action": "escalate_to_overseer",
                "pipeline_id": pipeline_id,
                "phase": current_phase.value,
                "blocking_agents": sorted(blocking_agents),
                "consecutive_ticks": ticks,
            },
        )

    # ------------------------------------------------------------------
    # Consensus querying
    # ------------------------------------------------------------------

    def _get_blocking_agents(self, pipeline_id: str, pipeline: Pipeline) -> list[str] | None:
        """Return list of agents blocking consensus, or None if unknown.

        Returns an empty list if consensus is complete.
        """
        # Strategy 1: check the in-memory tracker
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker is not None:
                result = tracker.evaluate()
                if result.get("is_complete"):
                    return []
                return result.get("blocking_agents", [])
        except Exception:
            logger.debug("Tracker evaluate failed", pipeline_id=pipeline_id, exc_info=True)

        # Strategy 2: infer from messages
        try:
            from message_store import get_message_store
            from review_graph import get_review_graph_for_phase

            phase_value = pipeline.current_phase.value
            graph = get_review_graph_for_phase(phase_value, repo=pipeline.repo)
            expected_roles = graph.all_roles()
            if not expected_roles:
                return None

            store = get_message_store()
            messages = store.get_messages(pipeline_id, limit=10000)
            confirmed_roles = {
                m.from_role for m in messages if m.message_type == "CONSENSUS_CONFIRMED"
            }
            blocking = sorted(expected_roles - confirmed_roles)
            return blocking
        except Exception:
            logger.debug(
                "Message-based blocking agent check failed",
                pipeline_id=pipeline_id,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_tracking(self, pipeline_id: str) -> None:
        self._prev_blocking.pop(pipeline_id, None)
        self._consecutive_ticks.pop(pipeline_id, None)

    def _healthy(self, reasoning: str) -> HealthResult:
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning=reasoning,
        )
