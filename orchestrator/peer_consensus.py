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


from action_guards import (
    InvariantViolation,
    check_ack_guard,
    check_confirm_guard,
    check_nack_guard,
    check_propose_guard,
    check_re_propose_guard,
    check_withdraw_guard,
)
from action_guards import (
    validate_invariants as _validate_invariants,
)
from approval_matrix import ApprovalMatrix, ApprovalState
from attestation_schemas import (
    AttestationStrictness,
    ProposalPayload,
    ReviewPayload,
    validate_attestation,
)
from egg_orchestrator.types import ConsensusPhase
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
        auto_repropose_debounce_seconds: int = 60,
        max_auto_repropose: int = 5,
        enable_invariant_checks: bool = False,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.graph = graph
        self.matrix = ApprovalMatrix(graph)
        self.attestation_strictness = attestation_strictness
        self.cooldown_seconds = cooldown_seconds
        self.max_flip_flops = max_flip_flops
        self.max_revision_rounds = max_revision_rounds
        self.auto_repropose_debounce_seconds = auto_repropose_debounce_seconds
        self.max_auto_repropose = max_auto_repropose
        self.enable_invariant_checks = enable_invariant_checks

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
        # Track proposal commit SHAs per producer (#1473)
        self._proposal_commit_shas: dict[str, str] = {}
        # Whether handle_timeout() has already processed the timeout
        self._timeout_handled: bool = False
        # Auto re-propose safety: debounce timestamps and counters
        self._last_auto_repropose_timestamp: dict[str, datetime] = {}
        self._auto_repropose_counts: dict[str, int] = {}
        # Track when producers explicitly propose (via handle_propose, NOT via
        # auto-repropose).  Used by check_auto_repropose to suppress redundant
        # auto-reproposals when a push arrives shortly after an explicit proposal.
        self._last_explicit_propose_timestamp: dict[str, datetime] = {}
        # Highest proposal version for which a producer has already received a
        # "ready to confirm" nudge.  A new proposal bumps the version and
        # naturally re-arms the nudge — see _collect_newly_ready_producers.
        # In-memory only by design: an orchestrator restart re-nudges any
        # still-ready producer on the next ACK/PROPOSE, which is harmless
        # because handle_confirmed is idempotent under check_confirm_guard.
        self._nudged_versions: dict[str, int] = {}
        # Open-NACK barrier (#2142): the latest NACK timestamp that the
        # producer has been informed of via a re_propose rejection.  Reset on
        # successful re_propose (version advances; prior NACKs are historical).
        # Forces aggregation in multi-reviewer concurrent BRC: a producer
        # cannot advance the proposal version while NACKs against the current
        # version remain undelivered.
        self._open_nack_notified_at: dict[str, datetime] = {}

    @property
    def confirmed_roles(self) -> frozenset[str]:
        """Read-only view of roles that have completed the full confirmation flow."""
        with self._lock:
            return frozenset(self._confirmed)

    def get_current_proposal_snapshot(self, producer: str) -> dict[str, Any]:
        """Return a structured snapshot of a producer's current proposal.

        Used by signal handlers to inline the current proposal artifacts in
        stale-version rejection envelopes (#2142) so a reviewer whose
        verdict targeted a superseded version can immediately re-review the
        latest one without a separate fetch.
        """
        with self._lock:
            return {
                "producer": producer,
                "version": self.matrix.get_proposal_version(producer),
                "artifacts": list(self._proposal_artifacts.get(producer, [])),
                "commit_sha": self._proposal_commit_shas.get(producer, ""),
            }

    def _current_proposal_snapshots(self, producers: list[str]) -> dict[str, dict[str, Any]]:
        """Return current proposal snapshots keyed by producer role.

        Caller MUST hold ``self._lock``.  De-duplicates and skips falsy
        entries; missing producers map to an empty snapshot.
        """
        out: dict[str, dict[str, Any]] = {}
        for producer in producers:
            if not producer or producer in out:
                continue
            out[producer] = {
                "producer": producer,
                "version": self.matrix.get_proposal_version(producer),
                "artifacts": list(self._proposal_artifacts.get(producer, [])),
                "commit_sha": self._proposal_commit_shas.get(producer, ""),
            }
        return out

    def register_agent(self, role: str) -> None:
        """Register an agent for consensus tracking."""
        with self._lock:
            if self.graph.is_producer(role):
                self._producer_phases[role] = ConsensusPhase.WORKING
            if self.graph.is_reviewer(role):
                self._reviewer_phases[role] = ConsensusPhase.WORKING

    def release_nudge(self, role: str, version: int) -> None:
        """Roll back a nudge memo entry recorded by ``_collect_newly_ready_producers``.

        Call this when the caller failed to actually emit the STATUS message
        for ``(role, version)`` so the producer can be re-nudged the next
        time consensus state changes.  No-op if the memo has already been
        advanced past ``version`` by a later proposal.
        """
        with self._lock:
            if self._nudged_versions.get(role) == version:
                del self._nudged_versions[role]

    def _rearm_nudge_on_guard_rejection(self, role: str) -> None:
        """Drop the nudge memo so a re-nudge fires when the guard finally clears.

        Caller MUST hold ``self._lock``.

        A producer that already received a "ready to confirm" STATUS will
        have an entry in ``_nudged_versions`` at its current proposal
        version.  If that producer then calls ``confirm`` and the guard
        rejects (``pending_acks``: peer hasn't proposed, reviewer hasn't
        ACKed; or a reviewer-side guard like ``stale_acks`` /
        ``unresolved_nacks`` on a dual-role agent), the laggard's later
        state change re-runs ``_collect_newly_ready_producers`` — but the
        memo would suppress the re-emit at the same version, leaving the
        producer asleep in ``message_wait_loop`` indefinitely (#2100).
        Dropping the memo here re-arms the nudge; the next sweep only
        emits if ``check_confirm_guard`` actually passes (which evaluates
        both producer- and reviewer-side guards for dual-role agents), so
        this can't fire prematurely.

        No-op for reviewer-only roles — they hold no producer-side memo.
        """
        if self.graph.is_producer(role):
            self._nudged_versions.pop(role, None)

    def _collect_newly_ready_producers(self) -> list[dict[str, Any]]:
        """Return producers that newly became ready-to-confirm.

        Caller MUST hold ``self._lock``.  Iterates all producers, checks
        ``check_confirm_guard``, and returns any producer whose current
        proposal version is higher than the last version we nudged for.  The
        memo (``_nudged_versions``) is updated in place — each emitted nudge
        is recorded so we don't spam.  Re-proposing bumps the version and
        re-arms the nudge naturally.
        """
        newly_ready: list[dict[str, Any]] = []
        for role in self.graph.all_roles():
            if not self.graph.is_producer(role):
                continue
            guard = check_confirm_guard(role, self.graph, self.matrix, self._confirmed)
            if not guard.allowed:
                continue
            version = self.matrix.get_proposal_version(role)
            if version <= self._nudged_versions.get(role, 0):
                continue
            self._nudged_versions[role] = version
            newly_ready.append({"role": role, "version": version})
        return newly_ready

    def _run_invariant_checks(self, action: str) -> None:
        """Run invariant checks after a state mutation (if enabled).

        Called internally after every state-mutating operation when
        ``enable_invariant_checks`` is True. Logs warnings for any
        violations but does not raise — this is a defensive check for
        detecting bugs early, not an enforcement mechanism.
        """
        if not self.enable_invariant_checks:
            return
        violations = _validate_invariants(
            self.graph,
            self.matrix,
            self._producer_phases,
            self._reviewer_phases,
            self._confirmed,
            proposal_commit_shas=self._proposal_commit_shas,
        )
        for v in violations:
            logger.warning(
                "Invariant violation after %s",
                action,
                invariant=v.invariant,
                agent=v.agent,
                description=v.description,
                details=v.details,
                pipeline_id=self.pipeline_id,
            )

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
        self._confirmed.discard(agent_role)  # Clear stale confirmed status (#1411)
        self._proposal_timestamps[agent_role] = datetime.now(UTC)
        self._proposal_artifacts[agent_role] = list(proposal.artifacts)
        self._proposal_commit_shas[agent_role] = proposal.commit_sha

        # Detect reviewers who confirmed on a stale version and need re-review.
        # This prevents deadlocks where a confirmed reviewer never sees a new
        # proposal version after the producer withdraws and re-proposes.
        stale_reviewers = self._un_confirm_stale_reviewers(agent_role, version)

        # Invalidate pre-proposal ACKs (version 0).  When a reviewer ACKs a
        # producer that hasn't proposed yet, the ACK is recorded at version 0.
        # After the producer proposes (version >= 1), these version-0 ACKs can
        # never satisfy is_fully_acked() and would create a permanent deadlock.
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
            if normalized_condition:
                event_data["pre_merge_condition"] = normalized_condition

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
                "newly_ready": self._collect_newly_ready_producers(),
            }
            if normalized_condition:
                result["pre_merge_condition"] = normalized_condition
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
                    self._flip_flop_counts[agent_role] = (
                        self._flip_flop_counts.get(agent_role, 0) + 1
                    )
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
            should_trigger, reason = self.check_auto_repropose(
                agent_role, commit_sha, changed_files
            )
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
            self._auto_repropose_counts[agent_role] = (
                self._auto_repropose_counts.get(agent_role, 0) + 1
            )

            logger.info(
                "Auto re-proposed on producer push",
                producer=agent_role,
                commit_sha=commit_sha,
                version=result.get("version"),
                invalidated_reviewers=invalidated,
                pipeline_id=self.pipeline_id,
            )

            return result

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
                    remaining_reviewers = [
                        r for r in self.graph.reviewers_for(producer) if r != role
                    ]
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
                raise ValueError(
                    f"Cannot excuse '{producer_role}': not a producer in the review graph"
                )

            reviewers = self.graph.reviewers_for(producer_role)
            for reviewer in reviewers:
                self.graph.remove_edge(reviewer, producer_role)

            self._producer_phases.pop(producer_role, None)
            self._confirmed.discard(producer_role)

            self._proposal_timestamps.pop(producer_role, None)
            self._flip_flop_counts.pop(producer_role, None)
            self._proposal_artifacts.pop(producer_role, None)
            self._proposal_commit_shas.pop(producer_role, None)

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

    def get_proposal_commit_sha(self, role: str) -> str:
        """Return the commit SHA from a producer's last proposal (#1473)."""
        return self._proposal_commit_shas.get(role, "")

    def get_pre_merge_conditions(self) -> list[dict[str, Any]]:
        """Return active conditional-ACK obligations across all producers.

        Delegates to the matrix, which scopes results to current-version ACKs
        so stale conditions from superseded proposals are dropped (#1998).

        Returns a list of dicts: ``{reviewer, producer, condition, version}``.
        Callers (PR body builder, HITL gate) surface these to humans so
        merge-time obligations aren't silently dropped.
        """
        with self._lock:
            return self.matrix.get_pre_merge_conditions()

    def get_latest_proposal_timestamp(self) -> datetime | None:
        """Return the timestamp of the most recent CONSENSUS_PROPOSE, or None."""
        with self._lock:
            if not self._proposal_timestamps:
                return None
            return max(self._proposal_timestamps.values())

    def get_latest_progress_timestamp(self) -> datetime | None:
        """Return the most recent BRC-bus activity timestamp, or None.

        Aggregates the latest CONSENSUS_PROPOSE timestamp with the latest
        ACK/NACK timestamp from the approval matrix. Used by the BRC
        progress gate (#2243) to defer the auto consensus-failure HITL
        decision while the bus is still moving.
        """
        with self._lock:
            latest = self.get_latest_proposal_timestamp()
            entry_ts = self.matrix.get_latest_entry_timestamp()
            if entry_ts is not None and (latest is None or entry_ts > latest):
                latest = entry_ts
            return latest

    def evaluate(self) -> dict[str, Any]:
        """Evaluate current consensus state.

        Returns dict compatible with the old ConsensusEvaluator.evaluate() format
        plus additional BRC-specific data.

        ``is_complete`` is only True when all agents have confirmed AND there
        are no unresolved NACK edges in the approval matrix.  This prevents
        the phase from completing when reviewers have NACKed but producers
        haven't yet iterated.
        """
        with self._lock:
            all_roles = self.graph.all_roles()
            all_confirmed = all_roles.issubset(self._confirmed)

            # Check the approval matrix for unresolved NACKs — even if all
            # agents are in the confirmed set, blocking edges mean producers
            # still need to iterate.
            blocking_edges = self.matrix.get_all_blocking_edges()
            has_unresolved_nacks = any(e.state.value == "nacked" for e in blocking_edges)

            is_complete = all_confirmed and not has_unresolved_nacks

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

            # Collect NACK details for callers that need to act on them
            unresolved_nack_details = []
            if has_unresolved_nacks:
                for entry in blocking_edges:
                    if entry.state.value == "nacked":
                        unresolved_nack_details.append(
                            {
                                "reviewer": entry.reviewer_role,
                                "producer": entry.producer_role,
                                "reason": entry.reason,
                                "version": entry.version,
                            }
                        )

            return {
                "is_complete": is_complete,
                "blocking_agents": blocking_agents,
                "has_objections": False,  # BRC doesn't use objections
                "has_unresolved_nacks": has_unresolved_nacks,
                "unresolved_nacks": unresolved_nack_details,
                "pre_merge_conditions": self.matrix.get_pre_merge_conditions(),
                "agents": agents,
                "approval_matrix": self.matrix.to_dict(),
                "review_graph": self.graph.to_dict(),
                "protocol": "brc",
            }

    def get_state(self) -> dict[str, Any]:
        """Alias for evaluate() -- compatibility with ConsensusEvaluator."""
        return self.evaluate()

    def is_producer_pending_confirm(self, role: str) -> bool:
        """True if ``role`` is a producer that has not yet reached CONFIRMED.

        Used by the ``/messages/wait`` endpoint to reject incoherent
        ``wait_loop --for CONSENSUS_CONFIRMED`` calls from producers
        whose own confirm hasn't succeeded — their confirm is part of
        what generates global consensus, so the wait would deadlock
        (#2064). Reviewer-only roles return False (they may legitimately
        wait on other agents' confirms).
        """
        with self._lock:
            if not self.graph.is_producer(role):
                return False
            return self._producer_phases.get(role) != ConsensusPhase.CONFIRMED

    def are_all_producers_working(self, reviewer: str) -> bool:
        """Check if all upstream producers for a reviewer are still in WORKING phase.

        Used by the health monitor to determine if a reviewer-only agent is
        legitimately idle waiting for upstream proposals (BRC-idle suppression).

        Args:
            reviewer: The agent role to check.

        Returns:
            True if the reviewer has upstream producers and all are in WORKING phase.
            False if the agent has no upstream producers or any has advanced past WORKING.
        """
        producers = self.graph.producers_for(reviewer)
        if not producers:
            return False
        with self._lock:
            return all(self._producer_phases.get(p) == ConsensusPhase.WORKING for p in producers)

    def get_earliest_proposal_time(self, reviewer: str) -> float | None:
        """Return the earliest proposal timestamp among the reviewer's upstream producers.

        Used by the health monitor to determine if a reviewer-only agent is
        within the post-propose grace period — the window after a producer
        proposes during which the reviewer should not be flagged for inactivity.

        Args:
            reviewer: The reviewer role to check.

        Returns:
            Earliest proposal epoch float among upstream producers, or None if
            no upstream producer has proposed.
        """
        producers = self.graph.producers_for(reviewer)
        if not producers:
            return None
        with self._lock:
            timestamps = [
                self._proposal_timestamps[p].timestamp()
                for p in producers
                if p in self._proposal_timestamps
            ]
        if not timestamps:
            return None
        return min(timestamps)

    def get_fully_acked_producers(self) -> dict[str, float]:
        """Return producers that are ready to confirm but have not yet done so.

        A producer is "ready" only when ``check_confirm_guard`` would actually
        allow ``mcp__brc__confirm`` to succeed. That includes the per-role
        fully-ACKed check AND the global zero-proposal guard (#1648): if any
        producer in the review graph has ``proposal_version == 0``, no agent
        can confirm yet, and a ``brc_confirmation_timeout`` alert against the
        patient producer would be a false positive (#2187).

        Returns:
            Dict mapping producer role to proposal timestamp (epoch float)
            for producers in ``PROPOSED`` phase whose confirm guard passes.
        """
        result: dict[str, float] = {}
        with self._lock:
            # Iterating ``_producer_phases`` (rather than
            # ``self.graph.all_roles()`` filtered by ``is_producer``, as
            # ``_collect_newly_ready_producers`` does) is safe today because
            # every graph producer is registered before consensus begins —
            # both iterations yield the same set. If registration ever
            # becomes optional, prefer the graph-based iteration so the
            # detector and the post-handler nudge stay in lockstep.
            for role, phase in self._producer_phases.items():
                if phase != ConsensusPhase.PROPOSED:
                    continue
                guard = check_confirm_guard(role, self.graph, self.matrix, self._confirmed)
                if not guard.allowed:
                    continue
                ts = self._proposal_timestamps.get(role)
                if ts is not None:
                    result[role] = ts.timestamp()
        return result

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
            self._timeout_handled = False
            self._last_auto_repropose_timestamp.clear()
            self._auto_repropose_counts.clear()
            self._last_explicit_propose_timestamp.clear()

    def _un_confirm_stale_reviewers(
        self,
        producer_role: str,
        new_version: int,
    ) -> list[str]:
        """Un-confirm reviewers who haven't ACKed the latest proposal version.

        When a producer withdraws and re-proposes, reviewers who already
        confirmed on a prior version must be notified to re-review.
        Without this, they remain in a terminal CONFIRMED state and the
        producer can never reach is_fully_acked(), causing a deadlock.

        Returns list of reviewer roles that were un-confirmed.
        """
        stale_reviewers: list[str] = []
        for reviewer in self.graph.reviewers_for(producer_role):
            entry = self.matrix.get_entry(reviewer, producer_role)
            if entry is None:
                continue

            # Check if reviewer is confirmed but their review is stale.
            # Since record_proposal just incremented to new_version, no
            # reviewer can have ACKed it yet. Any state other than
            # ACKED-on-new_version is stale (covers ACKED-on-old, PENDING,
            # and NACKED).
            reviewer_confirmed = reviewer in self._confirmed
            ack_is_stale = not (entry.state == ApprovalState.ACKED and entry.version == new_version)

            if reviewer_confirmed and ack_is_stale:
                # Un-confirm the reviewer so they re-enter the review loop
                self._confirmed.discard(reviewer)
                self._reviewer_phases[reviewer] = ConsensusPhase.REVIEWING
                # Reset the stale ACK to PENDING so is_fully_acked works correctly
                if entry.state == ApprovalState.ACKED:
                    self.matrix.invalidate_ack(reviewer, producer_role)
                stale_reviewers.append(reviewer)
                logger.info(
                    "Un-confirmed stale reviewer for re-review",
                    reviewer=reviewer,
                    producer=producer_role,
                    stale_version=entry.version,
                    new_version=new_version,
                    pipeline_id=self.pipeline_id,
                )

        return stale_reviewers

    def _invalidate_pre_proposal_acks(
        self,
        producer_role: str,
        new_version: int,
    ) -> list[str]:
        """Invalidate ACKs recorded before the producer's first proposal.

        When a reviewer ACKs a producer that hasn't proposed yet, the ACK is
        recorded at version 0.  After the producer proposes (version >= 1),
        these version-0 ACKs can never match and would block consensus
        permanently.

        Only processes non-confirmed reviewers — confirmed stale reviewers
        are already handled by ``_un_confirm_stale_reviewers``.

        ``new_version`` is included for log context only — the invalidation
        logic always targets version-0 ACKs regardless of the new version.

        Returns list of reviewer roles whose ACKs were invalidated.
        """
        invalidated: list[str] = []
        for reviewer in self.graph.reviewers_for(producer_role):
            if reviewer in self._confirmed:
                continue  # Already handled by _un_confirm_stale_reviewers
            entry = self.matrix.get_entry(reviewer, producer_role)
            if entry is not None and entry.state == ApprovalState.ACKED and entry.version == 0:
                self.matrix.invalidate_ack(reviewer, producer_role)
                invalidated.append(reviewer)
                logger.info(
                    "Invalidated pre-proposal ACK (version 0)",
                    reviewer=reviewer,
                    producer=producer_role,
                    new_version=new_version,
                    pipeline_id=self.pipeline_id,
                )
        return invalidated

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


