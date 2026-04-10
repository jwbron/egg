"""Tests for handle_producer_push and consensus_producer_push signal handler.

Covers the auto re-proposal mechanism triggered when a producer pushes new
commits after having already proposed.  This is the key enforcement of the
"all changes must be reviewed" principle in the BRC protocol.
"""

import sys
from pathlib import Path

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from approval_matrix import ApprovalState
from attestation_schemas import AttestationStrictness
from egg_orchestrator.types import ConsensusPhase
from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_proposal(summary="Test", artifacts=None, commit_sha="abc123"):
    return {
        "summary": summary,
        "artifacts": artifacts or ["src/main.py"],
        "commit_sha": commit_sha,
    }


def ack_producer(tracker, reviewer, producer, artifact_references=None):
    """Helper to ACK a producer with minimal boilerplate."""
    return tracker.handle_ack(
        reviewer,
        producer,
        {"artifact_references": artifact_references or ["src/main.py"]},
    )


def nack_producer(tracker, reviewer, producer, reason="needs fix", artifact_references=None):
    """Helper to NACK a producer."""
    return tracker.handle_nack(
        reviewer,
        producer,
        {
            "artifact_references": artifact_references or ["src/main.py"],
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def implement_graph():
    """Full implement-phase graph."""
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
    t = PeerConsensusTracker(
        "test-pipeline",
        implement_graph,
        cooldown_seconds=0,
        attestation_strictness=AttestationStrictness.RELAXED,
    )
    t.register_agent("coder")
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    t.register_agent("reviewer_contract")
    return t


@pytest.fixture
def simple_graph():
    """Minimal graph for focused tests."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def simple_tracker(simple_graph):
    t = PeerConsensusTracker(
        "test-pipeline",
        simple_graph,
        cooldown_seconds=0,
        attestation_strictness=AttestationStrictness.RELAXED,
    )
    t.register_agent("coder")
    t.register_agent("reviewer_code")
    return t


# ===========================================================================
# Happy path tests
# ===========================================================================


class TestProducerPushHappyPath:
    """Test auto re-proposal when a producer pushes after proposing."""

    def test_push_in_proposed_phase_auto_re_proposes(self, tracker):
        """Producer in PROPOSED phase pushes -> auto re-propose, version increments."""
        # Coder proposes v1
        result = tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert result["version"] == 1

        # Reviewer ACKs at v1
        ack_producer(tracker, "reviewer_code", "coder")
        ack_producer(tracker, "reviewer_contract", "coder")
        ack_producer(tracker, "tester", "coder")

        # Coder pushes new commits -> auto re-propose
        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        assert push_result["version"] == 2
        assert push_result["status"] == "proposed"
        assert "reviewers" in push_result
        assert "invalidated_reviewers" in push_result

    def test_push_in_confirmed_phase_auto_re_proposes(self, simple_tracker):
        """Producer in CONFIRMED phase pushes -> auto re-propose."""
        tracker = simple_tracker

        # Propose, ACK, confirm -> CONFIRMED
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")

        # Coder pushes after confirming
        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        assert push_result["version"] == 2
        # Producer should now be in PROPOSED (not CONFIRMED)
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED


# ===========================================================================
# No-op cases
# ===========================================================================


class TestProducerPushNoOp:
    """Test cases where push does NOT trigger re-proposal."""

    def test_push_in_working_phase_is_noop(self, tracker):
        """Producer in WORKING phase (hasn't proposed yet) -> no-op."""
        result = tracker.handle_producer_push("coder", "sha1")

        assert result["status"] == "no_op"
        assert "auto_re_propose" not in result
        assert "WORKING" in result["reason"]

    def test_non_producer_push_raises(self, tracker):
        """Non-producer agent pushing raises ValueError."""
        with pytest.raises(ValueError, match="not a producer"):
            tracker.handle_producer_push("reviewer_code", "sha1")

    def test_unregistered_non_producer_raises(self, tracker):
        """Unknown agent that is not a producer raises ValueError."""
        with pytest.raises(ValueError):
            tracker.handle_producer_push("random_agent", "sha1")


# ===========================================================================
# Scoped invalidation
# ===========================================================================


class TestProducerPushScopedInvalidation:
    """Test scoped ACK invalidation based on changed_files."""

    def test_changed_files_matching_ack_artifacts(self, tracker):
        """Only overlapping ACKs are invalidated when changed_files are given."""
        # Coder proposes with two artifacts
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/auth.py", "src/utils.py"], commit_sha="sha1"),
        )

        # reviewer_code ACKs referencing src/auth.py
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )
        # reviewer_contract ACKs referencing src/utils.py
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {"artifact_references": ["src/utils.py"]},
        )
        # tester ACKs referencing src/auth.py
        tracker.handle_ack(
            "tester",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )

        # Push changes only to src/auth.py
        push_result = tracker.handle_producer_push("coder", "sha2", changed_files=["src/auth.py"])

        assert push_result["auto_re_propose"] is True
        # Only reviewer_code and tester should be invalidated (they ACKed auth.py)
        invalidated = set(push_result["invalidated_reviewers"])
        assert "reviewer_code" in invalidated
        assert "tester" in invalidated
        # reviewer_contract ACKed utils.py, which was NOT changed
        assert "reviewer_contract" not in invalidated

    def test_changed_files_not_matching_any_ack(self, tracker):
        """Changed files don't overlap any ACK artifacts -> no invalidation but still re-proposes."""
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/auth.py"], commit_sha="sha1"),
        )

        # ACK references auth.py
        ack_producer(tracker, "reviewer_code", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "reviewer_contract", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "tester", "coder", artifact_references=["src/auth.py"])

        # Push changes to a completely different file
        push_result = tracker.handle_producer_push(
            "coder", "sha2", changed_files=["src/new_file.py"]
        )

        assert push_result["auto_re_propose"] is True
        assert push_result["version"] == 2
        # No ACKs should be invalidated since no overlap
        assert push_result["invalidated_reviewers"] == []

    def test_without_changed_files_conservative_invalidation(self, tracker):
        """Without changed_files, ALL ACKs are invalidated (conservative)."""
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/auth.py"], commit_sha="sha1"),
        )

        ack_producer(tracker, "reviewer_code", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "reviewer_contract", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "tester", "coder", artifact_references=["src/auth.py"])

        # Push without specifying changed_files
        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        # All ACKs invalidated
        invalidated = set(push_result["invalidated_reviewers"])
        assert "reviewer_code" in invalidated
        assert "reviewer_contract" in invalidated
        assert "tester" in invalidated


