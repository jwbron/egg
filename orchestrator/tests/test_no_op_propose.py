"""Tests for the generic no-op propose path (#3027).

A producer that finds it has no work in a slice submits a no-op proposal
(``no_changes_needed=true`` on the proposal payload) instead of an empty
proposal that a pure reviewer would NACK. The no-op:

* is submittable without ``artifacts`` / ``commit_sha`` (those validators
  are skipped) but requires ``no_changes_reason``;
* counts as "proposed" so the global zero-proposal guard clears;
* is treated as fully-acked / non-blocking by the matrix, and reviewers
  skip it (neither review nor NACK) so it cannot deadlock consensus;
* is **durable** — it rides the normal persisted ``CONSENSUS_PROPOSE``
  message, so it survives tracker reconstruction (unlike the in-memory
  #2581 pre-seed this replaced, which evaporated on restart / restart_agent).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from action_guards import check_confirm_guard
from approval_matrix import ApprovalMatrix
from attestation_schemas import ProposalPayload
from peer_consensus import (
    PeerConsensusTracker,
    _trackers,
    _trackers_lock,
    reconstruct_tracker_from_messages,
)
from review_graph import get_default_implement_graph

# --- ProposalPayload validator exemptions ---------------------------------


class TestProposalPayloadNoOp:
    def test_no_op_submittable_without_artifacts_or_commit(self):
        """A no-op proposal needs neither artifacts nor commit_sha."""
        payload = ProposalPayload(
            summary="documenter has no work in this code-only slice",
            no_changes_needed=True,
            no_changes_reason="no documented surface impacted by the coder's diff",
        )
        assert payload.no_changes_needed is True
        assert payload.artifacts == []
        assert payload.commit_sha == ""

    def test_no_op_requires_reason(self):
        with pytest.raises(ValueError, match="no_changes_reason"):
            ProposalPayload(summary="x" * 30, no_changes_needed=True)

    def test_no_op_blank_reason_rejected(self):
        with pytest.raises(ValueError, match="no_changes_reason"):
            ProposalPayload(summary="x" * 30, no_changes_needed=True, no_changes_reason="   ")

    def test_real_proposal_still_requires_artifacts_and_commit(self):
        """The exemption is scoped to no-op — a normal proposal is unchanged."""
        with pytest.raises(ValueError, match="artifact"):
            ProposalPayload(summary="x" * 30, commit_sha="abc123")
        with pytest.raises(ValueError, match="commit_sha"):
            ProposalPayload(summary="x" * 30, artifacts=["src/a.py"])


# --- ApprovalMatrix no-op semantics ---------------------------------------


class TestApprovalMatrixNoOp:
    def test_no_op_is_fully_acked_without_reviewer_acks(self):
        """A no-op proposal is fully-acked even for a producer WITH critical
        reviewers — proving the generic flag works for any role, not just the
        advisory-only documenter."""
        matrix = ApprovalMatrix(get_default_implement_graph())
        matrix.record_proposal("coder", no_changes=True)  # coder has critical reviewers
        assert matrix.is_no_changes_proposal("coder") is True
        assert matrix.is_fully_acked("coder") is True
        assert matrix.get_blocking_edges("coder") == []

    def test_real_proposal_after_no_op_clears_the_flag(self):
        matrix = ApprovalMatrix(get_default_implement_graph())
        matrix.record_proposal("coder", no_changes=True)
        assert matrix.is_fully_acked("coder") is True
        # A subsequent real proposal flips it back to needing reviews.
        matrix.record_proposal("coder", no_changes=False)
        assert matrix.is_no_changes_proposal("coder") is False
        assert matrix.is_fully_acked("coder") is False
        assert matrix.get_blocking_edges("coder")  # critical reviewers pending

    def test_version_zero_is_not_a_no_op(self):
        """No proposal at all is not a no-op (guards the version>0 check)."""
        matrix = ApprovalMatrix(get_default_implement_graph())
        assert matrix.is_no_changes_proposal("documenter") is False


# --- Tracker + reviewer behavior ------------------------------------------


class TestTrackerNoOp:
    def _implement_tracker(self) -> PeerConsensusTracker:
        graph = get_default_implement_graph()
        t = PeerConsensusTracker("test-3027", graph, cooldown_seconds=0)
        for role in graph.all_roles():
            t.register_agent(role)
        return t

    def test_documenter_no_op_propose_is_satisfied(self):
        t = self._implement_tracker()
        result = t.handle_propose(
            "documenter",
            {
                "summary": "no doc surface in this code-only slice",
                "no_changes_needed": True,
                "no_changes_reason": "no documented surface impacted",
            },
        )
        assert result["status"] == "proposed"
        assert t.matrix.is_no_changes_proposal("documenter") is True
        assert t.matrix.is_fully_acked("documenter") is True

    def test_reviewer_can_confirm_with_no_op_producer_present(self):
        """A pure reviewer's confirm guard is not blocked by a no-op producer
        it never reviewed — the wedge that #3027 fixes."""
        from routes.consensus import _has_pending_peer_proposals

        t = self._implement_tracker()
        # documenter does a no-op; reviewer_code reviews the documenter
        # (advisory) but must not be asked to review the no-op.
        t.handle_propose(
            "documenter",
            {
                "summary": "no doc surface here at all in this slice",
                "no_changes_needed": True,
                "no_changes_reason": "no documented surface impacted",
            },
        )
        has_pending, pending = _has_pending_peer_proposals(t, "reviewer_code")
        producers_pending = {p["producer"] for p in pending}
        assert "documenter" not in producers_pending

        # The confirm guard for reviewer_code excludes the no-op documenter
        # from its has-reviewed / zero-proposal checks.
        guard = check_confirm_guard("reviewer_code", t.graph, t.matrix, set())
        assert "documenter" not in guard.reason


