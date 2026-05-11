"""Tests for the pure-producer auto-ACK seed (#2581).

Covers ``ApprovalMatrix.seed_auto_ack_for_empty_pure_producers`` and the
delegating ``PeerConsensusTracker.seed_auto_ack_for_empty_pure_producers``
wrapper. The seed exists to prevent BRC consensus deadlock for slices
whose plan omits a producer role (e.g. a tester-only or documenter-only
slice): without it, CODER spawns with no work, its critical reviewers
have nothing to review, and consensus stalls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalMatrix, ApprovalState
from peer_consensus import PeerConsensusTracker
from review_graph import get_default_implement_graph


@pytest.fixture
def implement_graph():
    return get_default_implement_graph()


@pytest.fixture
def matrix(implement_graph):
    return ApprovalMatrix(implement_graph)


class TestSeedAutoAckEmptyPureProducers:
    """Direct tests against ``ApprovalMatrix``."""

    def test_documenter_only_slice_auto_acks_coder(self, matrix):
        """A documenter-only slice should auto-ACK CODER so its reviewers
        don't deadlock on an empty proposal."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert auto_acked == ["coder"]
        # CODER's consensus is fully satisfied after seeding — every
        # critical reviewer (including TESTER) has a seeded ACK at v1.
        assert matrix.is_fully_acked("coder") is True

    def test_tester_only_slice_auto_acks_coder_and_documenter(self, matrix):
        """A tester-only slice has neither a coder nor a documenter task —
        both pure producers should auto-ACK."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"tester"})
        assert auto_acked == ["coder", "documenter"]
        assert matrix.is_fully_acked("coder") is True
        # DOCUMENTER has no critical reviewers in the default implement
        # graph, so ``is_fully_acked`` returns True once any proposal is
        # recorded.
        assert matrix.is_fully_acked("documenter") is True

    def test_coder_only_slice_auto_acks_only_documenter(self, matrix):
        """A coder-only slice: DOCUMENTER (pure producer) auto-ACKs;
        TESTER is dual-role and is intentionally skipped so its reviewer
        responsibility for CODER stays active."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"coder"})
        assert auto_acked == ["documenter"]
        assert matrix.is_fully_acked("documenter") is True
        # TESTER not auto-ACKed — its producer-side proposal version is
        # still 0, so it isn't fully-ACKed.
        assert matrix.get_proposal_version("tester") == 0
        assert matrix.is_fully_acked("tester") is False

    def test_all_producers_present_is_noop(self, matrix):
        """When every producer has at least one task, seeding is a no-op."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers(
            {"coder", "tester", "documenter"}
        )
        assert auto_acked == []
        assert matrix.get_proposal_version("coder") == 0
        assert matrix.get_proposal_version("documenter") == 0

    def test_seeded_acks_carry_critical_reviewers(self, matrix):
        """The seeded proposal must satisfy every critical reviewer of
        the auto-ACKed producer, including dual-role reviewers like
        TESTER — that's what makes the empty proposal actually reach
        consensus instead of just being recorded."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        graph = matrix._graph
        for reviewer in graph.critical_reviewers_for("coder"):
            entry = matrix.get_entry(reviewer, "coder")
            assert entry is not None
            assert entry.state == ApprovalState.ACKED
            assert entry.version == 1

    def test_dual_role_producer_is_never_auto_acked(self, matrix):
        """Even when TESTER has no tasks (documenter-only slice), it must
        not be auto-ACKed as a producer — its dual role means it should
        always run so it can ACK/NACK CODER for real."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        # TESTER's producer-side state: untouched.
        assert matrix.get_proposal_version("tester") == 0

    def test_dual_role_reviewer_can_nack_to_override_seeded_ack(self, matrix):
        """The 'tester may need coder to do some work' recovery path:
        TESTER's seeded ACK of CODER is a starting state, not a verdict.
        If TESTER's producer work later uncovers a need for code, it can
        NACK at the seeded version and force CODER to re-propose."""
        matrix.seed_auto_ack_for_empty_pure_producers({"tester"})
        assert matrix.is_fully_acked("coder") is True

        matrix.record_nack("tester", "coder", version=1, reason="need helper module")

        assert matrix.is_fully_acked("coder") is False
        entry = matrix.get_entry("tester", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.NACKED

    def test_real_proposal_supersedes_seeded_acks(self, matrix):
        """If the producer's container later proposes for real (version
        bump), the seeded version-1 ACKs are no longer at the latest
        version — the normal flow re-acquires fresh ACKs at v2."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert matrix.get_proposal_version("coder") == 1

        new_version = matrix.record_proposal("coder")
        assert new_version == 2
        # is_fully_acked falls back to False because seeded ACKs are at v1.
        assert matrix.is_fully_acked("coder") is False

    def test_seed_called_twice_is_idempotent_in_effect(self, matrix):
        """Calling the seeder twice with the same task set bumps the
        proposal version but leaves consensus reachable — the second
        call records ACKs at the new (v2) version. Idempotent in the
        sense that the post-state is still fully-ACKed."""
        first = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        second = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert first == ["coder"]
        assert second == ["coder"]
        assert matrix.is_fully_acked("coder") is True


class TestTrackerDelegation:
    """The ``PeerConsensusTracker`` wrapper holds the lock and
    delegates to the matrix. These tests are thin — the heavy lifting
    is covered above."""

    def test_tracker_delegates_to_matrix(self, implement_graph):
        tracker = PeerConsensusTracker(
            pipeline_id="test-pipeline",
            graph=implement_graph,
        )
        auto_acked = tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert auto_acked == ["coder"]
        assert tracker.matrix.is_fully_acked("coder") is True

    def test_tracker_noop_when_all_producers_present(self, implement_graph):
        tracker = PeerConsensusTracker(
            pipeline_id="test-pipeline",
            graph=implement_graph,
        )
        auto_acked = tracker.seed_auto_ack_for_empty_pure_producers(
            {"coder", "tester", "documenter"}
        )
        assert auto_acked == []
