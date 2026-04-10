"""Tests for handle_producer_push, check_auto_repropose, and excuse_producer.

Covers the auto re-proposal mechanism triggered when a producer pushes new
commits after having already proposed, the safety gates that control when
auto re-propose fires, producer excusal, and new ApprovalMatrix helpers.
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


def ack_producer(tracker, reviewer, producer, artifact_references=None, commit_sha=""):
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
    """Full implement-phase graph: coder reviewed by 3, tester reviewed by 1."""
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
    """Tracker with auto-repropose ENABLED and zero debounce for fast tests."""
    t = PeerConsensusTracker(
        "test-pipeline",
        implement_graph,
        cooldown_seconds=0,
        attestation_strictness=AttestationStrictness.RELAXED,
        auto_repropose_enabled=True,
        auto_repropose_debounce_seconds=0,
    )
    t.register_agent("coder")
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    t.register_agent("reviewer_contract")
    return t


@pytest.fixture
def simple_graph():
    """Minimal graph: one producer, one reviewer."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def simple_tracker(simple_graph):
    """Simple tracker with auto-repropose ENABLED and zero debounce."""
    t = PeerConsensusTracker(
        "test-pipeline",
        simple_graph,
        cooldown_seconds=0,
        attestation_strictness=AttestationStrictness.RELAXED,
        auto_repropose_enabled=True,
        auto_repropose_debounce_seconds=0,
    )
    t.register_agent("coder")
    t.register_agent("reviewer_code")
    return t


@pytest.fixture
def default_tracker(simple_graph):
    """Simple tracker with DEFAULT settings (auto-repropose OFF)."""
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
# Section 1: Producer Push with Auto-Repropose ENABLED
# ===========================================================================


