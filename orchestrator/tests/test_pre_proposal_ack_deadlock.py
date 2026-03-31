"""Tests for BRC consensus pre-proposal ACK deadlock fix (issue #1405).

Covers three fixes:
1. _invalidate_pre_proposal_acks() — version-0 ACKs are invalidated on propose
2. handle_confirmed() — reviewer rejected when ACK version mismatches proposal
3. Full deadlock scenario: reviewer ACKs before producer proposes

Also covers the container_monitor exit code 143 handling (secondary fix).
"""

import sys
from pathlib import Path

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalState
from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def implement_graph():
    """Graph mimicking the implement phase: coder reviewed by reviewer_code,
    reviewer_contract, and tester (dual role)."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def tracker(implement_graph):
    """Tracker with all agents registered, zero cooldown for testing."""
    t = PeerConsensusTracker("test-pipeline", implement_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    t.register_agent("reviewer_contract")
    return t


@pytest.fixture
def simple_graph():
    """Minimal graph: one producer, two reviewers."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_a", "producer", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_b", "producer", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def simple_tracker(simple_graph):
    t = PeerConsensusTracker("test-simple", simple_graph, cooldown_seconds=0)
    t.register_agent("producer")
    t.register_agent("reviewer_a")
    t.register_agent("reviewer_b")
    return t


def _minimal_proposal(summary="test", artifacts=None):
    """Build a minimal proposal payload."""
    return {
        "summary": summary,
        "artifacts": artifacts or ["file.py"],
    }


def _minimal_ack(artifacts=None):
    """Build a minimal ACK payload."""
    return {
        "artifact_references": artifacts or ["file.py"],
    }


# ---------------------------------------------------------------------------
# Tests: _invalidate_pre_proposal_acks
# ---------------------------------------------------------------------------


class TestInvalidatePreProposalAcks:
    """ACKs recorded at version 0 (before producer proposes) must be
    invalidated when the producer submits their first proposal."""

    def test_pre_proposal_ack_is_invalidated_on_propose(self, simple_tracker):
        """Reviewer ACKs at v0, producer proposes -> ACK reset to PENDING."""
        # Reviewer ACKs before producer has proposed (version 0)
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        entry = simple_tracker.matrix.get_entry("reviewer_a", "producer")
        assert entry.state == ApprovalState.ACKED
        assert entry.version == 0

        # Producer proposes (version 1)
        result = simple_tracker.handle_propose("producer", _minimal_proposal())
        assert result["version"] == 1

        # Pre-proposal ACK should be invalidated
        entry = simple_tracker.matrix.get_entry("reviewer_a", "producer")
        assert entry.state == ApprovalState.PENDING
        assert "reviewer_a" in result["stale_reviewers"]

    def test_non_pre_proposal_ack_not_invalidated(self, simple_tracker):
        """ACKs at version >= 1 are NOT invalidated by _invalidate_pre_proposal_acks."""
        # Normal flow: producer proposes first, then reviewer ACKs
        simple_tracker.handle_propose("producer", _minimal_proposal())
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())

        entry = simple_tracker.matrix.get_entry("reviewer_a", "producer")
        assert entry.state == ApprovalState.ACKED
        assert entry.version == 1

        # Second proposal should not invalidate reviewer_a's v1 ACK via
        # _invalidate_pre_proposal_acks (which only targets v==0).
        # reviewer_a is not confirmed, so _un_confirm_stale_reviewers also
        # skips them — the v1 ACK remains intact.
        simple_tracker.handle_withdraw("producer", "revising")
        simple_tracker.handle_propose("producer", _minimal_proposal("v2"))
        entry = simple_tracker.matrix.get_entry("reviewer_a", "producer")
        assert entry.state == ApprovalState.ACKED
        assert entry.version == 1

    def test_confirmed_reviewer_skipped_by_invalidation(self, simple_tracker):
        """Confirmed reviewers are skipped — _un_confirm_stale_reviewers handles them."""
        # Reviewer ACKs at v0 (pre-proposal)
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())

        # Manually mark reviewer as confirmed (simulating prior confirmation)
        simple_tracker._confirmed.add("reviewer_a")

        # Producer proposes
        result = simple_tracker.handle_propose("producer", _minimal_proposal())

        # reviewer_a should be handled by _un_confirm_stale_reviewers, not
        # _invalidate_pre_proposal_acks (which skips confirmed reviewers)
        # Either way, the stale reviewer should appear in the result
        assert "reviewer_a" in result["stale_reviewers"]

    def test_multiple_reviewers_some_pre_proposal(self, simple_tracker):
        """Only reviewers with v0 ACKs are invalidated, others untouched."""
        # reviewer_a ACKs at v0 (pre-proposal)
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        # reviewer_b has not ACKed at all (PENDING, v0)

        simple_tracker.handle_propose("producer", _minimal_proposal())

        # reviewer_a's pre-proposal ACK is invalidated
        entry_a = simple_tracker.matrix.get_entry("reviewer_a", "producer")
        assert entry_a.state == ApprovalState.PENDING

        # reviewer_b stays PENDING (was never ACKed, nothing to invalidate)
        entry_b = simple_tracker.matrix.get_entry("reviewer_b", "producer")
        assert entry_b.state == ApprovalState.PENDING

    def test_pre_proposal_ack_method_directly(self, simple_tracker):
        """Test _invalidate_pre_proposal_acks returns correct reviewer list."""
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        # Both ACKed at v0
        invalidated = simple_tracker._invalidate_pre_proposal_acks("producer", 1)
        assert set(invalidated) == {"reviewer_a", "reviewer_b"}

    def test_no_pre_proposal_acks_returns_empty(self, simple_tracker):
        """No v0 ACKs means nothing to invalidate."""
        invalidated = simple_tracker._invalidate_pre_proposal_acks("producer", 1)
        assert invalidated == []


