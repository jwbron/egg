"""Integration tests for BRC peer consensus protocol.

Tests the full Broadcast-Review-Converge lifecycle using
PeerConsensusTracker directly (no Redis dependency).
"""

import sys
from pathlib import Path

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalState
from peer_consensus import (
    PeerConsensusTracker,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph, get_default_implement_graph


@pytest.fixture
def simple_graph():
    """Simple 2-producer, 2-reviewer graph for testing."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_code", "tester", ReviewCriticality.ADVISORY),
        ]
    )


@pytest.fixture
def tracker(simple_graph):
    """Create a tracker with the simple graph."""
    t = PeerConsensusTracker("test-pipeline", simple_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("tester")
    t.register_agent("reviewer_code")
    t.register_agent("checker")
    return t


class TestHappyPath:
    """Test the full BRC happy path: propose -> ACK -> confirm."""

    def test_full_lifecycle(self, tracker):
        """All producers propose, all reviewers ACK, all confirm."""
        # Coder proposes
        result = tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "attestation": {
                    "commit_shas": ["abc123"],
                    "files_changed": ["src/auth.py"],
                    "test_summary": "All pass",
                    "risk_considered": "None",
                },
            },
        )
        assert result["status"] == "proposed"
        assert result["version"] == 1

        # Tester proposes (producer side)
        result = tracker.handle_propose(
            "tester",
            {
                "summary": "Added tests",
                "artifacts": ["tests/test_auth.py"],
                "attestation": {
                    "tests_written": 5,
                    "tests_run": 5,
                    "coverage_delta": "+10%",
                    "edge_cases": ["null input"],
                    "concern_considered": "None",
                },
            },
        )
        assert result["status"] == "proposed"

        # reviewer_code ACKs coder
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "attestation": {
                    "files_reviewed": ["src/auth.py"],
                    "issues_found": 0,
                    "issues_resolved": 0,
                    "risk_considered": "None",
                },
                "artifact_references": ["src/auth.py"],
            },
        )
        assert result["status"] == "acked"

        # checker ACKs coder
        result = tracker.handle_ack(
            "checker",
            "coder",
            {
                "attestation": {
                    "lint_results": "clean",
                    "type_results": "pass",
                    "test_results": "20/20 pass",
                    "auto_fixes": [],
                    "remaining_warnings": [],
                },
                "artifact_references": ["src/auth.py"],
            },
        )
        assert result["fully_acked"] is True

        # reviewer_code ACKs tester
        tracker.handle_ack(
            "reviewer_code",
            "tester",
            {
                "attestation": {
                    "files_reviewed": ["tests/test_auth.py"],
                    "issues_found": 0,
                    "issues_resolved": 0,
                    "risk_considered": "None",
                },
                "artifact_references": ["tests/test_auth.py"],
            },
        )

        # All confirm
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("tester")  # Both producer and reviewer confirmed
        tracker.handle_confirmed("reviewer_code")
        result = tracker.handle_confirmed("checker")

        assert result["consensus_reached"] is True

        state = tracker.evaluate()
        assert state["is_complete"] is True
        assert state["protocol"] == "brc"


class TestNackAndRePropose:
    """Test NACK -> re-propose with scoped re-evaluation."""

    def test_nack_re_propose_cycle(self, tracker):
        # Coder proposes
        tracker.handle_propose(
            "coder",
            {
                "summary": "v1",
                "artifacts": ["src/auth.py", "src/utils.py"],
            },
        )

        # reviewer_code ACKs
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/utils.py"],
            },
        )

        # checker NACKs
        result = tracker.handle_nack(
            "checker",
            "coder",
            {
                "artifact_references": ["src/auth.py"],
                "reason": "SQL injection in auth.py:42",
            },
        )
        assert result["status"] == "nacked"
        assert result["revision_count"] == 1

        # Coder re-proposes with fix to auth.py only
        result = tracker.handle_re_propose(
            "coder",
            {
                "summary": "Fixed SQL injection",
                "artifacts": ["src/auth.py"],
            },
            changed_artifacts=["src/auth.py"],
        )

        # reviewer_code's ACK on utils.py should stand (no overlap)
        # But we need checker to re-review
        assert result["version"] == 2

    def test_nack_reason_required(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["src/a.py"]})
        with pytest.raises(ValueError, match="reason"):
            tracker.handle_nack(
                "reviewer_code",
                "coder",
                {
                    "artifact_references": ["src/a.py"],
                    "reason": "",  # Empty reason should fail
                },
            )


class TestCommitmentDevices:
    """Test cooldown, flip-flop lockout, and bounded revision rounds."""

    def test_flip_flop_lockout(self):
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test", graph, cooldown_seconds=0, max_flip_flops=2)
        t.register_agent("coder")
        t.register_agent("reviewer_code")

        # First proposal + withdrawal
        t.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        t.handle_withdraw("coder", "Changed approach")

        # Second proposal + withdrawal -- should trigger lockout
        t.handle_propose("coder", {"summary": "v2", "artifacts": ["a.py"]})
        result = t.handle_withdraw("coder", "Changed again")
        assert result["status"] == "locked_out"
        assert result["needs_escalation"] is True

    def test_withdrawal_requires_reason(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        with pytest.raises(ValueError, match="reason"):
            tracker.handle_withdraw("coder", "")

    def test_bounded_revision_rounds(self):
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test", graph, cooldown_seconds=0, max_revision_rounds=2)
        t.register_agent("coder")
        t.register_agent("reviewer_code")

        # First round
        t.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        t.handle_nack(
            "reviewer_code", "coder", {"artifact_references": ["a.py"], "reason": "bug 1"}
        )

        # Second round
        t.handle_propose("coder", {"summary": "v2", "artifacts": ["a.py"]})
        result = t.handle_nack(
            "reviewer_code", "coder", {"artifact_references": ["a.py"], "reason": "bug 2"}
        )
        assert result["needs_escalation"] is True


class TestTimeoutHandling:
    """Test consensus timeout with critical vs advisory roles."""

    def test_timeout_critical_blocker_escalates(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        # Only reviewer_code ACKs, checker doesn't
        tracker.handle_ack("reviewer_code", "coder", {"artifact_references": ["a.py"]})

        result = tracker.handle_timeout()
        assert result["action"] == "escalate"
        assert "critical_blockers" in result

    def test_timeout_advisory_only_proceeds(self):
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.ADVISORY),
            ]
        )
        t = PeerConsensusTracker("test", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("reviewer_code")
        t.register_agent("checker")

        t.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        t.handle_ack("reviewer_code", "coder", {"artifact_references": ["a.py"]})
        # checker hasn't ACKed but is advisory

        result = t.handle_timeout()
        assert result["action"] == "proceed_with_notification"


class TestAgentCrash:
    """Test agent crash handling."""

    def test_reviewer_crash_sole_reviewer(self):
        graph = ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test", graph)
        t.register_agent("coder")
        t.register_agent("reviewer_code")

        result = t.handle_agent_crash("reviewer_code")
        assert result["action"] == "escalate"

    def test_producer_crash_preserves_proposal(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        result = tracker.handle_agent_crash("coder")
        assert result["action"] == "continue"


class TestDelphiOrdering:
    """Test Delphi-style proposal visibility."""

    def test_reviewer_without_evaluation_hidden(self, tracker):
        """Verify the matrix tracks evaluation status correctly."""
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})

        # Before review, reviewer hasn't evaluated
        assert not tracker.matrix.has_reviewed("reviewer_code", "coder")

        # After ACK, reviewer has evaluated
        tracker.handle_ack("reviewer_code", "coder", {"artifact_references": ["a.py"]})
        assert tracker.matrix.has_reviewed("reviewer_code", "coder")


class TestReviewGraph:
    """Test review graph construction and queries."""

    def test_default_implement_graph(self):
        graph = get_default_implement_graph()
        assert "reviewer_code" in graph.reviewers_for("coder")
        assert "reviewer_contract" in graph.reviewers_for("coder")
        assert "checker" in graph.reviewers_for("coder")
        assert "tester" in graph.reviewers_for("coder")
        assert graph.is_dual_role("tester")
        assert graph.is_producer("coder")
        assert graph.is_reviewer("reviewer_code")
        assert not graph.is_reviewer("coder")

    def test_graph_serialization(self):
        graph = get_default_implement_graph()
        data = graph.to_dict()
        restored = ReviewGraph.from_dict(data)
        assert len(restored.edges) == len(graph.edges)


class TestApprovalMatrix:
    """Test approval matrix operations."""

    def test_scoped_re_evaluation(self):
        graph = ReviewGraph(
            [
                ReviewEdge("rev1", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("rev2", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)

        matrix.record_proposal("coder")
        matrix.record_ack("rev1", "coder", 1, artifact_refs=["auth.py"])
        matrix.record_ack("rev2", "coder", 1, artifact_refs=["utils.py"])

        # Re-proposal changes auth.py -- should invalidate rev1's ACK but not rev2's
        invalidated = matrix.invalidate_overlapping_acks("coder", ["auth.py"])
        assert "rev1" in invalidated
        assert "rev2" not in invalidated

        # rev1 should be pending, rev2 should still be acked
        assert matrix.get_entry("rev1", "coder").state == ApprovalState.PENDING
        assert matrix.get_entry("rev2", "coder").state == ApprovalState.ACKED


class TestAttestationSchemas:
    """Test attestation validation."""

    def test_proposal_rejects_empty_artifacts(self):
        from attestation_schemas import ProposalPayload

        with pytest.raises(ValueError, match="artifact"):
            ProposalPayload(summary="test", artifacts=[])

    def test_review_rejects_empty_references(self):
        from attestation_schemas import ReviewPayload

        with pytest.raises(ValueError, match="artifact"):
            ReviewPayload(verdict="ACK", artifact_references=[])

    def test_nack_requires_reason(self):
        from attestation_schemas import ReviewPayload

        with pytest.raises(ValueError, match="reason"):
            ReviewPayload(verdict="NACK", artifact_references=["a.py"], reason="")

    def test_strict_validation(self):
        from attestation_schemas import AttestationStrictness, validate_attestation

        with pytest.raises(ValueError, match="commit SHA"):
            validate_attestation("coder", {}, AttestationStrictness.STRICT, is_producer=True)

    def test_relaxed_validation(self):
        from attestation_schemas import AttestationStrictness, validate_attestation

        # Relaxed should not raise
        validate_attestation("coder", {}, AttestationStrictness.RELAXED, is_producer=True)


class TestScaledReEvaluation:
    """Test scoped re-evaluation at roster scale (6+ agents).

    These tests use realistic multi-agent review graphs (up to 7 roles)
    exercising concurrent NACKs, overlapping artifact changes, and
    context-change NACK detection.
    """

    @pytest.fixture
    def six_agent_graph(self):
        """6-agent graph: 2 producers, 4 reviewers with cross-review."""
        return ReviewGraph(
            [
                ReviewEdge("rev_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("rev_contract", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("rev_code", "tester", ReviewCriticality.CRITICAL),
                ReviewEdge("rev_contract", "tester", ReviewCriticality.ADVISORY),
                ReviewEdge("checker", "tester", ReviewCriticality.ADVISORY),
            ]
        )

    @pytest.fixture
    def six_agent_tracker(self, six_agent_graph):
        t = PeerConsensusTracker(
            "test-scaled", six_agent_graph, cooldown_seconds=0, max_revision_rounds=2
        )
        for role in ["coder", "tester", "rev_code", "rev_contract", "checker"]:
            t.register_agent(role)
        return t

    def test_concurrent_nacks_different_producers(self, six_agent_tracker):
        """Two reviewers NACK two different producers simultaneously.

        Each producer should only need re-review from its NACKing reviewer.
        """
        t = six_agent_tracker

        # Both producers propose
        t.handle_propose("coder", {"summary": "v1", "artifacts": ["src/auth.py"]})
        t.handle_propose("tester", {"summary": "tests v1", "artifacts": ["tests/test_auth.py"]})

        # rev_code NACKs coder, rev_contract NACKs tester (concurrent)
        r1 = t.handle_nack(
            "rev_code", "coder", {"artifact_references": ["src/auth.py"], "reason": "bug in auth"}
        )
        r2 = t.handle_nack(
            "rev_contract",
            "tester",
            {"artifact_references": ["tests/test_auth.py"], "reason": "missing edge case"},
        )

        assert r1["status"] == "nacked"
        assert r2["status"] == "nacked"

        # Coder's rev_contract and checker ACKs should still be possible
        # (they weren't affected by rev_code's NACK of coder)
        t.handle_ack("rev_contract", "coder", {"artifact_references": ["src/auth.py"]})
        t.handle_ack("checker", "coder", {"artifact_references": ["src/auth.py"]})

        # Coder re-proposes — only rev_code needs to re-review
        result = t.handle_re_propose(
            "coder",
            {"summary": "fixed auth", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )
        # rev_contract and checker ACKed auth.py, which is the changed artifact,
        # so they get invalidated
        assert result["version"] == 2

    def test_overlapping_artifact_invalidation(self, six_agent_tracker):
        """Producer re-proposes with artifacts that overlap some reviewers' ACKs."""
        t = six_agent_tracker

        t.handle_propose(
            "coder", {"summary": "v1", "artifacts": ["src/auth.py", "src/utils.py", "src/db.py"]}
        )

        # Different reviewers ACK referencing different files
        t.handle_ack("rev_code", "coder", {"artifact_references": ["src/auth.py"]})
        t.handle_ack("rev_contract", "coder", {"artifact_references": ["src/db.py"]})
        t.handle_ack("checker", "coder", {"artifact_references": ["src/utils.py"]})

        # Re-propose changes only auth.py
        invalidated = t.matrix.invalidate_overlapping_acks("coder", ["src/auth.py"])

        # Only rev_code's ACK should be invalidated (referenced auth.py)
        assert "rev_code" in invalidated
        assert "rev_contract" not in invalidated
        assert "checker" not in invalidated

        # Verify states
        assert t.matrix.get_entry("rev_code", "coder").state == ApprovalState.PENDING
        assert t.matrix.get_entry("rev_contract", "coder").state == ApprovalState.ACKED
        assert t.matrix.get_entry("checker", "coder").state == ApprovalState.ACKED

    def test_cascading_re_propose_preserves_unaffected_acks(self, six_agent_tracker):
        """In a 6-agent graph, one producer's re-proposal should not
        invalidate ACKs for a different producer."""
        t = six_agent_tracker

        # Both producers propose
        t.handle_propose("coder", {"summary": "code v1", "artifacts": ["src/auth.py"]})
        t.handle_propose("tester", {"summary": "tests v1", "artifacts": ["tests/test_auth.py"]})

        # All reviewers ACK both producers
        t.handle_ack("rev_code", "coder", {"artifact_references": ["src/auth.py"]})
        t.handle_ack("rev_contract", "coder", {"artifact_references": ["src/auth.py"]})
        t.handle_ack("checker", "coder", {"artifact_references": ["src/auth.py"]})
        t.handle_ack("rev_code", "tester", {"artifact_references": ["tests/test_auth.py"]})

        # Coder gets NACKed by checker and re-proposes
        t.handle_nack(
            "checker", "coder", {"artifact_references": ["src/auth.py"], "reason": "lint fail"}
        )
        t.handle_re_propose(
            "coder",
            {"summary": "code v2", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )

        # Tester's ACKs should be completely unaffected
        assert t.matrix.get_entry("rev_code", "tester").state == ApprovalState.ACKED

    def test_concurrent_nack_and_re_propose_race(self, six_agent_tracker):
        """One reviewer NACKs while another ACKs the same producer,
        then producer re-proposes. ACK should be preserved if its
        artifacts weren't changed."""
        t = six_agent_tracker

        t.handle_propose("coder", {"summary": "v1", "artifacts": ["src/auth.py", "src/utils.py"]})

        # rev_code ACKs (referencing utils.py only)
        t.handle_ack("rev_code", "coder", {"artifact_references": ["src/utils.py"]})
        # rev_contract ACKs (referencing auth.py)
        t.handle_ack("rev_contract", "coder", {"artifact_references": ["src/auth.py"]})
        # checker NACKs (referencing auth.py)
        t.handle_nack(
            "checker",
            "coder",
            {"artifact_references": ["src/auth.py"], "reason": "injection"},
        )

        # Coder re-proposes, changing only auth.py
        result = t.handle_re_propose(
            "coder",
            {"summary": "v2", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )

        # rev_code's ACK on utils.py should be preserved
        assert t.matrix.get_entry("rev_code", "coder").state == ApprovalState.ACKED
        # rev_contract's ACK on auth.py should be invalidated
        assert t.matrix.get_entry("rev_contract", "coder").state == ApprovalState.PENDING
        assert "rev_contract" in result["invalidated_reviewers"]
        assert "rev_code" not in result["invalidated_reviewers"]

    def test_context_change_nack_not_escalated(self):
        """Reviewer NACKs citing file A, producer fixes, reviewer ACKs,
        new commit changes file B, reviewer NACKs citing file B.
        Despite hitting revision count, needs_escalation should be False."""
        graph = ReviewGraph([ReviewEdge("rev_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test-ctx", graph, cooldown_seconds=0, max_revision_rounds=2)
        t.register_agent("coder")
        t.register_agent("rev_code")

        # Round 1: propose, NACK on file A
        t.handle_propose("coder", {"summary": "v1", "artifacts": ["src/auth.py"]})
        r1 = t.handle_nack(
            "rev_code",
            "coder",
            {"artifact_references": ["src/auth.py"], "reason": "bug in auth"},
        )
        assert r1["revision_count"] == 1
        assert r1["needs_escalation"] is False
        assert r1["context_change"] is False

        # Round 2: fix and re-propose, reviewer ACKs
        t.handle_re_propose(
            "coder",
            {"summary": "v2 - fixed auth", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )
        t.handle_ack("rev_code", "coder", {"artifact_references": ["src/auth.py"]})

        # New tester commit changes file B — coder re-proposes with B
        t.handle_re_propose(
            "coder",
            {"summary": "v3 - includes tester changes", "artifacts": ["src/auth.py", "src/db.py"]},
            changed_artifacts=["src/db.py"],
        )

        # Reviewer NACKs citing file B (context change)
        r2 = t.handle_nack(
            "rev_code",
            "coder",
            {"artifact_references": ["src/db.py"], "reason": "issue in db.py"},
        )
        # Revision count is 2 (>= max_revision_rounds), but it's a context change
        assert r2["revision_count"] == 2
        assert r2["context_change"] is True
        assert r2["needs_escalation"] is False

    def test_full_implement_graph(self):
        """Use the default implement graph (7 roles) and run a full
        propose/review/re-propose cycle to verify no invalidation bugs."""
        graph = get_default_implement_graph()
        t = PeerConsensusTracker("test-full", graph, cooldown_seconds=0)

        # Register all roles
        for role in graph.all_roles():
            t.register_agent(role)

        # Producers propose
        t.handle_propose(
            "coder",
            {"summary": "Implementation", "artifacts": ["src/main.py", "src/utils.py"]},
        )
        t.handle_propose(
            "tester",
            {"summary": "Tests", "artifacts": ["tests/test_main.py"]},
        )
        t.handle_propose(
            "documenter",
            {"summary": "Docs", "artifacts": ["docs/README.md"]},
        )

        # All reviewers ACK coder
        t.handle_ack("reviewer_code", "coder", {"artifact_references": ["src/main.py"]})
        t.handle_ack(
            "reviewer_contract", "coder", {"artifact_references": ["src/main.py", "src/utils.py"]}
        )
        t.handle_ack("checker", "coder", {"artifact_references": ["src/main.py", "src/utils.py"]})
        # tester (dual-role) ACKs coder
        t.handle_ack("tester", "coder", {"artifact_references": ["src/main.py"]})

        # reviewer_code ACKs tester and documenter
        t.handle_ack("reviewer_code", "tester", {"artifact_references": ["tests/test_main.py"]})
        t.handle_ack("reviewer_code", "documenter", {"artifact_references": ["docs/README.md"]})

        # Verify coder is fully acked
        assert t.matrix.is_fully_acked("coder")
        assert t.matrix.is_fully_acked("tester")
        assert t.matrix.is_fully_acked("documenter")

        # checker NACKs coder on utils.py
        t.handle_nack(
            "checker",
            "coder",
            {"artifact_references": ["src/utils.py"], "reason": "type error"},
        )

        assert not t.matrix.is_fully_acked("coder")

        # Coder re-proposes changing only utils.py
        result = t.handle_re_propose(
            "coder",
            {"summary": "Fixed utils", "artifacts": ["src/main.py", "src/utils.py"]},
            changed_artifacts=["src/utils.py"],
        )

        # reviewer_code ACKed main.py — should be preserved
        assert t.matrix.get_entry("reviewer_code", "coder").state == ApprovalState.ACKED
        # tester ACKed main.py — should be preserved
        assert t.matrix.get_entry("tester", "coder").state == ApprovalState.ACKED
        # reviewer_contract ACKed main.py AND utils.py — should be invalidated
        assert t.matrix.get_entry("reviewer_contract", "coder").state == ApprovalState.PENDING
        assert "reviewer_contract" in result["invalidated_reviewers"]

        # Tester and documenter ACKs should be completely unaffected
        assert t.matrix.get_entry("reviewer_code", "tester").state == ApprovalState.ACKED
        assert t.matrix.get_entry("reviewer_code", "documenter").state == ApprovalState.ACKED

        # checker re-reviews and ACKs (NACKing reviewer, needs to re-ACK)
        t.handle_ack("checker", "coder", {"artifact_references": ["src/utils.py"]})
        # reviewer_contract re-reviews and ACKs (invalidated, needs to re-ACK)
        t.handle_ack(
            "reviewer_contract", "coder", {"artifact_references": ["src/main.py", "src/utils.py"]}
        )
        # Preserved ACKs (reviewer_code, tester) are at old version — they need
        # to re-ACK at the new proposal version for is_fully_acked to pass
        t.handle_ack("reviewer_code", "coder", {"artifact_references": ["src/main.py"]})
        t.handle_ack("tester", "coder", {"artifact_references": ["src/main.py"]})

        # Now coder should be fully acked again
        assert t.matrix.is_fully_acked("coder")

        # All confirm
        for role in [
            "coder",
            "tester",
            "documenter",
            "reviewer_code",
            "reviewer_contract",
            "checker",
        ]:
            t.handle_confirmed(role)

        state = t.evaluate()
        assert state["is_complete"] is True


class TestTimeoutIdempotency:
    """Test that handle_timeout() is idempotent — second call returns cached result."""

    def test_handle_timeout_idempotent(self):
        """Calling handle_timeout() twice returns already_handled on second call."""
        graph = ReviewGraph([ReviewEdge("rev_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test-idem", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("rev_code")

        # Propose but don't ACK — creates a blocking edge
        t.handle_propose("coder", {"summary": "v1", "artifacts": ["src/main.py"]})

        result1 = t.handle_timeout()
        assert result1["action"] == "escalate"
        assert t.is_timeout_handled() is True

        result2 = t.handle_timeout()
        assert result2["action"] == "already_handled"
        assert result2["reason"] == "Timeout previously processed"

    def test_handle_timeout_idempotent_advisory(self):
        """Advisory-only timeout path is also idempotent."""
        graph = ReviewGraph([ReviewEdge("rev_code", "coder", ReviewCriticality.ADVISORY)])
        t = PeerConsensusTracker("test-idem-adv", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("rev_code")

        t.handle_propose("coder", {"summary": "v1", "artifacts": ["src/main.py"]})

        result1 = t.handle_timeout()
        assert result1["action"] == "proceed_with_notification"
        assert t.is_timeout_handled() is True

        result2 = t.handle_timeout()
        assert result2["action"] == "already_handled"


class TestAlternatingNackHardCap:
    """Test that alternating-file NACK patterns hit the hard cap."""

    def test_alternating_file_nacks_escalate_at_hard_cap(self):
        """Reviewer alternating NACKs between two files eventually
        triggers escalation at 3x max_revision_rounds."""
        graph = ReviewGraph([ReviewEdge("rev_code", "coder", ReviewCriticality.CRITICAL)])
        t = PeerConsensusTracker("test-hardcap", graph, cooldown_seconds=0, max_revision_rounds=2)
        t.register_agent("coder")
        t.register_agent("rev_code")

        files = ["src/auth.py", "src/db.py"]
        # Hard cap = max_revision_rounds * 3 = 6
        # Alternate NACKs on different files until we hit the cap
        for i in range(6):
            t.handle_propose("coder", {"summary": f"v{i + 1}", "artifacts": files})
            result = t.handle_nack(
                "rev_code",
                "coder",
                {
                    "artifact_references": [files[i % 2]],
                    "reason": f"issue in {files[i % 2]}",
                },
            )
            if i < 5:
                # Round 0: context_change=False but rev_count < max_revision_rounds
                # Rounds 1-4: context_change=True (alternating files) and rev_count < hard_cap
                assert result["needs_escalation"] is False, (
                    f"Unexpected escalation at round {i + 1}"
                )
            else:
                # At round 6 (rev_count=6 == hard_cap), escalation fires
                assert result["needs_escalation"] is True, f"Expected escalation at round {i + 1}"
                assert result["revision_count"] == 6


class TestWithdrawReProposalDeadlock:
    """Test fix for issue #1175: BRC consensus deadlock when proposal
    is withdrawn after partial reviewer confirmation.

    Reproduces the exact scenario: reviewer_agent_design confirms on v1,
    reviewer_refine NACKs v1, refiner re-proposes and eventually withdraws
    and re-submits. Without the fix, reviewer_agent_design stays confirmed
    on a stale version and the refiner can never reach is_fully_acked().
    """

    @pytest.fixture
    def refine_graph(self):
        """3-agent refine-phase graph matching the reproduction scenario."""
        return ReviewGraph(
            [
                ReviewEdge("reviewer_agent_design", "refiner", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_refine", "refiner", ReviewCriticality.CRITICAL),
            ]
        )

    @pytest.fixture
    def refine_tracker(self, refine_graph):
        t = PeerConsensusTracker("issue-1175", refine_graph, cooldown_seconds=0)
        t.register_agent("refiner")
        t.register_agent("reviewer_agent_design")
        t.register_agent("reviewer_refine")
        return t

    def test_withdraw_after_partial_confirm_unconfirms_stale_reviewer(self, refine_tracker):
        """Core deadlock scenario: after withdrawal + re-proposal,
        a reviewer who confirmed on v1 must be un-confirmed."""
        t = refine_tracker

        # v1: refiner proposes
        t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})

        # reviewer_agent_design ACKs and confirms on v1
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_agent_design")
        assert "reviewer_agent_design" in t._confirmed

        # reviewer_refine NACKs v1
        t.handle_nack(
            "reviewer_refine",
            "refiner",
            {"artifact_references": ["design.md"], "reason": "Missing error handling"},
        )

        # refiner re-proposes v2 (addresses NACK)
        t.handle_re_propose(
            "refiner",
            {"summary": "v2 - added error handling", "artifacts": ["design.md"]},
            changed_artifacts=["design.md"],
        )

        # reviewer_refine ACKs v2 and confirms
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_refine")

        # refiner withdraws (e.g., realized more changes needed)
        t.handle_withdraw("refiner", "Need to incorporate additional feedback")

        # refiner re-proposes v3 (new proposal after withdrawal)
        result = t.handle_propose("refiner", {"summary": "v3 - final", "artifacts": ["design.md"]})

        # The fix: reviewer_agent_design was already un-confirmed during
        # handle_re_propose(v2) because their ACK on design.md overlapped
        # the changed artifacts. By v3, they're already un-confirmed.
        assert "reviewer_agent_design" not in t._confirmed, (
            "Stale confirmed reviewer should have been un-confirmed"
        )

        # reviewer_refine confirmed on v2, now stale on v3 — un-confirmed here
        assert "reviewer_refine" not in t._confirmed
        assert "reviewer_refine" in result["stale_confirmed_reviewers"]

        # Now both reviewers can re-review and the cycle completes
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})

        # Now refiner should be fully ACKed
        assert t.matrix.is_fully_acked("refiner")

        # All can confirm
        t.handle_confirmed("refiner")
        t.handle_confirmed("reviewer_agent_design")
        result = t.handle_confirmed("reviewer_refine")
        assert result["consensus_reached"] is True

    def test_re_propose_after_withdraw_notifies_stale_reviewers(self, refine_tracker):
        """Verify stale_confirmed_reviewers is returned for notification."""
        t = refine_tracker

        # Quick setup: propose, both ACK and confirm, then withdraw and re-propose
        t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_agent_design")
        t.handle_confirmed("reviewer_refine")

        # Withdraw and re-propose
        t.handle_withdraw("refiner", "Revised approach needed")
        result = t.handle_propose("refiner", {"summary": "v3", "artifacts": ["design.md"]})

        # Both reviewers should be in the stale list
        stale = result["stale_confirmed_reviewers"]
        assert "reviewer_agent_design" in stale
        assert "reviewer_refine" in stale

    def test_no_stale_reviewers_on_first_proposal(self, refine_tracker):
        """First proposal should never have stale confirmed reviewers."""
        t = refine_tracker
        result = t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})
        assert result["stale_confirmed_reviewers"] == []

    def test_re_propose_via_changed_artifacts_also_unconfirms(self, refine_tracker):
        """handle_re_propose (with changed_artifacts) should also
        un-confirm stale reviewers, not just handle_propose."""
        t = refine_tracker

        t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_agent_design")

        # reviewer_refine NACKs
        t.handle_nack(
            "reviewer_refine",
            "refiner",
            {"artifact_references": ["design.md"], "reason": "issues"},
        )

        # Re-propose with changed artifacts
        result = t.handle_re_propose(
            "refiner",
            {"summary": "v2", "artifacts": ["design.md"]},
            changed_artifacts=["design.md"],
        )

        # reviewer_agent_design confirmed on v1, but v2 changed design.md
        # which overlaps their ACK — so they get both invalidated AND un-confirmed
        assert "reviewer_agent_design" not in t._confirmed
        assert "reviewer_agent_design" in result.get("stale_confirmed_reviewers", [])

    def test_producer_confirm_fails_without_re_review(self, refine_tracker):
        """Without re-review, producer cannot confirm after withdrawal."""
        t = refine_tracker

        t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_agent_design")
        t.handle_confirmed("reviewer_refine")

        # Withdraw and re-propose
        t.handle_withdraw("refiner", "Revised approach")
        t.handle_propose("refiner", {"summary": "v3", "artifacts": ["design.md"]})

        # Refiner should NOT be able to confirm (not fully ACKed on v3)
        # Returns pending_acks instead of raising ValueError (issue #1178)
        result = t.handle_confirmed("refiner")
        assert result["status"] == "pending_acks"

        # After both reviewers re-ACK, refiner can confirm
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("refiner")

    def test_nacked_then_confirmed_reviewer_is_unconfirmed_on_reproposal(self, refine_tracker):
        """A reviewer who NACKed and then confirmed must be un-confirmed
        when the producer withdraws and re-proposes.

        Regression test for: NACKED entries were not caught by
        _un_confirm_stale_reviewers, leaving the reviewer in _confirmed
        with a stale NACK and causing a deadlock.
        """
        t = refine_tracker

        # v1: refiner proposes
        t.handle_propose("refiner", {"summary": "v1", "artifacts": ["design.md"]})

        # reviewer_agent_design ACKs v1 and confirms
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_confirmed("reviewer_agent_design")

        # reviewer_refine NACKs v1 and confirms (has_reviewed returns True for NACK)
        t.handle_nack(
            "reviewer_refine",
            "refiner",
            {"artifact_references": ["design.md"], "reason": "Needs rework"},
        )
        t.handle_confirmed("reviewer_refine")
        assert "reviewer_refine" in t._confirmed

        # refiner withdraws and re-proposes v2
        t.handle_withdraw("refiner", "Addressing NACK feedback")
        result = t.handle_propose("refiner", {"summary": "v2", "artifacts": ["design.md"]})

        # Both reviewers must be un-confirmed — reviewer_refine had a stale NACK
        assert "reviewer_refine" not in t._confirmed, (
            "Reviewer with stale NACK should have been un-confirmed"
        )
        assert "reviewer_refine" in result["stale_confirmed_reviewers"]
        assert "reviewer_agent_design" not in t._confirmed

        # Both re-review, ACK, and confirm — no deadlock
        t.handle_ack("reviewer_agent_design", "refiner", {"artifact_references": ["design.md"]})
        t.handle_ack("reviewer_refine", "refiner", {"artifact_references": ["design.md"]})
        assert t.matrix.is_fully_acked("refiner")

        t.handle_confirmed("refiner")
        t.handle_confirmed("reviewer_agent_design")
        result = t.handle_confirmed("reviewer_refine")
        assert result["consensus_reached"] is True


class TestPrematureConfirmReturnsPending:
    """Test that handle_confirmed returns pending_acks instead of raising
    when a producer tries to confirm before being fully ACKed (issue #1178)."""

    def test_confirm_before_acked_returns_pending(self, tracker):
        """Producer confirming without full ACKs gets pending_acks, not ValueError."""
        # Coder proposes
        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented feature",
                "artifacts": ["src/feature.py"],
            },
        )

        # Only reviewer_code ACKs, but checker hasn't ACKed yet
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "attestation": {
                    "files_reviewed": ["src/feature.py"],
                    "issues_found": 0,
                    "issues_resolved": 0,
                    "risk_considered": "None",
                },
                "artifact_references": ["src/feature.py"],
            },
        )

        # Coder tries to confirm before checker ACKs — should return pending, not raise
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"
        assert "pending reviewers" in result["message"].lower()

        # Coder should NOT be in confirmed set
        assert "coder" not in tracker._confirmed

    def test_confirm_after_re_propose_invalidates_stale_acks(self, tracker):
        """After re-proposal un-confirms stale reviewers, premature confirm returns pending."""
        # Full happy path first: propose, ACK, but then re-propose
        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )
        tracker.handle_ack(
            "checker",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )

        # Coder re-proposes (invalidating stale ACKs)
        tracker.handle_re_propose(
            "coder",
            {"summary": "v2", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )

        # Now coder tries to confirm — should get pending_acks
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"


class TestReProposalGuard:
    """Test that re-proposing when fully ACKed is rejected (issue #1185)."""

    def test_propose_rejected_when_fully_acked(self, tracker):
        """Producer cannot re-propose after being fully ACKed."""
        # Coder proposes
        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )

        # Both reviewers ACK
        tracker.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )
        tracker.handle_ack(
            "checker", "coder", {"artifact_references": ["src/auth.py"]}
        )

        # Verify fully ACKed
        assert tracker.matrix.is_fully_acked("coder")

        # Re-proposing should raise ValueError
        with pytest.raises(ValueError, match="already fully ACKed"):
            tracker.handle_propose(
                "coder",
                {"summary": "v2", "artifacts": ["src/auth.py"]},
            )

    def test_re_propose_allowed_after_nack(self, tracker):
        """handle_re_propose is allowed after NACK (producer phase is WORKING)."""
        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )
        tracker.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )
        # Checker NACKs instead of ACKing
        tracker.handle_nack(
            "checker", "coder",
            {"artifact_references": ["src/auth.py"], "reason": "issues found"},
        )

        # Re-proposing after NACK should work (producer phase is WORKING)
        result = tracker.handle_re_propose(
            "coder",
            {"summary": "v2", "artifacts": ["src/auth.py"]},
            changed_artifacts=["src/auth.py"],
        )
        assert result["status"] == "proposed"