class TestProducerPushAutoReproposeEnabled:
    """Producer push triggers auto re-proposal when enabled."""

    def test_push_in_proposed_phase_auto_re_proposes(self, tracker):
        """Push at sha2 after propose at sha1 -> auto_re_propose=True, version 2."""
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert tracker.matrix.get_proposal_version("coder") == 1

        # ACK from all reviewers at v1
        ack_producer(tracker, "reviewer_code", "coder")
        ack_producer(tracker, "reviewer_contract", "coder")
        ack_producer(tracker, "tester", "coder")

        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        assert push_result["auto_trigger"] == "auto_push"
        assert push_result["version"] == 2
        assert push_result["status"] == "proposed"
        assert "reviewers" in push_result
        assert "invalidated_reviewers" in push_result

    def test_push_in_confirmed_phase_auto_re_proposes(self, simple_tracker):
        """Producer confirms (fully ACKed), then pushes -> auto re-propose."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        assert tracker._producer_phases["coder"] == ConsensusPhase.CONFIRMED

        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        assert push_result["version"] == 2
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED

    def test_scoped_invalidation_matching_changed_files(self, tracker):
        """ACK with artifact_refs=["src/main.py"], push with same -> ACK invalidated."""
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/main.py", "src/utils.py"], commit_sha="sha1"),
        )

        # reviewer_code ACKs referencing src/main.py
        tracker.handle_ack("reviewer_code", "coder", {"artifact_references": ["src/main.py"]})
        # reviewer_contract ACKs referencing src/utils.py
        tracker.handle_ack("reviewer_contract", "coder", {"artifact_references": ["src/utils.py"]})
        # tester ACKs referencing src/main.py
        tracker.handle_ack("tester", "coder", {"artifact_references": ["src/main.py"]})

        push_result = tracker.handle_producer_push("coder", "sha2", changed_files=["src/main.py"])

        assert push_result["auto_re_propose"] is True
        invalidated = set(push_result["invalidated_reviewers"])
        assert "reviewer_code" in invalidated
        assert "tester" in invalidated
        # reviewer_contract ACKed utils.py, which was NOT changed
        assert "reviewer_contract" not in invalidated

    def test_scoped_invalidation_non_matching_changed_files(self, tracker):
        """ACK with artifact_refs=["src/main.py"], push changes ["test/foo.py"] -> no overlap."""
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/main.py"], commit_sha="sha1"),
        )

        ack_producer(tracker, "reviewer_code", "coder", artifact_references=["src/main.py"])
        ack_producer(tracker, "reviewer_contract", "coder", artifact_references=["src/main.py"])
        ack_producer(tracker, "tester", "coder", artifact_references=["src/main.py"])

        # Push changes to a file that doesn't overlap with proposed artifacts
        # or any ACK artifacts -> the auto_repropose overlap check may skip it
        # BUT the overlap check also checks ACK artifact_refs. Since the ACKs
        # reference src/main.py and changed_files is test/foo.py, there IS
        # no overlap with either proposed artifacts OR ACK artifacts.
        # However, because proposed artifacts include src/main.py and
        # changed_files is test/foo.py, the overlap set is empty.
        # Then it checks ACK artifacts: src/main.py vs test/foo.py -> also empty.
        # So check_auto_repropose returns False -> push is "skipped".
        push_result = tracker.handle_producer_push("coder", "sha2", changed_files=["test/foo.py"])

        assert push_result["status"] == "skipped"
        assert push_result["auto_re_propose"] is False

    def test_conservative_invalidation_no_changed_files(self, tracker):
        """Push without changed_files -> ALL ACKs invalidated."""
        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/auth.py"], commit_sha="sha1"),
        )
        ack_producer(tracker, "reviewer_code", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "reviewer_contract", "coder", artifact_references=["src/auth.py"])
        ack_producer(tracker, "tester", "coder", artifact_references=["src/auth.py"])

        push_result = tracker.handle_producer_push("coder", "sha2")

        assert push_result["auto_re_propose"] is True
        invalidated = set(push_result["invalidated_reviewers"])
        assert "reviewer_code" in invalidated
        assert "reviewer_contract" in invalidated
        assert "tester" in invalidated

    def test_state_transitions_after_push(self, simple_tracker):
        """After push: producer -> PROPOSED, confirmed cleared, version incremented."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")

        assert "coder" in tracker._confirmed
        assert tracker._producer_phases["coder"] == ConsensusPhase.CONFIRMED

        tracker.handle_producer_push("coder", "sha2")

        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED
        assert "coder" not in tracker._confirmed
        assert tracker.matrix.get_proposal_version("coder") == 2

    def test_multiple_sequential_pushes_increment_version(self, simple_tracker):
        """Each push with unique SHA increments version."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        assert tracker.matrix.get_proposal_version("coder") == 1

        result1 = tracker.handle_producer_push("coder", "sha2")
        assert result1["version"] == 2

        result2 = tracker.handle_producer_push("coder", "sha3")
        assert result2["version"] == 3

        result3 = tracker.handle_producer_push("coder", "sha4")
        assert result3["version"] == 4

        assert tracker.matrix.get_proposal_version("coder") == 4


# ===========================================================================
# Section 2: Producer Push NO-OP Cases
# ===========================================================================


class TestProducerPushNoOp:
    """Cases where push does NOT trigger re-proposal."""

    def test_push_in_working_phase_is_noop(self, tracker):
        """Producer in WORKING phase -> no-op."""
        result = tracker.handle_producer_push("coder", "sha1")

        assert result["status"] == "no_op"
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
# Section 3: Auto-Repropose Safety Mechanisms
# ===========================================================================


class TestAutoReproposeSafety:
    """check_auto_repropose safety gates."""

    def test_feature_flag_off_default(self, default_tracker):
        """Default tracker has auto_repropose_enabled=False -> skipped."""
        tracker = default_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        result = tracker.handle_producer_push("coder", "sha2")

        assert result["status"] == "skipped"
        assert result["auto_re_propose"] is False
        assert "feature flag OFF" in result["reason"]

    def test_same_commit_sha_skipped(self, simple_tracker):
        """Propose at sha1, push at sha1 -> skipped, reason mentions unchanged."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))

        result = tracker.handle_producer_push("coder", "sha1")

        assert result["status"] == "skipped"
        assert "unchanged" in result["reason"].lower()

    def test_debounce_window_active(self):
        """Set debounce=600, push once (succeeds), push again -> skipped."""
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = PeerConsensusTracker(
            "test-pipeline",
            graph,
            cooldown_seconds=0,
            attestation_strictness=AttestationStrictness.RELAXED,
            auto_repropose_enabled=True,
            auto_repropose_debounce_seconds=600,
        )
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))

        # First push succeeds (no prior auto-repropose timestamp)
        result1 = tracker.handle_producer_push("coder", "sha2")
        assert result1["auto_re_propose"] is True

        # Second push within debounce window -> skipped
        result2 = tracker.handle_producer_push("coder", "sha3")
        assert result2["status"] == "skipped"
        assert "Debounce" in result2["reason"]

    def test_max_counter_exceeded(self):
        """Set max_auto_repropose=1, push once, push again -> skipped."""
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        tracker = PeerConsensusTracker(
            "test-pipeline",
            graph,
            cooldown_seconds=0,
            attestation_strictness=AttestationStrictness.RELAXED,
            auto_repropose_enabled=True,
            auto_repropose_debounce_seconds=0,
            max_auto_repropose=1,
        )
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))

        # First push succeeds (count goes to 1)
        result1 = tracker.handle_producer_push("coder", "sha2")
        assert result1["auto_re_propose"] is True

        # Second push -> max exceeded (count=1 >= max=1)
        result2 = tracker.handle_producer_push("coder", "sha3")
        assert result2["status"] == "skipped"
        assert "Max" in result2["reason"]

    def test_no_overlap_with_proposed_or_ack_artifacts(self, simple_tracker):
        """Propose with artifacts=["src/main.py"], push changed_files=["totally_unrelated.py"]
        and no ACKs -> skipped."""
        tracker = simple_tracker

        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/main.py"], commit_sha="sha1"),
        )
        # No ACKs recorded, so no ACK artifact overlap either.

        result = tracker.handle_producer_push(
            "coder", "sha2", changed_files=["totally_unrelated.py"]
        )

        assert result["status"] == "skipped"
        assert "don't overlap" in result["reason"].lower() or "overlap" in result["reason"].lower()

    def test_overlap_with_ack_artifacts_but_not_proposed_artifacts(self, simple_tracker):
        """ACK has artifact_refs=["test.py"], push changed_files=["test.py"]
        but proposed artifacts=["src/main.py"] -> should trigger because
        changed_files overlap with ACK artifacts."""
        tracker = simple_tracker

        tracker.handle_propose(
            "coder",
            make_proposal(artifacts=["src/main.py"], commit_sha="sha1"),
        )
        # Reviewer ACKs referencing test.py (different from proposed artifacts)
        tracker.handle_ack("reviewer_code", "coder", {"artifact_references": ["test.py"]})

        result = tracker.handle_producer_push("coder", "sha2", changed_files=["test.py"])

        # Should trigger because test.py overlaps with ACK artifact_refs
        assert result["auto_re_propose"] is True
        assert result["auto_trigger"] == "auto_push"


