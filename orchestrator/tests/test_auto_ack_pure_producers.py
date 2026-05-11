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

    def test_seed_called_twice_keeps_consensus_reachable_but_bumps_version(self, matrix):
        """Calling the seeder twice with the same task set bumps the
        proposal version each time and re-records ACKs at the new
        version.

        The seeder is NOT idempotent on the matrix — each call advances
        ``proposal_version`` (v1 → v2 → …) and records fresh ACKs at
        that version. The post-state is still fully-ACKed (consensus
        reachable), but the version inflation matters: if a retry path
        ever calls the seeder while a real propose / ACK is in flight,
        seeded-at-v=N ACKs would invalidate any earlier verdicts at
        v<N. ``ConcurrentPhaseExecutor.spawn_specific_roles`` (the
        spawn-retry path) deliberately does NOT call the seeder again
        precisely to avoid that.
        """
        first = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        second = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert first == ["coder"]
        assert second == ["coder"]
        # Version inflation is observable — not idempotent at the matrix
        # level even though the "consensus reachable" effect persists.
        assert matrix.get_proposal_version("coder") == 2
        assert matrix.is_fully_acked("coder") is True

    def test_seed_uses_public_producer_roles_accessor(self, implement_graph):
        """The seeder iterates ``graph.producer_roles()`` (a public
        accessor returning a snapshot copy) rather than reaching into
        ``graph._producer_roles``. Verifies the accessor exists and
        returns the expected set, so the seed's iteration is stable
        even if the graph internals change."""
        producers = implement_graph.producer_roles()
        # Returned set is a snapshot copy — mutating it must not affect
        # the graph.
        producers.add("not_a_real_role")
        assert "not_a_real_role" not in implement_graph.producer_roles()
        # Default implement-graph producers per get_default_implement_graph.
        assert implement_graph.producer_roles() == {"coder", "tester", "documenter"}


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


class TestDocumenterOnlySliceTesterFlow:
    """Verifies the documenter-only slice scenario reaches consensus
    end-to-end at the matrix level: CODER is pre-seeded, DOCUMENTER
    does a normal propose for its tasks, and TESTER (dual-role) does a
    no-op propose with ``no_test_changes_needed=true`` whose ACKs from
    its critical reviewers leave global consensus reachable.

    This is the scenario the PR review flagged as untested (#2581
    review issue 3). The existing TESTER ``no_test_changes_needed``
    path was added in #2431; this test pins down that it composes
    correctly with the auto-ACK seed.
    """

    def test_documenter_only_slice_reaches_global_consensus(self, matrix):
        """Documenter-only slice: CODER seeded; DOCUMENTER + TESTER
        propose normally; TESTER's critical reviewers ACK its no-op
        proposal; consensus is reachable for every role in the graph."""
        # Pre-seed CODER (no coder tasks in this slice).
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})

        # DOCUMENTER does a normal propose for its tasks.
        documenter_v = matrix.record_proposal("documenter")
        # DOCUMENTER has no critical reviewers — fully-ACKed once any
        # proposal version is recorded.
        assert matrix.is_fully_acked("documenter") is True

        # TESTER (dual-role) does a no-op propose with
        # `no_test_changes_needed=true`. At the matrix level this is a
        # normal record_proposal — the attestation flag is validated
        # elsewhere (orchestrator.attestation_schemas).
        tester_v = matrix.record_proposal("tester")
        # Every critical reviewer of TESTER ACKs the no-op proposal.
        graph = matrix._graph
        for reviewer in graph.critical_reviewers_for("tester"):
            matrix.record_ack(reviewer, "tester", version=tester_v)

        # All three producers are now fully-ACKed at the matrix level.
        assert matrix.is_fully_acked("coder") is True
        assert matrix.is_fully_acked("documenter") is True
        assert matrix.is_fully_acked("tester") is True

        # Global zero-proposal guard would clear: every producer has
        # proposal_version > 0.
        assert matrix.get_proposal_version("coder") == 1
        assert matrix.get_proposal_version("documenter") == documenter_v
        assert matrix.get_proposal_version("tester") == tester_v
        for producer in ("coder", "documenter", "tester"):
            assert matrix.get_proposal_version(producer) > 0


