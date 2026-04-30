"""Tests for the conditional-ACK 3-way HITL gate (issue #2004).

The gate sits at ``complete_phase`` and forces a human to explicitly
accept, reject, or redirect pre-merge obligations attached to a
conditional ACK before the phase can close. This module covers:

- ``complete_phase`` queues a gate when tracker.get_pre_merge_conditions()
  is non-empty, and the existing unresolved-decisions guard blocks the
  phase until the gate resolves.
- Resolution dispatch: approve+accept persists to
  ``contract.pr.deferred_actions``; reject force-NACKs each edge;
  address-in-pipeline invalidates each ACK.
- The PR body renderer prefers ``contract.pr.deferred_actions`` over the
  live tracker so obligations survive tracker teardown.
- Idempotence: a second complete_phase call does not re-queue a gate
  when one is already pending.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalState  # noqa: E402
from models import DecisionStatus, HITLDecision, Pipeline, PipelinePhase  # noqa: E402
from peer_consensus import PeerConsensusTracker  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402
from routes.decisions import decisions_bp  # noqa: E402
from routes.phases import (  # noqa: E402
    CONDITIONAL_ACK_ADDRESS,
    CONDITIONAL_ACK_APPROVE,
    CONDITIONAL_ACK_GATE_MARKER,
    CONDITIONAL_ACK_OPTIONS,
    CONDITIONAL_ACK_REJECT,
    phases_bp,
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.register_blueprint(decisions_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def graph():
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


def _make_tracker(graph, condition="git mv legacy/x new/x before merge"):
    """Return a tracker with one active conditional ACK from reviewer_code."""
    tracker = PeerConsensusTracker("pipeline-x", graph, cooldown_seconds=0)
    tracker.register_agent("coder")
    tracker.register_agent("reviewer_code")
    tracker.register_agent("reviewer_contract")
    tracker.handle_propose(
        "coder",
        {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
    )
    if condition:
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": condition,
            },
        )
    return tracker


def _make_pipeline(pipeline_id="pipeline-x", phase=PipelinePhase.IMPLEMENT):
    return Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        has_contract=False,
        current_phase=phase,
    )


class TestGateEnqueueOnCompletePhase:
    """complete_phase queues the 3-way gate when conditions are live."""

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    @patch("routes.phases.get_peer_consensus_tracker")
    def test_pending_condition_queues_gate_and_returns_409(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_clear,
        client,
        graph,
        tmp_path,
    ):
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = tmp_path
        mock_get_store.return_value = (mock_store, pipeline)
        # After the gate writes through queue_decision, load_pipeline
        # returns the same pipeline with the new decision on it.
        mock_store.load_pipeline.return_value = pipeline
        mock_get_tracker.return_value = _make_tracker(graph)

        # Stub queue_decision so we don't touch the real state store —
        # append directly to pipeline.decisions so the downstream
        # unresolved-decisions guard sees it.
        def fake_queue(*, question, context, options, decision_type, phase):
            decision = pipeline.add_decision(
                question=question,
                options=options,
                decision_type=decision_type,
                phase=phase,
            )
            decision.context = context
            return decision

        with patch("routes.phases.get_decision_queue") as mock_get_queue:
            mock_get_queue.return_value.queue_decision.side_effect = fake_queue
            resp = client.post("/api/v1/pipelines/pipeline-x/phase/complete")

        assert resp.status_code == 409
        body = resp.get_json()
        assert body["reason"] == "unresolved_hitl_decisions"
        # Exactly one pending decision, and it's our gate (context marker).
        assert len(pipeline.decisions) == 1
        gate = pipeline.decisions[0]
        assert gate.decision_type == "choice"
        assert gate.options == list(CONDITIONAL_ACK_OPTIONS)
        assert gate.context.startswith(CONDITIONAL_ACK_GATE_MARKER)
        payload = json.loads(gate.context[len(CONDITIONAL_ACK_GATE_MARKER) :])
        conds = payload["conditions"]
        assert len(conds) == 1
        assert conds[0]["reviewer"] == "reviewer_code"
        assert conds[0]["producer"] == "coder"
        assert conds[0]["condition"].startswith("git mv")
        # Question text must expose the obligation so humans aren't forced
        # to crack open the context JSON.
        assert "git mv" in gate.question
        assert "reviewer_code" in gate.question

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    @patch("routes.phases.get_peer_consensus_tracker")
    def test_no_conditions_does_not_queue_gate(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_clear,
        client,
        graph,
        tmp_path,
    ):
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = tmp_path
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_tracker.return_value = _make_tracker(graph, condition="")

        with patch("routes.phases.get_decision_queue") as mock_get_queue:
            resp = client.post("/api/v1/pipelines/pipeline-x/phase/complete")

        assert resp.status_code == 200
        mock_get_queue.return_value.queue_decision.assert_not_called()
        assert pipeline.decisions == []

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    @patch("routes.phases.get_peer_consensus_tracker")
    def test_force_skips_gate_enqueue(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_clear,
        client,
        graph,
        tmp_path,
    ):
        """force=true drains the pipeline without surfacing the gate.

        Matches the existing force=true semantics on the HITL
        unresolved-decisions guard (#1788).
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = tmp_path
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_tracker.return_value = _make_tracker(graph)

        with patch("routes.phases.get_decision_queue") as mock_get_queue:
            resp = client.post(
                "/api/v1/pipelines/pipeline-x/phase/complete",
                json={"force": True, "force_reason": "ops override"},
            )

        assert resp.status_code == 200
        mock_get_queue.return_value.queue_decision.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    @patch("routes.phases.get_peer_consensus_tracker")
    def test_idempotent_when_gate_already_pending(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_clear,
        client,
        graph,
        tmp_path,
    ):
        pipeline = _make_pipeline()
        # Pre-populate a pending gate decision with the marker context.
        existing = pipeline.add_decision(
            question="already queued",
            options=list(CONDITIONAL_ACK_OPTIONS),
            decision_type="choice",
            phase=PipelinePhase.IMPLEMENT,
        )
        existing.context = CONDITIONAL_ACK_GATE_MARKER + json.dumps({"conditions": []})

        mock_store = MagicMock()
        mock_store.repo_path = tmp_path
        mock_get_store.return_value = (mock_store, pipeline)
        mock_store.load_pipeline.return_value = pipeline
        mock_get_tracker.return_value = _make_tracker(graph)

        with patch("routes.phases.get_decision_queue") as mock_get_queue:
            resp = client.post("/api/v1/pipelines/pipeline-x/phase/complete")

        # Guard blocks the phase but nothing new was queued.
        assert resp.status_code == 409
        mock_get_queue.return_value.queue_decision.assert_not_called()
        # Decision list still has exactly the pre-existing one.
        assert len(pipeline.decisions) == 1
        assert pipeline.decisions[0].id == existing.id


class TestResolutionDispatch:
    """Resolving the gate triggers the right tracker/contract mutation."""

    def _gate_context(self, conditions):
        return CONDITIONAL_ACK_GATE_MARKER + json.dumps({"conditions": conditions})

    @patch("routes.decisions._persist_deferred_actions")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_approve_accepts_persists_deferred_actions(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_persist,
        client,
        tmp_path,
    ):
        conditions = [
            {"reviewer": "reviewer_code", "producer": "coder", "condition": "git mv X Y"},
        ]
        resolved = HITLDecision(
            id="decision-1",
            question="conditional ACK",
            status=DecisionStatus.RESOLVED,
            resolution=CONDITIONAL_ACK_APPROVE,
            context=self._gate_context(conditions),
        )
        mock_store = MagicMock(repo_path=tmp_path)
        mock_get_store_for_pipeline.return_value = (mock_store, MagicMock())
        mock_get_queue.return_value.resolve_decision.return_value = resolved

        resp = client.post(
            "/api/v1/pipelines/pipeline-x/decisions/decision-1/resolve",
            json={"resolution": CONDITIONAL_ACK_APPROVE},
        )
        assert resp.status_code == 200
        mock_persist.assert_called_once_with("pipeline-x", conditions, tmp_path)

    @patch("routes.decisions._force_nack_conditional_edges")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_reject_force_nacks_edges(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_force_nack,
        client,
        tmp_path,
    ):
        conditions = [
            {"reviewer": "reviewer_code", "producer": "coder", "condition": "git mv X Y"},
        ]
        resolved = HITLDecision(
            id="decision-1",
            question="conditional ACK",
            status=DecisionStatus.RESOLVED,
            resolution=CONDITIONAL_ACK_REJECT,
            context=self._gate_context(conditions),
        )
        mock_store = MagicMock(repo_path=tmp_path)
        mock_get_store_for_pipeline.return_value = (mock_store, MagicMock())
        mock_get_queue.return_value.resolve_decision.return_value = resolved

        resp = client.post(
            "/api/v1/pipelines/pipeline-x/decisions/decision-1/resolve",
            json={"resolution": CONDITIONAL_ACK_REJECT},
        )
        assert resp.status_code == 200
        mock_force_nack.assert_called_once_with("pipeline-x", conditions)

    @patch("routes.decisions._invalidate_conditional_acks")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_address_in_pipeline_invalidates_acks(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_invalidate,
        client,
        tmp_path,
    ):
        conditions = [
            {"reviewer": "reviewer_code", "producer": "coder", "condition": "git mv X Y"},
        ]
        resolved = HITLDecision(
            id="decision-1",
            question="conditional ACK",
            status=DecisionStatus.RESOLVED,
            resolution=CONDITIONAL_ACK_ADDRESS,
            context=self._gate_context(conditions),
        )
        mock_store = MagicMock(repo_path=tmp_path)
        mock_get_store_for_pipeline.return_value = (mock_store, MagicMock())
        mock_get_queue.return_value.resolve_decision.return_value = resolved

        resp = client.post(
            "/api/v1/pipelines/pipeline-x/decisions/decision-1/resolve",
            json={"resolution": CONDITIONAL_ACK_ADDRESS},
        )
        assert resp.status_code == 200
        mock_invalidate.assert_called_once_with("pipeline-x", conditions)

    @patch("routes.decisions._persist_deferred_actions")
    @patch("routes.decisions._force_nack_conditional_edges")
    @patch("routes.decisions._invalidate_conditional_acks")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_non_gate_context_is_ignored(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_invalidate,
        mock_force_nack,
        mock_persist,
        client,
        tmp_path,
    ):
        """Context without the gate marker must not trigger any dispatch."""

        resolved = HITLDecision(
            id="decision-1",
            question="other decision",
            status=DecisionStatus.RESOLVED,
            resolution="Approve",
            context="failed_role:reviewer_x",
        )
        mock_store = MagicMock(repo_path=tmp_path)
        mock_get_store_for_pipeline.return_value = (mock_store, MagicMock())
        mock_get_queue.return_value.resolve_decision.return_value = resolved

        resp = client.post(
            "/api/v1/pipelines/pipeline-x/decisions/decision-1/resolve",
            json={"resolution": "Approve"},
        )
        assert resp.status_code == 200
        mock_persist.assert_not_called()
        mock_force_nack.assert_not_called()
        mock_invalidate.assert_not_called()


class TestForceNackDispatchIntegration:
    """_force_nack_conditional_edges drives the real tracker."""

    def test_force_nack_transitions_edge_and_producer(self, graph):
        from peer_consensus import ConsensusPhase
        from routes.decisions import _force_nack_conditional_edges

        tracker = _make_tracker(graph)
        conditions = [{"reviewer": "reviewer_code", "producer": "coder"}]

        with patch(
            "routes.decisions.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            _force_nack_conditional_edges("pipeline-x", conditions)

        entry = tracker.matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.NACKED
        assert entry.reason == "human rejected conditional ACK"
        # Conditions dropped with the NACK (approval_matrix clears them).
        assert tracker.get_pre_merge_conditions() == []
        # Producer must be back in WORKING so it can re-propose.
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING


class TestInvalidateDispatchIntegration:
    """_invalidate_conditional_acks drives the real tracker."""

    def test_invalidate_drops_ack_back_to_pending(self, graph):
        from peer_consensus import ConsensusPhase
        from routes.decisions import _invalidate_conditional_acks

        tracker = _make_tracker(graph)
        conditions = [{"reviewer": "reviewer_code", "producer": "coder"}]

        with patch(
            "routes.decisions.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            _invalidate_conditional_acks("pipeline-x", conditions)

        entry = tracker.matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.PENDING
        assert entry.pre_merge_condition == ""
        # Producer must be back in WORKING so it can re-propose.
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING


class TestPrRenderPrefersContract:
    """PR body renderer tier-1 is contract.pr.deferred_actions (#2004)."""

    def test_contract_lines_take_precedence_over_tracker(self, graph):
        from egg_contracts.models import DeferredAction
        from routes import pipelines as p

        tracker = _make_tracker(graph, condition="stale tracker-only condition")

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=[
                    DeferredAction(
                        reviewer="reviewer_code",
                        condition="git mv durable/X new/X",
                    )
                ],
            )

        assert "Pre-merge Obligations" in section
        assert "reviewer_code" in section
        assert "git mv durable/X new/X" in section
        # Tracker-only condition must not appear — contract wins.
        assert "stale tracker-only condition" not in section

    def test_empty_contract_falls_back_to_tracker(self, graph):
        from routes import pipelines as p

        tracker = _make_tracker(graph, condition="git mv tracker/X new/X")

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=[],
            )

        assert "Pre-merge Obligations" in section
        assert "git mv tracker/X new/X" in section

    def test_whitespace_only_contract_entries_do_not_render(self):
        from routes import pipelines as p

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=["  ", "\n"],
            )

        assert section == ""

    def test_none_contract_falls_back_to_tracker(self, graph):
        """Callers that don't know about contracts still get tracker output."""
        from routes import pipelines as p

        tracker = _make_tracker(graph, condition="git mv foo bar")

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            section = p._build_pre_merge_obligations_section("pipeline-x")

        assert "git mv foo bar" in section

    def test_legacy_string_entries_still_render(self):
        """Pre-#2336 contracts persisted ``list[str]``; renderer still loads them."""
        from routes import pipelines as p

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=["reviewer_code: git mv durable/X new/X"],
            )

        assert "Pre-merge Obligations" in section
        # Legacy string is parsed into reviewer + condition and rendered with
        # the same bullet shape as a structured DeferredAction.
        assert "reviewer_code" in section
        assert "git mv durable/X new/X" in section


