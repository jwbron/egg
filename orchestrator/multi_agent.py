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
from events import EventType, emit_event
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


"""Type alias for the spawner callable.

The spawn function receives (role, prompt, extra_env) and returns (exit_code, logs).
When provided, the executor uses this instead of the Docker client to spawn agents.
"""
SpawnFn = Callable[
    [AgentRole, str, dict[str, str]],
    tuple[int, str],
]


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
        spawn_fn: SpawnFn | None = None,
        max_parallel_agents: int = 10,
    ):
        """Initialize executor.

        Args:
            pipeline: Pipeline to execute
            repo_path: Path to repository
            dispatcher: Optional dispatcher (created if not provided)
            spawn_fn: Optional callable to spawn agents. When provided,
                agents are spawned via this function instead of Docker.
                Signature: (role, prompt, extra_env) -> (exit_code, logs)
            max_parallel_agents: Maximum agents to run concurrently in a wave
        """
        self.pipeline = pipeline
        self.repo_path = repo_path
        self.dispatcher = dispatcher or create_dispatcher(pipeline, repo_path)
        self.spawn_fn = spawn_fn
        self.max_parallel_agents = max_parallel_agents

        if spawn_fn is None:
            self.docker_client = get_docker_client()
        else:
            self.docker_client = None

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

                emit_event(
                    EventType.AGENT_STARTED,
                    self.pipeline.id,
                    data={
                        "role": role.value,
                        "container_id": info.container_id[:12],
                        "wave": wave.wave_number,
                        "phase": self.pipeline.current_phase.value,
                        "status": "running",
                    },
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

            event_type = EventType.AGENT_COMPLETED if success else EventType.AGENT_FAILED
            emit_event(
                event_type,
                self.pipeline.id,
                data={
                    "role": role.value,
                    "success": success,
                    "commit": commit,
                    "phase": self.pipeline.current_phase.value,
                    "status": "complete" if success else "failed",
                    "error": error,
                },
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

    def _execute_wave_with_spawn_fn(
        self,
        wave: AgentWave,
        agent_prompts: dict[AgentRole, str],
        on_complete: Callable[[AgentRole, AgentExecution], None] | None = None,
    ) -> AgentWave:
        """Execute a wave using the spawn_fn callable.

        Each agent is run in its own thread (up to max_parallel_agents concurrent).
        The spawn function blocks until completion.

        Args:
            wave: Wave to execute
            agent_prompts: Role-to-prompt mapping for agents in this wave
            on_complete: Optional callback when each agent completes

        Returns:
            Completed wave
        """
        import json
        from concurrent.futures import ThreadPoolExecutor

        assert self.spawn_fn is not None

        wave.started_at = datetime.utcnow()
        self.current_wave = wave

        semaphore = threading.Semaphore(self.max_parallel_agents)

        def run_agent(role: AgentRole) -> None:
            with semaphore:
                try:
                    # Get handoff data from completed agents
                    handoff_data = self.dispatcher.get_handoff_data(role)

                    extra_env = {
                        "EGG_AGENT_ROLE": role.value,
                        "EGG_HANDOFF_DATA": json.dumps(handoff_data) if handoff_data else "{}",
                        "EGG_WAVE_NUMBER": str(wave.wave_number),
                    }

                    prompt = agent_prompts.get(role, "")

                    # Skip agents that don't have a prompt for this phase.
                    # The contract may contain roles from other phases (e.g.
                    # DOCUMENTER in the plan phase) — spawning them with an
                    # empty prompt crashes Claude Code.
                    if not prompt:
                        logger.info(
                            "Skipping agent with no prompt for current phase",
                            pipeline_id=self.pipeline.id,
                            role=role.value,
                            phase=self.pipeline.current_phase.value,
                        )
                        # record_agent_result already calls
                        # dispatcher.complete_agent(), so no separate call
                        # is needed.
                        execution = self.record_agent_result(
                            role,
                            success=True,
                            error=None,
                        )
                        if on_complete:
                            on_complete(role, execution)
                        return

                    # Mark as started
                    self.dispatcher.start_agent(role)

                    logger.info(
                        "Agent spawning via spawn_fn",
                        pipeline_id=self.pipeline.id,
                        role=role.value,
                        wave=wave.wave_number,
                    )

                    emit_event(
                        EventType.AGENT_STARTED,
                        self.pipeline.id,
                        data={
                            "role": role.value,
                            "wave": wave.wave_number,
                            "phase": self.pipeline.current_phase.value,
                            "status": "running",
                        },
                    )

                    exit_code, logs = self.spawn_fn(role, prompt, extra_env)
                    success = exit_code == 0

                    execution = self.record_agent_result(
                        role,
                        success=success,
                        error=f"Exit code: {exit_code}" if not success else None,
                    )

                    if on_complete:
                        on_complete(role, execution)

                except Exception as e:
                    logger.error(
                        "Agent spawn_fn failed",
                        pipeline_id=self.pipeline.id,
                        role=role.value,
                        error=str(e),
                    )
                    execution = self.record_agent_result(
                        role,
                        success=False,
                        error=str(e),
                    )
                    if on_complete:
                        on_complete(role, execution)

        with ThreadPoolExecutor(max_workers=self.max_parallel_agents) as executor:
            futures = [executor.submit(run_agent, role) for role in wave.agents]
            for future in futures:
                future.result()  # Wait for all to complete

        return self.complete_current_wave() or wave

    def execute_wave(
        self,
        wave: AgentWave,
        on_complete: Callable[[AgentRole, AgentExecution], None] | None = None,
        agent_prompts: dict[AgentRole, str] | None = None,
    ) -> AgentWave:
        """Execute a wave and wait for completion.

        This is a synchronous method that blocks until all agents complete.

        Args:
            wave: Wave to execute
            on_complete: Optional callback when each agent completes
            agent_prompts: Role-to-prompt mapping (required when using spawn_fn)

        Returns:
            Completed wave
        """
        # Use spawn_fn path if available
        if self.spawn_fn is not None:
            return self._execute_wave_with_spawn_fn(
                wave,
                agent_prompts=agent_prompts or {},
                on_complete=on_complete,
            )

        # Docker-based path (original)
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
        agent_prompts: dict[AgentRole, str] | None = None,
        max_waves: int = 5,
        health_check_runner: Any = None,
    ) -> list[AgentWave]:
        """Execute all waves until completion or failure.

        Args:
            on_wave_complete: Optional callback after each wave
            agent_prompts: Role-to-prompt mapping (required when using spawn_fn)
            max_waves: Safety cap on number of wave iterations (default: 5)
            health_check_runner: Optional HealthCheckRunner for WAVE_COMPLETE checks

        Returns:
            List of completed waves
        """
        waves_executed = 0
        while True:
            if waves_executed >= max_waves:
                logger.warning(
                    "Max waves reached, stopping execution",
                    pipeline_id=self.pipeline.id,
                    max_waves=max_waves,
                )
                break

            wave = self.get_next_wave()
            if not wave:
                break

            completed = self.execute_wave(
                wave,
                agent_prompts=agent_prompts,
            )
            waves_executed += 1

            if on_wave_complete:
                on_wave_complete(completed)

            # Run WAVE_COMPLETE health checks
            if health_check_runner is not None:
                if self._run_wave_health_checks(health_check_runner, waves_executed):
                    logger.warning(
                        "Health check requested pipeline failure after wave",
                        pipeline_id=self.pipeline.id,
                        wave=completed.wave_number,
                    )
                    break

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

    def _run_wave_health_checks(self, runner: Any, wave_number: int) -> bool:
        """Run WAVE_COMPLETE health checks.

        Args:
            runner: HealthCheckRunner instance
            wave_number: Current wave number

        Returns:
            True if FAIL_PIPELINE action was returned by any check
        """
        try:
            from health_checks.context import PipelineHealthContext
            from health_checks.runner import worst_action
            from health_checks.types import HealthAction, HealthTrigger

            ctx = PipelineHealthContext(
                pipeline=self.pipeline,
                repo_path=self.repo_path,
                trigger=HealthTrigger.WAVE_COMPLETE.value,
                wave_number=wave_number,
            )
            results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)
            return worst_action(results) == HealthAction.FAIL_PIPELINE
        except Exception as exc:
            logger.debug(
                "WAVE_COMPLETE health check failed",
                pipeline_id=self.pipeline.id,
                error=str(exc),
            )
            return False

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