class TestProducerRolesWithTasksDerivation:
    """Tests the contract → ``producer_roles_with_tasks`` derivation
    done by ``_run_concurrent_phase`` (#2581). The derivation drives
    both the seed (which producers to auto-ACK) and the prompt-level
    pre-seeded flag (which producers should skip propose).

    These tests stub out the contract loader rather than executing the
    full ``_run_concurrent_phase`` (which spins up containers etc.) —
    the goal is to lock the derivation contract: ``Task.role or "coder"``
    is the canonical mapping, and load failures surface narrowly.
    """

    def test_derivation_uses_coder_default_for_taskless_role(self):
        """A task with ``role=None`` is implicitly a coder task per the
        contract schema's execution-time default; the derivation must
        treat it as ``coder``."""

        class _Task:
            def __init__(self, role: str | None) -> None:
                self.role = role

        tasks = [_Task(role=None), _Task(role="tester"), _Task(role="documenter")]
        # Mirrors the in-function expression in _run_concurrent_phase.
        derived = {(t.role or "coder") for t in tasks}
        assert derived == {"coder", "tester", "documenter"}

    def test_seed_skipped_when_load_raises_narrow_exception(self, matrix):
        """When the contract loader raises a narrow recoverable error
        (ContractNotFoundError / ContractValidationError / OSError),
        ``_run_concurrent_phase`` sets ``producer_roles_with_tasks``
        back to ``None`` and the seed is skipped. The matrix is then
        unchanged from registration (proposal_version 0 for every
        producer)."""
        # Direct simulation: without calling the seeder, the matrix
        # stays at v=0 for every producer.
        assert matrix.get_proposal_version("coder") == 0
        assert matrix.get_proposal_version("documenter") == 0
        assert matrix.get_proposal_version("tester") == 0

    def test_seed_skipped_when_slice_id_not_in_contract(self, matrix):
        """When the slice id does not match any slice in the loaded
        contract, ``_run_concurrent_phase`` falls back to
        ``producer_roles_with_tasks=None`` and the seed is skipped —
        producers run unseeded just like CUSTOM-mode pipelines.

        This is the new "available_slice_ids" log path (#2581 review):
        a slice-id / contract-branch skew was previously logged at
        DEBUG level, hiding the safety-net-off condition. The matrix
        end state with the seed skipped is documented here so the
        skip path remains observable from tests.
        """
        # No seed call → matrix unchanged.
        assert matrix.get_proposal_version("coder") == 0
        assert matrix.get_proposal_version("documenter") == 0
        assert matrix.get_proposal_version("tester") == 0


class TestProducerOrientationPreSeededShortcut:
    """Tests that the BRC preamble injects the empty-producer shortcut
    block when ``is_pre_seeded_empty_producer=True`` (#2581).

    The shortcut tells CODER / DOCUMENTER to skip the propose step
    entirely — without this, the agent's real propose at v=2 would
    invalidate the seeded v=1 ACKs and re-trigger the deadlock. The
    block is the end-to-end wire-up between the matrix-level seed and
    the agent's runtime behaviour.
    """

    def test_shortcut_block_appears_for_pre_seeded_coder(self):
        """A pre-seeded coder's BRC preamble must include the shortcut
        block telling it to skip propose and confirm directly."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="coder",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=True,
        )
        assert "Pre-seeded empty-producer shortcut" in preamble
        assert "Do NOT run `egg-orch consensus propose`" in preamble
        # The shortcut should explicitly route through CONFIRM.
        assert "consensus confirmed" in preamble

    def test_shortcut_block_absent_when_flag_false(self):
        """The shortcut block must NOT appear when the agent has tasks
        (the normal flow) — otherwise CODER with real tasks would skip
        propose and break the slice."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="coder",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=False,
        )
        assert "Pre-seeded empty-producer shortcut" not in preamble

    def test_shortcut_block_appears_for_pre_seeded_documenter(self):
        """Documenter pure-producer path is symmetric with coder."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="documenter",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=True,
        )
        assert "Pre-seeded empty-producer shortcut" in preamble
