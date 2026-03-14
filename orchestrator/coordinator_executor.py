"""
Coordinator executor for coordinator-driven pipelines.

Manages the lifecycle of the coordinator agent container:
- Spawns coordinator as first agent when coordinator_enabled is true
- Injects coordinator-specific environment
- Monitors coordinator health and handles crash recovery
- Enforces global guardrails
"""

import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:
        return logging.getLogger(name)


from events import EventType, emit_event
from models import (
    AgentExecutionStatus,
    AgentRole,
    ContainerStatus,
    CoordinatorState,
    PipelineStatus,
)
from state_store import get_pipeline_state_lock, get_state_store

logger = get_logger("orchestrator.coordinator_executor")


@dataclass
class CoordinatorConfig:
    """Configuration for coordinator execution."""

    max_agents: int = 10
    max_retries_per_role: int = 2
    max_respawns: int = 2
    max_wall_clock_minutes: int = 120


class CoordinatorExecutor:
    """Manages the coordinator container lifecycle."""

    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path)
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def should_use_coordinator(self, pipeline_id: str) -> bool:
        """Check if a pipeline should use coordinator mode."""
        store = get_state_store(self.repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        return pipeline.config.coordinator_enabled

    def init_coordinator_state(self, pipeline_id: str) -> None:
        """Initialise coordinator state and mark pipeline as running.

        Call this before spawning the coordinator container via _spawn_and_wait.
        Raises ValueError if coordinator is not enabled for the pipeline.
        """
        store = get_state_store(self.repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            if not pipeline.config.coordinator_enabled:
                raise ValueError(f"Pipeline {pipeline_id} does not have coordinator enabled")

            if pipeline.coordinator_state is None:
                pipeline.coordinator_state = CoordinatorState()

            pipeline.status = PipelineStatus.RUNNING
            store.save_pipeline(pipeline)

        emit_event(
            EventType.COORDINATOR_SPAWN,
            pipeline_id,
            data={"role": "coordinator", "action": "start"},
        )

    def handle_coordinator_completion(self, pipeline_id: str, exit_code: int = 0):
        """Handle coordinator container exit.

        Also marks the coordinator's container/agent entries in phase_execution
        as exited, preventing the background container monitor from finding
        stale RUNNING entries and marking the pipeline FAILED.
        """
        store = get_state_store(self.repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            # Mark coordinator container/agent entries in phase_execution as
            # exited so the background monitor won't race and mark the pipeline
            # FAILED after a respawn sets it back to RUNNING.
            now = datetime.utcnow()
            container_status = ContainerStatus.EXITED if exit_code == 0 else ContainerStatus.FAILED
            agent_status = (
                AgentExecutionStatus.COMPLETE if exit_code == 0 else AgentExecutionStatus.FAILED
            )
            for phase_execution in pipeline.phases.values():
                for ci in phase_execution.containers:
                    if (
                        ci.agent_role == AgentRole.COORDINATOR
                        and ci.status == ContainerStatus.RUNNING
                    ):
                        ci.status = container_status
                        ci.exit_code = exit_code
                        ci.exited_at = now
                for agent in phase_execution.agents:
                    if (
                        agent.role == AgentRole.COORDINATOR
                        and agent.status == AgentExecutionStatus.RUNNING
                    ):
                        agent.status = agent_status
                        agent.completed_at = now

            # If the pipeline was already cancelled or failed (e.g. via
            # cancel_task), do not respawn — just persist the container/agent
            # status updates and exit.
            if pipeline.status in (
                PipelineStatus.CANCELLED,
                PipelineStatus.FAILED,
                PipelineStatus.COMPLETE,
            ):
                logger.info(
                    "Coordinator exited but pipeline already terminated, skipping respawn",
                    pipeline_id=pipeline_id,
                    pipeline_status=pipeline.status.value,
                    exit_code=exit_code,
                )
                store.save_pipeline(pipeline, expected_version=pipeline.version)
                return "failed"

            if exit_code == 0:
                # Check if all spawned agents are done
                if pipeline.coordinator_state:
                    running = [
                        a
                        for a in pipeline.coordinator_state.agents_spawned
                        if a.status == "running"
                    ]
                    if running:
                        logger.warning(
                            "Coordinator exited but agents still running",
                            pipeline_id=pipeline_id,
                            running_agents=[a.role for a in running],
                        )

                pipeline.status = PipelineStatus.COMPLETE
                logger.info("Coordinator completed successfully", pipeline_id=pipeline_id)
            else:
                # Coordinator crashed — check if we should respawn
                state = pipeline.coordinator_state or CoordinatorState()
                config = CoordinatorConfig(
                    max_respawns=pipeline.config.coordinator_max_respawns,
                )

                if state.guardrail_counters.coordinator_respawns < config.max_respawns:
                    state.guardrail_counters.coordinator_respawns += 1
                    pipeline.coordinator_state = state
                    pipeline.status = PipelineStatus.RUNNING
                    store.save_pipeline(pipeline, expected_version=pipeline.version)

                    logger.info(
                        "Coordinator crashed, will respawn",
                        pipeline_id=pipeline_id,
                        respawn_count=state.guardrail_counters.coordinator_respawns,
                    )

                    emit_event(
                        EventType.COORDINATOR_LOOPBACK,
                        pipeline_id,
                        data={
                            "reason": "coordinator_crash_respawn",
                            "respawn_count": state.guardrail_counters.coordinator_respawns,
                        },
                    )
                    return "respawn"
                else:
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = (
                        f"Coordinator failed (exit code {exit_code}), max respawns reached"
                    )
                    logger.error(
                        "Coordinator failed, no more respawns",
                        pipeline_id=pipeline_id,
                    )

            store.save_pipeline(pipeline, expected_version=pipeline.version)

        return "complete" if exit_code == 0 else "failed"
