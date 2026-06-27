"""PeerConsensusTracker state / registration / nudge / lifecycle method bodies (#3312, slice-10).

Method bodies extracted verbatim from the pre-split ``orchestrator/peer_consensus.py``
and bound onto ``PeerConsensusTracker`` in the barrel
(``orchestrator/peer_consensus/__init__.py``). They take ``self`` explicitly and
are AST-identical to the originals. ``logger`` is imported from the package barrel
so it stays a single binding.
"""

from __future__ import annotations

from typing import Any

from action_guards import check_confirm_guard
from action_guards import validate_invariants as _validate_invariants
from approval_matrix import ApprovalState
from egg_orchestrator.types import ConsensusPhase
from events import EventType, emit_event
from peer_consensus import logger


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
        self._proposal_commit_shas.clear()
        self._proposal_commit_sha_history.clear()
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