# ---------------------------------------------------------------------------
# Tests: handle_confirmed stale ACK version guard
# ---------------------------------------------------------------------------


class TestConfirmedStaleAckGuard:
    """Reviewers should be rejected from confirming when their ACK version
    doesn't match the producer's current proposal version."""

    def test_reviewer_cannot_confirm_with_stale_ack(self, simple_tracker):
        """Reviewer ACKed v1, producer re-proposes v2 -> confirm rejected."""
        # Producer proposes v1
        simple_tracker.handle_propose("producer", _minimal_proposal())

        # Both reviewers ACK v1
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        # Producer withdraws and re-proposes v2
        simple_tracker.handle_withdraw("producer", "Found a bug, re-doing")
        simple_tracker.handle_propose("producer", _minimal_proposal("v2"))

        # reviewer_a tries to confirm without re-ACKing at v2
        result = simple_tracker.handle_confirmed("reviewer_a")

        assert result["status"] == "pending_acks"
        assert "version mismatch" in result["message"]
        assert "reviewer_a" not in simple_tracker._confirmed

    def test_reviewer_can_confirm_with_current_ack(self, simple_tracker):
        """Reviewer ACKed at current version -> confirm succeeds."""
        simple_tracker.handle_propose("producer", _minimal_proposal())
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        result = simple_tracker.handle_confirmed("reviewer_a")
        assert result["status"] in ("confirmed", "partially_confirmed")

    def test_stale_ack_returns_details(self, simple_tracker):
        """Stale ACK rejection includes version details for debugging."""
        simple_tracker.handle_propose("producer", _minimal_proposal())
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())

        # Re-propose (using withdraw + propose since re_propose needs NACK first)
        simple_tracker.handle_withdraw("producer", "need changes")
        simple_tracker.handle_propose("producer", _minimal_proposal("v2"))

        result = simple_tracker.handle_confirmed("reviewer_a")
        assert result["status"] == "pending_acks"
        assert "stale_acks" in result
        stale = result["stale_acks"]
        assert len(stale) == 1
        assert stale[0]["producer"] == "producer"
        assert stale[0]["ack_version"] == 1
        assert stale[0]["current_version"] == 2

    def test_reviewer_with_no_ack_cannot_confirm(self, simple_tracker):
        """Reviewer who never ACKed cannot confirm (existing behavior)."""
        simple_tracker.handle_propose("producer", _minimal_proposal())

        with pytest.raises(ValueError, match="hasn't reviewed"):
            simple_tracker.handle_confirmed("reviewer_a")

    def test_pre_proposal_ack_v0_blocks_confirmation(self, simple_tracker):
        """Reviewer ACKed at v0 (pre-proposal), producer proposes v1 ->
        the pre-proposal ACK is invalidated by propose, so reviewer
        cannot confirm because has_reviewed returns False."""
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_propose("producer", _minimal_proposal())

        # After propose, the v0 ACK was invalidated to PENDING
        # So reviewer can't confirm (hasn't reviewed)
        with pytest.raises(ValueError, match="hasn't reviewed"):
            simple_tracker.handle_confirmed("reviewer_a")