# ===========================================================================
# State transitions
# ===========================================================================


class TestProducerPushStateTransitions:
    """Test state transitions caused by producer push re-proposal."""

    def test_stale_ack_version_after_re_propose(self, simple_tracker):
        """After re-propose, a reviewer who ACKed v1 now has a stale ACK at v2."""
        tracker = simple_tracker

        # Propose v1, reviewer ACKs
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # Push triggers re-propose to v2
        push_result = tracker.handle_producer_push("coder", "sha2")
        assert push_result["version"] == 2

        # The reviewer's old ACK should now be stale -- check via matrix
        entry = tracker.matrix.get_entry("reviewer_code", "coder")
        current_version = tracker.matrix.get_proposal_version("coder")
        assert current_version == 2
        # The ACK was invalidated (conservative, no changed_files), so state is PENDING
        assert entry.state == ApprovalState.PENDING

    def test_confirmed_reviewers_un_confirmed_after_push(self, simple_tracker):
        """Reviewers in CONFIRMED state are bounced back to REVIEWING."""
        tracker = simple_tracker

        # Full lifecycle: propose -> ACK -> confirm (both)
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        # Verify reviewer is confirmed
        assert "reviewer_code" in tracker._confirmed

        # Producer pushes
        push_result = tracker.handle_producer_push("coder", "sha2")

        # Reviewer should be un-confirmed and bounced to REVIEWING
        assert "reviewer_code" not in tracker._confirmed
        # The stale_reviewers list from _handle_propose_inner should include
        # the reviewer who was confirmed on v1
        assert "reviewer_code" in push_result.get("stale_reviewers", [])

    def test_producer_phase_transitions_to_proposed(self, simple_tracker):
        """After push triggers re-propose, producer should be in PROPOSED."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED

        # Push after proposing
        tracker.handle_producer_push("coder", "sha2")

        # Should still be PROPOSED (re-proposed)
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED


# ===========================================================================
# Integration with confirm guard
# ===========================================================================


class TestConfirmGuardAfterPush:
    """Test that confirm guards properly reject stale state after producer push."""

    def test_reviewer_confirm_rejected_with_stale_ack(self, simple_tracker):
        """Reviewer ACKed v1, producer pushes (v2), reviewer tries confirm -> rejected.

        The conservative invalidation resets the ACK to PENDING, so the confirm
        guard sees "hasn't reviewed" and raises ValueError.
        """
        tracker = simple_tracker

        # Propose v1
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        # Reviewer ACKs v1
        ack_producer(tracker, "reviewer_code", "coder")
        # Producer pushes -> v2 (invalidates ACKs, resets to PENDING)
        tracker.handle_producer_push("coder", "sha2")

        # Reviewer tries to confirm without re-ACKing -- rejected because
        # the ACK was invalidated back to PENDING (must_have_reviewed guard)
        with pytest.raises(ValueError, match="hasn't reviewed"):
            tracker.handle_confirmed("reviewer_code")

    def test_reviewer_re_acks_then_confirms_after_push(self, simple_tracker):
        """Reviewer ACKed v1, producer pushes (v2), reviewer re-ACKs v2, confirms -> allowed."""
        tracker = simple_tracker

        # Propose v1
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # Producer pushes -> v2
        tracker.handle_producer_push("coder", "sha2")

        # Reviewer re-ACKs at v2
        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 2

        # Now confirm should succeed (for the reviewer side)
        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] in ("confirmed", "partially_confirmed")

    def test_producer_confirm_rejected_after_push_invalidates_acks(self, simple_tracker):
        """Producer pushes after being fully ACKed -> no longer fully ACKed -> confirm fails."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # At this point coder is fully ACKed
        assert tracker.matrix.is_fully_acked("coder")

        # Push invalidates ACKs
        tracker.handle_producer_push("coder", "sha2")

        # Coder should NOT be fully ACKed anymore
        assert not tracker.matrix.is_fully_acked("coder")

        # Producer confirm should fail
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"