class TestReviewerCrashPendingAck:
    """Test reviewer crash with pending vs completed ACKs (issue #1185)."""

    def test_reviewer_crash_with_pending_ack_escalates(self):
        """Non-sole reviewer crash with pending ACK escalates to HITL."""
        # Use implement graph: coder has reviewers reviewer_code, reviewer_contract, checker, tester
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        t = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("reviewer_code")
        t.register_agent("checker")

        # Coder proposes
        t.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )

        # Only reviewer_code ACKs; checker hasn't reviewed yet
        t.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )

        # Checker crashes with pending review -> should escalate
        result = t.handle_agent_crash("checker")
        assert result["action"] == "escalate"
        assert "pending reviews" in result["reason"]
        assert "coder" in result["blocking_producers"]

    def test_reviewer_crash_with_completed_ack_continues(self):
        """Non-sole reviewer crash with completed ACK continues normally."""
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        t = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("reviewer_code")
        t.register_agent("checker")

        # Coder proposes
        t.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )

        # Both reviewers ACK
        t.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )
        t.handle_ack(
            "checker", "coder", {"artifact_references": ["src/auth.py"]}
        )

        # Checker crashes but already ACKed -> should continue
        result = t.handle_agent_crash("checker")
        assert result["action"] == "continue"
        assert result["crashed_role"] == "checker"