# ===========================================================================
# Section 4: Confirm Guard Integration After Push
# ===========================================================================


class TestConfirmGuardAfterPush:
    """Confirm guards properly reject stale state after producer push."""

    def test_reviewer_confirm_rejected_with_stale_ack(self, simple_tracker):
        """ACK at v1, push invalidates -> confirm rejected."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        tracker.handle_producer_push("coder", "sha2")

        # Reviewer tries to confirm without re-ACKing -> rejected
        with pytest.raises(ValueError, match="hasn't reviewed"):
            tracker.handle_confirmed("reviewer_code")

    def test_reviewer_re_acks_at_new_version_then_confirms(self, simple_tracker):
        """ACK at v1, push -> v2, reviewer re-ACKs at v2 -> confirm succeeds."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        tracker.handle_producer_push("coder", "sha2")

        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 2

        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] in ("confirmed", "partially_confirmed")

    def test_unresolved_nack_blocks_confirm(self, simple_tracker):
        """NACK without re-propose -> confirm rejected."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bugs found")

        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] == "pending_acks"
        assert "unresolved_nacks" in result


# ===========================================================================
# Section 5: excuse_producer()
# ===========================================================================


class TestExcuseProducer:
    """Tests for excuse_producer: removing a non-delivering producer."""

    def test_excuse_non_delivering_producer(self, tracker):
        """excuse_producer("coder") -> removes edges, status=excused."""
        result = tracker.excuse_producer("coder", reason="non-delivering")

        assert result["status"] == "excused"
        assert result["role"] == "coder"
        assert result["reason"] == "non-delivering"
        assert "affected_reviewers" in result
        affected = result["affected_reviewers"]
        # coder was reviewed by reviewer_code, reviewer_contract, tester
        assert "reviewer_code" in affected
        assert "reviewer_contract" in affected
        assert "tester" in affected

    def test_excuse_non_producer_raises(self, tracker):
        """excuse_producer("reviewer_code") -> ValueError (not a producer before excusal)."""
        # reviewer_code is only a reviewer, not a producer
        # (unless it has edges pointing to it as producer)
        # In implement_graph: reviewer_code reviews coder and tester but is not itself a producer
        # Actually: reviewer_contract reviews coder only, so reviewer_contract is only a reviewer
        with pytest.raises(ValueError, match="not a producer"):
            tracker.excuse_producer("reviewer_contract")

    def test_after_excuse_reviewers_can_confirm(self, tracker):
        """After excusing coder, reviewers no longer need to have reviewed coder."""
        # Tester proposes and gets ACKed
        tracker.handle_propose(
            "tester",
            make_proposal(summary="Tests", artifacts=["tests/test.py"], commit_sha="tsha1"),
        )
        ack_producer(tracker, "reviewer_code", "tester", artifact_references=["tests/test.py"])

        # Excuse coder entirely -- reviewers no longer need coder's deliverable
        tracker.excuse_producer("coder", reason="non-delivering")

        # Now reviewer_code only needs to have reviewed tester (which it has)
        result = tracker.handle_confirmed("reviewer_code")
        assert result["status"] in ("confirmed", "partially_confirmed")

    def test_excuse_clears_producer_state(self, tracker):
        """excuse_producer clears _producer_phases, _confirmed, _proposal_artifacts, etc."""
        # Setup: coder proposes and gets confirmed
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        # tester must also propose to pass global zero-proposal guard (#1648)
        tracker.handle_propose("tester", make_proposal(commit_sha="sha2"))
        ack_producer(tracker, "reviewer_code", "coder")
        ack_producer(tracker, "reviewer_contract", "coder")
        ack_producer(tracker, "tester", "coder")
        tracker.handle_confirmed("coder")

        assert "coder" in tracker._confirmed
        assert "coder" in tracker._producer_phases
        assert "coder" in tracker._proposal_artifacts
        assert "coder" in tracker._proposal_commit_shas

        tracker.excuse_producer("coder", reason="dropping")

        assert "coder" not in tracker._confirmed
        assert "coder" not in tracker._producer_phases
        assert "coder" not in tracker._proposal_artifacts
        assert "coder" not in tracker._proposal_commit_shas


# ===========================================================================
# Section 6: ApprovalMatrix Helper Tests
# ===========================================================================


class TestApprovalMatrixHelpers:
    """Tests for new ApprovalMatrix methods."""

    def test_get_nack_entries_for(self, simple_tracker):
        """NACK a producer -> get_nack_entries_for returns [(reviewer, entry)]."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bad code")

        entries = tracker.matrix.get_nack_entries_for("coder")
        assert len(entries) == 1
        reviewer_role, entry = entries[0]
        assert reviewer_role == "reviewer_code"
        assert entry.state == ApprovalState.NACKED
        assert entry.reason == "bad code"

    def test_has_unresolved_nacks_as_producer_current_version(self, simple_tracker):
        """NACK at current version -> has_unresolved_nacks_as_producer returns True."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bugs")

        assert tracker.matrix.has_unresolved_nacks_as_producer("coder") is True

    def test_has_unresolved_nacks_as_producer_old_version(self, simple_tracker):
        """NACK at v1, re-propose to v2 -> has_unresolved_nacks returns False."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        nack_producer(tracker, "reviewer_code", "coder", reason="bugs")

        # Re-propose at v2
        tracker.handle_re_propose(
            "coder",
            make_proposal(summary="Fixed", commit_sha="sha2"),
            changed_artifacts=["src/main.py"],
        )

        # NACK was at v1, current version is now v2 -> unresolved=False
        assert tracker.matrix.has_unresolved_nacks_as_producer("coder") is False

    def test_get_latest_review_versions(self, tracker):
        """ACK at v2, NACK at v1 -> correct versions returned."""
        # Coder proposes v1
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))

        # reviewer_code NACKs coder at v1
        nack_producer(tracker, "reviewer_code", "coder", reason="bad")

        # Tester proposes v1
        tracker.handle_propose(
            "tester",
            make_proposal(summary="Tests", artifacts=["tests/t.py"], commit_sha="tsha1"),
        )

        # reviewer_code ACKs tester at v1
        ack_producer(tracker, "reviewer_code", "tester", artifact_references=["tests/t.py"])

        # Coder re-proposes at v2
        tracker.handle_re_propose(
            "coder",
            make_proposal(summary="Fixed", commit_sha="sha2"),
            changed_artifacts=["src/main.py"],
        )

        # reviewer_code ACKs coder at v2
        ack_producer(tracker, "reviewer_code", "coder")

        versions = tracker.matrix.get_latest_review_versions("reviewer_code")
        assert versions["coder"] == 2
        assert versions["tester"] == 1

    def test_ack_commit_sha_stored(self, simple_tracker):
        """record_ack with commit_sha -> stored in entry.ack_commit_sha."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))

        version = tracker.matrix.get_proposal_version("coder")
        entry = tracker.matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/main.py"],
            commit_sha="ack_sha_123",
        )

        assert entry.ack_commit_sha == "ack_sha_123"

        # Also verify via get_entry
        fetched = tracker.matrix.get_entry("reviewer_code", "coder")
        assert fetched.ack_commit_sha == "ack_sha_123"


# ===========================================================================
# Section 7: validate_invariants Integration
# ===========================================================================


class TestInvariantsAfterProducerPush:
    """Invariants hold after producer push triggers re-proposal."""

    def test_invariants_hold_after_push_with_cleanup(self, simple_tracker):
        """Push -> un-confirm stale -> invariants pass."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        assert "reviewer_code" in tracker._confirmed
        assert "coder" in tracker._confirmed

        tracker.handle_producer_push("coder", "sha2")

        violations = tracker.validate_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"

    def test_full_recovery_from_push_invariants_hold(self, simple_tracker):
        """Push -> re-ACK -> confirm -> invariants hold."""
        tracker = simple_tracker

        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        # Push disrupts consensus
        tracker.handle_producer_push("coder", "sha2")

        # Recovery
        ack_producer(tracker, "reviewer_code", "coder")
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")

        violations = tracker.validate_invariants()
        assert len(violations) == 0, f"Unexpected violations: {violations}"


# ===========================================================================
# Section 8: Full Lifecycle with Push
# ===========================================================================


class TestFullLifecycleWithPush:
    """End-to-end scenarios: propose -> ACK -> push -> re-ACK -> confirm."""

    def test_end_to_end_propose_ack_push_re_ack_confirm(self, simple_tracker):
        """Full consensus after push interruption."""
        tracker = simple_tracker

        # v1: propose and ACK
        tracker.handle_propose("coder", make_proposal(commit_sha="sha1"))
        ack_producer(tracker, "reviewer_code", "coder")

        # Push disrupts -> v2
        push_result = tracker.handle_producer_push("coder", "sha2")
        assert push_result["version"] == 2
        assert push_result["auto_trigger"] == "auto_push"

        # Reviewer re-ACKs at v2
        ack_result = ack_producer(tracker, "reviewer_code", "coder")
        assert ack_result["version"] == 2
        assert ack_result["fully_acked"] is True

        # Both confirm
        tracker.handle_confirmed("coder")
        result = tracker.handle_confirmed("reviewer_code")
        assert result["consensus_reached"] is True

    def test_multiple_pushes_then_consensus(self, simple_tracker):
        """Multiple version bumps, final ACK, confirm -> consensus."""
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