class TestPrRenderResolvedObligations:
    """Resolved obligations move out of the merge-blocking section (#2336)."""

    def test_resolved_obligation_renders_under_resolved_section_only(self):
        from egg_contracts.models import DeferredAction
        from routes import pipelines as p

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=[
                    DeferredAction(
                        reviewer="reviewer_code",
                        condition="git mv legacy/x new/x before merge",
                        resolved_in_diff="2c319626a",
                    )
                ],
            )

        # All-resolved → no merge-blocking banner anywhere.
        assert "Pre-merge Obligations" not in section
        assert "Do **not** merge" not in section
        # Resolved subsection present, with SHA pointer.
        assert "Resolved within this PR" in section
        assert "git mv legacy/x new/x" in section
        assert "2c319626a" in section

    def test_mixed_open_and_resolved_renders_both_sections(self):
        from egg_contracts.models import DeferredAction
        from routes import pipelines as p

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=[
                    DeferredAction(
                        reviewer="reviewer_code",
                        condition="open obligation X",
                    ),
                    DeferredAction(
                        reviewer="reviewer_contract",
                        condition="resolved obligation Y",
                        resolved_in_diff="abc1234",
                    ),
                ],
            )

        # Open section + banner.
        assert "## ⚠️ Pre-merge Obligations" in section
        assert "Do **not** merge" in section
        assert "open obligation X" in section
        # Resolved section.
        assert "## ✅ Resolved within this PR" in section
        assert "resolved obligation Y" in section
        assert "abc1234" in section
        # Open section appears first (merge-blocking is the higher-priority
        # signal for the merger).
        assert section.find("## ⚠️ Pre-merge Obligations") < section.find(
            "## ✅ Resolved within this PR"
        )

    def test_resolved_via_live_tracker(self, graph):
        """Tier-2 (live tracker) also surfaces resolutions (#2336)."""
        from routes import pipelines as p

        tracker = PeerConsensusTracker("pipeline-x", graph, cooldown_seconds=0)
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
                "pre_merge_condition": "verify tester landed patch-path rewrite",
                "pre_merge_condition_resolved_in_diff": "2c319626a",
            },
        )

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            section = p._build_pre_merge_obligations_section(
                "pipeline-x",
                contract_deferred_actions=[],
            )

        assert "Resolved within this PR" in section
        assert "2c319626a" in section
        assert "Pre-merge Obligations" not in section