class TestExcuseReviewer:
    """Test excuse_reviewer unblocks consensus (issue #1185)."""

    def test_excuse_reviewer_unblocks_consensus(self):
        """Excusing a dead reviewer allows is_fully_acked to pass."""
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        t = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        t.register_agent("coder")
        t.register_agent("reviewer_code")
        t.register_agent("checker")

        # Coder proposes
        t.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )

        # Only reviewer_code ACKs
        t.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )

        # Not fully ACKed yet (checker hasn't reviewed)
        assert not t.matrix.is_fully_acked("coder")

        # Excuse checker (simulating HITL "Continue without" decision)
        result = t.excuse_reviewer("checker")
        assert result["status"] == "excused"
        assert "coder" in result["affected_producers"]

        # Now should be fully ACKed
        assert t.matrix.is_fully_acked("coder")

        # Checker should no longer be a reviewer in the graph
        assert not t.graph.is_reviewer("checker")


class TestRemoveEdge:
    """Test ReviewGraph.remove_edge() (issue #1185)."""

    def test_remove_edge_success(self):
        """remove_edge removes an existing edge and updates role sets."""
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        assert graph.is_reviewer("checker")
        assert len(graph.reviewers_for("coder")) == 2

        result = graph.remove_edge("checker", "coder")
        assert result is True
        assert len(graph.reviewers_for("coder")) == 1
        assert not graph.is_reviewer("checker")

    def test_remove_edge_not_found(self):
        """remove_edge returns False for nonexistent edge."""
        graph = ReviewGraph(
            [ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)]
        )
        result = graph.remove_edge("nonexistent", "coder")
        assert result is False

    def test_remove_edge_preserves_other_edges(self):
        """Removing one edge doesn't affect others."""
        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.ADVISORY),
                ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        graph.remove_edge("checker", "coder")
        # reviewer_code should still review both coder and tester
        assert graph.is_reviewer("reviewer_code")
        assert "reviewer_code" in graph.reviewers_for("coder")
        assert "reviewer_code" in graph.reviewers_for("tester")


class TestConfirmErrorListsPendingReviewers:
    """Test improved error message for premature confirm (issue #1185)."""

    def test_confirm_error_lists_pending_reviewers(self, tracker):
        """Premature confirm message lists which reviewers haven't ACKed."""
        tracker.handle_propose(
            "coder",
            {"summary": "v1", "artifacts": ["src/auth.py"]},
        )

        # Only reviewer_code ACKs
        tracker.handle_ack(
            "reviewer_code", "coder", {"artifact_references": ["src/auth.py"]}
        )

        # Try to confirm — message should list checker as pending
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"
        assert "checker" in result["message"]
        assert "Pending reviewers" in result["message"]
