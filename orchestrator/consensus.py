"""Consensus protocol for concurrent phase completion.

Tracks per-agent readiness states and evaluates whether all agents
agree the phase is complete. Supports objections and HITL escalation
on timeout.
"""

import threading
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReadinessState(StrEnum):
    """Agent readiness states for consensus."""

    WORKING = "WORKING"
    READY = "READY"
    BLOCKED = "BLOCKED"
    OBJECTING = "OBJECTING"


class AgentReadiness(BaseModel):
    """Readiness state for a single agent."""

    role: str = Field(..., description="Agent role")
    state: ReadinessState = Field(default=ReadinessState.WORKING)
    reason: str | None = Field(default=None, description="Reason for current state")
    timestamp: datetime | None = Field(default=None, description="Last state change")


class ConsensusEvaluator:
    """Evaluates consensus for concurrent phase completion.

    Tracks per-agent readiness per pipeline and determines when
    all agents agree the phase is complete.
    """

    def __init__(self) -> None:
        # pipeline_id -> {role -> AgentReadiness}
        self._states: dict[str, dict[str, AgentReadiness]] = {}
        self._lock = threading.RLock()

    def register_agent(self, pipeline_id: str, role: str) -> None:
        """Register an agent for consensus tracking."""
        with self._lock:
            if pipeline_id not in self._states:
                self._states[pipeline_id] = {}
            self._states[pipeline_id][role] = AgentReadiness(
                role=role,
                state=ReadinessState.WORKING,
                timestamp=datetime.now(UTC),
            )

    def update_readiness(
        self,
        pipeline_id: str,
        role: str,
        state: ReadinessState,
        reason: str | None = None,
    ) -> AgentReadiness:
        """Update an agent's readiness state.

        Args:
            pipeline_id: Pipeline ID.
            role: Agent role.
            state: New readiness state.
            reason: Optional reason for state change.

        Returns:
            Updated AgentReadiness.

        Raises:
            ValueError: If agent not registered.
        """
        with self._lock:
            agents = self._states.get(pipeline_id, {})
            if role not in agents:
                # Auto-register if not yet registered
                self.register_agent(pipeline_id, role)
                agents = self._states[pipeline_id]

            agents[role] = AgentReadiness(
                role=role,
                state=state,
                reason=reason,
                timestamp=datetime.now(UTC),
            )
            return agents[role]

    def evaluate(self, pipeline_id: str) -> dict[str, Any]:
        """Evaluate consensus for a pipeline.

        Returns:
            Dict with:
                is_complete: True if all agents are READY
                blocking_agents: List of roles not yet READY
                has_objections: True if any agent is OBJECTING
                agents: Dict of role -> readiness state
        """
        with self._lock:
            agents = self._states.get(pipeline_id, {})
            if not agents:
                return {
                    "is_complete": False,
                    "blocking_agents": [],
                    "has_objections": False,
                    "agents": {},
                }

            blocking = []
            has_objections = False
            for role, readiness in agents.items():
                if readiness.state != ReadinessState.READY:
                    blocking.append(role)
                if readiness.state == ReadinessState.OBJECTING:
                    has_objections = True

            return {
                "is_complete": len(blocking) == 0,
                "blocking_agents": blocking,
                "has_objections": has_objections,
                "agents": dict(agents.items()),
            }

    def get_state(self, pipeline_id: str) -> dict[str, Any]:
        """Get consensus state for status reporting."""
        return self.evaluate(pipeline_id)

    def remove_agent(self, pipeline_id: str, role: str) -> None:
        """Remove an agent from consensus tracking (e.g., on failure)."""
        with self._lock:
            agents = self._states.get(pipeline_id, {})
            agents.pop(role, None)

    def clear(self, pipeline_id: str) -> None:
        """Clear all consensus state for a pipeline."""
        with self._lock:
            self._states.pop(pipeline_id, None)


# Singleton
_consensus_evaluator: ConsensusEvaluator | None = None
_evaluator_lock = threading.Lock()


def get_consensus_evaluator() -> ConsensusEvaluator:
    """Get the singleton consensus evaluator."""
    global _consensus_evaluator
    if _consensus_evaluator is None:
        with _evaluator_lock:
            if _consensus_evaluator is None:
                _consensus_evaluator = ConsensusEvaluator()
    return _consensus_evaluator
