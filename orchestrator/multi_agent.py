"""
Multi-agent parallel execution support.

Manages parallel execution of multiple agents in waves,
with dependency tracking and result collection.
"""

import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from dispatch import PipelineDispatcher, create_dispatcher
from docker_client import get_docker_client
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
)
from sandbox_template import SandboxTemplate, create_sandbox_config
from state_store import get_state_store

logger = get_logger("orchestrator.multi_agent")


class AgentWave:
    """A wave of agents executing in parallel."""

    def __init__(self, wave_number: int, agents: list[AgentRole]):
        """Initialize wave.

        Args:
            wave_number: Wave number (1-indexed)
            agents: Agents in this wave
        """
        self.wave_number = wave_number
        self.agents = agents
        self.containers: dict[AgentRole, ContainerInfo] = {}
        self.results: dict[AgentRole, AgentExecution] = {}
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None

    @property
    def is_complete(self) -> bool:
        """Check if all agents in wave have completed."""
        return len(self.results) == len(self.agents)

    @property
    def has_failures(self) -> bool:
        """Check if any agent in wave failed."""
        return any(r.status == AgentExecutionStatus.FAILED for r in self.results.values())

    @property
    def all_succeeded(self) -> bool:
        """Check if all agents succeeded."""
        return all(r.status == AgentExecutionStatus.COMPLETE for r in self.results.values())