# ===========================================================================
# Unresolved NACK guard on confirm
# ===========================================================================


class TestUnresolvedNackGuardOnConfirm:
    """Test that unresolved NACKs prevent confirmation."""

    def test_nack_without_re_propose_blocks_confirm(self, simple_tracker):
        """Reviewer NACKs producer who hasn't re-proposed -> confirm rejected."""
        tracker = simple_tracker

        # Propose, NACK
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bugs found")

        # Reviewer tries to confirm -- blocked by unresolved NACK
        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] == "pending_acks"
        assert "unresolved_nacks" in result

    def test_nack_then_re_propose_then_re_ack_allows_confirm(self, simple_tracker):
        """NACK -> re-propose -> re-ACK -> confirm succeeds."""
        tracker = simple_tracker

        # Propose v1
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        # Reviewer NACKs
        nack_producer(tracker, "reviewer_code", "coder", reason="bugs")
        # Producer re-proposes v2
        tracker.handle_re_propose(
            "coder",
            make_proposal(summary="Fixed", commit_sha="sha2"),
            changed_artifacts=["src/main.py"],
        )
        # Reviewer re-ACKs at v2
        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 2

        # Confirm should now succeed
        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] in ("confirmed", "partially_confirmed")


# ===========================================================================
# Zero-proposal guard on confirm
# ===========================================================================


class TestZeroProposalGuardOnConfirm:
    """Test that zero-proposal producers prevent reviewer confirmation."""

    def test_reviewer_nacks_non_proposing_producer_then_confirm_rejected(self):
        """Reviewer NACKs a producer that has never proposed -> confirm blocked."""
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            ]
        )
        t = PeerConsensusTracker(
            "test-pipeline",
            graph,
            cooldown_seconds=0,
            attestation_strictness=AttestationStrictness.RELAXED,
        )
        t.register_agent("coder")
        t.register_agent("tester")
        t.register_agent("reviewer_code")

        # Coder proposes and gets ACKed
        t.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(t, "reviewer_code", "coder")

        # Tester has NEVER proposed (version 0)
        # Reviewer still needs to review tester, but tester hasn't proposed
        # Reviewer ACKs tester anyway (pre-proposal ACK)
        ack_producer(t, "reviewer_code", "tester")

        # Reviewer tries to confirm -- blocked because tester version is 0
        result = t.handle_confirmed("reviewer_code")
        assert result["status"] == "pending_acks"
        assert "zero_proposal_producers" in result
        assert "tester" in result["zero_proposal_producers"]


