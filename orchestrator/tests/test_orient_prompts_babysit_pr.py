"""Tests for babysit-mode orient/preparation prompts (#1748).

Covers ``_build_reviewer_preparation()`` and ``_build_producer_orientation()``
in ``orchestrator/routes/pipelines.py``. In babysit mode, the implement-phase
pipeline runs a one-off BRC cycle against an existing PR's diff. These tests
lock in:

- reviewers get a PR-diff-first orientation (not the contract-first text),
- producers get a rebase-and-stay-in-scope preamble instructing them to
  escalate cross-role conflicts to ``conflict_resolver``,
- issue-mode (non-babysit) text remains unchanged (regression guard),
- base-branch interpolation into ``origin/<branch>`` is consistent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import PipelineMode  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _build_producer_orientation,
    _build_reviewer_preparation,
)


class TestBabysitReviewerPreparation:
    """reviewer_code / tester prep in babysit mode reads the PR diff first."""

    def test_reviewer_code_mentions_pr_diff(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "read the PR diff" in result

    def test_reviewer_code_mentions_independent_concerns(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "form independent concerns BEFORE producers broadcast" in result

    def test_reviewer_code_pr_hint_contains_pr_number(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "PR #1748" in result

    def test_reviewer_code_contains_git_diff_snippet(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "git diff origin/main...HEAD" in result

    def test_reviewer_code_mentions_tests_execution_blocked(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "tests_execution_blocked" in result

    def test_tester_mentions_edge_cases_and_regressions(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "Identify edge cases and regressions" in result

    def test_tester_pr_hint_contains_pr_number(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "PR #1748" in result

    def test_tester_contains_git_diff_snippet(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="develop",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "git diff origin/develop...HEAD" in result


class TestIssueModeReviewerPreparationRegression:
    """When mode is not BABYSIT the legacy contract-first text is returned."""

    def test_reviewer_code_mentions_egg_contract_show(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        assert "egg-contract show" in result

    def test_reviewer_code_scrutinizes_tests_execution_blocked(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        assert "tests_execution_blocked" in result
        assert "scrutinize" in result

    def test_reviewer_code_lacks_babysit_framing(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        # The babysit-specific "read the PR diff" phrase should not appear
        # in issue-mode prep.
        assert "read the PR diff" not in result
        assert "form independent concerns BEFORE producers broadcast" not in result

    def test_reviewer_code_issue_mode_has_no_pr_number_hint(self) -> None:
        # Passing pr_number in issue-mode is ignored — legacy text has no PR #.
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
            pr_number=1748,
        )
        assert "PR #1748" not in result

    def test_tester_mentions_egg_contract_show_and_edge_cases(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        assert "egg-contract show" in result
        assert "edge cases" in result

    def test_tester_issue_mode_has_no_pr_number_hint(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            branch="egg/fix",
            base_branch="main",
            pr_number=1748,
        )
        assert "PR #1748" not in result


class TestBabysitProducerOrientation:
    """Producer orient text in babysit mode rebases and stays in role scope."""

    def test_coder_mentions_git_fetch_origin(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "git fetch origin" in result

    def test_coder_mentions_rebase(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "rebase" in result

    def test_coder_uses_configured_base_branch(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="develop",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "origin/develop" in result

    def test_coder_restricts_to_role_scope(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "ONLY within your role's file scope" in result
        assert "do not touch files outside your role's allowed_write patterns" in result

    def test_coder_mentions_conflict_resolver_escalation(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "conflict_resolver" in result

    def test_coder_warns_against_off_diff_refactors(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "do not refactor outside the diff" in result

    def test_coder_includes_pr_number_hint(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "PR #1748" in result

    def test_tester_and_coder_share_babysit_preamble(self) -> None:
        # In babysit mode the preamble is role-agnostic — same text for every
        # producer role.
        coder = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        tester = _build_producer_orientation(
            "tester",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        documenter = _build_producer_orientation(
            "documenter",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert coder == tester == documenter

    def test_reviewer_awareness_appears_when_reviewers_nonempty(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code", "tester"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "reviewer_code, tester" in result
        assert "reviewed by" in result

    def test_reviewer_awareness_absent_when_reviewers_empty(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "reviewed by" not in result


class TestIssueModeProducerOrientationRegression:
    """Issue-mode producer orient text is unchanged by the babysit work."""

    def test_coder_mentions_egg_contract_show(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
        )
        assert "egg-contract show" in result

    def test_coder_issue_mode_has_no_babysit_text(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
        )
        assert "rebase" not in result
        assert "conflict_resolver" not in result
        assert "PR #" not in result

    def test_tester_issue_mode_has_test_infrastructure_text(self) -> None:
        result = _build_producer_orientation(
            "tester",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
        )
        assert "test infrastructure" in result
        # No babysit-specific phrasing leaks in.
        assert "conflict_resolver" not in result
        assert "PR #" not in result


class TestBaseRefInterpolation:
    """Base-branch string lands as ``origin/<branch>`` everywhere."""

    def test_babysit_orient_interpolates_develop(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="develop",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "origin/develop" in result
        assert "origin/main" not in result

    def test_babysit_orient_none_base_branch_falls_back_to_main(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch=None,
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "origin/main" in result

    def test_babysit_orient_explicit_main(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "origin/main" in result

    def test_babysit_reviewer_prep_interpolates_develop(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="develop",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "git diff origin/develop...HEAD" in result
        assert "origin/main" not in result

    def test_babysit_reviewer_prep_none_base_branch_falls_back_to_main(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch=None,
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "git diff origin/main...HEAD" in result


class TestPRCheckoutStepB1:
    """Step (0) ``gh pr checkout`` must pin into producer orient + reviewer prep.

    Locks in the #1748 reviewer_code B1 fix (commit 7544b5300): before that
    commit the producer worktree stayed on the base branch and
    ``git diff base...HEAD`` was empty — the feature did nothing. A silent
    regression that drops the checkout step from either ``_build_producer_orientation``
    or ``_build_reviewer_preparation`` would pass the rest of the suite; this
    class exists specifically to prevent that.
    """

    def test_producer_orient_includes_gh_pr_checkout_with_pr_number(self) -> None:
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        # Exact ``gh pr checkout <N>`` command with the passed pr_number.
        assert "gh pr checkout 1748" in result
        # Step-(0) intent language — the "why" of the checkout.
        assert "check out the PR head" in result

    def test_producer_orient_uses_generic_fallback_when_pr_number_none(self) -> None:
        # When pr_number is missing the prompt falls back to the literal
        # placeholder ``<pr_number>`` (pipelines.py gh-checkout ternary).
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=None,
        )
        assert "gh pr checkout <pr_number>" in result
        assert "gh pr checkout 1748" not in result

    def test_producer_orient_explains_empty_diff_rationale(self) -> None:
        # The rationale ties the checkout step to the empty-diff failure mode
        # so the agent understands skipping it is not optional.
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "sitting on the base branch" in result
        assert "none of the PR's changes are" in result

    def test_reviewer_code_prep_includes_gh_pr_checkout_with_pr_number(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "gh pr checkout 1748" in result
        assert "check out the PR head" in result

    def test_reviewer_code_prep_uses_generic_fallback_when_pr_number_none(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=None,
        )
        assert "gh pr checkout <pr_number>" in result
        assert "gh pr checkout 1748" not in result

    def test_reviewer_code_prep_explains_empty_diff_rationale(self) -> None:
        # Step (0)'s "otherwise the diff will be empty" rationale — the
        # reviewer_code text says "diff below will be empty because your
        # worktree is on the base branch" (pipelines.py:6421-6424).
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "diff below will be empty" in result
        assert "base branch" in result

    def test_tester_prep_includes_gh_pr_checkout_with_pr_number(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        assert "gh pr checkout 1748" in result
        # Tester prep uses "Check out the PR head first" (capitalized C).
        assert "Check out the PR head" in result

    def test_tester_prep_uses_generic_fallback_when_pr_number_none(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=None,
        )
        assert "gh pr checkout <pr_number>" in result
        assert "gh pr checkout 1748" not in result

    def test_tester_prep_explains_empty_diff_rationale(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        # Tester text: "your worktree is sitting on the base branch and the
        # diff below will be empty" (pipelines.py:6440-6442).
        assert "diff below will be empty" in result
        assert "sitting on the base branch" in result

    def test_issue_mode_producer_orient_has_no_gh_pr_checkout(self) -> None:
        """``gh pr checkout`` is babysit-only — issue-mode must not mention it."""
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
        )
        assert "gh pr checkout" not in result
        # Also guard the fallback form in case someone wires it in loosely.
        assert "<pr_number>" not in result

    def test_issue_mode_producer_orient_has_no_gh_pr_checkout_with_mode_none(self) -> None:
        """Explicit ``mode=None`` must also avoid the babysit-specific text."""
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=["reviewer_code"],
            branch="egg/fix",
            base_branch="main",
            mode=None,
            pr_number=1748,
        )
        assert "gh pr checkout" not in result

    def test_issue_mode_reviewer_code_prep_has_no_gh_pr_checkout(self) -> None:
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        assert "gh pr checkout" not in result
        assert "<pr_number>" not in result

    def test_issue_mode_tester_prep_has_no_gh_pr_checkout(self) -> None:
        result = _build_reviewer_preparation(
            "tester",
            "implement",
            branch="egg/fix",
            base_branch="main",
        )
        assert "gh pr checkout" not in result
        assert "<pr_number>" not in result

    def test_producer_orient_checkout_command_precedes_rebase(self) -> None:
        """Step (0) checkout must come BEFORE step (1) rebase — otherwise the
        rebase runs against the base-branch tree and any conflicts are fake."""
        result = _build_producer_orientation(
            "coder",
            "implement",
            reviewers=[],
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        checkout_idx = result.find("gh pr checkout 1748")
        rebase_idx = result.find("rebase")
        assert checkout_idx != -1, "producer orient should include gh pr checkout"
        assert rebase_idx != -1, "producer orient should include rebase step"
        assert checkout_idx < rebase_idx, (
            "gh pr checkout (step 0) must appear before rebase (step 1)"
        )

    def test_reviewer_code_prep_checkout_precedes_diff(self) -> None:
        """Step (0) checkout must come BEFORE the ``git diff`` step so the
        reviewer reads the right tree."""
        result = _build_reviewer_preparation(
            "reviewer_code",
            "implement",
            base_branch="main",
            mode=PipelineMode.BABYSIT,
            pr_number=1748,
        )
        checkout_idx = result.find("gh pr checkout 1748")
        diff_idx = result.find("git diff origin/main...HEAD")
        assert checkout_idx != -1
        assert diff_idx != -1
        assert checkout_idx < diff_idx, (
            "gh pr checkout (step 0) must appear before git diff (step 1)"
        )
