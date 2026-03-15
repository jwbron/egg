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
from peer_consensus import PeerConsensusTracker, create_peer_consensus_tracker, remove_peer_consensus_tracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph, get_default_implement_graph


@pytest.fixture
def simple_graph():
    """Simple 2-producer, 2-reviewer graph for testing."""
    return ReviewGraph([
        ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
        ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
        ReviewEdge("reviewer_code", "tester", ReviewCriticality.ADVISORY),
    ])


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
        result = tracker.handle_propose("coder", {
            "summary": "Implemented auth",
            "artifacts": ["src/auth.py"],
            "attestation": {"commit_shas": ["abc123"], "files_changed": ["src/auth.py"], "test_summary": "All pass", "risk_considered": "None"},
        })
        assert result["status"] == "proposed"
        assert result["version"] == 1

        # Tester proposes (producer side)
        result = tracker.handle_propose("tester", {
            "summary": "Added tests",
            "artifacts": ["tests/test_auth.py"],
            "attestation": {"tests_written": 5, "tests_run": 5, "coverage_delta": "+10%", "edge_cases": ["null input"], "concern_considered": "None"},
        })
        assert result["status"] == "proposed"

        # reviewer_code ACKs coder
        result = tracker.handle_ack("reviewer_code", "coder", {
            "attestation": {"files_reviewed": ["src/auth.py"], "issues_found": 0, "issues_resolved": 0, "risk_considered": "None"},
            "artifact_references": ["src/auth.py"],
        })
        assert result["status"] == "acked"

        # checker ACKs coder
        result = tracker.handle_ack("checker", "coder", {
            "attestation": {"lint_results": "clean", "type_results": "pass", "test_results": "20/20 pass", "auto_fixes": [], "remaining_warnings": []},
            "artifact_references": ["src/auth.py"],
        })
        assert result["fully_acked"] is True

        # reviewer_code ACKs tester
        tracker.handle_ack("reviewer_code", "tester", {
            "attestation": {"files_reviewed": ["tests/test_auth.py"], "issues_found": 0, "issues_resolved": 0, "risk_considered": "None"},
            "artifact_references": ["tests/test_auth.py"],
        })

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
        tracker.handle_propose("coder", {
            "summary": "v1",
            "artifacts": ["src/auth.py", "src/utils.py"],
        })

        # reviewer_code ACKs
        tracker.handle_ack("reviewer_code", "coder", {
            "artifact_references": ["src/utils.py"],
        })

        # checker NACKs
        result = tracker.handle_nack("checker", "coder", {
            "artifact_references": ["src/auth.py"],
            "reason": "SQL injection in auth.py:42",
        })
        assert result["status"] == "nacked"
        assert result["revision_count"] == 1

        # Coder re-proposes with fix to auth.py only
        result = tracker.handle_re_propose("coder", {
            "summary": "Fixed SQL injection",
            "artifacts": ["src/auth.py"],
        }, changed_artifacts=["src/auth.py"])

        # reviewer_code's ACK on utils.py should stand (no overlap)
        # But we need checker to re-review
        assert result["version"] == 2

    def test_nack_reason_required(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["src/a.py"]})
        with pytest.raises(ValueError, match="reason"):
            tracker.handle_nack("reviewer_code", "coder", {
                "artifact_references": ["src/a.py"],
                "reason": "",  # Empty reason should fail
            })


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
        t.handle_nack("reviewer_code", "coder", {"artifact_references": ["a.py"], "reason": "bug 1"})

        # Second round
        t.handle_propose("coder", {"summary": "v2", "artifacts": ["a.py"]})
        result = t.handle_nack("reviewer_code", "coder", {"artifact_references": ["a.py"], "reason": "bug 2"})
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
        graph = ReviewGraph([
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("checker", "coder", ReviewCriticality.ADVISORY),
        ])
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
        graph = ReviewGraph([
            ReviewEdge("rev1", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("rev2", "coder", ReviewCriticality.CRITICAL),
        ])
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
