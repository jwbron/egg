"""PeerConsensusTracker confirm / re-propose / auto-repropose / push method bodies (#3312, slice-10).

Method bodies extracted verbatim from the pre-split ``orchestrator/peer_consensus.py``
and bound onto ``PeerConsensusTracker`` in the barrel
(``orchestrator/peer_consensus/__init__.py``). They take ``self`` explicitly and
are AST-identical to the originals. ``logger`` is imported from the package barrel
so it stays a single binding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from action_guards import check_confirm_guard, check_re_propose_guard
from approval_matrix import ApprovalState
from egg_orchestrator.types import ConsensusPhase
from events import EventType, emit_event
from peer_consensus import logger


def handle_confirmed(self, agent_role: str) -> dict[str, Any]:
    """Handle a CONSENSUS_CONFIRMED from an agent.

    Delegates precondition checking to the formal action guard
    (``check_confirm_guard``) which encapsulates all producer and
    reviewer confirmation guards:

    - Global: all producers must have proposed (proposal_version > 0).
    - Producer: must be fully ACKed.
    - Reviewer: must have reviewed all producers, ACK versions must match,
      no unresolved NACKs, no zero-proposal producers (defense-in-depth).
    """
    with self._lock:
        guard = check_confirm_guard(
            agent_role,
            self.graph,
            self.matrix,
            self._confirmed,
        )

        if not guard.allowed:
            guard_type = guard.details.get("guard", "unknown")

            # Re-arm the "ready to confirm" nudge for this producer so
            # the laggard's later proposal/ACK wakes it up via the
            # normal _collect_newly_ready_producers sweep (#2100).
            # Runs before guard_type dispatch so it covers every
            # rejection path (pending_acks branches and reviewer-side
            # ValueErrors); guarded by ``is_producer`` so reviewer-only
            # roles are a no-op and the next sweep's check_confirm_guard
            # still blocks emission while any side of the guard fails.
            self._rearm_nudge_on_guard_rejection(agent_role)

            # Global zero-proposal guard (#1648): any producer has
            # never proposed — blocks all agents from confirming.
            if guard_type == "global_zero_proposal":
                logger.warning(
                    "handle_confirmed rejected: global zero-proposal producers",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    producers=guard.details.get("producers"),
                )
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                    "zero_proposal_producers": guard.details.get("producers"),
                }

            # Producer guard failures return pending_acks status
            if guard_type == "producer_not_fully_acked":
                logger.warning(
                    "handle_confirmed rejected: producer not fully ACKed",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    pending_reviewers=guard.details.get("pending_reviewers"),
                    blocking_states=guard.details.get("blocking_states"),
                )
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                }

            # Reviewer guard failures
            if guard_type == "must_have_reviewed":
                raise ValueError(guard.reason)

            if guard_type == "zero_proposal_producers":
                logger.warning(
                    "handle_confirmed rejected: zero-proposal producers",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    producers=guard.details.get("producers"),
                )
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                    "zero_proposal_producers": guard.details.get("producers"),
                }

            if guard_type == "stale_acks":
                logger.warning(
                    "handle_confirmed rejected: reviewer ACK version mismatch",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    stale_acks=guard.details.get("stale_acks"),
                )
                stale_acks = guard.details.get("stale_acks") or []
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                    "stale_acks": stale_acks,
                    # Inline the current proposal of each producer the
                    # reviewer must re-ACK so the reviewer can re-review
                    # without a separate fetch (#2142, symmetry with the
                    # ack/nack stale_version envelope).
                    "current_proposals": self._current_proposal_snapshots(
                        [s.get("producer", "") for s in stale_acks]
                    ),
                }

            if guard_type == "unresolved_nacks":
                logger.warning(
                    "handle_confirmed rejected: reviewer has unresolved NACKs",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    unresolved_nacks=guard.details.get("unresolved_nacks"),
                )
                unresolved_nacks = guard.details.get("unresolved_nacks") or []
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                    "unresolved_nacks": unresolved_nacks,
                    "current_proposals": self._current_proposal_snapshots(
                        [n.get("producer", "") for n in unresolved_nacks]
                    ),
                }

            if guard_type == "stale_nacks":
                logger.warning(
                    "handle_confirmed rejected: reviewer has stale NACKs",
                    pipeline_id=self.pipeline_id,
                    role=agent_role,
                    stale_nacks=guard.details.get("stale_nacks"),
                )
                stale_nacks = guard.details.get("stale_nacks") or []
                return {
                    "status": "pending_acks",
                    "message": guard.reason,
                    "stale_nacks": stale_nacks,
                    "current_proposals": self._current_proposal_snapshots(
                        [s.get("producer", "") for s in stale_nacks]
                    ),
                }

            # Fallback for any unhandled guard type
            logger.warning(
                "handle_confirmed rejected by guard",
                pipeline_id=self.pipeline_id,
                role=agent_role,
                guard=guard_type,
                reason=guard.reason,
            )
            return {
                "status": "pending_acks",
                "message": guard.reason,
            }

        # Guards passed — apply state transitions
        if self.graph.is_producer(agent_role):
            self._producer_phases[agent_role] = ConsensusPhase.CONFIRMED

        if self.graph.is_reviewer(agent_role):
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

        self._run_invariant_checks("confirmed")

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
    """Handle a re-proposal after NACK. Triggers scoped re-evaluation.

    Holds the lock for the entire operation (invalidation + re-proposal)
    to prevent race conditions between releasing and re-acquiring.

    **Open-NACK barrier (#2142):** if NACKs landed against the current
    proposal version that the producer has not yet been notified of
    (i.e. landed since the last re_propose attempt), the orchestrator
    rejects with status ``open_nacks_blocked`` and surfaces every
    current-version NACK inline. This forces multi-reviewer aggregation:
    producers must address all known NACKs, not just the first one they
    saw via wait-loop. After one rejection the producer has been
    informed; a retry with the same NACK set is allowed (the producer's
    re_propose is the act of addressing them).
    """
    with self._lock:
        barrier = self._open_nacks_barrier_response(agent_role)
        if barrier is not None:
            return barrier

        # First, do scoped re-evaluation
        if changed_artifacts:
            invalidated = self.matrix.invalidate_overlapping_acks(agent_role, changed_artifacts)
        else:
            # Conservative: invalidate all ACKs
            invalidated = []
            for reviewer in self.graph.reviewers_for(agent_role):
                if self.matrix.invalidate_ack(reviewer, agent_role):
                    invalidated.append(reviewer)

        # The NACKing reviewer(s) always need to re-review
        # (their state is already NACKED in the matrix)

        # Handle as a normal proposal while still holding the lock.
        # Skip the ACK guard — re-propose is always legitimate.
        result = self._handle_propose_inner(agent_role, payload, _skip_ack_guard=True)
        result["invalidated_reviewers"] = invalidated

        # Version advanced — prior-version NACKs are historical now.
        self._open_nack_notified_at.pop(agent_role, None)

        self._run_invariant_checks("re_propose")

        return result


def _open_nacks_barrier_response(self, producer: str) -> dict[str, Any] | None:
    """Return a structured rejection if the producer has unnotified NACKs.

    Caller MUST hold ``self._lock``.

    The barrier rejects re_propose calls when NACKs against the current
    proposal version exist that the producer has not yet been informed of
    via a prior rejection.  After one rejection the ``notified_at``
    watermark advances; a retry with no new NACKs proceeds normally.
    Returns ``None`` if there is no active barrier.

    **Scoping**: the barrier only fires when **two or more distinct
    reviewers** have NACKed the current version.  Single-reviewer NACKs
    cannot race the multi-reviewer aggregation hazard the barrier exists
    to prevent — the producer received that one NACK via wait-loop and
    is acting on it.  Forcing an extra round-trip there would add cost
    with no protection benefit (#2142).
    """
    current_version = self.matrix.get_proposal_version(producer)
    if current_version == 0:
        return None

    relevant: list[tuple[str, Any]] = []
    for reviewer, entry in self.matrix.get_nack_entries_for(producer):
        if entry.version == current_version and entry.timestamp is not None:
            relevant.append((reviewer, entry))
    # Only the multi-reviewer aggregation race is in scope (#2142).
    # ``get_nack_entries_for`` returns one entry per reviewer, so
    # ``len(relevant)`` == number of distinct NACKing reviewers.
    if len(relevant) < 2:
        return None

    max_ts = max(entry.timestamp for _, entry in relevant)
    last_notified = self._open_nack_notified_at.get(producer)
    if last_notified is not None and max_ts <= last_notified:
        # Producer has already seen this NACK set — let re_propose proceed.
        return None

    self._open_nack_notified_at[producer] = max_ts

    nacks_payload = [
        {
            "reviewer": reviewer,
            "version": entry.version,
            "reason": entry.reason,
            "artifact_refs": list(entry.nack_artifact_refs),
            "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
        }
        for reviewer, entry in relevant
    ]
    nacking_reviewers = [reviewer for reviewer, _ in relevant]

    emit_event(
        EventType.CONSENSUS_NACK_RECEIVED,
        self.pipeline_id,
        data={
            "barrier": "open_nacks_blocked",
            "producer": producer,
            "version": current_version,
            "nacking_reviewers": nacking_reviewers,
        },
    )

    return {
        "status": "open_nacks_blocked",
        "producer": producer,
        "current_version": current_version,
        "nacking_reviewers": nacking_reviewers,
        "nacks": nacks_payload,
        "message": (
            f"Re-propose blocked: {len(nacks_payload)} unresolved "
            f"NACK(s) on v{current_version} from {nacking_reviewers}. "
            f"The full NACK content is included inline; address every "
            f"finding from every NACKing reviewer in your next re-propose."
        ),
    }


def check_auto_repropose(
    self,
    producer_role: str,
    new_commit_sha: str,
    changed_files: list[str] | None = None,
) -> tuple[bool, str]:
    """Check whether auto re-propose should trigger for a producer push.

    Safety mechanisms:
    - Explicit proposal cover: skip if producer explicitly proposed within
      the debounce window (the push is already covered by the proposal)
    - Debounce: skip if within auto_repropose_debounce_seconds of last auto re-propose
    - Max counter: skip if auto_repropose_counts >= max_auto_repropose
    - Overlap: skip if changed_files don't overlap with any existing ACK artifacts

    Args:
        producer_role: The producer that pushed.
        new_commit_sha: The new commit SHA.
        changed_files: Optional list of changed files for overlap check.

    Returns:
        Tuple of (should_trigger, reason) where reason explains the decision.
    """
    # Check if producer explicitly proposed recently — the push is already
    # covered by the explicit proposal and doesn't need an auto re-propose.
    # This prevents redundant re-reviews when push + propose happen together
    # (e.g. via "egg-orch consensus propose --push").
    explicit_ts = self._last_explicit_propose_timestamp.get(producer_role)
    if explicit_ts is not None:
        elapsed = (datetime.now(UTC) - explicit_ts).total_seconds()
        if elapsed < self.auto_repropose_debounce_seconds:
            return False, (
                f"Push covered by recent explicit proposal "
                f"({elapsed:.0f}s ago, within {self.auto_repropose_debounce_seconds}s window)"
            )

    # Check debounce window
    last_ts = self._last_auto_repropose_timestamp.get(producer_role)
    if last_ts is not None:
        elapsed = (datetime.now(UTC) - last_ts).total_seconds()
        if elapsed < self.auto_repropose_debounce_seconds:
            return False, (
                f"Debounce active: {self.auto_repropose_debounce_seconds - elapsed:.0f}s "
                f"remaining (last auto re-propose {elapsed:.0f}s ago)"
            )

    # Check max counter
    count = self._auto_repropose_counts.get(producer_role, 0)
    if count >= self.max_auto_repropose:
        return False, (
            f"Max auto re-propose limit reached ({count}/{self.max_auto_repropose}). "
            f"Producer must explicitly re-propose."
        )

    # Check commit SHA difference
    current_sha = self._proposal_commit_shas.get(producer_role, "")
    if current_sha and current_sha == new_commit_sha:
        return False, "Commit SHA unchanged — no new changes to re-propose"

    # Check overlap with proposed artifacts (if changed_files provided)
    if changed_files:
        proposed_artifacts = self._proposal_artifacts.get(producer_role, [])
        if proposed_artifacts:
            overlap = set(changed_files) & set(proposed_artifacts)
            if not overlap:
                # Also check if any reviewer has ACKed artifacts that overlap
                has_overlapping_acks = False
                for reviewer in self.graph.reviewers_for(producer_role):
                    entry = self.matrix.get_entry(reviewer, producer_role)
                    if entry and entry.state == ApprovalState.ACKED:
                        if set(entry.artifact_refs) & set(changed_files):
                            has_overlapping_acks = True
                            break
                if not has_overlapping_acks:
                    return False, (
                        "Changed files don't overlap with proposed artifacts "
                        "or any existing ACK artifacts"
                    )

    return True, "All safety checks passed"


def handle_producer_push(
    self,
    agent_role: str,
    commit_sha: str,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Handle a producer pushing new commits after proposing.

    When a producer pushes or commits new changes after having already
    proposed, this triggers an automatic re-proposal.  This invalidates
    existing ACKs and forces reviewers back to REVIEWING state so they
    re-review the updated work before confirming.

    This is the key mechanism for the "all changes must be reviewed"
    principle — without it, a producer could push new code after
    consensus and bypass review entirely.

    Args:
        agent_role: The producer role that pushed.
        commit_sha: The new commit SHA.
        changed_files: Optional list of changed files for scoped
            re-evaluation.

    Returns:
        Dict with re-proposal result, or no-op status if producer
        hasn't proposed yet.
    """
    with self._lock:
        guard = check_re_propose_guard(agent_role, self.graph)
        if not guard.allowed:
            raise ValueError(guard.reason)

        # Only auto re-propose if the producer has already proposed.
        # If they're still in WORKING phase, the push is just normal
        # development work — not a protocol event.
        current_phase = self._producer_phases.get(agent_role, ConsensusPhase.WORKING)
        if current_phase == ConsensusPhase.WORKING:
            return {
                "status": "no_op",
                "reason": (
                    f"Producer {agent_role} is still in WORKING phase. "
                    f"Push registered but no re-proposal needed."
                ),
            }

        # Check auto re-propose safety mechanisms
        should_trigger, reason = self.check_auto_repropose(agent_role, commit_sha, changed_files)
        if not should_trigger:
            logger.info(
                "Auto re-propose skipped by safety mechanism",
                producer=agent_role,
                commit_sha=commit_sha,
                reason=reason,
                pipeline_id=self.pipeline_id,
            )
            return {
                "status": "skipped",
                "reason": reason,
                "auto_re_propose": False,
            }

        # Build a minimal payload for the auto re-proposal.
        # Use changed_files if available, otherwise fall back to the
        # previous proposal's artifacts (ProposalPayload requires at
        # least one artifact).
        artifacts = changed_files or self._proposal_artifacts.get(agent_role, [])
        if not artifacts:
            artifacts = [commit_sha]  # Last resort: use the commit SHA itself
        payload = {
            "summary": (
                f"Auto re-proposal: new push by {agent_role} (commit {commit_sha[:8]}). "
                f"Prior proposal invalidated by new commits — re-review required."
            ),
            "artifacts": artifacts,
            "commit_sha": commit_sha,
        }

        # Use changed_files for scoped invalidation if available
        if changed_files:
            invalidated = self.matrix.invalidate_overlapping_acks(agent_role, changed_files)
        else:
            # Conservative: invalidate all ACKs
            invalidated = []
            for reviewer in self.graph.reviewers_for(agent_role):
                if self.matrix.invalidate_ack(reviewer, agent_role):
                    invalidated.append(reviewer)

        result = self._handle_propose_inner(agent_role, payload, _skip_ack_guard=True)
        result["invalidated_reviewers"] = invalidated
        result["auto_re_propose"] = True
        result["auto_trigger"] = "auto_push"

        # Version advanced — prior-version NACKs are historical now (#2142).
        # The barrier doesn't fire on auto-push (system-triggered, not an
        # explicit producer action), but the watermark must still pop so a
        # subsequent explicit propose isn't rejected against a stale entry
        # that no longer applies to the current version.
        self._open_nack_notified_at.pop(agent_role, None)

        # Update auto re-propose tracking state
        self._last_auto_repropose_timestamp[agent_role] = datetime.now(UTC)
        self._auto_repropose_counts[agent_role] = self._auto_repropose_counts.get(agent_role, 0) + 1

        logger.info(
            "Auto re-proposed on producer push",
            producer=agent_role,
            commit_sha=commit_sha,
            version=result.get("version"),
            invalidated_reviewers=invalidated,
            pipeline_id=self.pipeline_id,
        )

        return result