# ===========================================================================
# Validate invariants integration
# ===========================================================================


class TestInvariantsAfterProducerPush:
    """Test that invariants hold after producer push triggers re-proposal."""

    def test_invariants_hold_after_push_with_cleanup(self, simple_tracker):
        """After push triggers re-propose and un-confirms stale reviewers, invariants hold."""
        tracker = simple_tracker

        # Full lifecycle: propose -> ACK -> confirm (both)
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        # Verify consensus
        assert "reviewer_code" in tracker._confirmed
        assert "coder" in tracker._confirmed

        # Producer pushes -- should clean up stale confirmations
        tracker.handle_producer_push("coder", "sha2")

        # Invariants should hold (no stale confirmed reviewers)
        violations = tracker.validate_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"

    def test_invariants_hold_after_push_no_changed_files(self, tracker):
        """Conservative invalidation (no changed_files) should keep invariants clean."""
        # Coder proposes
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/auth.py"], commit_sha="sha1"),
        )

        # All reviewers ACK
        ack_producer(tracker, "reviewer_code", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "reviewer_contract", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "tester", "coder", artifact_references=["src/auth.py"])

        # Push without changed_files -> conservative invalidation
        tracker.handle_producer_push("coder", "sha2")

        violations = tracker.validate_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"

    def test_invariants_hold_after_full_recovery_from_push(self, simple_tracker):
        """Full recovery: push -> re-ACK -> re-confirm -> invariants hold."""
        tracker = simple_tracker

        # Setup: propose, ACK, confirm
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        # Push disrupts consensus
        tracker.handle_producer_push("coder", "sha2")

        # Recovery: re-ACK, re-confirm
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        violations = tracker.validate_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"


# ===========================================================================
# Edge cases
# ===========================================================================