# --- In-cycle resolution interactions with the gate (#2338) ----------------


class TestResolvedObligationsSkipGate:
    """An obligation that has been resolved in-cycle by another agent (e.g.
    the tester cherry-picking the satisfying commit) must not fire the
    HITL gate at complete_phase, and must not appear in the auto-created
    PR body — its presence in either is busywork, since the conditioning
    work is already on the branch (#2338)."""

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    @patch("routes.phases.get_peer_consensus_tracker")
    def test_resolved_obligation_does_not_queue_gate(
        self,
        mock_get_tracker,
        mock_get_store,
        _mock_clear,
        client,
        graph,
        tmp_path,
    ):
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = tmp_path
        mock_get_store.return_value = (mock_store, pipeline)
        tracker = _make_tracker(graph, condition="tester must commit X")
        # Resolve the only live obligation before complete_phase fires.
        tracker.handle_resolve_obligation(
            resolver_role="tester",
            reviewer_role="reviewer_code",
            producer_role="coder",
            commit_sha="abc1234",
        )
        mock_get_tracker.return_value = tracker

        with patch("routes.phases.get_decision_queue") as mock_get_queue:
            resp = client.post("/api/v1/pipelines/pipeline-x/phase/complete")

        # No gate queued — phase proceeds normally.
        assert resp.status_code == 200
        mock_get_queue.return_value.queue_decision.assert_not_called()
        assert pipeline.decisions == []

    def test_resolved_obligation_omitted_from_pr_body(self, graph):
        """The PR body's Pre-merge Obligations section must drop resolved
        entries when reading from the live tracker. The filter lives in
        ``ApprovalMatrix.get_pre_merge_conditions`` upstream of both this
        renderer and the HITL gate — so a resolved obligation never makes
        it into the live-tracker fallback path."""
        from routes import pipelines as p

        tracker = _make_tracker(graph, condition="tester must commit X")
        tracker.handle_resolve_obligation(
            resolver_role="tester",
            reviewer_role="reviewer_code",
            producer_role="coder",
            commit_sha="abc1234",
        )

        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            section = p._build_pre_merge_obligations_section("pipeline-x")

        assert section == ""


