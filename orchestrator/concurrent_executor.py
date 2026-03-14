"""Concurrent phase executor for running multiple agents simultaneously.

Spawns all agents at phase start, each with its own worktree branch.
Monitors agent health, collects completion signals, and manages
consensus-based phase completion.
"""

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from consensus import get_consensus_evaluator
from egg_agent import build_agent_command
from events import EventType, emit_event
from message_store import Message, MessageType, get_message_store
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    Pipeline,
)

logger = get_logger("orchestrator.concurrent_executor")

# Type alias for spawn function (matches multi_agent.py pattern)
SpawnFn = Callable[..., Any]

# Failure detection window: multiple failures within this window trigger abort
MULTI_FAILURE_WINDOW_SECONDS = 60


class ConcurrentPhaseExecutor:
    """Executes a pipeline phase with all agents running concurrently.

    Each agent gets its own worktree branch (egg/issue-{N}/{role}) and
    communicates via the orchestrator message bus. Phase completion
    requires consensus from all agents.

    Container failure behavior:
    - Single failure: Log, notify other agents, create HITL decision
      with retry/abort/continue options.
    - Multiple failures (2+ within 60s): Abort phase immediately.
    - Failure during consensus: Remove READY signal, treat as single failure.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        spawn_fn: SpawnFn,
        max_concurrent: int = 6,
    ) -> None:
        self.pipeline = pipeline
        self.spawn_fn = spawn_fn
        self.max_concurrent = max_concurrent
        self._failure_times: list[datetime] = []
        self._lock = threading.Lock()

    def get_agent_roles(self) -> list[AgentRole]:
        """Get the agent roles for concurrent execution.

        Returns implement-phase roles: coder, tester, documenter,
        checker, reviewer_code, reviewer_contract.
        """
        return [
            AgentRole.CODER,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.CHECKER,
            AgentRole.REVIEWER_CODE,
            AgentRole.REVIEWER_CONTRACT,
        ]

    def get_worktree_branch(self, role: AgentRole) -> str:
        """Get the worktree branch name for an agent role."""
        issue = self.pipeline.issue_number or self.pipeline.id
        return f"egg/issue-{issue}/{role.value}"

    def get_agent_env(self, role: AgentRole) -> dict[str, str]:
        """Get additional environment variables for concurrent mode."""
        config = self.pipeline.config
        poll_interval = getattr(config, "message_poll_hint_seconds", 30)
        return {
            "EGG_CONCURRENT_MODE": "true",
            "EGG_MESSAGE_POLL_INTERVAL": str(poll_interval),
        }

    def spawn_all(
        self,
        agent_prompts: dict[AgentRole, str] | None = None,
    ) -> list[AgentExecution]:
        """Spawn all agent containers concurrently.

        Args:
            agent_prompts: Mapping of role to prompt text. When provided,
                each agent container is started with a Claude CLI command
                using the role-specific prompt.

        Returns:
            List of AgentExecution records for spawned agents.
        """
        roles = self.get_agent_roles()
        evaluator = get_consensus_evaluator()
        executions: list[AgentExecution] = []

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {}
            for role in roles:
                # Register agent for consensus tracking
                evaluator.register_agent(self.pipeline.id, role.value)

                prompt_text = (agent_prompts or {}).get(role, "")
                future = pool.submit(self._spawn_agent, role, prompt_text)
                futures[future] = role

            for future in as_completed(futures):
                role = futures[future]
                try:
                    execution = future.result()
                    executions.append(execution)
                    emit_event(
                        EventType.AGENT_STARTED,
                        self.pipeline.id,
                        data={"role": role.value},
                    )
                except Exception as e:
                    logger.error(
                        "Failed to spawn agent",
                        role=role.value,
                        error=str(e),
                        pipeline_id=self.pipeline.id,
                    )
                    executions.append(
                        AgentExecution(
                            role=role,
                            status=AgentExecutionStatus.FAILED,
                            error=str(e),
                        )
                    )

        return executions

    def _spawn_agent(self, role: AgentRole, prompt_text: str = "") -> AgentExecution:
        """Spawn a single agent container.

        Args:
            role: The agent role to spawn.
            prompt_text: The prompt to pass to the Claude CLI. When non-empty,
                a sandbox command is built and passed to the spawn function.
        """
        branch = self.get_worktree_branch(role)
        env = self.get_agent_env(role)

        command: list[str] | None = None
        if prompt_text:
            command = build_agent_command(prompt_text)

        result = self.spawn_fn(
            role=role,
            branch=branch,
            extra_env=env,
            command=command,
        )

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.RUNNING,
            container_id=result.container_info.container_id,
            started_at=datetime.now(UTC),
        )

    def handle_agent_failure(self, role: str, error: str) -> dict[str, Any]:
        """Handle an agent failure during concurrent execution.

        Args:
            role: The failed agent's role.
            error: Error description.

        Returns:
            Dict describing the action taken: 'hitl_decision' or 'phase_abort'.
        """
        now = datetime.now(UTC)

        with self._lock:
            self._failure_times.append(now)

            # Check for multiple simultaneous failures
            recent = [
                t
                for t in self._failure_times
                if (now - t).total_seconds() < MULTI_FAILURE_WINDOW_SECONDS
            ]

            if len(recent) >= 2:
                return self._abort_phase(error, recent_failures=len(recent))

        # Single failure: notify other agents and create HITL decision
        return self._handle_single_failure(role, error)

    def _handle_single_failure(self, role: str, error: str) -> dict[str, Any]:
        """Handle a single agent failure."""
        # Notify other agents via message bus
        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=self.pipeline.id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.AGENT_FAILED,
                subject=f"Agent {role} failed",
                body=error,
                phase=self.pipeline.current_phase.value,
            )
        )

        # Remove from consensus
        evaluator = get_consensus_evaluator()
        evaluator.remove_agent(self.pipeline.id, role)

        emit_event(
            EventType.AGENT_FAILED,
            self.pipeline.id,
            data={"role": role, "error": error},
        )

        # Create HITL decision
        decision = self.pipeline.add_decision(
            question=f"Agent '{role}' failed: {error}. How to proceed?",
            options=["Retry (respawn agent)", "Abort phase", "Continue without"],
            phase=self.pipeline.current_phase,
        )

        logger.warning(
            "Single agent failure, HITL decision created",
            role=role,
            error=error,
            decision_id=decision.id,
            pipeline_id=self.pipeline.id,
        )

        return {
            "action": "hitl_decision",
            "decision_id": decision.id,
            "failed_role": role,
        }

    def _abort_phase(self, error: str, recent_failures: int) -> dict[str, Any]:
        """Abort the phase due to multiple simultaneous failures."""
        emit_event(
            EventType.PHASE_FAILED,
            self.pipeline.id,
            data={
                "reason": "multiple_agent_failures",
                "recent_failures": recent_failures,
                "error": error,
            },
        )

        decision = self.pipeline.add_decision(
            question=f"Multiple agent failures ({recent_failures} within {MULTI_FAILURE_WINDOW_SECONDS}s). Phase aborted. How to proceed?",
            options=["Retry phase", "Cancel pipeline"],
            phase=self.pipeline.current_phase,
        )

        logger.error(
            "Multiple agent failures, phase aborted",
            recent_failures=recent_failures,
            error=error,
            pipeline_id=self.pipeline.id,
        )

        return {
            "action": "phase_abort",
            "decision_id": decision.id,
            "recent_failures": recent_failures,
        }

    def check_consensus(self) -> dict[str, Any]:
        """Check if consensus has been reached for phase completion."""
        evaluator = get_consensus_evaluator()
        return evaluator.evaluate(self.pipeline.id)