class TestProducerPushEdgeCases:
    """Test edge cases for producer push handling."""

    def test_multiple_sequential_pushes_increment_version(self, simple_tracker):
        """Each push increments version."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert tracker.matrix.get_proposal_version("coder") == 1

        # First push -> v2
        result1 = tracker.handle_producer_push("coder", "sha2")
        assert result1["version"] == 2

        # Second push -> v3
        result2 = tracker.handle_producer_push("coder", "sha3")
        assert result2["version"] == 3

        # Third push -> v4
        result3 = tracker.handle_producer_push("coder", "sha4")
        assert result3["version"] == 4

        assert tracker.matrix.get_proposal_version("coder") == 4

    def test_push_after_nack_but_before_re_propose_is_noop(self, simple_tracker):
        """After NACK, producer is WORKING -> push is no-op."""
        tracker = simple_tracker

        # Propose, get NACKed -> producer transitions to WORKING
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bad code")

        # Producer is now WORKING due to NACK
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING

        # Push is a no-op since producer is WORKING
        result = tracker.handle_producer_push("coder", "sha2")
        assert result["status"] == "no_op"

    def test_push_with_no_previous_artifacts_uses_commit_sha(self, simple_tracker):
        """When no previous artifacts exist, commit SHA is used as artifact."""
        tracker = simple_tracker

        # Propose with artifacts, then clear them to simulate edge case
        tracker.handle_propose("coder", make_proposal(artifacts=["src/main.py"], commit_sha="sha1"))

        # Manually clear the stored artifacts to simulate edge case
        tracker._proposal_artifacts["coder"] = []

        # Push without changed_files and no stored artifacts
        result = tracker.handle_producer_push("coder", "sha2", changed_files=None)

        assert result["auto_re_propose"] is True
        # Should still succeed -- uses commit_sha as last-resort artifact

    def test_push_updates_proposal_commit_sha(self, simple_tracker):
        """After push, the stored commit SHA should be updated."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert tracker._proposal_commit_shas.get("coder") == "sha1"

        tracker.handle_producer_push("coder", "sha2")
        assert tracker._proposal_commit_shas.get("coder") == "sha2"

    def test_push_clears_confirmed_status_of_producer(self, simple_tracker):
        """Producer's confirmed status is cleared on re-propose via push."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")

        assert "coder" in tracker._confirmed

        tracker.handle_producer_push("coder", "sha2")

        # Producer should no longer be confirmed
        assert "coder" not in tracker._confirmed

    def test_push_with_changed_files_uses_them_as_artifacts(self, simple_tracker):
        """When changed_files are provided, they become the new proposal artifacts."""
        tracker = simple_tracker

        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/old.py"], commit_sha="sha1"),
        )

        push_result = tracker.handle_producer_push(
            "coder", "sha2", changed_files=["src/new.py", "src/other.py"]
        )

        assert push_result["auto_re_propose"] is True
        # The stored artifacts should now be the changed_files
        assert tracker._proposal_artifacts["coder"] == ["src/new.py", "src/other.py"]


# ===========================================================================
# Multi-producer scenarios
# ===========================================================================


class TestMultiProducerPush:
    """Test producer push in multi-producer graph."""

    def test_push_only_affects_pushing_producer(self, tracker):
        """Push from coder should not affect tester's proposal state."""
        # Both producers propose
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        tracker.handle_propose(
            "tester",
            make_proposal(
                summary="Tests",
                artifacts=["tests/test_main.py"],
                commit_sha="test_sha1",
            ),
        )

        # ACK both
        ack_producer(tracker, "reviewer_code", "coder")
        ack_producer(tracker, "reviewer_code", "tester", artifact_references=["tests/test_main.py"])

        # Coder pushes -- only coder's ACKs should be affected
        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True

        # Tester's state should be unchanged
        assert tracker._producer_phases["tester"] == ConsensusPhase.PROPOSED
        assert tracker.matrix.get_proposal_version("tester") == 1

        # Coder's version incremented
        assert tracker.matrix.get_proposal_version("coder") == 2

    def test_tester_as_dual_role_push(self, tracker):
        """Tester is both producer and reviewer -- push as producer works."""
        # Tester proposes
        tracker.handle_propose(
            "tester",
            make_proposal(
                summary="Tests",
                artifacts=["tests/test_main.py"],
                commit_sha="test_sha1",
            ),
        )

        # reviewer_code ACKs tester
        ack_producer(
            tracker,
            "reviewer_code",
            "tester",
            artifact_references=["tests/test_main.py"],
        )

        # Tester pushes new commits (as producer)
        push_result = tracker.handle_producer_push("tester", "test_sha2")

        assert push_result["auto_re_propose"] is True
        assert push_result["version"] == 2


# ===========================================================================
# Full lifecycle with push
# ===========================================================================


class TestFullLifecycleWithPush:
    """End-to-end test: propose -> ACK -> push -> re-ACK -> confirm -> consensus."""

    def test_consensus_after_push_and_recovery(self, simple_tracker):
        """Full lifecycle with a push interruption still reaches consensus."""
        tracker = simple_tracker

        # v1: propose and ACK
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # Push disrupts things -> v2
        push_result = tracker.handle_producer_push("coder", "sha2")
        assert push_result["version"] == 2

        # Reviewer re-ACKs at v2
        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 2
        assert ack_result["fully_acked"] is True

        # Both confirm
        tracker.handle_confirmed("coder")
        result = tracker.handle_confirmed("reviewer_code")
        assert result["consensus_reached"] is True

    def test_multiple_pushes_then_consensus(self, simple_tracker):
        """Multiple pushes, then reviewer re-ACKs latest version and consensus is reached."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # Three sequential pushes
        tracker.handle_producer_push("coder", "sha2")
        tracker.handle_producer_push("coder", "sha3")
        push_result = tracker.handle_producer_push("coder", "sha4")
        assert push_result["version"] == 4

        # Reviewer only needs to ACK the latest version
        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 4
        assert ack_result["fully_acked"] is True

        # Confirm
        tracker.handle_confirmed("coder")
        result = tracker.handle_confirmed("reviewer_code")
        assert result["consensus_reached"] is True

        # Invariants clean
        violations = tracker.validate_invariants()
        assert len(violations) == 0
