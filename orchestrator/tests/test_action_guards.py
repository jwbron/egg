"""Tests for action_guards module — formal BRC protocol action guards.

Covers:
- GuardResult dataclass (allowed/disallowed, frozen)
- check_propose_guard (producer check, fully-ACKed rejection, phase gating)
- check_re_propose_guard (producer check)
- check_ack_guard (reviewer check, edge existence)
- check_nack_guard (reviewer check, edge existence)
- check_confirm_guard (producer/reviewer paths, version-match, zero-proposal,
  unresolved-NACK, stale-ACK, dual-role agents)
- check_withdraw_guard (producer check, reason, cooldown, flip-flop lockout)
- validate_invariants (all 6 invariants, multiple violations)
"""

from datetime import UTC, datetime, timedelta

import pytest
from action_guards import (
    GuardResult,
    InvariantViolation,
    check_ack_guard,
    check_confirm_guard,
    check_nack_guard,
    check_propose_guard,
    check_re_propose_guard,
    check_withdraw_guard,
    validate_invariants,
)
from approval_matrix import ApprovalMatrix
from egg_orchestrator.types import ConsensusPhase
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph():
    """Standard implement-phase graph for testing."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def matrix(graph):
    return ApprovalMatrix(graph)


# ---------------------------------------------------------------------------
# GuardResult dataclass
# ---------------------------------------------------------------------------


class TestGuardResult:
    """Tests for the GuardResult dataclass."""

    def test_allowed_with_defaults(self):
        result = GuardResult(allowed=True)
        assert result.allowed is True
        assert result.reason == ""
        assert result.details == {}

    def test_not_allowed_with_reason_and_details(self):
        result = GuardResult(
            allowed=False,
            reason="Something went wrong",
            details={"guard": "test_guard", "extra": 42},
        )
        assert result.allowed is False
        assert result.reason == "Something went wrong"
        assert result.details == {"guard": "test_guard", "extra": 42}

    def test_frozen_immutable(self):
        result = GuardResult(allowed=True)
        with pytest.raises(AttributeError):
            result.allowed = False  # type: ignore[misc]
        with pytest.raises(AttributeError):
            result.reason = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# check_propose_guard
# ---------------------------------------------------------------------------


class TestCheckProposeGuard:
    """Tests for check_propose_guard."""

    def test_allowed_producer_not_fully_acked(self, graph, matrix):
        """Producer in graph, not fully ACKed -> allowed."""
        producer_phases = {"coder": ConsensusPhase.WORKING}
        result = check_propose_guard("coder", graph, matrix, producer_phases)
        assert result.allowed is True

    def test_not_a_producer(self, graph, matrix):
        """Non-producer role -> not allowed."""
        producer_phases: dict[str, ConsensusPhase] = {}
        result = check_propose_guard("reviewer_code", graph, matrix, producer_phases)
        assert result.allowed is False
        assert "not a producer" in result.reason
        assert result.details["guard"] == "not_producer"

    def test_fully_acked_rejection_in_proposed_phase(self, graph, matrix):
        """Producer fully ACKed and in PROPOSED phase -> not allowed."""
        # Set up fully-ACKed state
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        matrix.record_ack("reviewer_contract", "coder", version)
        matrix.record_ack("tester", "coder", version)
        assert matrix.is_fully_acked("coder")

        producer_phases = {"coder": ConsensusPhase.PROPOSED}
        result = check_propose_guard("coder", graph, matrix, producer_phases)
        assert result.allowed is False
        assert result.details["guard"] == "fully_acked_rejection"
        assert result.details["version"] == version

    def test_allowed_when_fully_acked_but_working_phase(self, graph, matrix):
        """Producer fully ACKed but phase is WORKING -> allowed (kicked back)."""
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        matrix.record_ack("reviewer_contract", "coder", version)
        matrix.record_ack("tester", "coder", version)
        assert matrix.is_fully_acked("coder")

        producer_phases = {"coder": ConsensusPhase.WORKING}
        result = check_propose_guard("coder", graph, matrix, producer_phases)
        assert result.allowed is True

    def test_rejected_when_in_proposed_phase(self, graph, matrix):
        """Producer in PROPOSED phase -> rejected (must use re-propose)."""
        matrix.record_proposal("coder")
        # Only one reviewer ACKed, not all
        matrix.record_ack("reviewer_code", "coder", 1)

        producer_phases = {"coder": ConsensusPhase.PROPOSED}
        result = check_propose_guard("coder", graph, matrix, producer_phases)
        assert result.allowed is False
        assert result.details["guard"] == "not_in_working_state"


# ---------------------------------------------------------------------------
# check_re_propose_guard
# ---------------------------------------------------------------------------


class TestCheckReProposeGuard:
    """Tests for check_re_propose_guard."""

    def test_allowed_producer(self, graph):
        """Producer in graph -> allowed."""
        result = check_re_propose_guard("coder", graph)
        assert result.allowed is True

    def test_not_a_producer(self, graph):
        """Non-producer -> not allowed."""
        result = check_re_propose_guard("reviewer_code", graph)
        assert result.allowed is False
        assert "not a producer" in result.reason
        assert result.details["guard"] == "not_producer"


# ---------------------------------------------------------------------------
# check_ack_guard
# ---------------------------------------------------------------------------


class TestCheckAckGuard:
    """Tests for check_ack_guard."""

    def test_allowed_reviewer_with_edge(self, graph):
        """Reviewer with edge to producer -> allowed."""
        result = check_ack_guard("reviewer_code", "coder", graph)
        assert result.allowed is True

    def test_not_a_reviewer(self, graph):
        """Non-reviewer role -> not allowed."""
        result = check_ack_guard("coder", "tester", graph)
        assert result.allowed is False
        assert "not a reviewer" in result.reason
        assert result.details["guard"] == "not_reviewer"

    def test_no_edge_to_producer(self, graph):
        """Reviewer exists but no edge to specific producer -> not allowed."""
        result = check_ack_guard("reviewer_contract", "tester", graph)
        assert result.allowed is False
        assert "No review edge" in result.reason
        assert result.details["guard"] == "no_review_edge"

    def test_version_match_allowed(self, graph, matrix):
        """ACK version matches current proposal version -> allowed."""
        matrix.record_proposal("coder")  # v1
        result = check_ack_guard("reviewer_code", "coder", graph, matrix=matrix, ack_version=1)
        assert result.allowed is True

    def test_version_mismatch_rejected(self, graph, matrix):
        """ACK version does not match current proposal version -> rejected."""
        matrix.record_proposal("coder")  # v1
        matrix.record_proposal("coder")  # v2
        result = check_ack_guard("reviewer_code", "coder", graph, matrix=matrix, ack_version=1)
        assert result.allowed is False
        assert "version mismatch" in result.reason.lower()
        assert result.details["guard"] == "version_mismatch"
        assert result.details["ack_version"] == 1
        assert result.details["current_version"] == 2


# ---------------------------------------------------------------------------
# check_nack_guard
# ---------------------------------------------------------------------------


class TestCheckNackGuard:
    """Tests for check_nack_guard."""

    def test_allowed_reviewer_with_edge(self, graph):
        """Reviewer with edge to producer -> allowed."""
        result = check_nack_guard("tester", "coder", graph)
        assert result.allowed is True

    def test_not_a_reviewer(self, graph):
        """Non-reviewer role -> not allowed."""
        result = check_nack_guard("coder", "tester", graph)
        assert result.allowed is False
        assert "not a reviewer" in result.reason
        assert result.details["guard"] == "not_reviewer"

    def test_no_edge_to_producer(self, graph):
        """Reviewer exists but no edge to specific producer -> not allowed."""
        result = check_nack_guard("reviewer_contract", "tester", graph)
        assert result.allowed is False
        assert "No review edge" in result.reason
        assert result.details["guard"] == "no_review_edge"

    def test_zero_version_nack_rejected(self, graph, matrix):
        """Cannot NACK a producer that hasn't proposed (version 0) (#1637)."""
        # Coder never proposed — version stays at 0
        result = check_nack_guard("reviewer_code", "coder", graph, matrix=matrix)
        assert result.allowed is False
        assert result.details["guard"] == "zero_version_nack"
        assert "no proposal exists" in result.reason
        assert result.details["producer"] == "coder"

    def test_nack_allowed_after_proposal(self, graph, matrix):
        """NACK allowed after producer proposes (version > 0)."""
        matrix.record_proposal("coder")
        result = check_nack_guard("reviewer_code", "coder", graph, matrix=matrix)
        assert result.allowed is True

    def test_nack_version_match_allowed(self, graph, matrix):
        """NACK version matches current proposal version -> allowed (#2142)."""
        matrix.record_proposal("coder")  # v1
        result = check_nack_guard("reviewer_code", "coder", graph, matrix=matrix, nack_version=1)
        assert result.allowed is True

    def test_nack_version_mismatch_rejected(self, graph, matrix):
        """NACK targeting a superseded version is rejected (#2142)."""
        matrix.record_proposal("coder")  # v1
        matrix.record_proposal("coder")  # v2
        result = check_nack_guard("reviewer_code", "coder", graph, matrix=matrix, nack_version=1)
        assert result.allowed is False
        assert "version mismatch" in result.reason.lower()
        assert result.details["guard"] == "version_mismatch"
        assert result.details["nack_version"] == 1
        assert result.details["current_version"] == 2


