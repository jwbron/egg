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
from models import CoordinatorState, PipelineStatus
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
        """Handle coordinator container exit."""
        store = get_state_store(self.repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

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
                    store.save_pipeline(pipeline)

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

            store.save_pipeline(pipeline)

        return "complete" if exit_code == 0 else "failed"