class TestApprovalMatrixResolvedField:
    """ApprovalEntry round-trips ``pre_merge_condition_resolved_in_diff`` (#2336)."""

    def test_record_ack_stores_resolution(self, graph):
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)
        version = matrix.record_proposal("coder")
        entry = matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/a.py"],
            pre_merge_condition="verify migration in prod",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        assert entry.pre_merge_condition == "verify migration in prod"
        assert entry.pre_merge_condition_resolved_in_diff == "abc1234"

    def test_resolution_dropped_without_condition(self, graph):
        """A resolution SHA on a plain ACK has nothing to attach to."""
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)
        version = matrix.record_proposal("coder")
        entry = matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/a.py"],
            pre_merge_condition="",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        assert entry.pre_merge_condition == ""
        assert entry.pre_merge_condition_resolved_in_diff == ""

    def test_nack_clears_resolution(self, graph):
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)
        version = matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/a.py"],
            pre_merge_condition="verify migration in prod",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        matrix.record_nack(
            "reviewer_code",
            "coder",
            version,
            reason="found a bug",
            artifact_refs=["src/a.py"],
        )
        entry = matrix._entries[("reviewer_code", "coder")]
        assert entry.pre_merge_condition == ""
        assert entry.pre_merge_condition_resolved_in_diff == ""

    def test_get_pre_merge_conditions_includes_resolution(self, graph):
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)
        version = matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/a.py"],
            pre_merge_condition="verify migration in prod",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        conditions = matrix.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["resolved_in_diff"] == "abc1234"

    def test_serialization_round_trip_preserves_resolution(self, graph):
        from approval_matrix import ApprovalMatrix

        matrix = ApprovalMatrix(graph)
        version = matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version,
            artifact_refs=["src/a.py"],
            pre_merge_condition="verify migration in prod",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        data = matrix.to_dict()
        # Carry proposal_versions through so the from_dict re-hydration
        # can answer "what's the latest version?".
        data.setdefault("proposal_versions", {"coder": version})
        restored = ApprovalMatrix.from_dict(data, graph)
        entry = restored._entries[("reviewer_code", "coder")]
        assert entry.pre_merge_condition_resolved_in_diff == "abc1234"


