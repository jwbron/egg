"""Tests for egg_babysit.steps — conflict, check_fix, review, feedback."""

from unittest.mock import patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.fixer import FixerResult
from egg_babysit.reviewer import ReviewResult
from egg_babysit.steps.check_fix import _match_job, fix_failed_checks
from egg_babysit.steps.conflict import resolve_conflicts
from egg_babysit.steps.feedback import address_feedback
from egg_babysit.steps.review import run_review
from egg_babysit.types import CICheckResult, CICheckStatus, PRState, ReviewVerdict


@pytest.fixture
def config():
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        max_retries_per_job=3,
        max_feedback_rounds=3,
    )


def _make_pr_state(**overrides):
    defaults = {
        "number": 42,
        "title": "Test PR",
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "abc123",
        "base_branch": "main",
        "head_branch": "feature",
    }
    defaults.update(overrides)
    return PRState(**defaults)


# --- Conflict resolution tests ---


class TestResolveConflicts:
    """Test resolve_conflicts step."""

    def test_resolve_conflicts_clean(self, config):
        """No conflicts means immediate success."""
        pr = _make_pr_state(mergeable_state="clean")
        result = resolve_conflicts(config, pr)

        assert result.success is True
        assert result.escalate is False

    @patch("egg_babysit.steps.conflict.fetch_pr_state")
    @patch("egg_babysit.steps.conflict.run_fixer")
    def test_resolve_conflicts_dirty_fixed(self, mock_fixer, mock_fetch, config):
        """Conflicts resolved by fixer agent."""
        pr = _make_pr_state(mergeable_state="dirty")
        mock_fixer.return_value = FixerResult(success=True, commit_sha="new_sha")
        mock_fetch.return_value = _make_pr_state(mergeable_state="clean")

        result = resolve_conflicts(config, pr)

        assert result.success is True
        assert result.escalate is False

    @patch("egg_babysit.steps.conflict.run_fixer")
    def test_resolve_conflicts_fixer_fails(self, mock_fixer, config):
        """Fixer fails to resolve conflicts -> escalate."""
        pr = _make_pr_state(mergeable_state="dirty")
        mock_fixer.return_value = FixerResult(success=False, error="Could not merge")

        result = resolve_conflicts(config, pr)

        assert result.success is False
        assert result.escalate is True

    @patch("egg_babysit.steps.conflict.fetch_pr_state")
    @patch("egg_babysit.steps.conflict.run_fixer")
    def test_resolve_conflicts_still_dirty_after_fix(self, mock_fixer, mock_fetch, config):
        """Fixer succeeds but conflicts persist -> escalate."""
        pr = _make_pr_state(mergeable_state="dirty")
        mock_fixer.return_value = FixerResult(success=True)
        mock_fetch.return_value = _make_pr_state(mergeable_state="dirty")

        result = resolve_conflicts(config, pr)

        assert result.success is False
        assert result.escalate is True

    @patch("egg_babysit.steps.conflict.fetch_pr_state")
    @patch("egg_babysit.steps.conflict.run_fixer")
    def test_resolve_conflicts_verify_fails(self, mock_fixer, mock_fetch, config):
        """Verification fetch fails, return failure without escalation to retry."""
        pr = _make_pr_state(mergeable_state="dirty")
        mock_fixer.return_value = FixerResult(success=True)
        mock_fetch.side_effect = Exception("API error")

        result = resolve_conflicts(config, pr)

        # Returns failure without escalation so the next iteration retries
        assert result.success is False
        assert result.escalate is False


# --- Check fix tests ---


