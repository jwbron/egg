"""Tests for the ``cq-N`` allocator helper in ``egg_contracts.decisions``.

The ``cq-N`` prefix is the agent/contract-side half of the
``Decision.id`` namespace split (#2616). The orchestrator's pipeline
side still allocates ``decision-N``; the helper here is what both
``register_open_question`` (sandbox) and ``_build_hitl_decision``
(orchestrator) call so neither path drifts from the other.
"""

from __future__ import annotations

from egg_contracts.decisions import CQ_ID_PATTERN, next_cq_id
from egg_contracts.models import Decision, DecisionType


class TestNextCqId:
    def test_returns_cq_1_for_empty_input(self):
        assert next_cq_id([]) == "cq-1"

    def test_handles_pydantic_decisions(self):
        existing = [
            Decision(id="cq-1", question="?", type=DecisionType.HITL),
            Decision(id="cq-2", question="?", type=DecisionType.HITL),
        ]
        assert next_cq_id(existing) == "cq-3"

    def test_handles_plain_dicts(self):
        existing = [{"id": "cq-1"}, {"id": "cq-2"}]
        assert next_cq_id(existing) == "cq-3"

    def test_ignores_legacy_decision_ids(self):
        """Legacy ``decision-N`` entries (mirrored by the bridge) must
        not perturb the ``cq-N`` counter — that's the whole point of
        the namespace split."""
        existing = [{"id": f"decision-{i}"} for i in range(1, 14)]
        assert next_cq_id(existing) == "cq-1"

    def test_counts_only_cq_entries_in_mixed_namespace(self):
        existing = [
            {"id": "decision-1"},
            {"id": "cq-1"},
            {"id": "decision-2"},
            {"id": "cq-5"},  # gap: next must be 6, not 2
        ]
        assert next_cq_id(existing) == "cq-6"

    def test_handles_missing_or_none_ids_defensively(self):
        """The gateway hands back Pydantic-validated dicts so a missing
        / ``None`` id is unlikely in practice, but the helper must not
        raise on it (a ``TypeError`` here would crash the handler
        retry loop)."""
        existing = [{}, {"id": None}, {"id": "cq-1"}]
        assert next_cq_id(existing) == "cq-2"

    def test_pattern_matches_only_cq_prefix(self):
        assert CQ_ID_PATTERN.match("cq-1") is not None
        assert CQ_ID_PATTERN.match("cq-42") is not None
        assert CQ_ID_PATTERN.match("decision-1") is None
        assert CQ_ID_PATTERN.match("cq-") is None
        assert CQ_ID_PATTERN.match("cq-1a") is None
