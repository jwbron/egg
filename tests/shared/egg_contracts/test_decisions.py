"""Tests for the ``cq-N`` allocator helper in ``egg_contracts.decisions``.

The ``cq-N`` prefix is the agent/contract-side half of the
``Decision.id`` namespace split (#2616). The orchestrator's pipeline
side still allocates ``decision-N``; the helper here is what both
``register_open_question`` (sandbox) and ``_build_hitl_decision``
(orchestrator) call so neither path drifts from the other.
"""

from __future__ import annotations

from egg_contracts.decisions import (
    CONTRACT_QUESTION_MAX_CHARS,
    CONTRACT_QUESTION_TRUNCATION_SUFFIX,
    CQ_ID_PATTERN,
    find_duplicate_open_question,
    next_cq_id,
    normalize_question,
    truncate_question,
)
from egg_contracts.models import Decision, DecisionType, PipelinePhase


class TestTruncateQuestion:
    """Single source of truth for the status/gate surface length cap (#3374 review)."""

    def test_short_question_unchanged(self):
        assert truncate_question("short") == "short"

    def test_question_at_cap_unchanged(self):
        q = "x" * CONTRACT_QUESTION_MAX_CHARS
        assert truncate_question(q) == q

    def test_overlong_question_truncated_with_suffix(self):
        q = "x" * (CONTRACT_QUESTION_MAX_CHARS + 50)
        out = truncate_question(q)
        assert out == "x" * CONTRACT_QUESTION_MAX_CHARS + CONTRACT_QUESTION_TRUNCATION_SUFFIX

    def test_respects_explicit_max_chars(self):
        assert (
            truncate_question("abcdef", max_chars=3) == "abc" + CONTRACT_QUESTION_TRUNCATION_SUFFIX
        )


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


class TestNormalizeQuestion:
    def test_lowercases_strips_and_collapses_whitespace(self):
        assert normalize_question("  Drop the   Slider?\n") == "drop the slider?"

    def test_non_string_normalizes_to_empty(self):
        assert normalize_question(None) == ""
        assert normalize_question(42) == ""

    def test_equivalent_reformattings_match(self):
        assert normalize_question("Drop the slider?") == normalize_question("drop   the\tslider?")


class TestFindDuplicateOpenQuestion:
    def test_returns_none_for_empty_question(self):
        existing = [{"id": "cq-1", "type": "hitl", "question": "x", "resolved": False}]
        assert find_duplicate_open_question(existing, "", None) is None

    def test_matches_equivalent_unresolved_hitl_in_same_phase(self):
        existing = [
            {
                "id": "cq-1",
                "type": "hitl",
                "question": "Drop the slider?",
                "phase": "plan",
                "resolved": False,
            }
        ]
        hit = find_duplicate_open_question(existing, "drop the   slider?", "plan")
        assert hit is not None
        assert hit["id"] == "cq-1"

    def test_no_match_when_phase_differs(self):
        existing = [
            {
                "id": "cq-1",
                "type": "hitl",
                "question": "Drop the slider?",
                "phase": "plan",
                "resolved": False,
            }
        ]
        assert find_duplicate_open_question(existing, "Drop the slider?", "refine") is None

    def test_skips_resolved_decisions(self):
        existing = [
            {
                "id": "cq-1",
                "type": "hitl",
                "question": "Drop the slider?",
                "phase": "plan",
                "resolved": True,
            }
        ]
        assert find_duplicate_open_question(existing, "Drop the slider?", "plan") is None

    def test_skips_non_hitl_decisions(self):
        existing = [
            {
                "id": "decision-1",
                "type": "phase_gate",
                "question": "Drop the slider?",
                "phase": "plan",
                "resolved": False,
            }
        ]
        assert find_duplicate_open_question(existing, "Drop the slider?", "plan") is None

    def test_matches_pydantic_decisions_with_enum_phase(self):
        existing = [
            Decision(
                id="cq-7",
                question="Need a V2 schema?",
                type=DecisionType.HITL,
                phase=PipelinePhase.PLAN,
            )
        ]
        hit = find_duplicate_open_question(existing, "need a v2 schema?", PipelinePhase.PLAN)
        assert hit is not None
        assert hit.id == "cq-7"

    def test_phaseless_questions_match_on_none(self):
        existing = [
            {"id": "cq-1", "type": "hitl", "question": "Q?", "phase": None, "resolved": False}
        ]
        hit = find_duplicate_open_question(existing, "q?", None)
        assert hit is not None and hit["id"] == "cq-1"