class TestFixFailedChecks:
    """Test fix_failed_checks step."""

    def test_no_failed_checks(self, config):
        result = fix_failed_checks(config, [], {})
        assert result.success is True

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix.run_fixer")
    def test_fix_failed_checks_llm_fallback(self, mock_fixer, mock_config, config):
        """No non-LLM fix available, falls back to LLM fixer."""
        mock_config.return_value = {}
        mock_fixer.return_value = FixerResult(success=True, commit_sha="abc")

        failed = [CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE")]
        retry_counts = {}

        result = fix_failed_checks(config, failed, retry_counts)

        assert result.success is True
        assert retry_counts["lint"] == 1
        mock_fixer.assert_called_once()

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix._commit_non_llm_fix")
    @patch("egg_babysit.steps.check_fix.run_non_llm_fix")
    def test_fix_failed_checks_non_llm_success(
        self, mock_non_llm, mock_commit, mock_config, config
    ):
        """Non-LLM fix command succeeds."""
        mock_config.return_value = {
            "workflows": {
                "Lint": {
                    "Python": {
                        "non_llm_fix": "make lint-fix",
                        "max_retries": 3,
                    }
                }
            },
            "defaults": {"max_retries": 3},
        }
        mock_non_llm.return_value = True
        mock_commit.return_value = True

        # Job name must match via substring
        failed = [
            CICheckResult(
                name="Python",
                status=CICheckStatus.FAILING,
                conclusion="FAILURE",
            )
        ]
        retry_counts = {}

        result = fix_failed_checks(config, failed, retry_counts)

        assert result.success is True
        mock_non_llm.assert_called_once()

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix.run_fixer")
    def test_fix_failed_checks_passes_base_branch(self, mock_fixer, mock_config, config):
        """base_branch is threaded through to load_check_fixers_config."""
        mock_config.return_value = {}
        mock_fixer.return_value = FixerResult(success=True, commit_sha="abc")

        failed = [CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE")]
        fix_failed_checks(config, failed, {}, base_branch="develop")

        mock_config.assert_called_once_with(config.check_fixers_path, base_branch="develop")

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    def test_fix_failed_checks_escalate_max_retries(self, mock_config, config):
        """Exceeding max retries escalates."""
        mock_config.return_value = {}

        failed = [CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE")]
        retry_counts = {"lint": 3}  # Already at max

        result = fix_failed_checks(config, failed, retry_counts)

        assert result.success is False
        assert result.escalate is True
        assert "max retries" in result.message.lower()


class TestMatchJob:
    """Test _match_job substring matching."""

    def test_exact_match(self):
        config = {"workflows": {"Lint": {"Python": {}}}}
        workflow, job = _match_job("Python", config)
        assert workflow == "Lint"
        assert job == "Python"

    def test_substring_match_job_in_name(self):
        config = {"workflows": {"Lint": {"Python": {}}}}
        workflow, job = _match_job("Lint / Python (3.12)", config)
        assert workflow == "Lint"
        assert job == "Python"

    def test_no_match(self):
        config = {"workflows": {"Lint": {"Python": {}}}}
        workflow, job = _match_job("Deploy", config)
        assert workflow == ""
        assert job == ""

    def test_empty_config(self):
        workflow, job = _match_job("Python", {})
        assert workflow == ""
        assert job == ""


# --- Review step tests ---


class TestRunReview:
    """Test run_review step."""

    @patch("egg_babysit.steps.review.run_reviewer")
    def test_run_review_approved(self, mock_reviewer, config):
        mock_reviewer.return_value = ReviewResult(
            verdict=ReviewVerdict.APPROVED,
            comments=["LGTM"],
        )

        result = run_review(config)

        assert result.verdict == ReviewVerdict.APPROVED
        assert result.success is True

    @patch("egg_babysit.steps.review.run_reviewer")
    def test_run_review_changes_requested(self, mock_reviewer, config):
        mock_reviewer.return_value = ReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            comments=["Fix the bug on line 42"],
        )

        result = run_review(config)

        assert result.verdict == ReviewVerdict.CHANGES_REQUESTED
        assert result.success is True
        assert len(result.comments) > 0

    @patch("egg_babysit.steps.review.run_reviewer")
    def test_run_review_error(self, mock_reviewer, config):
        mock_reviewer.return_value = ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error="Agent crashed",
        )

        result = run_review(config)

        assert result.success is False
        assert result.verdict == ReviewVerdict.PENDING


# --- Feedback step tests ---


class TestAddressFeedback:
    """Test address_feedback step."""

    @patch("egg_babysit.steps.feedback.run_fixer")
    def test_address_feedback_success(self, mock_fixer, config):
        mock_fixer.return_value = FixerResult(success=True, commit_sha="new_sha")

        result = address_feedback(config, ["Fix the typo"], round_number=1)

        assert result.success is True
        assert result.escalate is False

    def test_address_feedback_no_comments(self, config):
        result = address_feedback(config, [], round_number=1)
        assert result.success is True

    def test_address_feedback_max_rounds(self, config):
        """Exceeding max feedback rounds escalates."""
        result = address_feedback(
            config,
            ["some feedback"],
            round_number=config.max_feedback_rounds + 1,
        )

        assert result.success is False
        assert result.escalate is True
        assert "max feedback rounds" in result.message.lower()

    @patch("egg_babysit.steps.feedback.run_fixer")
    def test_address_feedback_fixer_fails(self, mock_fixer, config):
        mock_fixer.return_value = FixerResult(success=False, error="Agent failed")

        result = address_feedback(config, ["Fix it"], round_number=1)

        assert result.success is False
        assert result.escalate is False

    @patch("egg_babysit.steps.feedback.run_fixer")
    def test_address_feedback_at_max_round(self, mock_fixer, config):
        """At max round (not exceeding) should still work."""
        mock_fixer.return_value = FixerResult(success=True)

        result = address_feedback(
            config,
            ["Fix this"],
            round_number=config.max_feedback_rounds,
        )

        assert result.success is True