def _tracker_key(pipeline_id: str, slice_id: str | None = None) -> str:
    """Compose the tracker registry key.

    Slice-aware (#2137 TASK-4-3, refine-phase decision-14 hybrid):

    * When ``slice_id`` is supplied, the key is the nested form
      ``{pipeline_id}/{slice_id}``. Each slice's BRC consensus has
      its own tracker, completely isolated from siblings.
    * When ``slice_id`` is ``None``, the key is the bare pipeline_id
      — preserving the pre-slicing single-tracker semantics so
      cross-slice telemetry (HEARTBEAT, OVERSEER_ALERT) keeps
      flowing through the pipeline-scoped tracker.

    The function is idempotent on already-nested ids (callers that
    have constructed ``"issue-N/slice-M"`` themselves don't get a
    double prefix).
    """
    if slice_id is None:
        return pipeline_id
    if "/" in pipeline_id and pipeline_id.endswith(f"/{slice_id}"):
        return pipeline_id
    return f"{pipeline_id}/{slice_id}"


def get_peer_consensus_tracker(
    pipeline_id: str, slice_id: str | None = None
) -> PeerConsensusTracker | None:
    """Get the tracker for a pipeline (or per-slice tracker), if one exists."""
    return _trackers.get(_tracker_key(pipeline_id, slice_id))