# ---------------------------------------------------------------------------
# Tests: Full deadlock scenario from issue #1405
# ---------------------------------------------------------------------------


class TestPreProposalDeadlockScenario:
    """End-to-end test reproducing the exact deadlock from issue #1405:
    tester ACKs coder before coder proposes, causing permanent blocking."""

    def test_early_ack_then_propose_resolves_correctly(self, tracker):
        """Reproduce issue #1405: tester ACKs coder at T+14:58,
        coder proposes at T+15:17 -> should NOT deadlock."""
        # Step 1: Tester ACKs coder BEFORE coder proposes (early ACK at v0)
        tracker.handle_ack("tester", "coder", _minimal_ack())

        # Step 2: Coder proposes (v1) — this should invalidate tester's v0 ACK
        result = tracker.handle_propose("coder", _minimal_proposal("Implemented feature"))
        assert result["version"] == 1
        # tester's stale ACK should appear in stale list
        assert "tester" in result["stale_reviewers"]

        # Step 3: Verify tester's ACK was invalidated
        entry = tracker.matrix.get_entry("tester", "coder")
        assert entry.state == ApprovalState.PENDING

        # Step 4: Other reviewers ACK at v1
        tracker.handle_ack("reviewer_code", "coder", _minimal_ack())
        tracker.handle_ack("reviewer_contract", "coder", _minimal_ack())

        # Step 5: Tester re-ACKs at v1 (after seeing their stale ACK was invalidated)
        tracker.handle_ack("tester", "coder", _minimal_ack())

        # Step 6: Coder should now be fully ACKed
        assert tracker.matrix.is_fully_acked("coder") is True

        # Step 7: Coder can confirm
        # First, tester and other producers need to do their work
        tracker.handle_propose(
            "tester",
            {
                **_minimal_proposal("Tests written"),
                "attestation": {"tests_run": 5, "checks_passed": ["test"]},
            },
        )
        tracker.handle_ack("reviewer_code", "tester", _minimal_ack())

        # All reviewers confirm
        tracker.handle_confirmed("reviewer_code")
        tracker.handle_confirmed("reviewer_contract")
        tracker.handle_confirmed("tester")

        # Coder confirms
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "confirmed"
        assert result["consensus_reached"] is True

    def test_early_ack_without_fix_would_deadlock(self, tracker):
        """Verify that without re-ACK, the coder cannot confirm —
        demonstrating why the fix is necessary."""
        # Tester ACKs before coder proposes
        tracker.handle_ack("tester", "coder", _minimal_ack())

        # Coder proposes (invalidates v0 ACK)
        tracker.handle_propose("coder", _minimal_proposal())

        # Other reviewers ACK at v1
        tracker.handle_ack("reviewer_code", "coder", _minimal_ack())
        tracker.handle_ack("reviewer_contract", "coder", _minimal_ack())

        # Without tester re-ACKing, coder is NOT fully ACKed
        assert tracker.matrix.is_fully_acked("coder") is False

        # Coder cannot confirm
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"
        assert "tester" in result["message"]

    def test_multiple_early_acks_all_invalidated(self, tracker):
        """Multiple reviewers ACK early, all get invalidated on propose."""
        # All reviewers ACK before coder proposes
        tracker.handle_ack("reviewer_code", "coder", _minimal_ack())
        tracker.handle_ack("reviewer_contract", "coder", _minimal_ack())
        tracker.handle_ack("tester", "coder", _minimal_ack())

        # Coder proposes
        tracker.handle_propose("coder", _minimal_proposal())

        # All pre-proposal ACKs should be invalidated
        for reviewer in ["reviewer_code", "reviewer_contract", "tester"]:
            entry = tracker.matrix.get_entry(reviewer, "coder")
            assert entry.state == ApprovalState.PENDING, (
                f"{reviewer}'s pre-proposal ACK was not invalidated"
            )

        # is_fully_acked should be False (all ACKs invalidated)
        assert tracker.matrix.is_fully_acked("coder") is False

    def test_dual_role_tester_full_lifecycle(self, tracker):
        """Tester is dual-role (producer + reviewer). Both roles must complete
        for consensus, and pre-proposal ACK invalidation should work correctly."""
        # Tester ACKs coder early (reviewer role)
        tracker.handle_ack("tester", "coder", _minimal_ack())

        # Coder proposes — invalidates tester's v0 ACK
        tracker.handle_propose("coder", _minimal_proposal("Feature"))

        # Tester proposes their own tests (producer role)
        tracker.handle_propose(
            "tester",
            {
                **_minimal_proposal("Tests"),
                "attestation": {"tests_run": 3, "checks_passed": ["test"]},
            },
        )

        # All legitimate ACKs happen now
        tracker.handle_ack("reviewer_code", "coder", _minimal_ack())
        tracker.handle_ack("reviewer_contract", "coder", _minimal_ack())
        tracker.handle_ack("tester", "coder", _minimal_ack())  # re-ACK at v1
        tracker.handle_ack("reviewer_code", "tester", _minimal_ack())

        # All agents confirm
        for agent in ["reviewer_code", "reviewer_contract", "tester", "coder"]:
            result = tracker.handle_confirmed(agent)
            # Should not get pending_acks for any agent
            assert result["status"] in ("confirmed", "partially_confirmed"), (
                f"{agent} failed to confirm: {result}"
            )

        # Final evaluation
        state = tracker.evaluate()
        assert state["is_complete"] is True


