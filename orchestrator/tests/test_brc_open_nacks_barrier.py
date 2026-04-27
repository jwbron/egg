"""Tests for the multi-reviewer NACK aggregation barrier (#2142).

Producer must aggregate findings from every NACKing reviewer before
re-proposing.  The orchestrator enforces this with a structured
``open_nacks_blocked`` rejection that inlines every unresolved NACK so
the producer can address them all in one re-propose.

Companion stale-version rejection: ACK / NACK against a superseded
version is rejected with the producer's current proposal snapshot
inlined so the reviewer can re-review without a separate fetch.
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
def multi_reviewer_graph():
    """Three-reviewer implement graph — sufficient to exercise multi-NACK."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_security", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def tracker(multi_reviewer_graph):
    t = PeerConsensusTracker("test-pipeline-2142", multi_reviewer_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("reviewer_code")
    t.register_agent("reviewer_security")
    t.register_agent("reviewer_contract")
    return t


def _propose(tracker: PeerConsensusTracker, version_label: str) -> None:
    tracker.handle_propose(
        "coder",
        {
            "summary": (
                f"Proposal {version_label} with substantive content describing "
                f"the work, tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc1234",
        },
    )


def _nack(tracker: PeerConsensusTracker, reviewer: str, reason: str) -> None:
    tracker.handle_nack(
        reviewer,
        "coder",
        {
            "artifact_references": ["a.py"],
            "reason": (
                f"{reason} — full reason text long enough to satisfy the "
                f"≥50 char content gate enforced by _validate_brc_content."
            ),
        },
    )


def _re_propose(tracker: PeerConsensusTracker, version_label: str) -> dict:
    return tracker.handle_re_propose(
        "coder",
        {
            "summary": (
                f"Re-propose {version_label}: fixed all blocking findings "
                f"from prior reviewer NACKs and re-ran tests successfully."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc5678",
        },
        changed_artifacts=["a.py"],
    )


class TestOpenNacksBarrier:
    """The barrier rejects a multi-reviewer re_propose with NACKs inlined."""

    def test_single_reviewer_nack_does_not_trigger_barrier(self, tracker):
        """Single NACK case proceeds without rejection — only multi-NACK is racy."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "blocking issue in a.py:42")

        result = _re_propose(tracker, "v2")

        # No barrier — version advanced.
        assert result.get("status") != "open_nacks_blocked"
        assert result.get("version") == 2

    def test_multi_reviewer_nack_first_re_propose_blocked(self, tracker):
        """Two reviewers NACK -> first re_propose is rejected with both inline."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "SQL injection at a.py:42")
        _nack(tracker, "reviewer_security", "missing auth check at a.py:60")

        result = _re_propose(tracker, "v2")

        assert result["status"] == "open_nacks_blocked"
        assert result["current_version"] == 1
        assert set(result["nacking_reviewers"]) == {"reviewer_code", "reviewer_security"}
        assert len(result["nacks"]) == 2
        # NACK content is inlined verbatim — no separate fetch needed.
        reasons = " ".join(n["reason"] for n in result["nacks"])
        assert "SQL injection" in reasons
        assert "missing auth check" in reasons

    def test_retry_after_barrier_succeeds(self, tracker):
        """Once the producer has been notified, a retry advances the version."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "blocking 1")
        _nack(tracker, "reviewer_security", "blocking 2")

        first = _re_propose(tracker, "v2")
        assert first["status"] == "open_nacks_blocked"

        second = _re_propose(tracker, "v2-retry")
        assert second.get("status") != "open_nacks_blocked"
        assert second["version"] == 2

    def test_new_nack_arriving_during_grace_re_blocks(self, tracker):
        """A NACK landing after the first rejection re-blocks the next attempt."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "first finding")
        _nack(tracker, "reviewer_security", "second finding")

        first = _re_propose(tracker, "v2")
        assert first["status"] == "open_nacks_blocked"

        # Third reviewer NACKs after the first rejection — producer hasn't
        # been informed of this one yet, so the next re_propose must
        # re-block surfacing all three NACKs together.
        _nack(tracker, "reviewer_contract", "third finding")

        second = _re_propose(tracker, "v2-retry")
        assert second["status"] == "open_nacks_blocked"
        assert set(second["nacking_reviewers"]) == {
            "reviewer_code",
            "reviewer_security",
            "reviewer_contract",
        }
        assert any("third finding" in n["reason"] for n in second["nacks"])

        # Now the producer has been informed of all three — retry succeeds.
        third = _re_propose(tracker, "v2-final")
        assert third.get("status") != "open_nacks_blocked"
        assert third["version"] == 2

    def test_barrier_resets_after_version_advance(self, tracker):
        """A successful re_propose clears the watermark for the next round."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "v1 finding A")
        _nack(tracker, "reviewer_security", "v1 finding B")

        # Pay the barrier toll once, advance to v2.
        assert _re_propose(tracker, "v2-attempt-1")["status"] == "open_nacks_blocked"
        result_v2 = _re_propose(tracker, "v2")
        assert result_v2["version"] == 2

        # Two new NACKs land at v2.  Watermark must have reset, otherwise
        # the v1 watermark would suppress the v2 barrier.
        _nack(tracker, "reviewer_code", "v2 finding A")
        _nack(tracker, "reviewer_security", "v2 finding B")

        result_v3 = _re_propose(tracker, "v3-attempt")
        assert result_v3["status"] == "open_nacks_blocked"
        assert result_v3["current_version"] == 2


class TestStaleVersionGuard:
    """Stale-version guard for ACK and NACK after a producer re-proposes."""

    def test_ack_against_stale_version_raises(self, tracker):
        """Reviewer ACKing a superseded version triggers the version-match guard."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "v1 finding")
        _nack(tracker, "reviewer_security", "v1 finding")
        # Pay the barrier toll then advance to v2.
        assert _re_propose(tracker, "v2-attempt")["status"] == "open_nacks_blocked"
        _re_propose(tracker, "v2")

        with pytest.raises(ValueError, match="version mismatch"):
            tracker.handle_ack(
                "reviewer_contract",
                "coder",
                {
                    "artifact_references": ["a.py"],
                    "reason": "looks fine on v1 review of substantive size — over 50 chars",
                    "ack_version": 1,
                },
            )

    def test_nack_against_stale_version_raises(self, tracker):
        """Reviewer NACKing a superseded version triggers the version-match guard."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "v1 finding")
        _nack(tracker, "reviewer_security", "v1 finding")
        assert _re_propose(tracker, "v2-attempt")["status"] == "open_nacks_blocked"
        _re_propose(tracker, "v2")

        with pytest.raises(ValueError, match="version mismatch"):
            tracker.handle_nack(
                "reviewer_contract",
                "coder",
                {
                    "artifact_references": ["a.py"],
                    "reason": "still buggy on v1 — substantive analysis well over 50 chars long",
                    "nack_version": 1,
                },
            )

    def test_current_proposal_snapshot_returns_artifacts_and_commit(self, tracker):
        """Snapshot helper returns the inline data the rejection envelope needs."""
        _propose(tracker, "v1")
        snap = tracker.get_current_proposal_snapshot("coder")
        assert snap["producer"] == "coder"
        assert snap["version"] == 1
        assert snap["artifacts"] == ["a.py"]
        assert snap["commit_sha"] == "abc1234"


class TestProposeBarrierBypass:
    """Regression: barrier must fire from handle_propose too, not just handle_re_propose.

    The signal-handler path (``handle_consensus_propose_signal``) routes through
    ``handle_propose`` whenever the payload omits ``changed_artifacts``.  Before
    #2142 second pass, the barrier was wired only into ``handle_re_propose``,
    so producers could bypass the multi-NACK barrier by sending a CONSENSUS_PROPOSE
    without a delta.  These tests pin the barrier into ``handle_propose`` directly.
    """

    def test_propose_barrier_fires_with_multi_nack(self, tracker):
        """handle_propose returns open_nacks_blocked when ≥2 reviewers have NACKed."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "issue at a.py:42")
        _nack(tracker, "reviewer_security", "issue at a.py:60")

        # Re-propose via handle_propose (no changed_artifacts) — must still block.
        result = tracker.handle_propose(
            "coder",
            {
                "summary": (
                    "Re-propose without delta: addressed all reviewer findings "
                    "and re-ran the full test suite."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "deadbee",
            },
        )

        assert result["status"] == "open_nacks_blocked"
        assert result["current_version"] == 1
        assert set(result["nacking_reviewers"]) == {"reviewer_code", "reviewer_security"}
        # NACK reasons inlined verbatim — producer can address all in one go.
        assert len(result["nacks"]) == 2

    def test_propose_advances_after_barrier_toll(self, tracker):
        """After the barrier informs the producer once, the next handle_propose advances."""
        _propose(tracker, "v1")
        _nack(tracker, "reviewer_code", "first finding")
        _nack(tracker, "reviewer_security", "second finding")

        first = tracker.handle_propose(
            "coder",
            {
                "summary": (
                    "First retry after multi-NACK — barrier expected to block "
                    "this initial attempt and inline the open NACKs."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "deadbee",
            },
        )
        assert first["status"] == "open_nacks_blocked"

        second = tracker.handle_propose(
            "coder",
            {
                "summary": (
                    "Second retry once the producer has been informed — barrier "
                    "should not fire and the version should advance to 2."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "cafef00",
            },
        )
        assert second.get("status") != "open_nacks_blocked"
        assert second.get("version") == 2

    def test_propose_barrier_skips_at_v0(self, tracker):
        """First-ever proposal at v0 must never hit the barrier (no NACKs possible)."""
        # No prior _propose call — tracker is at v0 for coder.
        result = tracker.handle_propose(
            "coder",
            {
                "summary": (
                    "Initial proposal — substantive content describing the "
                    "first-pass implementation work and the tests run."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "abc1234",
            },
        )
        assert result.get("status") != "open_nacks_blocked"
        assert result.get("version") == 1