class TestNoOpDurableAcrossReconstruction:
    """The #3027 regression: a no-op survives tracker reconstruction.

    The replaced #2581 pre-seed was an in-memory mutation that emitted no
    message, so it was lost on orchestrator restart / restart_agent and the
    producer was told to propose again (empty) → NACK → HITL deadlock. A no-op
    propose is a real persisted ``CONSENSUS_PROPOSE``, so replay restores it.
    """

    class _Msg:
        def __init__(self, message_type, from_role, metadata, phase="implement"):
            self.id = f"msg-{id(self)}"
            self.message_type = message_type
            self.from_role = from_role
            self.to_role = "all"
            self.body = ""
            self.metadata = metadata
            self.timestamp = datetime.now(UTC)
            self.phase = phase

    class _Store:
        def __init__(self, messages):
            self._messages = messages

        def get_messages(self, pipeline_id, *, limit=100):
            return list(self._messages)

    def setup_method(self):
        with _trackers_lock:
            _trackers.pop("test-3027-recon", None)

    def teardown_method(self):
        with _trackers_lock:
            _trackers.pop("test-3027-recon", None)

    def test_no_op_propose_replays_as_satisfied(self):
        graph = get_default_implement_graph()
        messages = [
            self._Msg(
                "CONSENSUS_PROPOSE",
                "documenter",
                metadata={
                    "payload": {
                        "summary": "no doc surface in this slice at all",
                        "no_changes_needed": True,
                        "no_changes_reason": "no documented surface impacted",
                    },
                    "version": 1,
                },
            ),
        ]
        store = self._Store(messages)
        tracker = reconstruct_tracker_from_messages(
            "test-3027-recon", graph, message_store=store, phase="implement"
        )
        assert tracker is not None
        # The no-op state was reconstructed from the persisted message — the
        # documenter is satisfied without any reviewer ACK, and no reviewer is
        # left holding a blocking edge against it.
        assert tracker.matrix.is_no_changes_proposal("documenter") is True
        assert tracker.matrix.is_fully_acked("documenter") is True
        assert tracker.matrix.get_blocking_edges("documenter") == []
