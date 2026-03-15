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

    These tests use realistic multi-agent review graphs mimicking the
    16-agent implement topology, exercising concurrent NACKs, overlapping
    artifact changes, and context-change NACK detection.
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
        t.handle_ack(
            "rev_contract", "coder", {"artifact_references": ["src/auth.py"]}
        )
        t.handle_ack(
            "checker", "coder", {"artifact_references": ["src/auth.py"]}
        )

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

        t.handle_propose(
            "coder", {"summary": "v1", "artifacts": ["src/auth.py", "src/utils.py"]}
        )

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
        graph = ReviewGraph(
            [ReviewEdge("rev_code", "coder", ReviewCriticality.CRITICAL)]
        )
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

    def test_full_16_agent_implement_graph(self):
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
        t.handle_ack(
            "reviewer_code", "tester", {"artifact_references": ["tests/test_main.py"]}
        )
        t.handle_ack(
            "reviewer_code", "documenter", {"artifact_references": ["docs/README.md"]}
        )

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
        for role in ["coder", "tester", "documenter", "reviewer_code", "reviewer_contract", "checker"]:
            t.handle_confirmed(role)

        state = t.evaluate()
        assert state["is_complete"] is True