class TestDeferredActionLegacyMigration:
    """``PRMetadata.deferred_actions`` accepts legacy ``list[str]`` input (#2336)."""

    def test_legacy_string_promoted_to_deferred_action(self):
        from egg_contracts.models import PRMetadata

        meta = PRMetadata(
            title="t",
            deferred_actions=["reviewer_code: git mv legacy/x new/x"],
        )
        assert len(meta.deferred_actions) == 1
        action = meta.deferred_actions[0]
        assert action.reviewer == "reviewer_code"
        assert action.condition == "git mv legacy/x new/x"
        assert action.resolved_in_diff == ""

    def test_structured_input_passes_through(self):
        from egg_contracts.models import DeferredAction, PRMetadata

        meta = PRMetadata(
            title="t",
            deferred_actions=[
                DeferredAction(
                    reviewer="reviewer_code",
                    condition="verify migration",
                    resolved_in_diff="abc1234",
                )
            ],
        )
        assert meta.deferred_actions[0].resolved_in_diff == "abc1234"

    def test_legacy_string_without_separator_treated_as_unknown_reviewer(self):
        from egg_contracts.models import PRMetadata

        meta = PRMetadata(
            title="t",
            deferred_actions=["raw obligation text with no reviewer prefix"],
        )
        assert meta.deferred_actions[0].reviewer == ""
        assert meta.deferred_actions[0].condition == ("raw obligation text with no reviewer prefix")
