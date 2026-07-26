"""Tests for bare-string phase-gate resolution parsing (#3636).

A phase gate offers ``["approve", "request changes"]``, so operators answer
with the option word plus a justification. Matching the *whole* resolution
string against ``_APPROVE_KEYWORDS`` classified every such answer as free-text
change requests: the gate silently took the revision branch, burned an
``max_hitl_review_cycles`` slot, and fed the approval back to the producers as
revision feedback.

Covers the shared first-line classifier and both of its consumers:
``_classify_bare_gate_resolution`` and the ``_parse_resolution`` recovery path.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Mock heavy dependencies that the pipelines package imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (  # noqa: E402
    _classify_bare_gate_resolution,
    _parse_resolution,
)

# The exact shape from the issue report: the literal option word, a blank
# line, then the operator's justification.
APPROVE_WITH_CONTEXT = (
    "approve\n\nApproved. The analysis is sound in its conclusion and its scope; advance to plan."
)


class TestApproveWithContext:
    """The regression: option word + justification must read as approval."""

    def test_approve_plus_justification_is_approved(self):
        approved, feedback, context = _classify_bare_gate_resolution(APPROVE_WITH_CONTEXT)

        assert approved is True
        assert feedback is None

    def test_justification_is_preserved_as_context(self):
        _, _, context = _classify_bare_gate_resolution(APPROVE_WITH_CONTEXT)

        assert context.startswith("Approved. The analysis is sound")
        # The option word itself is the selection, not part of the note.
        assert not context.startswith("approve")

    @pytest.mark.parametrize(
        "resolution",
        [
            "approve\n\nLooks good.",
            "Approve\nShip it.",
            "APPROVED\n\nNo objections.",
            "lgtm\n\nThe scope is right.",
            "yes\n\nProceed.",
        ],
    )
    def test_every_approve_keyword_accepts_a_trailing_note(self, resolution):
        approved, feedback, context = _classify_bare_gate_resolution(resolution)

        assert approved is True
        assert feedback is None
        assert context

    @pytest.mark.parametrize("resolution", ["Approved.", "LGTM!", "approve."])
    def test_trailing_sentence_punctuation_is_not_part_of_the_option_word(self, resolution):
        approved, feedback, _ = _classify_bare_gate_resolution(resolution)

        assert approved is True
        assert feedback is None


class TestBareResolutions:
    """The pre-existing bare-word behaviour must not shift."""

    @pytest.mark.parametrize("resolution", ["approve", "approved", "lgtm", "yes", ""])
    def test_bare_approve_keyword_is_approved_with_no_context(self, resolution):
        assert _classify_bare_gate_resolution(resolution) == (True, None, "")

    def test_none_is_approved(self):
        """A timed-out / unset resolution advances, as it always has."""
        assert _classify_bare_gate_resolution(None) == (True, None, "")

    @pytest.mark.parametrize("resolution", ["request changes", "request_changes"])
    def test_bare_request_changes_has_no_actionable_feedback(self, resolution):
        """Feedback is None so the caller asks a follow-up for specifics."""
        assert _classify_bare_gate_resolution(resolution) == (False, None, "")

    def test_request_changes_with_specifics_carries_the_remainder(self):
        approved, feedback, _ = _classify_bare_gate_resolution(
            "request changes\n\nThe risk section omits the rollback path."
        )

        assert approved is False
        # The redundant option word is dropped; the actionable part remains.
        assert feedback == "The risk section omits the rollback path."


class TestFreeTextStillRequestsChanges:
    """Only the option-word-plus-context shape changes meaning."""

    @pytest.mark.parametrize(
        "resolution",
        [
            "please fix the error handling",
            "The risk section is missing.",
            # A first line that merely *contains* an approve keyword is not a
            # selection; it is a sentence, and it stays a change request.
            "approve the rewrite but drop the caching slice",
            "I would approve this if the rollback path were covered",
            "not approved\n\nThe scope is wrong.",
        ],
    )
    def test_free_text_is_revision_feedback(self, resolution):
        approved, feedback, context = _classify_bare_gate_resolution(resolution)

        assert approved is False
        assert feedback == resolution
        assert context == ""

    def test_free_text_feedback_keeps_its_full_text(self):
        """Multi-line free text is not truncated to its first line."""
        resolution = "The plan under-scopes slice 3.\n\nSplit it before advancing."
        approved, feedback, _ = _classify_bare_gate_resolution(resolution)

        assert approved is False
        assert feedback == resolution


class TestParseResolutionRecoveryPath:
    """``_parse_resolution`` backs the AWAITING_HUMAN restart path.

    It shared the whole-string membership test, so a restart re-derived the
    same wrong branch from the same stored resolution.
    """

    def test_approve_with_context_is_approved(self):
        assert _parse_resolution(APPROVE_WITH_CONTEXT) == (True, None)

    def test_bare_approve_is_approved(self):
        assert _parse_resolution("approve") == (True, None)

    def test_empty_and_none_are_approved(self):
        assert _parse_resolution("") == (True, None)
        assert _parse_resolution(None) == (True, None)

    def test_free_text_is_a_change_request(self):
        assert _parse_resolution("please fix the tests") == (False, "please fix the tests")

    def test_bare_request_changes_has_no_feedback(self):
        assert _parse_resolution("request changes") == (False, None)

    def test_json_approve_still_wins_over_keyword_matching(self):
        """JSON-first parsing is unchanged; the classifier is the fallback."""
        assert _parse_resolution('{"action": "approve", "context": "Advance."}') == (True, None)

    def test_json_request_changes_carries_its_feedback(self):
        assert _parse_resolution('{"action": "request_changes", "feedback": "Fix X"}') == (
            False,
            "Fix X",
        )
