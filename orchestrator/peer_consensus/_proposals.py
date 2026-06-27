"""PeerConsensusTracker propose / ack / nack / withdraw method bodies (#3312, slice-10).

Method bodies extracted verbatim from the pre-split ``orchestrator/peer_consensus.py``
and bound onto ``PeerConsensusTracker`` in the barrel
(``orchestrator/peer_consensus/__init__.py``). They take ``self`` explicitly and
are AST-identical to the originals. ``logger`` is imported from the package barrel
so it stays a single binding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from action_guards import (
    check_ack_guard,
    check_nack_guard,
    check_propose_guard,
    check_re_propose_guard,
    check_withdraw_guard,
)
from attestation_schemas import (
    AttestationStrictness,
    ProposalPayload,
    ReviewPayload,
    validate_attestation,
)
from egg_orchestrator.types import ConsensusPhase
from events import EventType, emit_event


def handle_propose(
    self,
    agent_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handle a CONSENSUS_PROPOSE from a producer.

    Validates attestation, transitions agent to PROPOSED, records
    in approval matrix.

    **Open-NACK barrier (#2142):** when the producer is past v0 (i.e.
    the call is effectively a re-propose without ``--changed-artifacts``)
    the barrier check fires here too — otherwise a producer could bypass
    ``handle_re_propose``'s aggregation enforcement by omitting the
    ``changed_artifacts`` field.  The barrier self-skips when there are
    fewer than 2 distinct NACKing reviewers.
    """
    with self._lock:
        barrier = self._open_nacks_barrier_response(agent_role)
        if barrier is not None:
            return barrier
        result = self._handle_propose_inner(agent_role, payload)
        # Record explicit proposal timestamp (not updated by auto-repropose)
        # so check_auto_repropose can suppress redundant re-reviews when a
        # push arrives shortly after an explicit proposal.
        self._last_explicit_propose_timestamp[agent_role] = datetime.now(UTC)
        # Version advanced — prior-version NACKs are historical now (#2142).
        self._open_nack_notified_at.pop(agent_role, None)
        return result


def _handle_propose_inner(
    self,
    agent_role: str,
    payload: dict[str, Any],
    *,
    _skip_ack_guard: bool = False,
) -> dict[str, Any]:
    """Inner propose logic. Caller MUST hold self._lock."""
    if not _skip_ack_guard:
        guard = check_propose_guard(agent_role, self.graph, self.matrix, self._producer_phases)
        if not guard.allowed:
            raise ValueError(guard.reason)
    else:
        # Even with skip_ack_guard, must still be a producer
        guard = check_re_propose_guard(agent_role, self.graph)
        if not guard.allowed:
            raise ValueError(guard.reason)

    # Validate payload
    proposal = ProposalPayload(**payload)
    no_changes = bool(proposal.no_changes_needed)

    # Validate role-specific attestation. A generic no-op propose (#3027)
    # means the producer did no work, so the strict per-role requirements
    # (commit_shas / tests_run / sections_updated …) don't apply — validate
    # in RELAXED mode. The no-op is justified by ``no_changes_reason``,
    # which ``ProposalPayload`` already enforces.
    if proposal.attestation:
        validate_attestation(
            agent_role,
            proposal.attestation,
            strictness=(
                AttestationStrictness.RELAXED if no_changes else self.attestation_strictness
            ),
            is_producer=True,
        )

    # Record proposal version (marking the no-op state, #3027)
    version = self.matrix.record_proposal(agent_role, no_changes=no_changes)

    # Transition to PROPOSED
    self._producer_phases[agent_role] = ConsensusPhase.PROPOSED
    self._confirmed.discard(agent_role)  # Clear stale confirmed status (#1411)
    self._proposal_timestamps[agent_role] = datetime.now(UTC)
    self._proposal_artifacts[agent_role] = list(proposal.artifacts)
    self._proposal_commit_shas[agent_role] = proposal.commit_sha
    # Pin this version's commit SHA in the accumulating history so a
    # later re-review notice can resolve any prior reviewer's
    # last-verdicted version back to the commit they actually saw
    # (#2887). A no-op propose carries no commit_sha; the guard skips
    # it so the history never holds an empty anchor.
    if proposal.commit_sha:
        self._proposal_commit_sha_history.setdefault(agent_role, {})[version] = proposal.commit_sha

    if no_changes:
        # A no-op proposal needs no review (is_fully_acked is True and it
        # has no blocking edges, #3027). Reviewers skip it, so there is no
        # stale-reviewer / pre-proposal-ACK bookkeeping to do — touching
        # reviewer confirm state here would only churn it for nothing.
        stale_reviewers: list[str] = []
    else:
        # Detect reviewers who confirmed on a stale version and need
        # re-review. This prevents deadlocks where a confirmed reviewer
        # never sees a new proposal version after the producer withdraws
        # and re-proposes.
        stale_reviewers = self._un_confirm_stale_reviewers(agent_role, version)

        # Invalidate pre-proposal ACKs (version 0).  When a reviewer ACKs a
        # producer that hasn't proposed yet, the ACK is recorded at version
        # 0. After the producer proposes (version >= 1), these version-0
        # ACKs can never satisfy is_fully_acked() and would create a
        # permanent deadlock.
        pre_proposal_stale = self._invalidate_pre_proposal_acks(agent_role, version)
        stale_reviewers.extend(pre_proposal_stale)

    emit_event(
        EventType.CONSENSUS_PROPOSE_RECEIVED,
        self.pipeline_id,
        data={
            "role": agent_role,
            "version": version,
            "artifacts": proposal.artifacts,
            "commit_sha": proposal.commit_sha,
            "stale_reviewers": stale_reviewers,
        },
    )

    self._run_invariant_checks("propose")

    return {
        "version": version,
        "status": "proposed",
        "commit_sha": proposal.commit_sha,
        "reviewers": self.graph.reviewers_for(agent_role),
        "stale_reviewers": stale_reviewers,
        "newly_ready": self._collect_newly_ready_producers(),
    }


