"""Tests for the lens-reviewer criteria loaders, dispatcher, and scope preambles.

Covers TASK-2-3 of issue #1965:

- ``_get_security_review_criteria`` and ``_get_concurrency_review_criteria``
  load from ``shared/prompts/`` when present.
- Both fall back to a non-empty inline string when the shared file is missing.
- ``_get_review_criteria_for_type`` dispatches the new ``"security"`` and
  ``"concurrency"`` types to the right loader (and NOT to
  ``_get_code_review_criteria``).
- ``_get_reviewer_scope_preamble`` returns lens-specific preambles for both
  types, distinct from the ``code`` preamble and from each other.
- Security preamble does NOT contain the self-contradictory literal
  "Do NOT review security"; concurrency preamble does NOT contain
  "Do NOT review concurrency".
- ``_build_review_prompt(reviewer_type="security", ...)`` produces a
  prompt containing the security criteria content and the security scope
  preamble.

These tests use the source-tree shared prompt files when they exist and
verify the inline-fallback path by patching ``_read_shared_criteria`` to
return ``None``.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub Docker the same way test_pipeline_prompts.py does.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (  # noqa: E402
    _build_review_prompt,
    _get_code_review_criteria,
    _get_concurrency_review_criteria,
    _get_review_criteria_for_type,
    _get_reviewer_scope_preamble,
    _get_security_review_criteria,
)

# ---------------------------------------------------------------------------
# Loader tests — shared-file load + inline-fallback parity.
# ---------------------------------------------------------------------------


class TestSecurityCriteriaLoader:
    def test_returns_non_empty(self) -> None:
        assert _get_security_review_criteria().strip() != ""

    def test_loads_from_shared_file(self) -> None:
        """Happy path: the loader returns the on-disk file's contents.

        Asserts the regression-guard markers from TASK-2-1's required body
        (``cross-file allowlist mismatch`` and
        ``handler-vs-validator path mismatch``) plus the section-4
        ``Dockerfile-symlink`` slug from the PR #1964 jira-wrapper pattern,
        so a future edit can't silently drop the lens.
        """
        content = _get_security_review_criteria()
        assert "cross-file allowlist mismatch" in content.lower()
        assert "handler-vs-validator path mismatch" in content.lower()
        assert "dockerfile-symlink" in content.lower()

    def test_inline_fallback_when_shared_file_missing(self) -> None:
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            content = _get_security_review_criteria()
        assert content.strip() != "", "Security inline fallback must be non-empty"
        # Section-4 parity with on-disk file: the inline fallback also names
        # the PR #1964 jira-wrapper Dockerfile/symlink-mismatch pattern, so a
        # future edit cannot silently drop the lens from the fallback path.
        assert "dockerfile-symlink" in content.lower()


class TestConcurrencyCriteriaLoader:
    def test_returns_non_empty(self) -> None:
        assert _get_concurrency_review_criteria().strip() != ""

    def test_loads_from_shared_file(self) -> None:
        """The on-disk concurrency criteria mention concurrency-lens topics."""
        content = _get_concurrency_review_criteria().lower()
        # At least one of the canonical concurrency topics enumerated in
        # TASK-2-1 (race, deadlock, async, retry-storm, …).
        assert any(
            keyword in content
            for keyword in (
                "race",
                "deadlock",
                "async",
                "retry",
                "shared state",
                "shared-state",
            )
        ), (
            "Concurrency criteria file should enumerate canonical "
            "concurrency-lens topics (race, deadlock, async, retry-storm, "
            "shared-state mutation)."
        )

    def test_inline_fallback_when_shared_file_missing(self) -> None:
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            content = _get_concurrency_review_criteria()
        assert content.strip() != "", "Concurrency inline fallback must be non-empty"


# ---------------------------------------------------------------------------
# Dispatcher — _get_review_criteria_for_type routes the new types.
# ---------------------------------------------------------------------------


class TestGetReviewCriteriaForTypeDispatch:
    def test_security_routes_to_security_loader(self) -> None:
        text = _get_review_criteria_for_type("security", "implement")
        # Identity check by sentinel string from the shared file.
        assert text == _get_security_review_criteria()

    def test_concurrency_routes_to_concurrency_loader(self) -> None:
        text = _get_review_criteria_for_type("concurrency", "implement")
        assert text == _get_concurrency_review_criteria()

    def test_security_does_not_route_to_code_loader(self) -> None:
        """Deliberate-regression guard from TASK-2-3 acceptance.

        If a future PR mistakenly aliases ``"security"`` back to
        ``_get_code_review_criteria()``, this test fires.
        """
        security_text = _get_review_criteria_for_type("security", "implement")
        code_text = _get_code_review_criteria()
        assert security_text != code_text, (
            "Dispatcher must not route reviewer_type='security' to the "
            "general code review criteria; it must use "
            "_get_security_review_criteria()."
        )

    def test_concurrency_does_not_route_to_code_loader(self) -> None:
        concurrency_text = _get_review_criteria_for_type("concurrency", "implement")
        code_text = _get_code_review_criteria()
        assert concurrency_text != code_text, (
            "Dispatcher must not route reviewer_type='concurrency' to the "
            "general code review criteria; it must use "
            "_get_concurrency_review_criteria()."
        )

    def test_existing_dispatcher_branches_unchanged(self) -> None:
        # Sanity: existing reviewer types still resolve.
        for reviewer_type in ("code", "contract", "agent-design", "refine", "plan"):
            text = _get_review_criteria_for_type(reviewer_type, "implement")
            assert text.strip() != ""


# ---------------------------------------------------------------------------
# Scope preambles — lens-specific framing.
# ---------------------------------------------------------------------------


class TestLensScopePreambles:
    def test_security_preamble_non_empty(self) -> None:
        preamble = _get_reviewer_scope_preamble("security", "implement")
        assert preamble.strip() != ""

    def test_concurrency_preamble_non_empty(self) -> None:
        preamble = _get_reviewer_scope_preamble("concurrency", "implement")
        assert preamble.strip() != ""

    def test_security_and_concurrency_preambles_differ(self) -> None:
        assert _get_reviewer_scope_preamble(
            "security", "implement"
        ) != _get_reviewer_scope_preamble("concurrency", "implement")

    def test_security_preamble_distinct_from_code_preamble(self) -> None:
        assert _get_reviewer_scope_preamble(
            "security", "implement"
        ) != _get_reviewer_scope_preamble("code", "implement")

    def test_concurrency_preamble_distinct_from_code_preamble(self) -> None:
        assert _get_reviewer_scope_preamble(
            "concurrency", "implement"
        ) != _get_reviewer_scope_preamble("code", "implement")

    def test_security_preamble_focuses_on_security(self) -> None:
        """The preamble explicitly scopes the reviewer to the security lens."""
        preamble = _get_reviewer_scope_preamble("security", "implement").lower()
        assert "security" in preamble

    def test_concurrency_preamble_focuses_on_concurrency(self) -> None:
        preamble = _get_reviewer_scope_preamble("concurrency", "implement").lower()
        assert "concurrency" in preamble

    def test_security_preamble_avoids_self_contradictory_phrasing(self) -> None:
        """TASK-2-2 forbids the literal 'Do NOT review security' phrasing.

        That phrasing was copy-pasted from the agent-design preamble and is
        actively self-contradictory for the security lens.
        """
        preamble = _get_reviewer_scope_preamble("security", "implement")
        assert "Do NOT review security" not in preamble, (
            "Security preamble must not contain the phrase 'Do NOT review "
            "security' — the security lens reviewer reviews security! "
            "Phrase the scope as 'Focus ONLY on the security lens; defer "
            "non-security findings to reviewer_code.'"
        )

    def test_concurrency_preamble_avoids_self_contradictory_phrasing(self) -> None:
        preamble = _get_reviewer_scope_preamble("concurrency", "implement")
        assert "Do NOT review concurrency" not in preamble, (
            "Concurrency preamble must not contain 'Do NOT review "
            "concurrency' — the concurrency lens reviewer reviews "
            "concurrency."
        )

    def test_security_preamble_is_critical_not_advisory(self) -> None:
        """Regression for PR #2152: the security lens is CRITICAL per #2139.

        The preamble must NOT call the review ADVISORY (the prior wording
        before lens promotion) and must announce CRITICAL gating with a
        #2139 reference so prompt drift can't quietly revert it.
        """
        preamble = _get_reviewer_scope_preamble("security", "implement")
        assert "ADVISORY" not in preamble, (
            "Security preamble must not label the review ADVISORY — the "
            "lens was promoted to CRITICAL in #2139."
        )
        assert "CRITICAL" in preamble, (
            "Security preamble must announce CRITICAL gating to match the "
            "review-graph edge (orchestrator/review_graph.py)."
        )
        assert "#2139" in preamble, (
            "Security preamble must reference #2139 (the lens-promotion "
            "issue) so the gating rationale is traceable."
        )

    def test_concurrency_preamble_is_critical_not_advisory(self) -> None:
        preamble = _get_reviewer_scope_preamble("concurrency", "implement")
        assert "ADVISORY" not in preamble, (
            "Concurrency preamble must not label the review ADVISORY — the "
            "lens was promoted to CRITICAL in #2139."
        )
        assert "CRITICAL" in preamble, (
            "Concurrency preamble must announce CRITICAL gating to match "
            "the review-graph edge (orchestrator/review_graph.py)."
        )
        assert "#2139" in preamble, (
            "Concurrency preamble must reference #2139 (the lens-promotion "
            "issue) so the gating rationale is traceable."
        )

    def test_security_preamble_warns_about_brc_minimum_content_length(self) -> None:
        """The security preamble warns the agent about the BRC content-length floor.

        Regression for the egg-reviewer feedback on PR #2061: the original
        wording ("a brief approval is acceptable") would have led security
        reviewers to draft sub-50-char ACK content that the BRC bus rejects
        (``_BRC_MIN_CONTENT_LEN = 50``). The preamble must steer the agent
        toward "at least a sentence or two" so a literal one-word ``LGTM``
        is avoided up front.
        """
        preamble = _get_reviewer_scope_preamble("security", "implement")
        assert "sentence or two" in preamble, (
            "Security preamble must mention 'sentence or two' to steer "
            "agents away from sub-50-char ACK content the BRC bus rejects."
        )

    def test_concurrency_preamble_warns_about_brc_minimum_content_length(self) -> None:
        """Concurrency-lens analogue of the BRC content-length warning."""
        preamble = _get_reviewer_scope_preamble("concurrency", "implement")
        assert "sentence or two" in preamble, (
            "Concurrency preamble must mention 'sentence or two' to steer "
            "agents away from sub-50-char ACK content the BRC bus rejects."
        )


# ---------------------------------------------------------------------------
# End-to-end: _build_review_prompt routes through to lens criteria.
# ---------------------------------------------------------------------------


class TestBuildReviewPromptUsesLensCriteria:
    @pytest.mark.parametrize(
        "reviewer_type, loader",
        [
            ("security", _get_security_review_criteria),
            ("concurrency", _get_concurrency_review_criteria),
        ],
    )
    def test_prompt_contains_lens_criteria_content(self, reviewer_type: str, loader) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type=reviewer_type,
            issue_number=100,
        )
        criteria_text = loader()
        # Sentinel: the first non-empty line of the criteria file should
        # appear verbatim in the assembled prompt.
        sentinel = next(
            (line.strip() for line in criteria_text.splitlines() if line.strip()),
            "",
        )
        assert sentinel and sentinel in prompt, (
            f"_build_review_prompt(reviewer_type={reviewer_type!r}) did not "
            "embed the lens criteria content. Verify the dispatcher and "
            "loader are wired up."
        )

    def test_security_prompt_uses_security_preamble(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="security",
            issue_number=100,
        )
        # The lens preamble's 'security' word should land in the assembled
        # prompt; the code preamble's 'comprehensive code review' should
        # NOT, since dispatch should select the security preamble.
        assert "comprehensive code review" not in prompt.lower()

    def test_concurrency_prompt_uses_concurrency_preamble(self) -> None:
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="concurrency",
            issue_number=100,
        )
        assert "comprehensive code review" not in prompt.lower()


class TestSharedPromptFilesExist:
    """The shared prompt files MUST be present on disk (TASK-2-1 acceptance)."""

    def test_security_review_criteria_exists(self) -> None:
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent.parent
            / "shared"
            / "prompts"
            / "security-review-criteria.md"
        )
        assert path.is_file(), (
            f"Missing required prompt file: {path}. TASK-2-1 requires "
            "shared/prompts/security-review-criteria.md to exist."
        )

    def test_concurrency_review_criteria_exists(self) -> None:
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent.parent
            / "shared"
            / "prompts"
            / "concurrency-review-criteria.md"
        )
        assert path.is_file(), (
            f"Missing required prompt file: {path}. TASK-2-1 requires "
            "shared/prompts/concurrency-review-criteria.md to exist."
        )

    def test_security_inherits_header_present(self) -> None:
        text = _get_security_review_criteria()
        assert (
            "Inherits from `code-review-criteria.md`" in text
            or "inherits from `code-review-criteria.md`" in text.lower()
        ), (
            "Security criteria file must open with the explicit "
            '"Inherits from `code-review-criteria.md`" header (TASK-2-1 '
            "acceptance)."
        )

    def test_concurrency_inherits_header_present(self) -> None:
        text = _get_concurrency_review_criteria()
        assert (
            "Inherits from `code-review-criteria.md`" in text
            or "inherits from `code-review-criteria.md`" in text.lower()
        ), (
            "Concurrency criteria file must open with the explicit "
            '"Inherits from `code-review-criteria.md`" header.'
        )
