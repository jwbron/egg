"""PeerConsensusTracker invariants / crash / stall / excuse / reopen / timeout method bodies (#3312, slice-10).

Method bodies extracted verbatim from the pre-split ``orchestrator/peer_consensus.py``
and bound onto ``PeerConsensusTracker`` in the barrel
(``orchestrator/peer_consensus/__init__.py``). They take ``self`` explicitly and
are AST-identical to the originals. ``logger`` is imported from the package barrel
so it stays a single binding.
"""

from __future__ import annotations

from typing import Any

from action_guards import InvariantViolation
from action_guards import validate_invariants as _validate_invariants
from approval_matrix import ApprovalState
from egg_orchestrator.types import ConsensusPhase
from events import EventType, emit_event
from peer_consensus import logger


def validate_invariants(self) -> list[InvariantViolation]:
    """Validate that all protocol invariants hold.

    Returns a list of violations (empty means all invariants hold).
    This method is intended for defensive checks — call it periodically
    or after state transitions to catch bugs early.

    Invariants checked:
    1. No confirmed agent with unresolved NACK.
    2. No confirmed reviewer with stale ACK.
    3. No confirmed reviewer with unreviewed producer changes.
    4. No confirmed reviewer with zero-proposal producers.
    5. is_fully_acked consistency with matrix state.
    6. ack_commit_sha consistency with proposal commit SHA.
    """
    with self._lock:
        return _validate_invariants(
            self.graph,
            self.matrix,
            self._producer_phases,
            self._reviewer_phases,
            self._confirmed,
            proposal_commit_shas=self._proposal_commit_shas,
        )


def handle_agent_crash(self, role: str) -> dict[str, Any]:
    """Handle an agent crash mid-protocol."""
    with self._lock:
        # Always clean up confirmed state on crash, even if we escalate
        self._confirmed.discard(role)

        if self.graph.is_producer(role):
            # Producer crash: proposal stands, reviewers continue
            # If reviewers NACK and producer can't respond, escalate
            pass

        if self.graph.is_reviewer(role):
            # Reviewer crash: check impact on each assigned producer
            producers = self.graph.producers_for(role)
            sole_reviewer_for = []
            blocking_producers = []
            for producer in producers:
                remaining_reviewers = [r for r in self.graph.reviewers_for(producer) if r != role]
                if not remaining_reviewers:
                    sole_reviewer_for.append(producer)
                else:
                    # Check if this reviewer had a pending (non-ACKed) review.
                    # Skip producers that haven't proposed yet (version 0) —
                    # there's nothing to review so no blocking relationship.
                    latest_version = self.matrix.get_proposal_version(producer)
                    if latest_version > 0:
                        entry = self.matrix.get_entry(role, producer)
                        if (
                            entry is None
                            or entry.state != ApprovalState.ACKED
                            or entry.version != latest_version
                        ):
                            blocking_producers.append(producer)

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
                result: dict[str, Any] = {
                    "action": "escalate",
                    "reason": f"Reviewer {role} crashed and was sole reviewer for {sole_reviewer_for}",
                }
                # Include blocking_producers so HITL gets complete info
                # (reviewer may also have pending reviews for other producers)
                if blocking_producers:
                    result["blocking_producers"] = blocking_producers
                return result

            if blocking_producers:
                emit_event(
                    EventType.CONSENSUS_FAILURE,
                    self.pipeline_id,
                    data={
                        "type": "reviewer_crash_pending",
                        "crashed_role": role,
                        "blocking_producers": blocking_producers,
                    },
                )
                return {
                    "action": "escalate",
                    "reason": f"Reviewer {role} crashed with pending reviews for {blocking_producers}",
                    "blocking_producers": blocking_producers,
                }

        return {"action": "continue", "crashed_role": role}


def handle_stall_demotion(self, role: str, reason: str) -> dict[str, Any]:
    """Demote a stalled dual-role agent's review edges to ADVISORY.

    When a dual-role agent (e.g. tester) stalls, its pending reviewer
    assignments should not block other agents from reaching consensus.
    This demotes all CRITICAL edges where the stalled agent is a reviewer
    to ADVISORY, allowing consensus to proceed without its ACK.

    Args:
        role: The stalled agent's role.
        reason: Why the agent is being demoted (e.g. "missed heartbeats for 5+ minutes").

    Returns:
        Dict with action taken and affected producers.

    Raises:
        ValueError: If the role is not a reviewer in the review graph.
    """
    with self._lock:
        if not self.graph.is_reviewer(role):
            raise ValueError(f"Cannot demote '{role}': not a reviewer in the review graph")

        demoted_edges = self.graph.demote_edges_for_reviewer(role)

        if demoted_edges:
            emit_event(
                EventType.CONSENSUS_FAILURE,
                self.pipeline_id,
                data={
                    "type": "stall_demotion",
                    "role": role,
                    "reason": reason,
                    "demoted_producers": demoted_edges,
                },
            )
            logger.info(
                "Demoted stalled reviewer edges to advisory",
                role=role,
                reason=reason,
                demoted_producers=demoted_edges,
                pipeline_id=self.pipeline_id,
            )

        return {
            "action": "demoted",
            "role": role,
            "reason": reason,
            "demoted_producers": demoted_edges,
        }