def handle_ack(
    self,
    reviewer_role: str,
    producer_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handle a CONSENSUS_ACK from a reviewer."""
    with self._lock:
        # Take the reviewer's version claim straight from the payload
        # — no fallback to the current version, otherwise the guard
        # silently passes whenever the caller doesn't set the field
        # (#2142).  When ``ack_version`` is ``None`` (in-process tests
        # that pre-date version plumbing) the guard skips its
        # version-match check; the production CLI / MCP path always
        # populates it so the strict check fires there.
        ack_version_claim = payload.get("ack_version")
        guard = check_ack_guard(
            reviewer_role,
            producer_role,
            self.graph,
            matrix=self.matrix,
            ack_version=ack_version_claim,
        )
        if not guard.allowed:
            raise ValueError(guard.reason)
        # The recorded ACK is always tied to the proposal version it
        # acknowledged — the guard above guaranteed the claim matches
        # current, or there was no claim (test path) and we record at
        # the current version.
        ack_version = (
            ack_version_claim
            if ack_version_claim is not None
            else self.matrix.get_proposal_version(producer_role)
        )

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

        proposal_commit_sha = self._proposal_commit_shas.get(producer_role, "")
        self.matrix.record_ack(
            reviewer_role,
            producer_role,
            ack_version,
            artifact_refs=review.artifact_references,
            commit_sha=proposal_commit_sha,
            pre_merge_condition=review.pre_merge_condition,
            pre_merge_condition_resolved_in_diff=(review.pre_merge_condition_resolved_in_diff),
        )

        # Transition reviewer to REVIEWING
        self._reviewer_phases[reviewer_role] = ConsensusPhase.REVIEWING

        # Check if producer is now fully ACKed
        fully_acked = self.matrix.is_fully_acked(producer_role)

        event_data: dict[str, Any] = {
            "reviewer": reviewer_role,
            "producer": producer_role,
            "version": ack_version,
            "fully_acked": fully_acked,
        }
        # Surface conditional-ACK obligations on the event stream so
        # downstream consumers (PR builder, HITL gate, audit log) can
        # react without having to inspect the matrix directly (#1998).
        # Use the normalized value (record_ack strips whitespace) so the
        # event stream is consistent with persisted matrix state.
        normalized_condition = (review.pre_merge_condition or "").strip()
        normalized_resolution = (review.pre_merge_condition_resolved_in_diff or "").strip()
        if normalized_condition:
            event_data["pre_merge_condition"] = normalized_condition
            if normalized_resolution:
                event_data["pre_merge_condition_resolved_in_diff"] = normalized_resolution

        emit_event(
            EventType.CONSENSUS_ACK_RECEIVED,
            self.pipeline_id,
            data=event_data,
        )

        self._run_invariant_checks("ack")

        result: dict[str, Any] = {
            "status": "acked",
            "fully_acked": fully_acked,
            "version": ack_version,
            # Surface the producer's commit SHA at review time so
            # the agent-side BRC memory writer (#2908 slice-1) can
            # store ``last_reviewed_commit_sha`` per producer.
            # Slice-3 reads that field to scope an adversarial
            # re-review git delta on the next re-proposal — the
            # SHA is mechanically derivable from the signal
            # payload here so a regression in propagation is
            # catchable at the boundary.
            "commit_sha": proposal_commit_sha,
            "newly_ready": self._collect_newly_ready_producers(),
        }
        if normalized_condition:
            result["pre_merge_condition"] = normalized_condition
            if normalized_resolution:
                result["pre_merge_condition_resolved_in_diff"] = normalized_resolution
        return result


def handle_nack(
    self,
    reviewer_role: str,
    producer_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handle a CONSENSUS_NACK from a reviewer."""
    with self._lock:
        # Take the reviewer's version claim straight from the payload
        # — no fallback to the current version, otherwise the guard
        # silently passes whenever the caller doesn't set the field
        # (#2142).  When ``nack_version`` is ``None`` (in-process
        # tests that pre-date version plumbing) the guard skips its
        # version-match check; the production CLI / MCP path always
        # populates it so the strict check fires there.
        nack_version = payload.get("nack_version")
        guard = check_nack_guard(
            reviewer_role,
            producer_role,
            self.graph,
            matrix=self.matrix,
            nack_version=nack_version,
        )
        if not guard.allowed:
            raise ValueError(guard.reason)

        # Validate review payload
        review = ReviewPayload(verdict="NACK", **payload)

        # Check for context-change NACK before recording (uses previous refs)
        context_change = self.matrix.is_context_change_nack(
            reviewer_role, producer_role, review.artifact_references
        )

        version = self.matrix.get_proposal_version(producer_role)
        # Capture the producer's current commit_sha before any further
        # state mutations so the agent-side BRC memory writer
        # (#2908 slice-1) can record ``last_reviewed_commit_sha``
        # per producer. Mirrors the symmetric capture in
        # ``handle_ack`` so both verdict paths share the same
        # signal payload contract.
        proposal_commit_sha = self._proposal_commit_shas.get(producer_role, "")
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

        # Check revision count — context-change NACKs increment the count
        # for observability but do not trigger escalation, since the
        # reviewer is flagging a different issue rather than oscillating
        # on the same one.  However, a hard cap at 3× max_revision_rounds
        # ensures escalation even for alternating-file NACK patterns.
        rev_count = self.matrix.revision_count(reviewer_role, producer_role)
        hard_cap = self.max_revision_rounds * 3
        needs_escalation = (
            rev_count >= self.max_revision_rounds and not context_change
        ) or rev_count >= hard_cap

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
                "context_change": context_change,
            },
        )

        self._run_invariant_checks("nack")

        return {
            "status": "nacked",
            "reason": review.reason,
            "revision_count": rev_count,
            "needs_escalation": needs_escalation,
            "context_change": context_change,
            # Surface the producer's commit SHA at review time so
            # the agent-side BRC memory writer (#2908 slice-1) can
            # store ``last_reviewed_commit_sha`` per producer.
            # Symmetric with the same field in ``handle_ack``.
            "commit_sha": proposal_commit_sha,
        }


