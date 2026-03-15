"""BRC (Broadcast-Review-Converge) peer consensus tracker.

Replaces the ConsensusEvaluator READY-tallying with a structured
peer consensus protocol. Agents propose, review, and confirm
through an asymmetric review graph.

State machine per producer:
    WORKING -> PROPOSED -> CONFIRMED
        ^         |
        └─────────┘  (NACK received -> address -> re-propose)

State machine per reviewer:
    WORKING -> REVIEWING -> CONFIRMED
                |   ^
                └───┘  (producer re-proposes -> re-review)
"""

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from egg_orchestrator.types import ConsensusPhase

from approval_matrix import ApprovalMatrix, ApprovalState
from attestation_schemas import (
    AttestationStrictness,
    ProposalPayload,
    ReviewPayload,
    validate_attestation,
)
from events import EventType, emit_event
from review_graph import ReviewGraph

logger = get_logger("orchestrator.peer_consensus")


# Default configuration
DEFAULT_COOLDOWN_SECONDS = 30
DEFAULT_MAX_FLIP_FLOPS = 3
DEFAULT_MAX_REVISION_ROUNDS = 2


class PeerConsensusTracker:
    """Tracks BRC consensus state for a single pipeline phase.

    Manages per-agent ConsensusPhase, the ReviewGraph, and the
    ApprovalMatrix. Handles proposals, ACKs, NACKs, withdrawals,
    and confirmations.
    """

    def __init__(
        self,
        pipeline_id: str,
        graph: ReviewGraph,
        *,
        attestation_strictness: AttestationStrictness = AttestationStrictness.STRICT,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        max_flip_flops: int = DEFAULT_MAX_FLIP_FLOPS,
        max_revision_rounds: int = DEFAULT_MAX_REVISION_ROUNDS,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.graph = graph
        self.matrix = ApprovalMatrix(graph)
        self.attestation_strictness = attestation_strictness
        self.cooldown_seconds = cooldown_seconds
        self.max_flip_flops = max_flip_flops
        self.max_revision_rounds = max_revision_rounds

        self._lock = threading.RLock()

        # Per-agent BRC phase (producer state machine)
        self._producer_phases: dict[str, ConsensusPhase] = {}
        # Per-agent BRC phase (reviewer state machine)
        self._reviewer_phases: dict[str, ConsensusPhase] = {}
        # Per-agent confirmed status (both state machines must confirm)
        self._confirmed: set[str] = set()
        # Timestamp of last proposal per producer (for cooldown)
        self._proposal_timestamps: dict[str, datetime] = {}
        # Flip-flop counter per producer (proposal -> withdraw cycles)
        self._flip_flop_counts: dict[str, int] = {}
        # Track proposal artifacts per producer (for scoped re-evaluation)
        self._proposal_artifacts: dict[str, list[str]] = {}

    def register_agent(self, role: str) -> None:
        """Register an agent for consensus tracking."""
        with self._lock:
            if self.graph.is_producer(role):
                self._producer_phases[role] = ConsensusPhase.WORKING
            if self.graph.is_reviewer(role):
                self._reviewer_phases[role] = ConsensusPhase.WORKING

    def handle_propose(
        self,
        agent_role: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a CONSENSUS_PROPOSE from a producer.

        Validates attestation, transitions agent to PROPOSED, records
        in approval matrix.
        """
        with self._lock:
            if not self.graph.is_producer(agent_role):
                raise ValueError(f"{agent_role} is not a producer in this review graph")

            # Validate payload
            proposal = ProposalPayload(**payload)

            # Validate role-specific attestation
            if proposal.attestation:
                validate_attestation(
                    agent_role,
                    proposal.attestation,
                    strictness=self.attestation_strictness,
                    is_producer=True,
                )

            # Record proposal version
            version = self.matrix.record_proposal(agent_role)

            # Transition to PROPOSED
            self._producer_phases[agent_role] = ConsensusPhase.PROPOSED
            self._proposal_timestamps[agent_role] = datetime.now(UTC)
            self._proposal_artifacts[agent_role] = list(proposal.artifacts)

            emit_event(
                EventType.CONSENSUS_PROPOSE_RECEIVED,
                self.pipeline_id,
                data={
                    "role": agent_role,
                    "version": version,
                    "artifacts": proposal.artifacts,
                },
            )

            return {
                "version": version,
                "status": "proposed",
                "reviewers": self.graph.reviewers_for(agent_role),
            }

    def handle_ack(
        self,
        reviewer_role: str,
        producer_role: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a CONSENSUS_ACK from a reviewer."""
        with self._lock:
            if not self.graph.is_reviewer(reviewer_role):
                raise ValueError(f"{reviewer_role} is not a reviewer in this review graph")
            if not self.graph.get_edge(reviewer_role, producer_role):
                raise ValueError(f"No review edge: {reviewer_role} -> {producer_role}")

            # Validate review payload
            review = ReviewPayload(verdict="ACK", **payload)

            # Validate role-specific attestation
            if review.attestation:
                validate_attestation(
                    reviewer_role,
                    review.attestation,
                    strictness=self.attestation_strictness,
                    is_producer=False,
                )

            version = self.matrix.get_proposal_version(producer_role)
            self.matrix.record_ack(
                reviewer_role,
                producer_role,
                version,
                artifact_refs=review.artifact_references,
            )

            # Transition reviewer to REVIEWING
            self._reviewer_phases[reviewer_role] = ConsensusPhase.REVIEWING

            emit_event(
                EventType.CONSENSUS_ACK_RECEIVED,
                self.pipeline_id,
                data={
                    "reviewer": reviewer_role,
                    "producer": producer_role,
                    "version": version,
                },
            )

            # Check if producer is now fully ACKed
            fully_acked = self.matrix.is_fully_acked(producer_role)

            return {
                "status": "acked",
                "fully_acked": fully_acked,
                "version": version,
            }

    def handle_nack(
        self,
        reviewer_role: str,
        producer_role: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle a CONSENSUS_NACK from a reviewer."""
        with self._lock:
            if not self.graph.is_reviewer(reviewer_role):
                raise ValueError(f"{reviewer_role} is not a reviewer")
            if not self.graph.get_edge(reviewer_role, producer_role):
                raise ValueError(f"No review edge: {reviewer_role} -> {producer_role}")

            # Validate review payload
            review = ReviewPayload(verdict="NACK", **payload)

            version = self.matrix.get_proposal_version(producer_role)
            self.matrix.record_nack(
                reviewer_role,
                producer_role,
                version,
                reason=review.reason,
                artifact_refs=review.artifact_references,
            )

            # Transition reviewer to REVIEWING
            self._reviewer_phases[reviewer_role] = ConsensusPhase.REVIEWING

            # Transition producer back to WORKING
            self._producer_phases[producer_role] = ConsensusPhase.WORKING

            # Check revision count
            rev_count = self.matrix.revision_count(reviewer_role, producer_role)
            needs_escalation = rev_count >= self.max_revision_rounds

            emit_event(
                EventType.CONSENSUS_NACK_RECEIVED,
                self.pipeline_id,
                data={
                    "reviewer": reviewer_role,
                    "producer": producer_role,
                    "version": version,
                    "reason": review.reason,
                    "revision_count": rev_count,
                    "needs_escalation": needs_escalation,
                },
            )

            return {
                "status": "nacked",
                "reason": review.reason,
                "revision_count": rev_count,
                "needs_escalation": needs_escalation,
            }

    def handle_withdraw(
        self,
        agent_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Handle a CONSENSUS_WITHDRAW from a producer."""
        with self._lock:
            if not self.graph.is_producer(agent_role):
                raise ValueError(f"{agent_role} is not a producer")

            if not reason:
                raise ValueError("Withdrawal requires a reason citing specific new information")

            # Check cooldown
            last_proposal = self._proposal_timestamps.get(agent_role)
            if last_proposal:
                elapsed = (datetime.now(UTC) - last_proposal).total_seconds()
                if elapsed < self.cooldown_seconds:
                    raise ValueError(
                        f"Cooldown active: {self.cooldown_seconds - elapsed:.0f}s remaining. "
                        f"Cannot withdraw within {self.cooldown_seconds}s of proposing."
                    )

            # Check flip-flop count
            self._flip_flop_counts[agent_role] = self._flip_flop_counts.get(agent_role, 0) + 1
            if self._flip_flop_counts[agent_role] >= self.max_flip_flops:
                emit_event(
                    EventType.CONSENSUS_FAILURE,
                    self.pipeline_id,
                    data={
                        "type": "flip_flop_lockout",
                        "role": agent_role,
                        "flip_flops": self._flip_flop_counts[agent_role],
                    },
                )
                return {
                    "status": "locked_out",
                    "reason": f"Locked out after {self._flip_flop_counts[agent_role]} flip-flops",
                    "needs_escalation": True,
                }

            # Transition back to WORKING
            self._producer_phases[agent_role] = ConsensusPhase.WORKING

            emit_event(
                EventType.CONSENSUS_WITHDRAW_RECEIVED,
                self.pipeline_id,
                data={"role": agent_role, "reason": reason},
            )

            return {"status": "withdrawn", "reason": reason}

    def handle_confirmed(self, agent_role: str) -> dict[str, Any]:
        """Handle a CONSENSUS_CONFIRMED from an agent."""
        with self._lock:
            # Check if agent can confirm
            if self.graph.is_producer(agent_role):
                if not self.matrix.is_fully_acked(agent_role):
                    raise ValueError(
                        f"Producer {agent_role} cannot confirm: not fully ACKed"
                    )
                self._producer_phases[agent_role] = ConsensusPhase.CONFIRMED

            if self.graph.is_reviewer(agent_role):
                # Check all assigned producers have been reviewed
                producers = self.graph.producers_for(agent_role)
                for producer in producers:
                    if not self.matrix.has_reviewed(agent_role, producer):
                        raise ValueError(
                            f"Reviewer {agent_role} cannot confirm: hasn't reviewed {producer}"
                        )
                self._reviewer_phases[agent_role] = ConsensusPhase.CONFIRMED

            # For dual-role agents (tester), both must be CONFIRMED
            is_fully_confirmed = True
            if self.graph.is_producer(agent_role):
                if self._producer_phases.get(agent_role) != ConsensusPhase.CONFIRMED:
                    is_fully_confirmed = False
            if self.graph.is_reviewer(agent_role):
                if self._reviewer_phases.get(agent_role) != ConsensusPhase.CONFIRMED:
                    is_fully_confirmed = False

            if is_fully_confirmed:
                self._confirmed.add(agent_role)

            emit_event(
                EventType.CONSENSUS_CONFIRMED_RECEIVED,
                self.pipeline_id,
                data={
                    "role": agent_role,
                    "fully_confirmed": is_fully_confirmed,
                },
            )

            # Check global consensus
            consensus_reached = self._check_consensus()

            return {
                "status": "confirmed" if is_fully_confirmed else "partially_confirmed",
                "consensus_reached": consensus_reached,
            }

    def handle_re_propose(
        self,
        agent_role: str,
        payload: dict[str, Any],
        changed_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Handle a re-proposal after NACK. Triggers scoped re-evaluation."""
        with self._lock:
            # First, do scoped re-evaluation
            if changed_artifacts:
                invalidated = self.matrix.invalidate_overlapping_acks(
                    agent_role, changed_artifacts
                )
            else:
                # Conservative: invalidate all ACKs
                invalidated = []
                for reviewer in self.graph.reviewers_for(agent_role):
                    if self.matrix.invalidate_ack(reviewer, agent_role):
                        invalidated.append(reviewer)

            # The NACKing reviewer(s) always need to re-review
            # (their state is already NACKED in the matrix)

        # Now handle as a normal proposal
        result = self.handle_propose(agent_role, payload)
        result["invalidated_reviewers"] = invalidated
        return result

    def handle_agent_crash(self, role: str) -> dict[str, Any]:
        """Handle an agent crash mid-protocol."""
        with self._lock:
            if self.graph.is_producer(role):
                # Producer crash: proposal stands, reviewers continue
                # If reviewers NACK and producer can't respond, escalate
                pass

            if self.graph.is_reviewer(role):
                # Reviewer crash: remove from graph review requirements
                # Check if remaining ACKs suffice
                producers = self.graph.producers_for(role)
                sole_reviewer_for = []
                for producer in producers:
                    remaining_reviewers = [
                        r for r in self.graph.reviewers_for(producer)
                        if r != role
                    ]
                    if not remaining_reviewers:
                        sole_reviewer_for.append(producer)

                if sole_reviewer_for:
                    emit_event(
                        EventType.CONSENSUS_FAILURE,
                        self.pipeline_id,
                        data={
                            "type": "reviewer_crash_sole",
                            "crashed_role": role,
                            "unreviewed_producers": sole_reviewer_for,
                        },
                    )
                    return {
                        "action": "escalate",
                        "reason": f"Reviewer {role} crashed and was sole reviewer for {sole_reviewer_for}",
                    }

            # Remove from confirmed set
            self._confirmed.discard(role)

            return {"action": "continue", "crashed_role": role}

    def handle_timeout(self) -> dict[str, Any]:
        """Handle consensus timeout. Evaluate by role criticality."""
        with self._lock:
            blocking = self.matrix.get_all_blocking_edges()
            if not blocking:
                return {"action": "proceed", "reason": "No blocking edges"}

            # Separate critical vs advisory blockers
            critical_blockers = []
            advisory_blockers = []
            for entry in blocking:
                edge = self.graph.get_edge(entry.reviewer_role, entry.producer_role)
                if edge and edge.criticality.value == "critical":
                    critical_blockers.append(entry)
                else:
                    advisory_blockers.append(entry)

            if critical_blockers:
                emit_event(
                    EventType.CONSENSUS_FAILURE,
                    self.pipeline_id,
                    data={
                        "type": "timeout_critical",
                        "critical_blockers": [e.to_dict() for e in critical_blockers],
                        "advisory_blockers": [e.to_dict() for e in advisory_blockers],
                    },
                )
                return {
                    "action": "escalate",
                    "reason": "Critical reviewers unconfirmed at timeout",
                    "critical_blockers": [e.to_dict() for e in critical_blockers],
                    "approval_matrix": self.matrix.to_dict(),
                }

            # Only advisory blockers — proceed with notification
            emit_event(
                EventType.CONSENSUS_TIMEOUT,
                self.pipeline_id,
                data={
                    "type": "timeout_advisory_only",
                    "advisory_blockers": [e.to_dict() for e in advisory_blockers],
                },
            )
            return {
                "action": "proceed_with_notification",
                "advisory_blockers": [e.to_dict() for e in advisory_blockers],
            }

    def evaluate(self) -> dict[str, Any]:
        """Evaluate current consensus state.

        Returns dict compatible with the old ConsensusEvaluator.evaluate() format
        plus additional BRC-specific data.
        """
        with self._lock:
            all_roles = self.graph.all_roles()
            is_complete = all_roles.issubset(self._confirmed)

            blocking_agents = [r for r in all_roles if r not in self._confirmed]

            agents: dict[str, dict[str, Any]] = {}
            for role in all_roles:
                phase_info: dict[str, Any] = {}
                if self.graph.is_producer(role):
                    phase_info["producer_phase"] = self._producer_phases.get(
                        role, ConsensusPhase.WORKING
                    ).value
                if self.graph.is_reviewer(role):
                    phase_info["reviewer_phase"] = self._reviewer_phases.get(
                        role, ConsensusPhase.WORKING
                    ).value
                phase_info["confirmed"] = role in self._confirmed
                agents[role] = phase_info

            return {
                "is_complete": is_complete,
                "blocking_agents": blocking_agents,
                "has_objections": False,  # BRC doesn't use objections
                "agents": agents,
                "approval_matrix": self.matrix.to_dict(),
                "review_graph": self.graph.to_dict(),
                "protocol": "brc",
            }

    def get_state(self) -> dict[str, Any]:
        """Alias for evaluate() -- compatibility with ConsensusEvaluator."""
        return self.evaluate()

    def get_agent_phase(self, role: str) -> dict[str, str]:
        """Get the BRC phase(s) for an agent."""
        with self._lock:
            result: dict[str, str] = {}
            if self.graph.is_producer(role):
                result["producer_phase"] = self._producer_phases.get(
                    role, ConsensusPhase.WORKING
                ).value
            if self.graph.is_reviewer(role):
                result["reviewer_phase"] = self._reviewer_phases.get(
                    role, ConsensusPhase.WORKING
                ).value
            result["confirmed"] = str(role in self._confirmed)
            return result

    def remove_agent(self, role: str) -> None:
        """Remove an agent from consensus tracking (e.g., on failure)."""
        with self._lock:
            self._producer_phases.pop(role, None)
            self._reviewer_phases.pop(role, None)
            self._confirmed.discard(role)

    def clear(self) -> None:
        """Clear all consensus state."""
        with self._lock:
            self._producer_phases.clear()
            self._reviewer_phases.clear()
            self._confirmed.clear()
            self._proposal_timestamps.clear()
            self._flip_flop_counts.clear()
            self._proposal_artifacts.clear()

    def _check_consensus(self) -> bool:
        """Check if all agents have confirmed. Emit event if so."""
        all_roles = self.graph.all_roles()
        if all_roles.issubset(self._confirmed):
            emit_event(
                EventType.CONSENSUS_REACHED,
                self.pipeline_id,
                data={
                    "protocol": "brc",
                    "confirmed_roles": sorted(self._confirmed),
                },
            )
            return True
        return False


# --- Pipeline-level tracker management ---

_trackers: dict[str, PeerConsensusTracker] = {}
_trackers_lock = threading.Lock()


def get_peer_consensus_tracker(pipeline_id: str) -> PeerConsensusTracker | None:
    """Get the tracker for a pipeline, if one exists."""
    return _trackers.get(pipeline_id)


def create_peer_consensus_tracker(
    pipeline_id: str,
    graph: ReviewGraph,
    **kwargs: Any,
) -> PeerConsensusTracker:
    """Create and register a tracker for a pipeline."""
    with _trackers_lock:
        tracker = PeerConsensusTracker(pipeline_id, graph, **kwargs)
        _trackers[pipeline_id] = tracker
    return tracker


def remove_peer_consensus_tracker(pipeline_id: str) -> None:
    """Remove a tracker for a pipeline."""
    with _trackers_lock:
        tracker = _trackers.pop(pipeline_id, None)
        if tracker:
            tracker.clear()
