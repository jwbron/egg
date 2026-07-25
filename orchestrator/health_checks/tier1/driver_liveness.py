"""
DriverLivenessCheck: detect a dead, hung, or no-progress pipeline driver (#3540).

The ``_run_pipeline`` driver is an in-memory thread; every other watcher
(the per-pipeline overseer pod, the BRC event loop, the Tier-1 health
monitor thread) is spawned BY that driver, so a wedged driver silences the
whole detection stack while the pipeline reads ``running``. #3540 observed
exactly that: 11+ hours of a RUNNING pipeline with zero spawns, zero
errors, and zero detection.

This check runs on the kubernetes-monitor RUNTIME_TICK sweep (the
orchestrator's own poll loop, independent of any driver thread) and fires
in three modes, most severe first:

* ``driver_dead``: no live ``pipeline-{id}*`` thread owns a RUNNING
  pipeline (past a short grace for transition gaps).
* ``driver_hung``: a driver thread exists but its work-loop heartbeat
  (``driver_heartbeat.record_tick``) has gone stale.
* ``driver_no_progress``: the driver is ticking, but nothing has spawned,
  no agent container is live (neither in persisted phase state nor as a
  live pod), and no HITL decision is pending; the phase is silently
  spinning (the #3540 signature).

Purely diagnostic: it never mutates state. Escalation (error log,
OVERSEER_ALERT broadcast, HITL decision) is driven by the caller in
``kubernetes_monitor``.
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
from models import AgentExecutionStatus, ContainerStatus, PipelineStatus

logger = get_logger("orchestrator.health_checks.driver_liveness")

# Grace before a missing driver thread on a RUNNING pipeline is treated as
# dead rather than mid-transition (phase advance/restart respawn gaps are
# sub-second; startup relaunch happens before the first sweep).
DEFAULT_DEAD_GRACE_SECONDS = 300

# Grace before a stale heartbeat / spawn-free window counts as a wedge.
# Set above the event loop's no-op park retry window (1800s,
# supervision_policy.SUPERVISION_NOOP_PARK_RETRY_SECONDS) so a legitimately
# parked pipeline's periodic respawns keep the spawn stamp fresh.
#
# The grace alone is NOT sufficient to rule out a healthy run: in event-pump
# mode a single long-running one-shot agent produces zero respawns for its
# whole runtime (the event loop's dedupe branch returns without spawning
# while the pod is live, so ``record_spawn`` is never re-stamped) and is
# never written to persisted phase state (``spawn_all()`` returns ``[]``,
# #3230). Such an agent would look identical to a wedge to the spawn-age and
# persisted-state signals. The live pod itself is therefore consulted as the
# ground-truth liveness signal before ``driver_no_progress`` fires
# (see ``_has_live_agent_pod``, #3540 re-review).
DEFAULT_STALL_GRACE_SECONDS = 2700

# HITL wiring shared with routes/decisions (single source of truth; the
# resolve-dispatch handler and the kubernetes_monitor escalation both
# import these).
DRIVER_LIVENESS_HITL_CONTEXT = "driver_liveness_stall"
DRIVER_LIVENESS_RETRY_OPTION = "Retry phase"
DRIVER_LIVENESS_DISMISS_OPTION = "Dismiss (recorded only)"
DRIVER_LIVENESS_HITL_OPTIONS = [
    DRIVER_LIVENESS_RETRY_OPTION,
    DRIVER_LIVENESS_DISMISS_OPTION,
]


class DriverLivenessCheck:
    """Detect a dead/hung/no-progress ``_run_pipeline`` driver (#3540)."""

    name: str = "driver_liveness"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.RUNTIME_TICK,
            HealthTrigger.ON_DEMAND,
        }
    )

    def __init__(
        self,
        dead_grace_seconds: int = DEFAULT_DEAD_GRACE_SECONDS,
        stall_grace_seconds: int = DEFAULT_STALL_GRACE_SECONDS,
    ):
        self._dead_grace = dead_grace_seconds
        self._stall_grace = stall_grace_seconds
        # First-observed clocks per (pipeline_id, condition). The check is
        # invoked once per sweep; a condition must persist across sweeps for
        # the full grace before it fires, and the clock resets the moment
        # the condition clears. This also makes the check restart-safe: an
        # empty heartbeat registry after an orchestrator restart starts a
        # fresh observation window instead of firing immediately.
        self._first_observed: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Observation clocks
    # ------------------------------------------------------------------

    def _observed_age(self, pipeline_id: str, condition: str) -> float:
        key = (pipeline_id, condition)
        now = time.monotonic()
        first = self._first_observed.setdefault(key, now)
        return now - first

    def _clear_observed(self, pipeline_id: str, conditions: set[str] | None = None) -> None:
        for key in list(self._first_observed):
            if key[0] != pipeline_id:
                continue
            if conditions is None or key[1] in conditions:
                del self._first_observed[key]

    def discard_pipeline(self, pipeline_id: str) -> None:
        """Forget all observation clocks for a terminal/removed pipeline.

        The kubernetes-monitor calls this when a pipeline leaves RUNNING.
        Non-running pipelines are skipped before :meth:`run` executes, so the
        in-band ``_clear_observed`` at the top of :meth:`run` never fires for
        them; without this the per-pipeline ``_first_observed`` entries would
        accumulate for the orchestrator's process lifetime (#3540 re-review).
        """
        self._clear_observed(pipeline_id)

    def _has_live_agent_pod(self, context: PipelineHealthContext, pipeline_id: str) -> bool:
        """Whether a RUNNING agent pod exists for the pipeline right now.

        Consults the container backend directly (the signal persisted phase
        state can no longer provide in event-pump mode, #3230). Returns
        ``True`` only when a live RUNNING pod is confirmed; ``False`` on an
        empty result or any query error, so an unconfirmable pipeline is not
        exempted and detection is preserved.
        """
        client = getattr(context, "docker_client", None)
        if client is None:
            return False
        try:
            from kubernetes_client import LABEL_PIPELINE_ID

            containers = client.list_containers(all=False, labels={LABEL_PIPELINE_ID: pipeline_id})
            return any(c.status == ContainerStatus.RUNNING for c in containers)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Check body
    # ------------------------------------------------------------------

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check that a RUNNING pipeline has a live, progressing driver."""
        pipeline = context.pipeline
        if pipeline.status != PipelineStatus.RUNNING:
            self._clear_observed(pipeline.id)
            return self._healthy("Pipeline is not running; driver liveness check skipped.")

        current_phase = pipeline.current_phase
        phase_exec = pipeline.phases.get(current_phase.value) if current_phase else None
        if phase_exec is None or phase_exec.status != PipelineStatus.RUNNING:
            self._clear_observed(pipeline.id)
            return self._healthy("Phase execution is not running; check skipped.")

        try:
            from routes.pipelines import has_live_pipeline_driver
        except ImportError:
            return self._healthy("Driver-thread registry unavailable; check skipped.")

        try:
            import driver_heartbeat
        except ImportError:
            try:
                from orchestrator import driver_heartbeat  # type: ignore[no-redef]
            except ImportError:
                return self._healthy("Heartbeat registry unavailable; check skipped.")

        # --- Mode 1: driver thread gone entirely -----------------------
        if not has_live_pipeline_driver(pipeline.id):
            dead_age = self._observed_age(pipeline.id, "driver_dead")
            if dead_age < self._dead_grace:
                return self._healthy(
                    f"No driver thread observed for {dead_age:.0f}s, within the "
                    f"{self._dead_grace}s transition grace."
                )
            return self._degraded(
                pipeline,
                mode="driver_dead",
                reasoning=(
                    "Pipeline is RUNNING but no live pipeline-{id} driver thread "
                    f"exists (observed for {dead_age:.0f}s). Nothing is driving "
                    "the phase; no other watcher will notice (#3540)."
                ),
                observed_for_s=dead_age,
            )
        self._clear_observed(pipeline.id, {"driver_dead"})

        # A pending HITL decision means the driver may be legitimately
        # blocked on wait_for_decision while status reads RUNNING; the
        # operator already has a surfaced decision to act on.
        if pipeline.get_pending_decisions():
            self._clear_observed(pipeline.id, {"no_tick", "driver_no_progress"})
            return self._healthy("Pending HITL decisions; driver is legitimately waiting.")

        # --- Mode 2: driver thread exists but its loops stopped ticking -
        tick_age = driver_heartbeat.tick_age_seconds(pipeline.id)
        if tick_age is None:
            # No stamp yet (orchestrator restart, or a driver wedged before
            # entering any work loop): fall back to the observation clock.
            tick_age = self._observed_age(pipeline.id, "no_tick")
        else:
            self._clear_observed(pipeline.id, {"no_tick"})
        if tick_age > self._stall_grace:
            return self._degraded(
                pipeline,
                mode="driver_hung",
                reasoning=(
                    "A driver thread exists but its work loops have not ticked "
                    f"for {tick_age:.0f}s (grace {self._stall_grace}s); the "
                    "driver appears hung mid-call (#3540)."
                ),
                tick_age_s=tick_age,
            )

        # --- Mode 3: ticking but zero progress --------------------------
        # Live agent work in the persisted phase state exempts the pipeline
        # (classic long-lived pods; event-loop one-shots are covered by the
        # spawn stamp instead, since they are never persisted, #3230).
        containers_running = any(c.status == ContainerStatus.RUNNING for c in phase_exec.containers)
        agents_running = any(a.status == AgentExecutionStatus.RUNNING for a in phase_exec.agents)
        if containers_running or agents_running:
            self._clear_observed(pipeline.id, {"driver_no_progress"})
            return self._healthy("Agent containers are live; driver is progressing.")

        # A phase that just started deserves its full setup window before
        # "no spawns yet" means anything.
        if phase_exec.started_at is not None:
            phase_age = time.time() - phase_exec.started_at.timestamp()
            if phase_age < self._stall_grace:
                self._clear_observed(pipeline.id, {"driver_no_progress"})
                return self._healthy(
                    f"Phase started {phase_age:.0f}s ago, within the "
                    f"{self._stall_grace}s no-progress grace."
                )

        spawn_age = driver_heartbeat.spawn_age_seconds(pipeline.id)
        effective_age = (
            spawn_age
            if spawn_age is not None
            else self._observed_age(pipeline.id, "driver_no_progress")
        )
        if effective_age <= self._stall_grace:
            return self._healthy(
                f"Last spawn activity {effective_age:.0f}s ago, within the "
                f"{self._stall_grace}s no-progress grace."
            )

        # Ground-truth liveness check before declaring a wedge. In event-pump
        # mode a genuinely-working single agent leaves persisted phase state
        # empty and stops re-stamping the spawn clock for its whole runtime
        # (#3230), so the checks above cannot distinguish it from a wedge. The
        # live pod is the signal the empty phase state can no longer provide:
        # if an agent pod is actually RUNNING for this pipeline, the driver is
        # progressing and firing here would tear down in-flight work (#3540).
        if self._has_live_agent_pod(context, pipeline.id):
            self._clear_observed(pipeline.id, {"driver_no_progress"})
            return self._healthy(
                "A live agent pod is running; driver is progressing "
                "(event-pump one-shot not reflected in persisted phase state)."
            )
        return self._degraded(
            pipeline,
            mode="driver_no_progress",
            reasoning=(
                "The driver is alive and ticking, but nothing has spawned for "
                f"{effective_age:.0f}s (grace {self._stall_grace}s), no agent "
                "container is live, and no HITL decision is pending; the "
                "phase is silently spinning (#3540)."
            ),
            tick_age_s=tick_age,
            spawn_age_s=spawn_age,
        )

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------

    def _healthy(self, reasoning: str) -> HealthResult:
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning=reasoning,
        )

    def _degraded(self, pipeline, mode: str, reasoning: str, **extra) -> HealthResult:
        phase = pipeline.current_phase.value if pipeline.current_phase else None
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning=reasoning,
            action=HealthAction.ALERT,
            details={
                "pipeline_id": pipeline.id,
                "phase": phase,
                "mode": mode,
                "dead_grace_s": self._dead_grace,
                "stall_grace_s": self._stall_grace,
                **extra,
            },
        )
