"""Concurrent phase executor for running multiple agents simultaneously.

Spawns all agents at phase start, all sharing the pipeline branch.
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


from consensus_wrapper import build_consensus_wrapped_command
from events import EventType, emit_event
from message_store import Message, MessageType, get_message_store
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    Pipeline,
)
from peer_consensus import (
    create_peer_consensus_tracker,
    get_peer_consensus_tracker,
)
from review_graph import get_review_graph_for_phase

logger = get_logger("orchestrator.concurrent_executor")

# Type alias for spawn function (matches multi_agent.py pattern)
SpawnFn = Callable[..., Any]

# Failure detection window: multiple failures within this window trigger abort
MULTI_FAILURE_WINDOW_SECONDS = 60


class ConcurrentPhaseExecutor:
    """Executes a pipeline phase with all agents running concurrently.

    All agents share the pipeline branch and communicate via the
    orchestrator message bus. Phase completion requires consensus
    from all agents.

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

        Returns roles appropriate for the pipeline's current phase,
        including both primary and reviewer roles.
        """
        from egg_contracts.agent_roles import get_roles_for_phase

        phase = self.pipeline.current_phase.value
        contract_roles = get_roles_for_phase(phase, include_reviewers=True)
        return [AgentRole(r.value) for r in contract_roles]

    def get_worktree_branch(self, role: AgentRole) -> str:
        """Get the worktree branch name for an agent role.

        Returns the pipeline's shared branch when set, falling back to
        an issue-based branch name.  All agents share the same branch
        so their commits land on a single history.
        """
        if self.pipeline.branch:
            return self.pipeline.branch
        issue = self.pipeline.issue_number or self.pipeline.id
        return f"egg/issue-{issue}"

    def get_agent_env(self, role: AgentRole) -> dict[str, str]:
        """Get additional environment variables for concurrent mode."""
        config = self.pipeline.config
        poll_interval = getattr(config, "message_poll_hint_seconds", 30)
        env = {
            "EGG_CONCURRENT_MODE": "true",
            "EGG_MESSAGE_POLL_INTERVAL": str(poll_interval),
        }
        # Add review graph info for BRC protocol
        graph = get_review_graph_for_phase(self.pipeline.current_phase.value)
        if graph.is_producer(role.value):
            env["EGG_BRC_ROLE_TYPE"] = "producer"
            env["EGG_BRC_REVIEWERS"] = ",".join(graph.reviewers_for(role.value))
        if graph.is_reviewer(role.value):
            env["EGG_BRC_ROLE_TYPE"] = env.get("EGG_BRC_ROLE_TYPE", "") + (
                ",reviewer" if env.get("EGG_BRC_ROLE_TYPE") else "reviewer"
            )
            env["EGG_BRC_PRODUCERS"] = ",".join(graph.producers_for(role.value))
        return env

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
        graph = get_review_graph_for_phase(self.pipeline.current_phase.value)
        tracker = create_peer_consensus_tracker(self.pipeline.id, graph)
        executions: list[AgentExecution] = []

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {}
            for role in roles:
                # Register agent for consensus tracking
                tracker.register_agent(role.value)

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
            command = build_consensus_wrapped_command(prompt_text)

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
        tracker = get_peer_consensus_tracker(self.pipeline.id)
        crash_result = None
        if tracker:
            crash_result = tracker.handle_agent_crash(role)
            if crash_result.get("action") == "escalate":
                logger.warning(
                    "Agent crash requires escalation", role=role, reason=crash_result.get("reason")
                )

        emit_event(
            EventType.AGENT_FAILED,
            self.pipeline.id,
            data={"role": role, "error": error},
        )

        # Build context-aware HITL question
        question = f"Agent '{role}' failed: {error}."
        if crash_result and crash_result.get("blocking_producers"):
            blocking = crash_result["blocking_producers"]
            question += f" Reviewer had pending reviews for: {blocking}."
        question += " How to proceed?"

        # Create HITL decision
        decision = self.pipeline.add_decision(
            question=question,
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
            "crash_result": crash_result,
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
        tracker = get_peer_consensus_tracker(self.pipeline.id)
        if tracker:
            return tracker.evaluate()
        return {"is_complete": False, "blocking_agents": [], "has_objections": False, "agents": {}}
