"""Tests for the conditional-ACK path (issue #1998).

Conditional ACKs let a reviewer approve a proposal while attaching an
obligation that a human must perform at merge time — e.g. a ``git mv``
that agents cannot push through the gateway. These tests cover:

- Schema validation on ``ReviewPayload``
- Persistence + scoping on ``ApprovalMatrix``
- Carry-through via ``PeerConsensusTracker.handle_ack``
- Rendering of the Pre-merge Obligations section in the PR body
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalMatrix, ApprovalState
from attestation_schemas import ReviewPayload
from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# --- Schema -----------------------------------------------------------------


class TestReviewPayloadCondition:
    def test_ack_accepts_condition(self):
        payload = ReviewPayload(
            verdict="ACK",
            artifact_references=["src/a.py"],
            pre_merge_condition="git mv legacy/x new/x before merge",
        )
        assert payload.pre_merge_condition.startswith("git mv")

    def test_ack_defaults_to_no_condition(self):
        payload = ReviewPayload(verdict="ACK", artifact_references=["src/a.py"])
        assert payload.pre_merge_condition == ""

    def test_nack_rejects_condition(self):
        # A conditional NACK is nonsensical — NACK already blocks the
        # producer, so there's nothing to defer.
        with pytest.raises(ValueError, match="only valid on ACK"):
            ReviewPayload(
                verdict="NACK",
                artifact_references=["src/a.py"],
                reason="broken",
                pre_merge_condition="do something",
            )


# --- Matrix ----------------------------------------------------------------


@pytest.fixture
def matrix_graph():
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


class TestApprovalMatrixCondition:
    def test_record_ack_stores_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="human must git mv X Y",
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == "human must git mv X Y"

    def test_whitespace_only_condition_normalized_to_empty(self, matrix_graph):
        """Whitespace-only conditions are stripped at the source (#1998 review)."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="   ",
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == ""
        # Should not appear in active conditions either.
        assert matrix.get_pre_merge_conditions() == []

    def test_nack_clears_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="obligation",
        )
        matrix.record_nack(
            "reviewer_code",
            "coder",
            version=1,
            reason="changed my mind",
            artifact_refs=["src/a.py"],
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == ""

    def test_invalidate_ack_clears_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="obligation",
        )
        matrix.invalidate_ack("reviewer_code", "coder")
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.PENDING
        assert entry.pre_merge_condition == ""

    def test_get_pre_merge_conditions_returns_current(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="A",
        )
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            # No condition on this ACK — should not appear in the list.
        )
        conditions = matrix.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["reviewer"] == "reviewer_code"
        assert conditions[0]["producer"] == "coder"
        assert conditions[0]["condition"] == "A"
        assert conditions[0]["version"] == 1

    def test_get_pre_merge_conditions_skips_stale(self, matrix_graph):
        """Condition recorded against version 1 vanishes after re-propose."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")  # v1
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="A",
        )
        matrix.record_proposal("coder")  # v2 — supersedes v1
        # The ACK on v1 is now stale — the reviewer has not re-asserted
        # the obligation on v2, so we must not render it as an active
        # obligation.
        assert matrix.get_pre_merge_conditions() == []

    def test_round_trip_persistence(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="mv thing",
        )
        data = matrix.to_dict()
        restored = ApprovalMatrix.from_dict(data, matrix_graph)
        entry = restored.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == "mv thing"


# --- Tracker ---------------------------------------------------------------


class TestTrackerCondition:
    def test_handle_ack_persists_condition(self, matrix_graph):
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")

        tracker.handle_propose(
            "coder",
            {
                "summary": "impl",
                "artifacts": ["src/a.py"],
                "commit_sha": "abc123",
            },
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "git mv X Y before merge",
            },
        )
        assert result["status"] == "acked"
        assert result["pre_merge_condition"] == "git mv X Y before merge"

        conditions = tracker.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["condition"] == "git mv X Y before merge"

    def test_handle_ack_whitespace_condition_excluded_from_event_and_result(self, matrix_graph):
        """Whitespace-only condition should not appear in the return value or
        event data — it must be consistent with the matrix normalization."""
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")

        tracker.handle_propose(
            "coder",
            {
                "summary": "impl",
                "artifacts": ["src/a.py"],
                "commit_sha": "abc123",
            },
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "   ",
            },
        )
        # Whitespace-only condition should be treated as no condition.
        assert "pre_merge_condition" not in result
        assert tracker.get_pre_merge_conditions() == []

    def test_handle_ack_without_condition_is_unchanged(self, matrix_graph):
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        assert "pre_merge_condition" not in result
        assert tracker.get_pre_merge_conditions() == []


# --- PR body rendering -----------------------------------------------------


class TestPrBodyRendering:
    def test_section_rendered_when_conditions_exist(self, matrix_graph):
        tracker = PeerConsensusTracker("pipeline-X", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "git mv legacy/x new/x",
            },
        )

        # Patch the tracker lookup to return our in-memory tracker, since
        # the module-level registry uses its own dict that may be empty
        # in some test environments.
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            from routes import pipelines as p

            section = p._build_pre_merge_obligations_section("pipeline-X")

        assert "Pre-merge Obligations" in section
        assert "reviewer_code" in section
        assert "git mv legacy/x new/x" in section

    def test_section_empty_when_no_tracker(self):
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            from routes import pipelines as p

            assert p._build_pre_merge_obligations_section("missing-pipeline") == ""

    def test_section_empty_when_no_conditions(self, matrix_graph):
        tracker = PeerConsensusTracker("pipeline-Y", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            from routes import pipelines as p

            assert p._build_pre_merge_obligations_section("pipeline-Y") == ""
