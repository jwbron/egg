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

from action_guards import check_confirm_guard, check_nack_guard, validate_invariants
from approval_matrix import ApprovalMatrix
from attestation_schemas import ProposalPayload
from peer_consensus import (
    PeerConsensusTracker,
    _trackers,
    _trackers_lock,
    reconstruct_tracker_from_messages,
)
from review_graph import get_default_implement_graph, get_default_plan_graph

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

    def test_no_op_replay_does_not_inject_sentinel_commit_sha(self):
        """Review feedback (item 2): the RECONSTRUCTED_NO_SHA sentinel must
        not land in a no-op producer's commit-sha history — a no-op carries
        no commit by design and the sentinel would be misleading audit data.
        """
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
        history = tracker._proposal_commit_sha_history.get("documenter", {})
        assert "RECONSTRUCTED_NO_SHA" not in history.values()
        # The current commit-sha for the producer is empty (no real propose).
        assert tracker._proposal_commit_shas.get("documenter", "") == ""


# --- Mutual exclusion: no_changes_needed + tests_execution_blocked --------


class TestProposalPayloadMutualExclusion:
    """Review feedback (item 7): folding the per-role
    ``no_test_changes_needed`` into the proposal-level ``no_changes_needed``
    dropped the mutual-exclusion check against ``tests_execution_blocked``.
    Restore it at the proposal layer."""

    def test_no_changes_with_tests_blocked_is_rejected(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            ProposalPayload(
                summary="x" * 30,
                no_changes_needed=True,
                no_changes_reason="no work in this slice",
                attestation={
                    "tests_execution_blocked": True,
                    "tests_execution_blocked_reason": "no network",
                },
            )

    def test_tests_blocked_alone_is_fine(self):
        # blocked-without-no-op is the original real-proposal path.
        payload = ProposalPayload(
            summary="x" * 30,
            artifacts=["src/a.py"],
            commit_sha="abc123",
            attestation={
                "tests_execution_blocked": True,
                "tests_execution_blocked_reason": "no network",
            },
        )
        assert payload.no_changes_needed is False

    def test_no_op_alone_is_fine(self):
        # no-op-without-blocked is the new path.
        payload = ProposalPayload(
            summary="x" * 30,
            no_changes_needed=True,
            no_changes_reason="no work in this slice",
        )
        assert payload.no_changes_needed is True


# --- Action guards: NACK against a no-op --------------------------------


class TestNackGuardNoOp:
    """Review feedback (item 4): a NACK against a no-op proposal must be
    rejected — the matrix would silently mask it as the no-op is treated as
    non-blocking, hiding what could be a real disagreement."""

    def test_nack_against_no_op_is_rejected(self):
        graph = get_default_implement_graph()
        matrix = ApprovalMatrix(graph)
        matrix.record_proposal("documenter", no_changes=True)

        # reviewer_code has a (advisory) edge → documenter in the default
        # implement graph; the guard must still reject the NACK.
        result = check_nack_guard("reviewer_code", "documenter", graph, matrix=matrix)
        assert result.allowed is False
        assert result.details and result.details.get("guard") == "no_op_nack"
        assert "no work" in result.reason or "no-op" in result.reason

    def test_nack_against_real_proposal_is_allowed(self):
        graph = get_default_implement_graph()
        matrix = ApprovalMatrix(graph)
        # Real proposal — flag stays False.
        matrix.record_proposal("documenter", no_changes=False)
        result = check_nack_guard(
            "reviewer_code", "documenter", graph, matrix=matrix, nack_version=1
        )
        assert result.allowed is True


# --- Invariants: no-op short-circuit ------------------------------------


class TestValidateInvariantsNoOp:
    """Review feedback (item 3): the fully_acked_consistency invariant must
    short-circuit on a no-op producer the same way ``is_fully_acked`` and
    ``get_blocking_edges`` do, or it flags every no-op as inconsistent.
    """

    def test_no_op_does_not_trigger_fully_acked_violation(self):
        graph = get_default_implement_graph()
        matrix = ApprovalMatrix(graph)
        matrix.record_proposal("coder", no_changes=True)  # coder has CRITICAL reviewers
        violations = validate_invariants(
            graph,
            matrix,
            producer_phases={},
            reviewer_phases={},
            confirmed=set(),
        )
        fully_acked_violations = [v for v in violations if v.invariant == "fully_acked_consistency"]
        assert fully_acked_violations == []


# --- BRC preamble: no-op prose conditioned on phase ----------------------


class TestBRCPreambleNoOpPhaseGating:
    """Review feedback (items 1+6): the producer-lifecycle no-op invitation
    text must appear only for implement-phase producers. In refine/plan the
    producer's draft is mandatory and a no-op is explicitly rejected, so
    surfacing the affordance there is misleading and lets an architect /
    risk_analyst bypass plan-phase authoring.
    """

    def test_implement_producer_sees_no_op_prose(self):
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble("coder", phase="implement")
        assert "no-changes-needed" in preamble
        assert "Submit a no-op propose" in preamble

    def test_plan_producer_does_not_see_no_op_prose(self):
        from routes.pipelines import _build_brc_preamble

        for role in ("architect", "task_planner", "risk_analyst"):
            preamble = _build_brc_preamble(role, phase="plan")
            assert "no-changes-needed" not in preamble, role
            assert "Submit a no-op propose" not in preamble, role

    def test_refine_producer_does_not_see_no_op_prose(self):
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble("refiner", phase="refine")
        assert "no-changes-needed" not in preamble
        assert "Submit a no-op propose" not in preamble

    def test_plan_phase_graph_has_three_critical_producers(self):
        """Sanity check the assumption behind item 1: architect /
        task_planner / risk_analyst are all plan-phase producers, so the
        signals-layer guard must cover all three (which the phase-based
        gate does automatically)."""
        graph = get_default_plan_graph()
        producers = {r for r in graph.all_roles() if graph.is_producer(r)}
        assert {"architect", "task_planner", "risk_analyst"}.issubset(producers)
