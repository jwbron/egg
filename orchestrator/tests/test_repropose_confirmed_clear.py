"""Tests for BRC producer confirmed status clearing on re-propose (issue #1411).

When a producer re-proposes (after NACK or withdrawal), their stale entry in
``self._confirmed`` must be discarded so that ``is_fully_confirmed()`` does not
return incorrect results during the window between re-proposal and
re-confirmation.
"""

import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph


@pytest.fixture
def simple_graph():
    """One producer, two reviewers."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_a", "producer", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_b", "producer", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def tracker(simple_graph):
    t = PeerConsensusTracker("test-pipeline", simple_graph, cooldown_seconds=0)
    t.register_agent("producer")
    t.register_agent("reviewer_a")
    t.register_agent("reviewer_b")
    return t


def _propose(tracker, role="producer", version=1):
    return tracker.handle_propose(role, {"summary": f"v{version}", "artifacts": ["a.py"]})


def _ack(tracker, reviewer, producer="producer"):
    return tracker.handle_ack(
        reviewer, producer, {"artifact_references": ["a.py"], "reason": "LGTM"}
    )


def _nack(tracker, reviewer, producer="producer"):
    return tracker.handle_nack(
        reviewer, producer, {"artifact_references": ["a.py"], "reason": "needs fix"}
    )


class TestReProposeClearsConfirmed:
    """Producer confirmed status must be cleared on re-propose."""

    def test_confirmed_cleared_on_handle_propose_after_full_cycle(self, tracker):
        """Scenario from issue #1411: producer confirms v1, then re-proposes v2."""
        # v1: propose -> ACK -> confirm
        _propose(tracker)
        _ack(tracker, "reviewer_a")
        _ack(tracker, "reviewer_b")
        result = tracker.handle_confirmed("producer")
        assert result["status"] == "confirmed"
        assert "producer" in tracker._confirmed

        # Simulate withdrawal (transition back to WORKING)
        tracker.handle_withdraw("producer", "reworking")

        # Re-propose v2 — confirmed status should be cleared
        _propose(tracker, version=2)
        assert "producer" not in tracker._confirmed

    def test_confirmed_cleared_on_handle_re_propose(self, tracker):
        """handle_re_propose delegates to _handle_propose_inner and should
        also clear confirmed status."""
        # v1: full cycle
        _propose(tracker)
        _ack(tracker, "reviewer_a")
        _ack(tracker, "reviewer_b")
        tracker.handle_confirmed("producer")
        assert "producer" in tracker._confirmed

        # NACK triggers re-propose path
        _nack(tracker, "reviewer_a")
        tracker.handle_re_propose(
            "producer",
            {"summary": "v2", "artifacts": ["a.py"]},
            changed_artifacts=["a.py"],
        )
        assert "producer" not in tracker._confirmed

    def test_confirmed_cleared_immediately_on_withdraw(self, tracker):
        """Confirmed status must be cleared by withdraw itself, not deferred to re-propose."""
        # v1: propose -> ACK -> confirm
        _propose(tracker)
        _ack(tracker, "reviewer_a")
        _ack(tracker, "reviewer_b")
        result = tracker.handle_confirmed("producer")
        assert result["status"] == "confirmed"
        assert "producer" in tracker._confirmed

        # Withdraw — confirmed must be cleared immediately, before any re-propose
        tracker.handle_withdraw("producer", "reworking")
        assert "producer" not in tracker._confirmed

    def test_consensus_not_reached_after_repropose(self, tracker):
        """After re-propose, consensus must not be reached until full re-review."""
        # v1: full consensus
        _propose(tracker)
        _ack(tracker, "reviewer_a")
        _ack(tracker, "reviewer_b")
        tracker.handle_confirmed("producer")
        tracker.handle_confirmed("reviewer_a")
        tracker.handle_confirmed("reviewer_b")

        state = tracker.evaluate()
        assert state["is_complete"] is True

        # Producer withdraws and re-proposes
        tracker.handle_withdraw("producer", "reworking")
        _propose(tracker, version=2)

        state = tracker.evaluate()
        assert state["is_complete"] is False
        assert "producer" not in tracker._confirmed
