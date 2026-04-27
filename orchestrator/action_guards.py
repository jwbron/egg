"""Formal action guards for the BRC consensus protocol state machine.

Each protocol action (propose, re_propose, ack, nack, confirm, withdraw) has
a corresponding guard that defines its preconditions.  Guards return a
``GuardResult`` indicating whether the action is allowed and, if not, why.

The guards are designed to be the single source of truth for "when is this
action valid?" — the ``PeerConsensusTracker`` delegates to them before mutating
state.

State transition diagram
========================

Producer state machine::

    WORKING ──propose──▶ PROPOSED ──confirm──▶ CONFIRMED
       ▲                    │
       │                    │ (NACK received)
       └────────────────────┘
       │                    │
       │                    ▼ (push/commit triggers auto re-propose)
       │                 PROPOSED (new version, invalidates existing ACKs)
       │
       └── withdraw ◀── PROPOSED

Reviewer state machine::

    WORKING ──ack/nack──▶ REVIEWING ──confirm──▶ CONFIRMED
                             │   ▲
                             │   │ (producer re-proposes → un-confirm)
                             └───┘

Invariants
==========

The following invariants must hold at all times:

1. No agent in CONFIRMED with an unresolved NACK where producer hasn't
   re-proposed since the NACK.
2. No agent in CONFIRMED with a stale ACK (ACK version != current proposal
   version for any assigned producer).
3. No reviewer in CONFIRMED if any assigned producer has changes that
   haven't been reviewed (produced since the reviewer's last ACK).
4. No agent in CONFIRMED if any producer has never proposed
   (proposal version == 0).  This is a global check (#1648).
5. ``is_fully_acked`` must be consistent with the actual approval matrix
   state — every critical reviewer must have ACKED at the current proposal
   version.
6. ``ack_commit_sha`` consistency — when a reviewer's ACK is at the current
   proposal version, the recorded ``ack_commit_sha`` must match the
   producer's proposal commit SHA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from approval_matrix import ApprovalState

if TYPE_CHECKING:
    from approval_matrix import ApprovalMatrix
    from egg_orchestrator.types import ConsensusPhase
    from review_graph import ReviewGraph


@dataclass(frozen=True)
class GuardResult:
    """Result of evaluating an action guard.

    Attributes:
        allowed: Whether the action is permitted.
        reason: Human-readable explanation (always set when ``allowed`` is False).
        details: Machine-readable details for programmatic consumers.
    """

    allowed: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Propose guards
# ---------------------------------------------------------------------------


def check_propose_guard(
    agent_role: str,
    graph: ReviewGraph,
    matrix: ApprovalMatrix,
    producer_phases: dict[str, ConsensusPhase],
) -> GuardResult:
    """Check whether a producer is allowed to propose.

    Preconditions:
    - Agent must be a producer in the review graph.
    - Agent must be in WORKING state (not already PROPOSED or CONFIRMED).
    - Agent must not be fully ACKed while still in PROPOSED state
      (issue #1185).  If fully ACKed, the agent should confirm instead.
    """
    from egg_orchestrator.types import ConsensusPhase

    if not graph.is_producer(agent_role):
        return GuardResult(
            allowed=False,
            reason=f"{agent_role} is not a producer in this review graph",
            details={"guard": "not_producer"},
        )

    # Working-state guard: producers must be in WORKING to propose.
    # Re-proposals after NACK go through handle_re_propose / check_re_propose_guard.
    current_phase = producer_phases.get(agent_role)
    if current_phase is not None and current_phase != ConsensusPhase.WORKING:
        # Reject when already fully ACKed and in PROPOSED state.
        if matrix.is_fully_acked(agent_role) and current_phase == ConsensusPhase.PROPOSED:
            version = matrix.get_proposal_version(agent_role)
            return GuardResult(
                allowed=False,
                reason=(
                    f"Producer {agent_role} is already fully ACKed "
                    f"(v{version}). "
                    f"Call `egg-orch consensus confirmed` instead of re-proposing. "
                    f"Re-proposing when fully ACKed is not allowed — confirm to "
                    f"complete the BRC protocol."
                ),
                details={"guard": "fully_acked_rejection", "version": version},
            )

        return GuardResult(
            allowed=False,
            reason=(
                f"Producer {agent_role} cannot propose: currently in "
                f"{current_phase.value} state. Proposals are only allowed "
                f"from WORKING state. Use re-propose to update an existing "
                f"proposal."
            ),
            details={
                "guard": "not_in_working_state",
                "current_phase": current_phase.value,
            },
        )

    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# Re-propose guards
# ---------------------------------------------------------------------------


def check_re_propose_guard(
    agent_role: str,
    graph: ReviewGraph,
) -> GuardResult:
    """Check whether a producer is allowed to re-propose.

    Re-propose is always legitimate after a NACK or when new artifacts are
    produced.  The only precondition is that the agent is a producer.
    """
    if not graph.is_producer(agent_role):
        return GuardResult(
            allowed=False,
            reason=f"{agent_role} is not a producer in this review graph",
            details={"guard": "not_producer"},
        )
    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# ACK guards
# ---------------------------------------------------------------------------


def check_ack_guard(
    reviewer_role: str,
    producer_role: str,
    graph: ReviewGraph,
    matrix: ApprovalMatrix | None = None,
    ack_version: int | None = None,
) -> GuardResult:
    """Check whether a reviewer is allowed to ACK a producer.

    Preconditions:
    - Agent must be a reviewer in the review graph.
    - A review edge must exist from reviewer to producer.
    - If ``ack_version`` is provided, it must match the producer's current
      proposal version (version-match guard).
    """
    if not graph.is_reviewer(reviewer_role):
        return GuardResult(
            allowed=False,
            reason=f"{reviewer_role} is not a reviewer in this review graph",
            details={"guard": "not_reviewer"},
        )

    if not graph.get_edge(reviewer_role, producer_role):
        return GuardResult(
            allowed=False,
            reason=f"No review edge: {reviewer_role} -> {producer_role}",
            details={"guard": "no_review_edge"},
        )

    # Version-match guard: ACK version must match the producer's current
    # proposal version to prevent stale ACKs.
    if matrix is not None and ack_version is not None:
        current_version = matrix.get_proposal_version(producer_role)
        if current_version > 0 and ack_version != current_version:
            return GuardResult(
                allowed=False,
                reason=(
                    f"ACK version mismatch: reviewer {reviewer_role} is ACKing "
                    f"v{ack_version} but producer {producer_role} is at "
                    f"v{current_version}. Re-review the latest proposal."
                ),
                details={
                    "guard": "version_mismatch",
                    "ack_version": ack_version,
                    "current_version": current_version,
                },
            )

    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# NACK guards
# ---------------------------------------------------------------------------


def check_nack_guard(
    reviewer_role: str,
    producer_role: str,
    graph: ReviewGraph,
    matrix: ApprovalMatrix | None = None,
    nack_version: int | None = None,
) -> GuardResult:
    """Check whether a reviewer is allowed to NACK a producer.

    Preconditions:
    - Agent must be a reviewer in the review graph.
    - A review edge must exist from reviewer to producer.
    - Producer must have proposed at least once (version > 0) — NACKing
      a producer that hasn't proposed is meaningless.
    - If ``nack_version`` is provided, it must match the producer's current
      proposal version.  Surfaces stale-version NACKs the same way ACKs do
      so the reviewer is forced to re-review the latest proposal (#2142).
    """
    if not graph.is_reviewer(reviewer_role):
        return GuardResult(
            allowed=False,
            reason=f"{reviewer_role} is not a reviewer in this review graph",
            details={"guard": "not_reviewer"},
        )

    if not graph.get_edge(reviewer_role, producer_role):
        return GuardResult(
            allowed=False,
            reason=f"No review edge: {reviewer_role} -> {producer_role}",
            details={"guard": "no_review_edge"},
        )

    # Zero-version NACK rejection: cannot NACK a producer that hasn't
    # proposed yet.
    if matrix is not None:
        current_version = matrix.get_proposal_version(producer_role)
        if current_version == 0:
            return GuardResult(
                allowed=False,
                reason=(
                    f"Cannot NACK producer {producer_role}: no proposal exists "
                    f"(version 0). Wait for the producer to propose before NACKing."
                ),
                details={
                    "guard": "zero_version_nack",
                    "producer": producer_role,
                },
            )

        # Version-match guard: NACK version must match the producer's
        # current proposal version (#2142).  Forces reviewers whose verdict
        # was racing a producer's re-propose to re-review the new version.
        if nack_version is not None and nack_version != current_version:
            return GuardResult(
                allowed=False,
                reason=(
                    f"NACK version mismatch: reviewer {reviewer_role} is NACKing "
                    f"v{nack_version} but producer {producer_role} is at "
                    f"v{current_version}. Re-review the latest proposal."
                ),
                details={
                    "guard": "version_mismatch",
                    "nack_version": nack_version,
                    "current_version": current_version,
                },
            )

    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# Confirm guards
# ---------------------------------------------------------------------------


def check_confirm_guard(
    agent_role: str,
    graph: ReviewGraph,
    matrix: ApprovalMatrix,
    confirmed: set[str],
) -> GuardResult:
    """Check whether an agent is allowed to confirm consensus.

    This is the most complex guard, with distinct checks for producers and
    reviewers.

    Producer preconditions:
    - Must be fully ACKed by all critical reviewers at the current proposal
      version.

    Reviewer preconditions:
    - Must have reviewed (ACK or NACK) all assigned producers.
    - ACK version must match the current proposal version for all ACKed
      producers (version-match guard).
    - Must not have unresolved NACKs (NACKed a producer who hasn't
      re-proposed since).
    - All assigned producers must have proposed at least once (zero-proposal
      guard, #1598).
    - Must have reviewed the latest version from each producer — cannot
      confirm if a producer has produced new changes since the reviewer's
      last review (unreviewed-changes guard).
    """

    is_producer = graph.is_producer(agent_role)
    is_reviewer = graph.is_reviewer(agent_role)

    # --- Phantom agent guard: reject agents not in the review graph ---
    if not is_producer and not is_reviewer:
        return GuardResult(
            allowed=False,
            reason=(
                f"Agent {agent_role} cannot confirm: not a participant in "
                f"the review graph (neither producer nor reviewer)."
            ),
            details={"guard": "phantom_agent", "agent_role": agent_role},
        )

    # --- Global zero-proposal guard (#1648): reject if ANY producer in the
    # review graph has never proposed (proposal_version == 0).  The existing
    # per-reviewer guard (Guard 2 below) only checks assigned producers,
    # which allows reviewers like reviewer_contract (who only reviews coder)
    # to confirm even when tester has never proposed.  This global guard
    # closes that gap by checking all producers regardless of review
    # assignments.  Applies to both producers and reviewers. ---
    all_producers = [r for r in graph.all_roles() if graph.is_producer(r)]
    global_zero_producers = [p for p in all_producers if matrix.get_proposal_version(p) == 0]
    if global_zero_producers:
        return GuardResult(
            allowed=False,
            reason=(
                f"Agent {agent_role} cannot confirm: producers "
                f"{global_zero_producers} have never proposed "
                f"(proposal_version == 0). All producers must propose "
                f"before any agent can confirm consensus."
            ),
            details={
                "guard": "global_zero_proposal",
                "producers": global_zero_producers,
            },
        )

    # --- Producer confirmation guard ---
    if is_producer:
        if not matrix.is_fully_acked(agent_role):
            blocking = matrix.get_blocking_edges(agent_role)
            pending_reviewers = [e.reviewer_role for e in blocking]
            return GuardResult(
                allowed=False,
                reason=(
                    f"Producer {agent_role} cannot confirm: not fully ACKed. "
                    f"Pending reviewers: {pending_reviewers}"
                ),
                details={
                    "guard": "producer_not_fully_acked",
                    "pending_reviewers": pending_reviewers,
                    "blocking_states": [
                        {
                            "reviewer": e.reviewer_role,
                            "state": e.state.value,
                            "version": e.version,
                        }
                        for e in blocking
                    ],
                },
            )

    # --- Reviewer confirmation guards ---
    if is_reviewer:
        producers = graph.producers_for(agent_role)

        # Guard 1: Must have reviewed all producers
        for producer in producers:
            if not matrix.has_reviewed(agent_role, producer):
                return GuardResult(
                    allowed=False,
                    reason=f"Reviewer {agent_role} cannot confirm: hasn't reviewed {producer}",
                    details={"guard": "must_have_reviewed", "producer": producer},
                )

        # Guard 2: Zero-proposal guard (#1598) — reject if any assigned
        # producer has never proposed (version == 0).  A reviewer can NACK a
        # non-delivering producer then confirm, which allows consensus to
        # complete without the primary deliverable.
        # NOTE: This guard is unreachable for zero-proposal cases since the
        # global guard above (Guard #1648) fires first and is strictly
        # stronger.  Retained as defense-in-depth in case the global guard
        # is ever refactored or removed.
        zero_proposal_producers: list[str] = []
        for producer in producers:
            if matrix.get_proposal_version(producer) == 0:
                zero_proposal_producers.append(producer)
        if zero_proposal_producers:
            return GuardResult(
                allowed=False,
                reason=(
                    f"Reviewer {agent_role} cannot confirm: producers "
                    f"{zero_proposal_producers} have never proposed. "
                    f"Wait for them to propose before confirming."
                ),
                details={
                    "guard": "zero_proposal_producers",
                    "producers": zero_proposal_producers,
                },
            )

        # Guard 3: Version-match guard — reject when any ACK is stale
        stale_acks: list[dict[str, Any]] = []
        for producer in producers:
            entry = matrix.get_entry(agent_role, producer)
            current_version = matrix.get_proposal_version(producer)
            if (
                entry is not None
                and entry.state == ApprovalState.ACKED
                and current_version > 0
                and entry.version != current_version
            ):
                stale_acks.append(
                    {
                        "producer": producer,
                        "ack_version": entry.version,
                        "current_version": current_version,
                    }
                )
        if stale_acks:
            stale_producers = [s["producer"] for s in stale_acks]
            return GuardResult(
                allowed=False,
                reason=(
                    f"Reviewer {agent_role} cannot confirm: ACK version mismatch. "
                    f"Re-ACK the following producers at their current proposal "
                    f"version: {stale_producers}"
                ),
                details={
                    "guard": "stale_acks",
                    "stale_acks": stale_acks,
                },
            )

        # Guard 4a: Unresolved-NACK guard — reject when the reviewer has
        # NACKed a producer that hasn't re-proposed since the NACK.
        # Only matches when entry.version == current_version (producer
        # hasn't re-proposed since the NACK).
        unresolved_nacks: list[dict[str, Any]] = []
        for producer in producers:
            entry = matrix.get_entry(agent_role, producer)
            current_version = matrix.get_proposal_version(producer)
            if (
                entry is not None
                and entry.state == ApprovalState.NACKED
                and current_version > 0
                and entry.version == current_version
            ):
                unresolved_nacks.append(
                    {
                        "producer": producer,
                        "nack_version": entry.version,
                        "current_version": current_version,
                    }
                )
        if unresolved_nacks:
            nacked_producers = [n["producer"] for n in unresolved_nacks]
            return GuardResult(
                allowed=False,
                reason=(
                    f"Reviewer {agent_role} cannot confirm: unresolved NACKs. "
                    f"Wait for these producers to re-propose before confirming: "
                    f"{nacked_producers}"
                ),
                details={
                    "guard": "unresolved_nacks",
                    "unresolved_nacks": unresolved_nacks,
                },
            )

        # Guard 4b: Stale-NACK guard — reject when the reviewer NACKed at
        # an old version but the producer has since re-proposed.  The
        # reviewer must re-review the new proposal before confirming.
        stale_nacks: list[dict[str, Any]] = []
        for producer in producers:
            entry = matrix.get_entry(agent_role, producer)
            current_version = matrix.get_proposal_version(producer)
            if (
                entry is not None
                and entry.state == ApprovalState.NACKED
                and current_version > 0
                and entry.version < current_version
            ):
                stale_nacks.append(
                    {
                        "producer": producer,
                        "nack_version": entry.version,
                        "current_version": current_version,
                    }
                )
        if stale_nacks:
            stale_nack_producers = [s["producer"] for s in stale_nacks]
            return GuardResult(
                allowed=False,
                reason=(
                    f"Reviewer {agent_role} cannot confirm: NACKed producers have "
                    f"re-proposed since your NACK. Re-review their latest proposal "
                    f"before confirming: {stale_nack_producers}"
                ),
                details={
                    "guard": "stale_nacks",
                    "stale_nacks": stale_nacks,
                },
            )

    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# Withdraw guards
# ---------------------------------------------------------------------------


def check_withdraw_guard(
    agent_role: str,
    graph: ReviewGraph,
    proposal_timestamps: dict[str, Any],
    flip_flop_counts: dict[str, int],
    cooldown_seconds: int,
    max_flip_flops: int,
    reason: str,
) -> GuardResult:
    """Check whether a producer is allowed to withdraw.

    Preconditions:
    - Agent must be a producer.
    - Reason must be non-empty.
    - Must have waited at least ``cooldown_seconds`` since proposing.
    - Must not have exceeded ``max_flip_flops`` proposal/withdraw cycles.
    """
    from datetime import UTC, datetime

    if not graph.is_producer(agent_role):
        return GuardResult(
            allowed=False,
            reason=f"{agent_role} is not a producer",
            details={"guard": "not_producer"},
        )

    if not reason:
        return GuardResult(
            allowed=False,
            reason="Withdrawal requires a reason citing specific new information",
            details={"guard": "missing_reason"},
        )

    # Cooldown check
    last_proposal = proposal_timestamps.get(agent_role)
    if last_proposal:
        elapsed = (datetime.now(UTC) - last_proposal).total_seconds()
        if elapsed < cooldown_seconds:
            return GuardResult(
                allowed=False,
                reason=(
                    f"Cooldown active: {cooldown_seconds - elapsed:.0f}s remaining. "
                    f"Cannot withdraw within {cooldown_seconds}s of proposing."
                ),
                details={
                    "guard": "cooldown",
                    "remaining_seconds": cooldown_seconds - elapsed,
                },
            )

    # Flip-flop check (peek — don't increment yet, tracker does that)
    current_count = flip_flop_counts.get(agent_role, 0) + 1
    if current_count >= max_flip_flops:
        return GuardResult(
            allowed=False,
            reason=f"Locked out after {current_count} flip-flops",
            details={
                "guard": "flip_flop_lockout",
                "flip_flops": current_count,
            },
        )

    return GuardResult(allowed=True)


# ---------------------------------------------------------------------------
# Invariant validation
# ---------------------------------------------------------------------------


@dataclass
class InvariantViolation:
    """A single invariant violation."""

    invariant: str
    agent: str
    description: str
    details: dict[str, Any] = field(default_factory=dict)


def validate_invariants(
    graph: ReviewGraph,
    matrix: ApprovalMatrix,
    producer_phases: dict[str, ConsensusPhase],
    reviewer_phases: dict[str, ConsensusPhase],
    confirmed: set[str],
    proposal_commit_shas: dict[str, str] | None = None,
) -> list[InvariantViolation]:
    """Validate that all protocol invariants hold.

    Returns a list of violations (empty means all invariants hold).

    Invariants checked:
    1. No confirmed agent with unresolved NACK (NACK version == current
       proposal version and producer hasn't re-proposed).
    2. No confirmed reviewer with stale ACK (ACK version != current proposal
       version).
    3. No confirmed reviewer with unreviewed producer changes (producer has
       proposed at a version newer than the reviewer's ACK).
    4. No confirmed agent with zero-proposal producers (#1648 — applies
       globally, not just to reviewers).
    5. is_fully_acked consistency with approval matrix.
    6. ack_commit_sha consistency — when a reviewer's ACK is at the current
       proposal version, the recorded ack_commit_sha must match the
       producer's proposal commit SHA.
    """

    violations: list[InvariantViolation] = []

    # Invariant 4 (global): No confirmed agent when ANY producer has never
    # proposed (#1648).  Check all producers once, then flag every confirmed
    # agent if any are at version 0.
    all_producers = [r for r in graph.all_roles() if graph.is_producer(r)]
    global_zero_producers = [p for p in all_producers if matrix.get_proposal_version(p) == 0]
    if global_zero_producers:
        for agent in confirmed:
            for producer in global_zero_producers:
                violations.append(
                    InvariantViolation(
                        invariant="no_confirmed_with_zero_proposal_producer",
                        agent=agent,
                        description=(
                            f"Agent {agent} is CONFIRMED but producer "
                            f"{producer} has never proposed (version 0)"
                        ),
                        details={"producer": producer},
                    )
                )

    # Check each confirmed agent
    for agent in confirmed:
        # Invariant 1 & 2 & 3: Reviewer-side checks
        if graph.is_reviewer(agent):
            producers = graph.producers_for(agent)
            for producer in producers:
                entry = matrix.get_entry(agent, producer)
                current_version = matrix.get_proposal_version(producer)

                # Skip zero-proposal producers — already handled by
                # the global INV-4 check above.
                if current_version == 0:
                    continue

                if entry is None:
                    continue

                # Invariant 1: Unresolved NACK
                if entry.state == ApprovalState.NACKED:
                    violations.append(
                        InvariantViolation(
                            invariant="no_confirmed_with_unresolved_nack",
                            agent=agent,
                            description=(
                                f"Reviewer {agent} is CONFIRMED but has an "
                                f"unresolved NACK against producer {producer}"
                            ),
                            details={
                                "producer": producer,
                                "nack_version": entry.version,
                                "current_version": current_version,
                            },
                        )
                    )

                # Invariant 2: Stale ACK (version mismatch)
                # Invariant 3 (unreviewed changes) is a strict subset — only
                # report the more specific one to avoid double-reporting.
                if entry.state == ApprovalState.ACKED and entry.version != current_version:
                    if entry.version < current_version:
                        # Invariant 3: Unreviewed changes (ACK at older version)
                        violations.append(
                            InvariantViolation(
                                invariant="no_confirmed_with_unreviewed_changes",
                                agent=agent,
                                description=(
                                    f"Reviewer {agent} is CONFIRMED but producer "
                                    f"{producer} has new changes since last review "
                                    f"(reviewed v{entry.version}, current v{current_version})"
                                ),
                                details={
                                    "producer": producer,
                                    "reviewed_version": entry.version,
                                    "current_version": current_version,
                                },
                            )
                        )
                    else:
                        # Invariant 2: Stale ACK (future version — shouldn't
                        # happen but catch it)
                        violations.append(
                            InvariantViolation(
                                invariant="no_confirmed_with_stale_ack",
                                agent=agent,
                                description=(
                                    f"Reviewer {agent} is CONFIRMED with stale ACK "
                                    f"for producer {producer} "
                                    f"(ACK v{entry.version} != current v{current_version})"
                                ),
                                details={
                                    "producer": producer,
                                    "ack_version": entry.version,
                                    "current_version": current_version,
                                },
                            )
                        )

                # Invariant 6: ack_commit_sha consistency — when ACK is at
                # current version, the ack_commit_sha must match the
                # producer's proposal commit SHA.
                if (
                    entry.state == ApprovalState.ACKED
                    and entry.version == current_version
                    and proposal_commit_shas is not None
                ):
                    expected_sha = proposal_commit_shas.get(producer, "")
                    if (
                        expected_sha
                        and entry.ack_commit_sha
                        and entry.ack_commit_sha != expected_sha
                    ):
                        violations.append(
                            InvariantViolation(
                                invariant="ack_commit_sha_consistency",
                                agent=agent,
                                description=(
                                    f"Reviewer {agent} ACKed producer {producer} "
                                    f"at v{entry.version} but ack_commit_sha "
                                    f"({entry.ack_commit_sha[:8]}) does not match "
                                    f"proposal commit ({expected_sha[:8]})"
                                ),
                                details={
                                    "producer": producer,
                                    "ack_commit_sha": entry.ack_commit_sha,
                                    "proposal_commit_sha": expected_sha,
                                    "version": entry.version,
                                },
                            )
                        )

    # Invariant 5: is_fully_acked consistency
    for producer in [r for r in graph.all_roles() if graph.is_producer(r)]:
        is_acked = matrix.is_fully_acked(producer)
        current_version = matrix.get_proposal_version(producer)
        if current_version == 0:
            if is_acked:
                violations.append(
                    InvariantViolation(
                        invariant="fully_acked_consistency",
                        agent=producer,
                        description=(
                            f"Producer {producer} reports fully_acked=True "
                            f"but has never proposed (version 0)"
                        ),
                    )
                )
            continue

        # Verify is_fully_acked matches actual matrix state
        critical_reviewers = graph.critical_reviewers_for(producer)
        all_acked = True
        for reviewer in critical_reviewers:
            entry = matrix.get_entry(reviewer, producer)
            if (
                entry is None
                or entry.state != ApprovalState.ACKED
                or entry.version != current_version
            ):
                all_acked = False
                break

        if is_acked != all_acked:
            violations.append(
                InvariantViolation(
                    invariant="fully_acked_consistency",
                    agent=producer,
                    description=(
                        f"is_fully_acked({producer}) returns {is_acked} "
                        f"but matrix state says {all_acked}"
                    ),
                    details={
                        "is_fully_acked": is_acked,
                        "matrix_says": all_acked,
                        "current_version": current_version,
                    },
                )
            )

    return violations
