"""Tests for ``decisions.find_resolved_question`` carry-forward (#3392).

The converge-before-advance HITL loop re-runs a refine/plan phase after
the operator resolves its decisions, so the phase's agents can re-register
a question that was already *answered* in a prior round. Minting a fresh
``cq-N`` for an answered question would re-surface it every round and the
loop would never reach a fixpoint. ``find_resolved_question`` is the
resolved-aware counterpart to ``find_duplicate_open_question`` that lets
the registration paths adopt the prior resolved decision instead.

These tests cover the matcher in isolation against both ``Decision``
instances and plain contract dicts, and assert the two functions partition
the open/resolved sets (neither bleeds into the other).
"""

from __future__ import annotations

from egg_contracts.decisions import (
    find_duplicate_open_question,
    find_resolved_question,
)


def _dec(
    id_: str,
    question: str,
    *,
    phase: str | None,
    resolved: bool,
    type_: str = "hitl",
) -> dict:
    """A plain contract-dict decision (the gateway JSON shape)."""
    return {
        "id": id_,
        "question": question,
        "type": type_,
        "phase": phase,
        "resolved": resolved,
        "resolution": "answered" if resolved else None,
    }


class TestFindResolvedQuestion:
    def test_matches_resolved_same_question_and_phase(self):
        existing = [_dec("cq-1", "Drop the legacy filter?", phase="plan", resolved=True)]
        match = find_resolved_question(existing, "Drop the legacy filter?", "plan")
        assert match is not None
        assert match["id"] == "cq-1"

    def test_ignores_unresolved(self):
        existing = [_dec("cq-1", "Drop the legacy filter?", phase="plan", resolved=False)]
        assert find_resolved_question(existing, "Drop the legacy filter?", "plan") is None

    def test_phase_scoped(self):
        existing = [_dec("cq-1", "Drop the legacy filter?", phase="refine", resolved=True)]
        # Same question text, different phase → not a match (cross-phase
        # re-ask is a distinct decision by design).
        assert find_resolved_question(existing, "Drop the legacy filter?", "plan") is None

    def test_normalization_matches_whitespace_and_case(self):
        existing = [_dec("cq-2", "Drop the legacy filter?", phase="plan", resolved=True)]
        match = find_resolved_question(existing, "  drop   the LEGACY filter? ", "plan")
        assert match is not None
        assert match["id"] == "cq-2"

    def test_ignores_non_hitl(self):
        existing = [
            _dec("cq-1", "Drop the legacy filter?", phase="plan", resolved=True, type_="choice")
        ]
        assert find_resolved_question(existing, "Drop the legacy filter?", "plan") is None

    def test_partitions_open_vs_resolved(self):
        """The open and resolved matchers must not bleed into each other."""
        resolved = _dec("cq-1", "Q?", phase="plan", resolved=True)
        assert find_resolved_question([resolved], "Q?", "plan") is not None
        assert find_duplicate_open_question([resolved], "Q?", "plan") is None

        open_ = _dec("cq-2", "Q?", phase="plan", resolved=False)
        assert find_duplicate_open_question([open_], "Q?", "plan") is not None
        assert find_resolved_question([open_], "Q?", "plan") is None

    def test_empty_question_never_matches(self):
        existing = [_dec("cq-1", "", phase="plan", resolved=True)]
        assert find_resolved_question(existing, "", "plan") is None
