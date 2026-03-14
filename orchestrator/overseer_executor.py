"""
Overseer executor for pipeline health monitoring.

Manages the lifecycle of the overseer agent container:
- Spawns overseer alongside coordinator when overseer_enabled is true
- Overseer has no repo access (same isolation as coordinator)
- Overseer crash does NOT fail the pipeline (advisory role)
- Overseer is stopped when the pipeline completes
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


from models import (
    AgentExecutionStatus,
    AgentRole,
    ContainerStatus,
)
from state_store import get_pipeline_state_lock, get_state_store

logger = get_logger("orchestrator.overseer_executor")


@dataclass
class OverseerConfig:
    """Configuration for overseer execution."""

    poll_interval_seconds: int = 30
    stall_base_threshold_seconds: int = 120
    max_redirects_before_escalation: int = 2


class OverseerExecutor:
    """Manages the overseer container lifecycle.

    The overseer is an advisory health monitor — its crash should never
    fail the pipeline. It runs in a background thread alongside the
    coordinator and is stopped when the pipeline completes.
    """

    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def should_spawn_overseer(self, pipeline_id: str) -> bool:
        """Check if the overseer should be spawned for this pipeline."""
        store = get_state_store(self.repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        return pipeline.config.coordinator_enabled and pipeline.config.overseer_enabled

    def spawn_in_background(
        self,
        spawn_fn,
        **spawn_kwargs,
    ) -> None:
        """Spawn the overseer container in a background thread.

        Args:
            spawn_fn: The _spawn_and_wait function to call.
            **spawn_kwargs: Arguments to pass to _spawn_and_wait.
        """
        pipeline_id = spawn_kwargs.get("pipeline_id", "unknown")

        def _run():
            try:
                logger.info(
                    "Overseer container starting",
                    pipeline_id=pipeline_id,
                )
                exit_code, logs = spawn_fn(**spawn_kwargs)
                self.handle_overseer_completion(pipeline_id, exit_code)
            except Exception as e:
                logger.warning(
                    "Overseer container failed to spawn or crashed",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                # Overseer crash is non-fatal — just log and continue
                self.handle_overseer_completion(pipeline_id, exit_code=1)

        self._thread = threading.Thread(
            target=_run,
            name=f"overseer-{pipeline_id}",
            daemon=True,
        )
        self._thread.start()

    def handle_overseer_completion(self, pipeline_id: str, exit_code: int = 0) -> None:
        """Handle overseer container exit.

        Unlike the coordinator, overseer exit (even with non-zero code)
        does NOT affect pipeline status. We just update container/agent
        status for tracking.
        """
        store = get_state_store(self.repo_path)

        try:
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                now = datetime.utcnow()
                container_status = (
                    ContainerStatus.EXITED if exit_code == 0 else ContainerStatus.FAILED
                )
                agent_status = (
                    AgentExecutionStatus.COMPLETE
                    if exit_code == 0
                    else AgentExecutionStatus.FAILED
                )

                # Mark overseer container/agent entries as exited
                for phase_execution in pipeline.phases.values():
                    for ci in phase_execution.containers:
                        if (
                            ci.agent_role == AgentRole.OVERSEER
                            and ci.status == ContainerStatus.RUNNING
                        ):
                            ci.status = container_status
                            ci.exit_code = exit_code
                            ci.exited_at = now
                    for agent in phase_execution.agents:
                        if (
                            agent.role == AgentRole.OVERSEER
                            and agent.status == AgentExecutionStatus.RUNNING
                        ):
                            agent.status = agent_status
                            agent.completed_at = now

                store.save_pipeline(pipeline, expected_version=pipeline.version)

            if exit_code == 0:
                logger.info("Overseer completed successfully", pipeline_id=pipeline_id)
            else:
                logger.warning(
                    "Overseer exited with non-zero code (pipeline unaffected)",
                    pipeline_id=pipeline_id,
                    exit_code=exit_code,
                )
        except Exception as e:
            # Even if state update fails, don't crash — overseer is advisory
            logger.warning(
                "Failed to update overseer completion state",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    def wait_for_completion(self, timeout: float | None = None) -> None:
        """Wait for the overseer background thread to finish."""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