# ---------------------------------------------------------------------------
# check_confirm_guard
# ---------------------------------------------------------------------------


class TestCheckConfirmGuardProducer:
    """Tests for check_confirm_guard — producer path."""

    def test_producer_allowed_when_fully_acked(self, graph, matrix):
        """Producer fully ACKed -> allowed (all producers must have proposed)."""
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        matrix.record_ack("reviewer_contract", "coder", version)
        matrix.record_ack("tester", "coder", version)
        # tester must also have proposed to pass the global zero-proposal guard (#1648)
        matrix.record_proposal("tester")

        result = check_confirm_guard(
            "coder",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is True

    def test_producer_not_fully_acked(self, graph, matrix):
        """Producer not fully ACKed -> not allowed."""
        matrix.record_proposal("coder")
        # Only one ACK
        matrix.record_ack("reviewer_code", "coder", 1)
        # tester must also have proposed to pass the global zero-proposal guard (#1648)
        matrix.record_proposal("tester")

        result = check_confirm_guard(
            "coder",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "producer_not_fully_acked"
        assert "pending_reviewers" in result.details
        assert "blocking_states" in result.details
        # reviewer_contract and tester haven't ACKed
        pending = result.details["pending_reviewers"]
        assert len(pending) >= 1


class TestCheckConfirmGuardReviewer:
    """Tests for check_confirm_guard — reviewer path."""

    def test_reviewer_allowed(self, graph, matrix):
        """Reviewer has reviewed all producers at current version -> allowed."""
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        # reviewer_code also reviews tester
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is True

    def test_reviewer_hasnt_reviewed(self, graph, matrix):
        """Reviewer hasn't reviewed a producer -> not allowed."""
        # Coder proposed but reviewer_code hasn't ACKed/NACKed
        matrix.record_proposal("coder")
        matrix.record_proposal("tester")

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "must_have_reviewed"

    def test_zero_proposal_producers(self, graph, matrix):
        """Producer has version 0 (never proposed) -> not allowed (#1598).

        Since #1648, the global zero-proposal guard fires before the
        per-reviewer guard, returning 'global_zero_proposal' instead of
        'zero_proposal_producers'.
        """
        # Coder proposed and reviewer_code ACKed
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        # tester has version 0 — never proposed.
        # reviewer_code reviews both coder and tester.
        # We must make reviewer_code pass the must_have_reviewed guard
        # for tester first. Record a NACK at version 0 so has_reviewed
        # returns True while proposal version stays at 0.
        matrix.record_nack("reviewer_code", "tester", 0, reason="No proposal yet")

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        # Global zero-proposal guard (#1648) fires before the per-reviewer guard
        assert result.details["guard"] == "global_zero_proposal"
        assert "tester" in result.details["producers"]

    def test_stale_ack(self, graph, matrix):
        """Reviewer ACKed at old version -> not allowed."""
        # Coder proposes v1, reviewer ACKs v1, coder proposes v2
        v1 = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", v1)
        # Also review tester so we pass the must_have_reviewed guard
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)
        # Coder re-proposes, creating v2 — now reviewer_code's ACK is stale
        matrix.record_proposal("coder")  # v2

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "stale_acks"
        stale = result.details["stale_acks"]
        assert len(stale) == 1
        assert stale[0]["producer"] == "coder"
        assert stale[0]["ack_version"] == 1
        assert stale[0]["current_version"] == 2

    def test_unresolved_nack(self, graph, matrix):
        """Reviewer NACKed producer who hasn't re-proposed -> not allowed (#1576)."""
        version = matrix.record_proposal("coder")
        matrix.record_nack("reviewer_code", "coder", version, reason="Bug found")
        # reviewer_code also reviews tester — ACK that at current version
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "unresolved_nacks"
        nacks = result.details["unresolved_nacks"]
        assert len(nacks) == 1
        assert nacks[0]["producer"] == "coder"

    def test_stale_nack_blocks_confirm(self, graph, matrix):
        """Reviewer NACKed at v1, producer re-proposed v2 -> stale NACK blocks confirm (#1637)."""
        # Coder proposes v1, reviewer_code NACKs
        v1 = matrix.record_proposal("coder")
        matrix.record_nack("reviewer_code", "coder", v1, reason="Bug found")
        # Coder re-proposes v2 — the NACK is now stale (at old version)
        matrix.record_proposal("coder")  # v2

        # reviewer_code also reviews tester — ACK that at current version
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "stale_nacks"
        stale = result.details["stale_nacks"]
        assert len(stale) == 1
        assert stale[0]["producer"] == "coder"
        assert stale[0]["nack_version"] == 1
        assert stale[0]["current_version"] == 2

    def test_guard_priority_zero_proposal_before_stale_acks(self, graph, matrix):
        """Zero-proposal guard fires before stale_acks guard.

        Since #1648, the global zero-proposal guard fires first (before the
        per-reviewer zero-proposal guard and stale_acks guard).
        """
        # reviewer_code reviews coder (stale ACK) and tester (never proposed)
        v1 = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", v1)
        matrix.record_proposal("coder")  # v2 — makes ACK stale
        # tester never proposed (version 0). Record a NACK so
        # must_have_reviewed passes for tester.
        matrix.record_nack("reviewer_code", "tester", 0, reason="No proposal yet")

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        # Global zero-proposal guard (#1648) fires before per-reviewer guard
        assert result.details["guard"] == "global_zero_proposal"

    def test_guard_priority_stale_acks_before_unresolved_nacks(self):
        """Stale ACKs guard fires before unresolved NACKs guard."""
        # Build a graph where reviewer reviews two producers
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer_a", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer", "producer_b", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)

        # producer_a: ACK at v1, then re-propose to v2 -> stale ACK
        v1a = m.record_proposal("producer_a")
        m.record_ack("reviewer", "producer_a", v1a)
        m.record_proposal("producer_a")  # v2

        # producer_b: NACK at v1, no re-propose -> unresolved NACK
        v1b = m.record_proposal("producer_b")
        m.record_nack("reviewer", "producer_b", v1b, reason="Issue")

        result = check_confirm_guard(
            "reviewer",
            g,
            m,
            confirmed=set(),
        )
        assert result.allowed is False
        # Stale ACKs checked before unresolved NACKs
        assert result.details["guard"] == "stale_acks"

    def test_dual_role_tester_producer_side(self, graph, matrix):
        """Dual-role agent (tester) tested as producer."""
        # Tester is both producer (reviewed by reviewer_code) and reviewer (of coder)
        # Test the producer side: tester needs to be fully ACKed to confirm as producer
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        # Also set up tester's reviewer side: needs to have reviewed coder
        c_version = matrix.record_proposal("coder")
        matrix.record_ack("tester", "coder", c_version)

        result = check_confirm_guard(
            "tester",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is True

    def test_dual_role_tester_producer_not_acked(self, graph, matrix):
        """Dual-role agent (tester) not fully ACKed as producer -> blocked."""
        # Tester proposed but reviewer_code hasn't ACKed
        matrix.record_proposal("tester")

        # Tester has reviewed coder though
        c_version = matrix.record_proposal("coder")
        matrix.record_ack("tester", "coder", c_version)

        result = check_confirm_guard(
            "tester",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "producer_not_fully_acked"

    def test_dual_role_tester_reviewer_not_reviewed(self, graph, matrix):
        """Dual-role agent (tester) hasn't reviewed coder -> blocked on reviewer side."""
        # Tester is fully ACKed as producer
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        # But tester hasn't reviewed coder
        matrix.record_proposal("coder")

        result = check_confirm_guard(
            "tester",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "must_have_reviewed"


class TestCheckConfirmGuardGlobalZeroProposal:
    """Tests for the global zero-proposal guard (#1648).

    The global guard prevents ANY agent from confirming when ANY producer
    in the review graph has never proposed (proposal_version == 0), regardless
    of review assignments.  This fixes the bypass where reviewer_contract
    (who only reviews coder) could confirm even when tester had never proposed.
    """

    def test_global_zero_proposal_blocks_unassigned_reviewer(self, graph, matrix):
        """reviewer_contract blocked when tester has v0, even though
        reviewer_contract doesn't review tester (#1648 exact scenario)."""
        # Coder proposed and reviewer_contract ACKed coder
        c_version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_contract", "coder", c_version)
        # tester never proposed — version stays at 0

        result = check_confirm_guard(
            "reviewer_contract",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        assert "tester" in result.details["producers"]
        assert "proposal_version == 0" in result.reason

    def test_global_zero_proposal_blocks_producer(self, graph, matrix):
        """coder (a producer) blocked when peer producer tester has v0."""
        # Coder proposed and got all ACKs (fully ACKed)
        c_version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", c_version)
        matrix.record_ack("reviewer_contract", "coder", c_version)
        matrix.record_ack("tester", "coder", c_version)
        assert matrix.is_fully_acked("coder")
        # tester never proposed — version stays at 0

        result = check_confirm_guard(
            "coder",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        assert "tester" in result.details["producers"]

    def test_global_zero_proposal_clears_when_all_proposed(self, graph, matrix):
        """Guard passes once all producers have proposed."""
        # Both producers propose
        c_version = matrix.record_proposal("coder")
        matrix.record_proposal("tester")
        # reviewer_contract ACKs coder (its only assigned producer)
        matrix.record_ack("reviewer_contract", "coder", c_version)

        result = check_confirm_guard(
            "reviewer_contract",
            graph,
            matrix,
            confirmed=set(),
        )
        # Global guard passes; may still fail on other guards but NOT on
        # global_zero_proposal.
        assert result.details.get("guard") != "global_zero_proposal"

    def test_global_zero_proposal_blocks_dual_role_agent(self, graph, matrix):
        """Dual-role agent (tester) blocked by global guard when coder hasn't proposed."""
        # tester proposed and is fully ACKed as a producer
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)
        # coder never proposed — version stays at 0

        result = check_confirm_guard(
            "tester",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        assert "coder" in result.details["producers"]

    def test_global_guard_fires_before_per_reviewer_guard(self, graph, matrix):
        """Global zero-proposal guard fires before the per-reviewer zero-proposal guard.

        Both guards would reject, but the global one is checked first.
        """
        # reviewer_code reviews both coder and tester
        # Neither has proposed, so both would trigger the per-reviewer guard.
        # But the global guard should fire first.
        # We need to make reviewer_code pass the must_have_reviewed guard
        # first — record NACKs at v0 so has_reviewed returns True.
        matrix.record_nack("reviewer_code", "coder", 0, reason="No proposal")
        matrix.record_nack("reviewer_code", "tester", 0, reason="No proposal")

        result = check_confirm_guard(
            "reviewer_code",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        # The global guard fires first, not the per-reviewer guard
        assert result.details["guard"] == "global_zero_proposal"
        # Both producers are listed
        assert "coder" in result.details["producers"]
        assert "tester" in result.details["producers"]

    def test_global_guard_fires_before_producer_not_fully_acked(self, graph, matrix):
        """Global zero-proposal fires before producer_not_fully_acked.

        When a producer tries to confirm but another producer hasn't proposed,
        the global guard should fire before the fully-acked check.
        """
        # Coder proposed but has no ACKs (not fully ACKed)
        matrix.record_proposal("coder")
        # tester never proposed

        result = check_confirm_guard(
            "coder",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        assert "tester" in result.details["producers"]

    def test_global_guard_lists_multiple_zero_producers(self, graph, matrix):
        """When multiple producers have never proposed, all are listed."""
        # Neither coder nor tester have proposed

        result = check_confirm_guard(
            "reviewer_contract",
            graph,
            matrix,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        producers = result.details["producers"]
        assert "coder" in producers
        assert "tester" in producers

    def test_global_guard_does_not_fire_for_phantom_agent(self):
        """Phantom agent guard fires before global zero-proposal guard."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        # producer never proposed, but "stranger" is not in the graph at all

        result = check_confirm_guard(
            "stranger",
            g,
            m,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "phantom_agent"

    def test_global_guard_with_single_producer_graph(self):
        """Global guard works correctly with single-producer graph."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        # producer never proposed

        result = check_confirm_guard(
            "reviewer",
            g,
            m,
            confirmed=set(),
        )
        assert result.allowed is False
        assert result.details["guard"] == "global_zero_proposal"
        assert result.details["producers"] == ["producer"]

    def test_global_guard_passes_single_producer_proposed(self):
        """Global guard passes when the only producer has proposed."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        v1 = m.record_proposal("producer")
        m.record_ack("reviewer", "producer", v1)

        result = check_confirm_guard(
            "reviewer",
            g,
            m,
            confirmed=set(),
        )
        # Global guard passes; should be fully allowed
        assert result.allowed is True


# ---------------------------------------------------------------------------
# check_withdraw_guard
# ---------------------------------------------------------------------------


class TestCheckWithdrawGuard:
    """Tests for check_withdraw_guard."""

    def test_allowed(self, graph):
        """Producer, has reason, no cooldown, under flip-flop limit -> allowed."""
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={},
            flip_flop_counts={},
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="Found a critical flaw in the approach",
        )
        assert result.allowed is True

    def test_not_a_producer(self, graph):
        """Non-producer -> not allowed."""
        result = check_withdraw_guard(
            agent_role="reviewer_code",
            graph=graph,
            proposal_timestamps={},
            flip_flop_counts={},
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="Some reason",
        )
        assert result.allowed is False
        assert result.details["guard"] == "not_producer"

    def test_missing_reason(self, graph):
        """Empty reason -> not allowed."""
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={},
            flip_flop_counts={},
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="",
        )
        assert result.allowed is False
        assert result.details["guard"] == "missing_reason"

    def test_cooldown_active(self, graph):
        """Proposed recently -> not allowed, has remaining_seconds."""
        recent = datetime.now(UTC) - timedelta(seconds=10)
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={"coder": recent},
            flip_flop_counts={},
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="Good reason",
        )
        assert result.allowed is False
        assert result.details["guard"] == "cooldown"
        assert "remaining_seconds" in result.details
        assert result.details["remaining_seconds"] > 0

    def test_flip_flop_lockout(self, graph):
        """At max flip-flops -> not allowed."""
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={},
            flip_flop_counts={"coder": 4},  # +1 in guard = 5, >= max_flip_flops=5
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="Good reason",
        )
        assert result.allowed is False
        assert result.details["guard"] == "flip_flop_lockout"
        assert result.details["flip_flops"] == 5

    def test_no_last_proposal_skips_cooldown(self, graph):
        """No proposal timestamp -> cooldown skipped, allowed."""
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={},  # No entry for coder
            flip_flop_counts={},
            cooldown_seconds=300,
            max_flip_flops=5,
            reason="Valid reason",
        )
        assert result.allowed is True

    def test_cooldown_expired(self, graph):
        """Cooldown has elapsed -> allowed."""
        old_proposal = datetime.now(UTC) - timedelta(seconds=120)
        result = check_withdraw_guard(
            agent_role="coder",
            graph=graph,
            proposal_timestamps={"coder": old_proposal},
            flip_flop_counts={},
            cooldown_seconds=60,
            max_flip_flops=5,
            reason="Valid reason",
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# validate_invariants
# ---------------------------------------------------------------------------


class TestValidateInvariants:
    """Tests for validate_invariants."""

    def test_no_violations_clean_state(self, graph, matrix):
        """Clean state -> empty violations list."""
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        matrix.record_ack("reviewer_contract", "coder", version)
        matrix.record_ack("tester", "coder", version)

        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED, "tester": ConsensusPhase.PROPOSED},
            reviewer_phases={
                "reviewer_code": ConsensusPhase.REVIEWING,
                "reviewer_contract": ConsensusPhase.REVIEWING,
                "tester": ConsensusPhase.REVIEWING,
            },
            confirmed={"reviewer_code", "reviewer_contract", "tester"},
        )
        assert violations == []

    def test_invariant_1_confirmed_with_unresolved_nack(self, graph, matrix):
        """Confirmed reviewer with unresolved NACK -> violation."""
        version = matrix.record_proposal("coder")
        matrix.record_nack("reviewer_code", "coder", version, reason="Bug")
        # Also review tester so the graph is set up
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED, "tester": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed={"reviewer_code"},
        )
        nack_violations = [
            v for v in violations if v.invariant == "no_confirmed_with_unresolved_nack"
        ]
        assert len(nack_violations) == 1
        assert nack_violations[0].agent == "reviewer_code"
        assert nack_violations[0].details["producer"] == "coder"

    def test_invariant_2_confirmed_with_stale_ack(self, graph, matrix):
        """Confirmed reviewer with stale ACK (version < current) -> INV-3 violation.

        When ACK version < current version, this reports as
        'no_confirmed_with_unreviewed_changes' (INV-3) which is the more
        specific invariant.  INV-2 ('no_confirmed_with_stale_ack') only
        fires for the anomalous case where ACK version > current version.
        """
        v1 = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", v1)
        # Coder re-proposes, ACK is now stale
        matrix.record_proposal("coder")  # v2

        # reviewer_code also reviews tester — ACK at current version
        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED, "tester": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed={"reviewer_code"},
        )
        # Version < current -> reported as INV-3 (unreviewed changes), not INV-2
        unreviewed = [
            v for v in violations if v.invariant == "no_confirmed_with_unreviewed_changes"
        ]
        assert len(unreviewed) == 1
        assert unreviewed[0].agent == "reviewer_code"
        assert unreviewed[0].details["producer"] == "coder"
        assert unreviewed[0].details["reviewed_version"] == 1
        assert unreviewed[0].details["current_version"] == 2
        # No double-reporting: INV-2 should NOT fire for this case
        stale = [v for v in violations if v.invariant == "no_confirmed_with_stale_ack"]
        assert len(stale) == 0

    def test_invariant_3_confirmed_with_unreviewed_changes(self, graph, matrix):
        """Confirmed reviewer with unreviewed producer changes -> violation."""
        v1 = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", v1)
        matrix.record_proposal("coder")  # v2 — new changes since review

        t_version = matrix.record_proposal("tester")
        matrix.record_ack("reviewer_code", "tester", t_version)

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED, "tester": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed={"reviewer_code"},
        )
        unreviewed_violations = [
            v for v in violations if v.invariant == "no_confirmed_with_unreviewed_changes"
        ]
        assert len(unreviewed_violations) == 1
        assert unreviewed_violations[0].agent == "reviewer_code"
        assert unreviewed_violations[0].details["reviewed_version"] == 1
        assert unreviewed_violations[0].details["current_version"] == 2

    def test_invariant_4_confirmed_with_zero_proposal_producer(self, graph, matrix):
        """Confirmed reviewer when producer never proposed -> violation."""
        # Coder proposed and reviewer_code ACKed, but tester never proposed
        version = matrix.record_proposal("coder")
        matrix.record_ack("reviewer_code", "coder", version)
        # tester never proposed — version stays at 0

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed={"reviewer_code"},
        )
        zero_violations = [
            v for v in violations if v.invariant == "no_confirmed_with_zero_proposal_producer"
        ]
        assert len(zero_violations) == 1
        assert zero_violations[0].agent == "reviewer_code"
        assert zero_violations[0].details["producer"] == "tester"

    def test_invariant_5_fully_acked_consistency(self):
        """is_fully_acked inconsistency -> violation."""
        # Create a minimal graph and manually tamper with internal state
        # to cause an inconsistency between is_fully_acked and matrix
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)

        # Producer proposed but reviewer hasn't ACKed —
        # is_fully_acked should return False.
        # If we manually set the proposal version but DON'T ACK,
        # is_fully_acked returns False, and all_acked computed is also False.
        # So no inconsistency in the normal case.

        # To trigger the inconsistency, we need is_fully_acked to return
        # a different value than what the manual check computes.
        # Tamper: set proposal version and mark ACK, but at wrong version
        m.record_proposal("producer")  # v1
        m.record_ack("reviewer", "producer", 1)  # ACKed at v1
        # Now bump proposal to v2 behind the scenes
        m._proposal_versions["producer"] = 2
        # is_fully_acked checks version 2 but ACK is at v1 -> False
        # But manual check also finds no ACK at v2 -> all_acked=False
        # So this is consistent (both False). We need them to disagree.

        # To force disagreement: pretend is_fully_acked returns True
        # when matrix says False. We can do this by injecting a version
        # mismatch in the opposite direction.
        # Actually the simplest inconsistency: producer has version 0
        # but is_fully_acked returns True (never happens normally).
        # We can test that the code handles version-0 check.

        # Test the version-0 branch: producer never proposed, is_fully_acked=True
        # would be a violation. Force this:
        g2 = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m2 = ApprovalMatrix(g2)
        # Monkey-patch is_fully_acked to return True for version-0 producer
        original_is_fully_acked = m2.is_fully_acked
        m2.is_fully_acked = lambda p: True if p == "producer" else original_is_fully_acked(p)

        violations = validate_invariants(
            g2,
            m2,
            producer_phases={},
            reviewer_phases={},
            confirmed=set(),
        )
        consistency_violations = [v for v in violations if v.invariant == "fully_acked_consistency"]
        assert len(consistency_violations) == 1
        assert consistency_violations[0].agent == "producer"
        assert "version 0" in consistency_violations[0].description

    def test_invariant_5_fully_acked_mismatch_non_zero_version(self):
        """is_fully_acked disagrees with manual check at non-zero version."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        m.record_proposal("producer")  # v1
        m.record_ack("reviewer", "producer", 1)

        # Now is_fully_acked("producer") returns True. Make it return False
        # by monkey-patching to simulate inconsistency.
        m.is_fully_acked = lambda p: False if p == "producer" else False

        violations = validate_invariants(
            g,
            m,
            producer_phases={"producer": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer": ConsensusPhase.REVIEWING},
            confirmed=set(),
        )
        consistency_violations = [v for v in violations if v.invariant == "fully_acked_consistency"]
        assert len(consistency_violations) == 1
        assert consistency_violations[0].details["is_fully_acked"] is False
        assert consistency_violations[0].details["matrix_says"] is True

    def test_invariant_6_ack_commit_sha_mismatch(self):
        """ACK commit SHA differs from proposal commit SHA -> violation (#1637)."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        v1 = m.record_proposal("producer")
        # ACK with a different commit SHA than the proposal
        m.record_ack("reviewer", "producer", v1, commit_sha="aaaa1111")

        violations = validate_invariants(
            g,
            m,
            producer_phases={"producer": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer": ConsensusPhase.REVIEWING},
            confirmed={"reviewer"},
            proposal_commit_shas={"producer": "bbbb2222"},
        )
        sha_violations = [v for v in violations if v.invariant == "ack_commit_sha_consistency"]
        assert len(sha_violations) == 1
        assert sha_violations[0].agent == "reviewer"
        assert sha_violations[0].details["producer"] == "producer"
        assert sha_violations[0].details["ack_commit_sha"] == "aaaa1111"
        assert sha_violations[0].details["proposal_commit_sha"] == "bbbb2222"

    def test_invariant_6_no_violation_when_shas_match(self):
        """ACK commit SHA matches proposal commit SHA -> no violation."""
        g = ReviewGraph(
            [
                ReviewEdge("reviewer", "producer", ReviewCriticality.CRITICAL),
            ]
        )
        m = ApprovalMatrix(g)
        v1 = m.record_proposal("producer")
        m.record_ack("reviewer", "producer", v1, commit_sha="aaaa1111")

        violations = validate_invariants(
            g,
            m,
            producer_phases={"producer": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer": ConsensusPhase.REVIEWING},
            confirmed={"reviewer"},
            proposal_commit_shas={"producer": "aaaa1111"},
        )
        sha_violations = [v for v in violations if v.invariant == "ack_commit_sha_consistency"]
        assert len(sha_violations) == 0

    def test_multiple_violations(self, graph, matrix):
        """When multiple invariants are violated, all should be reported."""
        # Set up: reviewer_code confirmed but...
        # 1. NACK against coder (invariant 1)
        version = matrix.record_proposal("coder")
        matrix.record_nack("reviewer_code", "coder", version, reason="Bug")
        # 2. tester never proposed (invariant 4)

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed={"reviewer_code"},
        )
        invariant_types = {v.invariant for v in violations}
        assert "no_confirmed_with_unresolved_nack" in invariant_types
        assert "no_confirmed_with_zero_proposal_producer" in invariant_types
        assert len(violations) >= 2

    def test_non_confirmed_agent_not_checked(self, graph, matrix):
        """Non-confirmed agents should not trigger invariant violations."""
        # reviewer_code has NACK against coder but is NOT confirmed
        version = matrix.record_proposal("coder")
        matrix.record_nack("reviewer_code", "coder", version, reason="Bug")

        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={"coder": ConsensusPhase.PROPOSED},
            reviewer_phases={"reviewer_code": ConsensusPhase.REVIEWING},
            confirmed=set(),  # Nobody confirmed
        )
        # Invariants 1-4 only apply to confirmed agents, so nothing fires
        # (invariant 5 doesn't depend on confirmed set)
        invariant_1_4 = [
            v
            for v in violations
            if v.invariant
            in {
                "no_confirmed_with_unresolved_nack",
                "no_confirmed_with_stale_ack",
                "no_confirmed_with_unreviewed_changes",
                "no_confirmed_with_zero_proposal_producer",
            }
        ]
        assert invariant_1_4 == []


# ---------------------------------------------------------------------------
# InvariantViolation dataclass
# ---------------------------------------------------------------------------


class TestInvariantViolation:
    """Tests for InvariantViolation dataclass."""

    def test_basic_construction(self):
        v = InvariantViolation(
            invariant="test_invariant",
            agent="coder",
            description="Something is wrong",
            details={"key": "value"},
        )
        assert v.invariant == "test_invariant"
        assert v.agent == "coder"
        assert v.description == "Something is wrong"
        assert v.details == {"key": "value"}

    def test_default_details(self):
        v = InvariantViolation(
            invariant="test_invariant",
            agent="coder",
            description="Something is wrong",
        )
        assert v.details == {}

    def test_not_frozen(self):
        """InvariantViolation is a regular (non-frozen) dataclass."""
        v = InvariantViolation(
            invariant="test",
            agent="coder",
            description="desc",
        )
        v.description = "updated"
        assert v.description == "updated"