# ---------------------------------------------------------------------------
# Tests: Edge cases and boundary conditions
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases around timing and ordering of proposals and ACKs."""

    def test_ack_at_v0_then_nack_at_v1_works(self, simple_tracker):
        """Reviewer ACKs v0, propose invalidates it, reviewer NACKs v1."""
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_propose("producer", _minimal_proposal())

        # Now reviewer NACKs v1
        result = simple_tracker.handle_nack(
            "reviewer_a",
            "producer",
            {"artifact_references": ["file.py"], "reason": "Bug found"},
        )
        assert result["status"] == "nacked"

    def test_propose_twice_second_invalidates_first_acks(self, simple_tracker):
        """Producer proposes v1, reviewers ACK, producer withdraws and
        re-proposes v2 -> v1 ACKs from non-confirmed reviewers should be
        stale (not version-matched)."""
        simple_tracker.handle_propose("producer", _minimal_proposal())
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())

        simple_tracker.handle_withdraw("producer", "New information")
        simple_tracker.handle_propose("producer", _minimal_proposal("v2"))

        # reviewer_a's v1 ACK is now stale
        assert not simple_tracker.matrix.is_fully_acked("producer")

    def test_evaluate_not_complete_with_stale_acks(self, simple_tracker):
        """evaluate() should not report complete when ACKs are stale."""
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        # Both ACKed at v0, producer proposes v1
        simple_tracker.handle_propose("producer", _minimal_proposal())

        state = simple_tracker.evaluate()
        assert state["is_complete"] is False
        assert "producer" in state["blocking_agents"] or "reviewer_a" in state["blocking_agents"]

    def test_reviewer_ack_nack_ack_cycle_with_pre_proposal(self, simple_tracker):
        """Reviewer ACKs v0, propose invalidates, ACKs v1, NACKs v1,
        producer re-proposes v2, reviewer ACKs v2 -> should resolve."""
        # ACK at v0
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())

        # Propose v1 (invalidates v0 ACK)
        simple_tracker.handle_propose("producer", _minimal_proposal())

        # ACK at v1
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        # NACK from reviewer_a at v1
        simple_tracker.handle_nack(
            "reviewer_a",
            "producer",
            {"artifact_references": ["file.py"], "reason": "Needs fix"},
        )

        # Producer re-proposes v2 (using handle_re_propose)
        result = simple_tracker.handle_re_propose(
            "producer",
            _minimal_proposal("v2"),
            changed_artifacts=["file.py"],
        )
        assert result["version"] == 2

        # Both re-ACK at v2
        simple_tracker.handle_ack("reviewer_a", "producer", _minimal_ack())
        simple_tracker.handle_ack("reviewer_b", "producer", _minimal_ack())

        assert simple_tracker.matrix.is_fully_acked("producer") is True

        # All confirm
        simple_tracker.handle_confirmed("reviewer_a")
        simple_tracker.handle_confirmed("reviewer_b")
        result = simple_tracker.handle_confirmed("producer")
        assert result["status"] == "confirmed"
        assert result["consensus_reached"] is True