class MultiAgentExecutor:
    """Manages multi-agent parallel execution.

    Coordinates spawning, monitoring, and result collection
    for multiple agents executing in parallel waves.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        repo_path: Path,
        dispatcher: PipelineDispatcher | None = None,
    ):
        """Initialize executor.

        Args:
            pipeline: Pipeline to execute
            repo_path: Path to repository
            dispatcher: Optional dispatcher (created if not provided)
        """
        self.pipeline = pipeline
        self.repo_path = repo_path
        self.dispatcher = dispatcher or create_dispatcher(pipeline, repo_path)
        self.docker_client = get_docker_client()

        self.current_wave: AgentWave | None = None
        self.completed_waves: list[AgentWave] = []
        self._lock = threading.Lock()

    def get_next_wave(self) -> AgentWave | None:
        """Get the next wave of agents to execute.

        Returns:
            AgentWave or None if no more waves
        """
        agents = self.dispatcher.get_agents_to_run()
        if not agents:
            return None

        decision = self.dispatcher.get_next_dispatch()
        wave_number = decision.wave_number

        return AgentWave(wave_number, agents)

    def spawn_wave(self, wave: AgentWave) -> dict[AgentRole, ContainerInfo]:
        """Spawn containers for all agents in a wave.

        Args:
            wave: Wave to spawn

        Returns:
            Dictionary of agent role to container info
        """
        containers = {}

        for role in wave.agents:
            try:
                # Get handoff data from completed agents
                handoff_data = self.dispatcher.get_handoff_data(role)

                # Create sandbox config
                config = create_sandbox_config(
                    pipeline_id=self.pipeline.id,
                    agent_role=role,
                    issue_number=self.pipeline.issue_number,
                    extra_env={
                        "EGG_HANDOFF_DATA": str(handoff_data),
                    },
                )

                # Build docker config
                template = SandboxTemplate(config)
                docker_config = template.to_docker_config()

                # Create and start container
                info = self.docker_client.create_container(
                    name=template.get_container_name(),
                    **docker_config,
                )
                info = self.docker_client.start_container(info.container_id)

                containers[role] = info

                # Mark agent as started
                self.dispatcher.start_agent(role)

                logger.info(
                    "Agent spawned",
                    pipeline_id=self.pipeline.id,
                    role=role.value,
                    container_id=info.container_id[:12],
                    wave=wave.wave_number,
                )

            except Exception as e:
                logger.error(
                    "Failed to spawn agent",
                    pipeline_id=self.pipeline.id,
                    role=role.value,
                    error=str(e),
                )
                # Create a failed container info
                containers[role] = ContainerInfo(
                    container_id="failed",
                    container_name=f"failed-{role.value}",
                    status=ContainerStatus.FAILED,
                )

        wave.containers = containers
        wave.started_at = datetime.utcnow()
        self.current_wave = wave

        return containers

    def record_agent_result(
        self,
        role: AgentRole,
        success: bool,
        commit: str | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentExecution:
        """Record the result of an agent execution.

        Args:
            role: Agent role
            success: Whether execution succeeded
            commit: Git commit SHA if changes made
            outputs: Handoff data
            error: Error message if failed

        Returns:
            AgentExecution with result
        """
        with self._lock:
            if success:
                execution = self.dispatcher.complete_agent(role, commit, outputs)
            else:
                execution = self.dispatcher.fail_agent(role, error or "Unknown error")

            if self.current_wave:
                self.current_wave.results[role] = execution

            logger.info(
                "Agent result recorded",
                pipeline_id=self.pipeline.id,
                role=role.value,
                success=success,
                commit=commit,
            )

            return execution

    def check_wave_completion(self) -> bool:
        """Check if current wave is complete.

        Returns:
            True if wave is complete
        """
        if not self.current_wave:
            return True

        return self.current_wave.is_complete

    def complete_current_wave(self) -> AgentWave | None:
        """Mark current wave as complete and prepare for next.

        Returns:
            Completed wave or None
        """
        with self._lock:
            if not self.current_wave:
                return None

            self.current_wave.completed_at = datetime.utcnow()
            completed = self.current_wave
            self.completed_waves.append(completed)
            self.current_wave = None

            logger.info(
                "Wave completed",
                pipeline_id=self.pipeline.id,
                wave=completed.wave_number,
                agents=[r.value for r in completed.agents],
                success=completed.all_succeeded,
            )

            return completed

    def execute_wave(
        self,
        wave: AgentWave,
        on_complete: Callable[[AgentRole, AgentExecution], None] | None = None,
    ) -> AgentWave:
        """Execute a wave and wait for completion.

        This is a synchronous method that blocks until all agents complete.

        Args:
            wave: Wave to execute
            on_complete: Optional callback when each agent completes

        Returns:
            Completed wave
        """
        self.spawn_wave(wave)

        # Wait for all containers to complete
        for role, container in wave.containers.items():
            if container.status == ContainerStatus.FAILED:
                # Already failed during spawn
                execution = self.record_agent_result(
                    role,
                    success=False,
                    error="Container failed to spawn",
                )
                if on_complete:
                    on_complete(role, execution)
                continue

            try:
                # Wait for container to exit
                final_info = self.docker_client.wait_for_container(
                    container.container_id,
                    timeout=3600,  # 1 hour timeout
                )

                success = final_info.exit_code == 0

                execution = self.record_agent_result(
                    role,
                    success=success,
                    error=f"Exit code: {final_info.exit_code}" if not success else None,
                )

                if on_complete:
                    on_complete(role, execution)

            except Exception as e:
                execution = self.record_agent_result(
                    role,
                    success=False,
                    error=str(e),
                )
                if on_complete:
                    on_complete(role, execution)

        return self.complete_current_wave() or wave

    def execute_all_waves(
        self,
        on_wave_complete: Callable[[AgentWave], None] | None = None,
    ) -> list[AgentWave]:
        """Execute all waves until completion or failure.

        Args:
            on_wave_complete: Optional callback after each wave

        Returns:
            List of completed waves
        """
        while True:
            wave = self.get_next_wave()
            if not wave:
                break

            completed = self.execute_wave(wave)

            if on_wave_complete:
                on_wave_complete(completed)

            if completed.has_failures:
                logger.error(
                    "Wave failed, stopping execution",
                    pipeline_id=self.pipeline.id,
                    wave=completed.wave_number,
                )
                break

        # Save final state
        self.dispatcher.save_contract()

        return self.completed_waves

    def get_execution_status(self) -> dict[str, Any]:
        """Get current execution status.

        Returns:
            Status dictionary
        """
        return {
            "pipeline_id": self.pipeline.id,
            "current_wave": self.current_wave.wave_number if self.current_wave else None,
            "completed_waves": len(self.completed_waves),
            "is_complete": self.dispatcher.is_complete(),
            "has_failures": self.dispatcher.has_failures(),
            "dispatcher_status": self.dispatcher.get_status_summary(),
        }


def create_multi_agent_executor(
    pipeline_id: str,
    repo_path: Path | str,
) -> MultiAgentExecutor:
    """Create a multi-agent executor for a pipeline.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository

    Returns:
        MultiAgentExecutor instance
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)

    store = get_state_store(repo_path)
    pipeline = store.load_pipeline(pipeline_id)

    return MultiAgentExecutor(pipeline, repo_path)