def excuse_reviewer(self, role: str) -> dict[str, Any]:
    """Remove a reviewer from the review graph (HITL-gated).

    Called when a human decides to continue without a failed reviewer.
    Removes all edges from this reviewer, allowing is_fully_acked()
    to pass without their ACK.

    Raises ValueError if the role is not a reviewer in the graph.
    """
    with self._lock:
        if not self.graph.is_reviewer(role):
            raise ValueError(f"Cannot excuse '{role}': not a reviewer in the review graph")
        producers = self.graph.producers_for(role)
        for producer in producers:
            self.graph.remove_edge(role, producer)
        self._confirmed.discard(role)
        self._reviewer_phases.pop(role, None)
        return {"status": "excused", "role": role, "affected_producers": producers}


def excuse_producer(self, producer_role: str, reason: str = "") -> dict[str, Any]:
    """Remove a non-delivering producer from the review graph (HITL-gated).

    Called when a human decides to continue without a failed producer.
    Removes all edges targeting this producer, allowing reviewers to
    confirm without reviewing this producer's (non-existent) deliverable.

    Must be called only after HITL approval — this permanently removes
    the producer from the consensus protocol for this phase.

    Raises ValueError if the role is not a producer in the graph.
    """
    with self._lock:
        if not self.graph.is_producer(producer_role):
            raise ValueError(f"Cannot excuse '{producer_role}': not a producer in the review graph")

        reviewers = self.graph.reviewers_for(producer_role)
        for reviewer in reviewers:
            self.graph.remove_edge(reviewer, producer_role)

        self._producer_phases.pop(producer_role, None)
        self._confirmed.discard(producer_role)

        self._proposal_timestamps.pop(producer_role, None)
        self._flip_flop_counts.pop(producer_role, None)
        self._proposal_artifacts.pop(producer_role, None)
        self._proposal_commit_shas.pop(producer_role, None)
        self._proposal_commit_sha_history.pop(producer_role, None)

        # Remaining producers may now be fully_acked if the excused
        # producer held a dual role (producer + reviewer).  The next
        # call to is_fully_acked / try_confirm will pick this up
        # automatically via the updated graph edges.

        emit_event(
            EventType.CONSENSUS_FAILURE,
            self.pipeline_id,
            data={
                "type": "producer_excused",
                "role": producer_role,
                "reason": reason,
                "affected_reviewers": reviewers,
            },
        )

        logger.info(
            "Excused non-delivering producer",
            producer=producer_role,
            reason=reason,
            affected_reviewers=reviewers,
            pipeline_id=self.pipeline_id,
        )

        return {
            "status": "excused",
            "role": producer_role,
            "reason": reason,
            "affected_reviewers": reviewers,
        }


def reopen_producer(self, agent_role: str, reason: str = "") -> dict[str, Any]:
    """Reopen a CONFIRMED producer's consensus participation (#3124).

    A task reassignment (impasse delegation or a direct operator
    mutation of ``phases.*.tasks.*.role``) can land on a producer
    that has already CONFIRMED. Confirmation is otherwise a one-way
    lock — ``check_propose_guard`` rejects proposals from the
    CONFIRMED phase — so the contract would carry an incomplete
    task row that no live agent is permitted to deliver, while the
    #3114 completeness gate (correctly) refuses to close the slice
    over it. Reopening flips the producer's FSM back to WORKING and
    clears its confirmed status so the next ``next-action`` poll
    derives ``propose``; from there the existing re-propose
    machinery applies (#1411 stale-confirm discard on propose,
    ``_un_confirm_stale_reviewers`` forcing re-review of the new
    version).

    Dual-role agents keep their reviewer-side FSM untouched — their
    prior reviews of peers remain valid; only their own deliverable
    is reopened. ``handle_confirmed`` already requires both FSMs to
    be CONFIRMED before re-adding the role to the confirmed set.

    Idempotent: returns ``{"status": "noop"}`` when the producer is
    not confirmed (nothing to reopen), so message replay can apply
    it blindly.

    Raises ValueError if the role is not a producer in the graph.
    """
    with self._lock:
        if not self.graph.is_producer(agent_role):
            raise ValueError(f"Cannot reopen '{agent_role}': not a producer in the review graph")

        was_confirmed = (
            agent_role in self._confirmed
            or self._producer_phases.get(agent_role) == ConsensusPhase.CONFIRMED
        )
        if not was_confirmed:
            return {"status": "noop", "role": agent_role}

        self._producer_phases[agent_role] = ConsensusPhase.WORKING
        self._confirmed.discard(agent_role)

        emit_event(
            EventType.CONSENSUS_PRODUCER_REOPENED,
            self.pipeline_id,
            data={"role": agent_role, "reason": reason},
        )

        logger.info(
            "Reopened confirmed producer",
            producer=agent_role,
            reason=reason,
            pipeline_id=self.pipeline_id,
        )

        self._run_invariant_checks("reopen_producer")

        return {"status": "reopened", "role": agent_role, "reason": reason}


def is_timeout_handled(self) -> bool:
    """Check whether the BRC tracker has already handled the timeout."""
    with self._lock:
        return self._timeout_handled


def handle_timeout(self) -> dict[str, Any]:
    """Handle consensus timeout. Evaluate by role criticality.

    Idempotent: returns a cached result if already called.
    """
    with self._lock:
        if self._timeout_handled:
            return {"action": "already_handled", "reason": "Timeout previously processed"}

        blocking = self.matrix.get_all_blocking_edges()
        if not blocking:
            self._timeout_handled = True
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
            self._timeout_handled = True
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
        self._timeout_handled = True
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
