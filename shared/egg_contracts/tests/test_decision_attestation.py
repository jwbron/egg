"""Tests for the decision-ledger attestation helpers (#3390).

Covers the two shared building blocks of the registration guarantee:

- :func:`egg_contracts.decisions.extract_cq_citations` — the draft-side
  citation scan the propose-time validator runs against attested ids.
- :func:`egg_contracts.decisions.decision_attestation_errors` — the
  exactly-one-of shape check shared by the orchestrator's Pydantic
  attestation model and the signal validator.
"""

from __future__ import annotations

from egg_contracts.decisions import (
    candidate_considered_errors,
    decision_attestation_errors,
    extract_cq_citations,
)

_CANDIDATE = {
    "question": "Should the fallback cache default on?",
    "disposition": "deferred_to_plan",
    "why": "depends on the plan's storage design",
}


class TestExtractCqCitations:
    def test_bare_id_in_prose(self):
        assert extract_cq_citations("we defer this to cq-3 for the operator") == {"cq-3"}

    def test_html_marker_from_add_decision_markdown(self):
        text = "## Open Questions\n<!-- egg-hitl-decision id=cq-12 -->\n- [ ] Option A"
        assert extract_cq_citations(text) == {"cq-12"}

    def test_multiple_and_repeated_ids(self):
        text = "cq-1 then cq-2, and cq-1 again (see cq-10)"
        assert extract_cq_citations(text) == {"cq-1", "cq-2", "cq-10"}

    def test_no_partial_word_matches(self):
        # ``acq-1`` and ``cq-`` must not match; word-boundary on both sides.
        assert extract_cq_citations("acq-1 cq- cqN qc-1") == set()

    def test_non_string_input(self):
        assert extract_cq_citations(None) == set()
        assert extract_cq_citations(42) == set()


class TestDecisionAttestationErrors:
    def test_registered_ids_valid(self):
        assert decision_attestation_errors(["cq-1", "cq-2"], None) == []

    def test_rationale_with_candidates_valid(self):
        assert (
            decision_attestation_errors([], "task is mechanical; no operator call", [_CANDIDATE])
            == []
        )
        assert decision_attestation_errors(None, "no ambiguity in scope", [_CANDIDATE]) == []

    def test_rationale_without_candidates_is_error(self):
        # #3526: a bare rationale paragraph is no longer a valid
        # explicit-none: the empty ledger must enumerate what was
        # considered.
        for candidates in (None, []):
            errors = decision_attestation_errors(
                [], "task is mechanical; no operator call", candidates
            )
            assert len(errors) == 1
            assert "candidates_considered" in errors[0]

    def test_candidates_alongside_registered_valid(self):
        assert decision_attestation_errors(["cq-1"], None, [_CANDIDATE]) == []

    def test_neither_is_error(self):
        errors = decision_attestation_errors([], "")
        assert len(errors) == 1
        assert "must carry either" in errors[0]

    def test_both_is_error(self):
        errors = decision_attestation_errors(["cq-1"], "also none needed")
        assert len(errors) == 1
        assert "mutually exclusive" in errors[0]

    def test_whitespace_rationale_is_empty(self):
        errors = decision_attestation_errors(None, "   ")
        assert len(errors) == 1
        assert "must carry either" in errors[0]

    def test_malformed_ids_reported_individually(self):
        errors = decision_attestation_errors(["cq-1", "decision-2", "CQ-3", 7], None)
        assert len(errors) == 3
        assert any("'decision-2'" in e for e in errors)
        assert any("'CQ-3'" in e for e in errors)
        assert any("7" in e for e in errors)

    def test_non_list_registered_rejected(self):
        errors = decision_attestation_errors("cq-1", None)
        assert len(errors) == 1
        assert "must be a list" in errors[0]

    def test_non_string_rationale_rejected(self):
        errors = decision_attestation_errors(None, ["not", "a", "string"])
        assert len(errors) == 1
        assert "must be a string" in errors[0]


class TestCandidateConsideredErrors:
    def test_absent_is_valid(self):
        assert candidate_considered_errors(None) == []

    def test_well_formed_candidates_valid(self):
        assert candidate_considered_errors([_CANDIDATE]) == []
        assert (
            candidate_considered_errors(
                [{"question": "q?", "disposition": "not_operator_grade", "why": "w"}]
            )
            == []
        )

    def test_non_list_rejected(self):
        errors = candidate_considered_errors("not a list")
        assert len(errors) == 1
        assert "must be a list" in errors[0]

    def test_missing_fields_reported_per_entry(self):
        errors = candidate_considered_errors([{"question": "", "disposition": "bogus"}])
        assert len(errors) == 3
        assert any("question" in e for e in errors)
        assert any("disposition" in e for e in errors)
        assert any("why" in e for e in errors)

    def test_unknown_disposition_rejected(self):
        errors = candidate_considered_errors(
            [{"question": "q?", "disposition": "resolved_by_context", "why": "w"}]
        )
        assert len(errors) == 1
        assert "resolved_by_context" in errors[0]
