"""Tests for ``feedback.find_carry_forward_feedback`` carry-forward (#3392).

The converge-before-advance HITL loop re-runs a refine/plan phase after the
operator answers its feedback, so the phase's agents re-register the same
feedback request in a prior-answered round. Replacing the single *submitted*
feedback slot with a fresh ``submitted=False`` entry would re-surface an
answered request via the orchestrator bridge, re-tick the convergence count,
and the loop would never reach a fixpoint. ``find_carry_forward_feedback`` is
the single-slot counterpart to ``decisions.find_resolved_question`` that lets
``request_feedback`` adopt the prior submitted feedback instead.

These tests cover the matcher in isolation against the plain contract-dict
feedback shape (the gateway JSON payload).
"""

from __future__ import annotations

from egg_contracts.feedback import find_carry_forward_feedback


def _fb(
    id_: str,
    questions: list[str],
    *,
    phase: str | None,
    submitted: bool,
) -> dict:
    """A plain contract-dict feedback slot (the gateway JSON shape)."""
    return {
        "id": id_,
        "phase": phase,
        "submitted": submitted,
        "questions": [
            {"id": f"Q{i}", "question": q, "answer": "answered" if submitted else None}
            for i, q in enumerate(questions, start=1)
        ],
    }


class TestFindCarryForwardFeedback:
    def test_matches_submitted_same_questions_and_phase(self):
        existing = _fb("feedback-1", ["What scope?", "Why now?"], phase="plan", submitted=True)
        match = find_carry_forward_feedback(existing, ["What scope?", "Why now?"], "plan")
        assert match is not None
        assert match["id"] == "feedback-1"

    def test_ignores_unsubmitted(self):
        existing = _fb("feedback-1", ["What scope?"], phase="plan", submitted=False)
        assert find_carry_forward_feedback(existing, ["What scope?"], "plan") is None

    def test_none_existing_never_matches(self):
        assert find_carry_forward_feedback(None, ["What scope?"], "plan") is None

    def test_phase_scoped(self):
        existing = _fb("feedback-1", ["What scope?"], phase="refine", submitted=True)
        # Same questions, different phase → not a match (cross-phase re-ask
        # is a distinct request by design).
        assert find_carry_forward_feedback(existing, ["What scope?"], "plan") is None

    def test_normalization_matches_whitespace_and_case(self):
        existing = _fb("feedback-2", ["Drop the legacy filter?"], phase="plan", submitted=True)
        match = find_carry_forward_feedback(existing, ["  drop   the LEGACY filter? "], "plan")
        assert match is not None
        assert match["id"] == "feedback-2"

    def test_order_insensitive(self):
        existing = _fb("feedback-1", ["Why now?", "What scope?"], phase="plan", submitted=True)
        # Re-registered in a different order → still the same question set.
        match = find_carry_forward_feedback(existing, ["What scope?", "Why now?"], "plan")
        assert match is not None

    def test_different_question_set_does_not_match(self):
        existing = _fb("feedback-1", ["What scope?"], phase="plan", submitted=True)
        # A genuinely-new question set is a distinct request and must be
        # registered fresh, not carried forward.
        assert find_carry_forward_feedback(existing, ["A different question?"], "plan") is None

    def test_subset_does_not_match(self):
        existing = _fb("feedback-1", ["What scope?", "Why now?"], phase="plan", submitted=True)
        assert find_carry_forward_feedback(existing, ["What scope?"], "plan") is None

    def test_empty_questions_never_match(self):
        existing = _fb("feedback-1", ["What scope?"], phase="plan", submitted=True)
        assert find_carry_forward_feedback(existing, [], "plan") is None

    def test_blank_question_never_matches(self):
        existing = _fb("feedback-1", [""], phase="plan", submitted=True)
        assert find_carry_forward_feedback(existing, [""], "plan") is None
