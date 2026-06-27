"""PeerConsensusTracker read-only query / evaluate method bodies (#3312, slice-10).

Method bodies extracted verbatim from the pre-split ``orchestrator/peer_consensus.py``
and bound onto ``PeerConsensusTracker`` in the barrel
(``orchestrator/peer_consensus/__init__.py``). They take ``self`` explicitly and
are AST-identical to the originals. ``logger`` is imported from the package barrel
so it stays a single binding.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from action_guards import check_confirm_guard
from egg_orchestrator.types import ConsensusPhase


def get_proposal_commit_sha(self, role: str) -> str:
    """Return the commit SHA from a producer's last proposal (#1473)."""
    return self._proposal_commit_shas.get(role, "")


def get_commit_sha_for_version(self, producer: str, version: int) -> str:
    """Return the commit SHA a producer's proposal was at for ``version``.

    Resolves a reviewer's last-verdicted version (``entry.version``)
    back to the commit they actually reviewed, so a re-review notice
    can emit an authoritative per-reviewer delta range
    ``<that_sha>..HEAD`` instead of the legacy hardcoded v1→v2 anchor
    (#2887). Returns "" when no commit was pinned for that version
    (version 0 / pre-proposal verdicts, or a producer this tracker
    has no proposal history for), letting callers fall back to the
    reviewer-self-tracked ``last_reviewed_commit`` range from
    REVIEWER-SYNC.md.
    """
    return self._proposal_commit_sha_history.get(producer, {}).get(version, "")


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
            result["producer_phase"] = self._producer_phases.get(role, ConsensusPhase.WORKING).value
        if self.graph.is_reviewer(role):
            result["reviewer_phase"] = self._reviewer_phases.get(role, ConsensusPhase.WORKING).value
        result["confirmed"] = str(role in self._confirmed)
        return result