def handle_withdraw(
    self,
    agent_role: str,
    reason: str,
) -> dict[str, Any]:
    """Handle a CONSENSUS_WITHDRAW from a producer."""
    with self._lock:
        guard = check_withdraw_guard(
            agent_role,
            self.graph,
            self._proposal_timestamps,
            self._flip_flop_counts,
            self.cooldown_seconds,
            self.max_flip_flops,
            reason,
        )

        if not guard.allowed:
            guard_type = guard.details.get("guard", "unknown")
            if guard_type == "flip_flop_lockout":
                # Increment the counter since the guard only peeked
                self._flip_flop_counts[agent_role] = self._flip_flop_counts.get(agent_role, 0) + 1
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
                    "reason": guard.reason,
                    "needs_escalation": True,
                }
            raise ValueError(guard.reason)

        # Increment flip-flop counter
        self._flip_flop_counts[agent_role] = self._flip_flop_counts.get(agent_role, 0) + 1

        # Transition back to WORKING
        self._producer_phases[agent_role] = ConsensusPhase.WORKING
        self._confirmed.discard(agent_role)  # Clear stale confirmed status (#1411)

        emit_event(
            EventType.CONSENSUS_WITHDRAW_RECEIVED,
            self.pipeline_id,
            data={"role": agent_role, "reason": reason},
        )

        self._run_invariant_checks("withdraw")

        return {"status": "withdrawn", "reason": reason}


def handle_resolve_obligation(
    self,
    resolver_role: str,
    reviewer_role: str,
    producer_role: str,
    commit_sha: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Mark a conditional-ACK obligation as satisfied in-cycle (#2338).

    Called by the agent that landed the conditioning commit (typically the
    tester picking up work the coder is gateway-blocked from). The matrix
    keeps the original ``pre_merge_condition`` text for audit but
    ``get_pre_merge_conditions`` filters out resolved entries, so the
    PR-body builder and HITL gate stop surfacing the obligation.

    ``resolver_role`` is the caller's role; it is recorded for audit and
    included on the emitted ``CONSENSUS_OBLIGATION_RESOLVED`` event so
    downstream consumers can attribute the resolution. The matrix raises
    ``ValueError`` when the edge is missing, not in ACKED state, or has no
    active obligation.
    """
    with self._lock:
        entry = self.matrix.mark_obligation_resolved(
            reviewer_role,
            producer_role,
            resolved_by=resolver_role,
            commit_sha=commit_sha,
            note=note,
        )

        event_data: dict[str, Any] = {
            "reviewer": reviewer_role,
            "producer": producer_role,
            "resolver": resolver_role,
            "version": entry.version,
            "condition": entry.pre_merge_condition,
        }
        if commit_sha:
            event_data["commit_sha"] = commit_sha
        if note:
            event_data["note"] = note

        emit_event(
            EventType.CONSENSUS_OBLIGATION_RESOLVED,
            self.pipeline_id,
            data=event_data,
        )

        return {
            "status": "resolved",
            "reviewer": reviewer_role,
            "producer": producer_role,
            "resolver": resolver_role,
            "version": entry.version,
            "condition": entry.pre_merge_condition,
            "remaining_pre_merge_conditions": self.matrix.get_pre_merge_conditions(),
        }