def create_peer_consensus_tracker(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    slice_id: str | None = None,
    **kwargs: Any,
) -> PeerConsensusTracker:
    """Create and register a tracker for a pipeline (or per-slice scope).

    When ``slice_id`` is supplied the tracker's logical pipeline_id
    is the nested ``{pipeline_id}/{slice_id}`` so CONSENSUS_* messages
    naturally route to the per-slice tracker. The bare pipeline-level
    tracker (without slice_id) keeps existing single-tracker pipelines
    working unchanged.
    """
    key = _tracker_key(pipeline_id, slice_id)
    with _trackers_lock:
        tracker = PeerConsensusTracker(key, graph, **kwargs)
        _trackers[key] = tracker
    return tracker


def remove_peer_consensus_tracker(pipeline_id: str, slice_id: str | None = None) -> None:
    """Remove a tracker for a pipeline (or per-slice scope)."""
    key = _tracker_key(pipeline_id, slice_id)
    with _trackers_lock:
        tracker = _trackers.pop(key, None)
        if tracker:
            tracker.clear()


def reconstruct_tracker_from_messages(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    message_store: Any = None,
) -> PeerConsensusTracker | None:
    """Reconstruct a consensus tracker by replaying messages from the message store.

    Called when the in-memory tracker is lost (e.g. after orchestrator restart)
    but consensus messages are preserved in Redis. Replays PROPOSE, ACK, NACK,
    WITHDRAW, and CONFIRMED messages in timestamp order to rebuild state.

    Args:
        pipeline_id: Pipeline ID to reconstruct.
        graph: ReviewGraph for the pipeline's current phase.
        message_store: Optional message store override (for testing).

    Returns:
        Reconstructed tracker registered in the global tracker dict,
        or None if no consensus messages were found.
    """
    if message_store is None:
        try:
            from message_store import get_message_store

            message_store = get_message_store()
        except ImportError:
            logger.warning("Cannot reconstruct tracker: message_store unavailable")
            return None

    # Fetch all messages for this pipeline (generous limit for reconstruction)
    messages = message_store.get_messages(pipeline_id, limit=10000)

    # Filter to consensus-related message types
    consensus_types = {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_WITHDRAW",
        "CONSENSUS_CONFIRMED",
    }
    consensus_msgs = [m for m in messages if m.message_type in consensus_types]

    if not consensus_msgs:
        return None

    # Create tracker with relaxed attestation and no cooldown for replaying
    # historical messages. RELAXED mode is kept for the tracker's remaining
    # lifetime because: (1) reconstructed trackers are near end-of-life —
    # consensus is typically already reached or close to it, and (2) any new
    # proposals post-reconstruction will still be validated by the review
    # graph structure (required reviewers, quorum), just not by attestation
    # signature checks.
    tracker = PeerConsensusTracker(
        pipeline_id,
        graph,
        attestation_strictness=AttestationStrictness.RELAXED,
        cooldown_seconds=0,
    )

    # Discover and register agents from message from_role and to_role fields
    discovered_roles: set[str] = set()
    for msg in consensus_msgs:
        discovered_roles.add(msg.from_role)
        if msg.to_role and msg.to_role != "all":
            discovered_roles.add(msg.to_role)

    # Only register roles that exist in the review graph
    all_graph_roles = graph.all_roles()
    for role in discovered_roles:
        if role in all_graph_roles:
            tracker.register_agent(role)

    # Sort by timestamp for deterministic replay.  Use message sequence
    # number as tiebreaker when timestamps match, ensuring stable replay
    # order for auto-re-propose deduplication.
    consensus_msgs.sort(key=lambda m: (m.timestamp, getattr(m, "id", "")))

    # Track auto-re-propose timestamps per producer for debounce during
    # replay — prevents redundant version inflation from rapid auto-re-
    # propose messages within the debounce window.
    _replay_auto_repropose_ts: dict[str, datetime] = {}
    _auto_repropose_debounce = 60  # seconds — match default debounce

    # Replay messages
    for msg in consensus_msgs:
        try:
            if msg.message_type == "CONSENSUS_PROPOSE":
                metadata = msg.metadata or {}
                payload = metadata.get("payload", {})
                if not payload:
                    # Minimal payload for reconstruction
                    payload = {"summary": msg.body or "reconstructed", "artifacts": []}
                # Ensure commit_sha is present for reconstruction (#1473).
                # Historical messages may pre-date this requirement.
                # Use an explicit sentinel so callers of
                # get_proposal_commit_sha() can distinguish it from a real SHA.
                if not payload.get("commit_sha"):
                    payload["commit_sha"] = "RECONSTRUCTED_NO_SHA"

                # Debounce auto-re-propose messages during replay:
                # If this is an auto-triggered re-propose (trigger=auto_push),
                # check the debounce window to avoid inflating proposal versions
                # from rapid pushes during a single development session.
                is_auto = metadata.get("auto_re_propose") or metadata.get("trigger") == "auto_push"
                if is_auto:
                    producer = msg.from_role
                    last_auto_ts = _replay_auto_repropose_ts.get(producer)
                    if last_auto_ts is not None:
                        elapsed = (msg.timestamp - last_auto_ts).total_seconds()
                        if elapsed < _auto_repropose_debounce:
                            logger.debug(
                                "Skipping debounced auto-re-propose during reconstruction",
                                producer=producer,
                                elapsed_seconds=elapsed,
                                pipeline_id=pipeline_id,
                            )
                            continue
                    _replay_auto_repropose_ts[producer] = msg.timestamp

                tracker.handle_propose(msg.from_role, payload)

            elif msg.message_type == "CONSENSUS_ACK":
                producer_role = msg.to_role
                payload = msg.metadata.get("payload", {})
                if not payload:
                    payload = {"reason": msg.body or "reconstructed"}
                # Ensure artifact_references is non-empty (ReviewPayload validates this)
                if not payload.get("artifact_references"):
                    payload["artifact_references"] = ["reconstructed"]
                tracker.handle_ack(msg.from_role, producer_role, payload)

            elif msg.message_type == "CONSENSUS_NACK":
                producer_role = msg.to_role
                payload = msg.metadata.get("payload", {})
                if not payload:
                    payload = {"reason": msg.metadata.get("reason", msg.body or "reconstructed")}
                if not payload.get("artifact_references"):
                    payload["artifact_references"] = ["reconstructed"]
                tracker.handle_nack(msg.from_role, producer_role, payload)

            elif msg.message_type == "CONSENSUS_WITHDRAW":
                reason = msg.body or ""
                tracker.handle_withdraw(msg.from_role, reason)

            elif msg.message_type == "CONSENSUS_CONFIRMED":
                tracker.handle_confirmed(msg.from_role)

        except Exception as e:
            # Best-effort reconstruction: log and skip messages that fail
            logger.warning(
                "Skipping message during tracker reconstruction",
                pipeline_id=pipeline_id,
                message_id=msg.id,
                message_type=msg.message_type,
                from_role=msg.from_role,
                error=str(e),
            )

    # Register the reconstructed tracker globally, but avoid overwriting
    # a tracker that was created by a concurrent reconstruction or live messages.
    with _trackers_lock:
        if pipeline_id not in _trackers:
            _trackers[pipeline_id] = tracker
            was_registered = True
        else:
            tracker = _trackers[pipeline_id]
            was_registered = False

    if was_registered:
        logger.info(
            "Reconstructed consensus tracker from messages",
            pipeline_id=pipeline_id,
            messages_replayed=len(consensus_msgs),
            confirmed_roles=sorted(tracker.confirmed_roles),
        )
    else:
        logger.info(
            "Reconstruction discarded: tracker already exists",
            pipeline_id=pipeline_id,
        )

    return tracker
