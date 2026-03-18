"""Gap tests for egg_babysit.steps — non-LLM fix fallback, commit logic, edge cases."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.fixer import FixerResult
from egg_babysit.reviewer import ReviewResult
from egg_babysit.steps.check_fix import _commit_non_llm_fix, _match_job, fix_failed_checks
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


class TestNonLlmFixFallback:
    """Test non-LLM fix failing falls back to LLM fixer."""

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix._commit_non_llm_fix")
    @patch("egg_babysit.steps.check_fix.run_non_llm_fix")
    @patch("egg_babysit.steps.check_fix.run_fixer")
    def test_non_llm_fails_then_llm_succeeds(
        self, mock_fixer, mock_non_llm, mock_commit, mock_config, config
    ):
        """Non-LLM fix fails → falls back to LLM fixer which succeeds."""
        mock_config.return_value = {
            "workflows": {"Lint": {"Python": {"non_llm_fix": "make lint-fix"}}},
            "defaults": {"max_retries": 3},
        }
        mock_non_llm.return_value = False  # Non-LLM fix fails
        mock_fixer.return_value = FixerResult(success=True, commit_sha="abc")

        failed = [CICheckResult(name="Python", status=CICheckStatus.FAILING, conclusion="FAILURE")]

        result = fix_failed_checks(config, failed, {})

        assert result.success is True
        mock_non_llm.assert_called_once()
        mock_fixer.assert_called_once()

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix._commit_non_llm_fix")
    @patch("egg_babysit.steps.check_fix.run_non_llm_fix")
    @patch("egg_babysit.steps.check_fix.run_fixer")
    def test_non_llm_succeeds_but_no_changes(
        self, mock_fixer, mock_non_llm, mock_commit, mock_config, config
    ):
        """Non-LLM fix runs but produces no changes → falls back to LLM."""
        mock_config.return_value = {
            "workflows": {"Lint": {"Python": {"non_llm_fix": "make lint-fix"}}},
            "defaults": {"max_retries": 3},
        }
        mock_non_llm.return_value = True  # Command succeeds
        mock_commit.return_value = False  # But no changes to commit
        mock_fixer.return_value = FixerResult(success=True, commit_sha="abc")

        failed = [CICheckResult(name="Python", status=CICheckStatus.FAILING, conclusion="FAILURE")]

        result = fix_failed_checks(config, failed, {})

        assert result.success is True
        mock_fixer.assert_called_once()  # Fell back to LLM

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    @patch("egg_babysit.steps.check_fix.run_fixer")
    def test_multiple_jobs_mixed_results(self, mock_fixer, mock_config, config):
        """Multiple failing jobs: one succeeds, one fails."""
        mock_config.return_value = {}
        mock_fixer.side_effect = [
            FixerResult(success=True, commit_sha="abc"),
            FixerResult(success=False, error="Could not fix"),
        ]

        failed = [
            CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"),
            CICheckResult(name="test", status=CICheckStatus.FAILING, conclusion="FAILURE"),
        ]

        result = fix_failed_checks(config, failed, {})

        assert result.success is False
        assert "test" in result.message

    @patch("egg_babysit.steps.check_fix.load_check_fixers_config")
    def test_all_jobs_exceeding_retries(self, mock_config, config):
        """All jobs at max retries → escalation."""
        mock_config.return_value = {}

        failed = [
            CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"),
            CICheckResult(name="test", status=CICheckStatus.FAILING, conclusion="FAILURE"),
        ]
        retry_counts = {"lint": 3, "test": 3}

        result = fix_failed_checks(config, failed, retry_counts)

        assert result.success is False
        assert result.escalate is True
        assert "lint" in result.message
        assert "test" in result.message


class TestCommitNonLlmFix:
    """Test _commit_non_llm_fix internal function."""

    @patch("subprocess.run")
    def test_commit_succeeds(self, mock_run):
        """Normal commit flow: status → add → commit → push."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n"),  # git status
            MagicMock(returncode=0),  # git add -u
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
        ]

        result = _commit_non_llm_fix("lint", "/path/to/repo")

        assert result is True
        assert mock_run.call_count == 4

    @patch("subprocess.run")
    def test_commit_no_changes(self, mock_run):
        """No changes detected → returns False without committing."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")  # No changes

        result = _commit_non_llm_fix("lint", "/path/to/repo")

        assert result is False
        assert mock_run.call_count == 1  # Only git status called

    @patch("subprocess.run")
    def test_commit_push_fails(self, mock_run):
        """Push failure returns False."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n"),  # git status
            MagicMock(returncode=0),  # git add -u
            MagicMock(returncode=0),  # git commit
            subprocess.CalledProcessError(1, "git push"),  # push fails
        ]

        result = _commit_non_llm_fix("lint", "/path/to/repo")

        assert result is False

    @patch.dict("os.environ", {"EGG_REPO_PATH": "/env/repo"})
    @patch("subprocess.run")
    def test_commit_uses_env_fallback(self, mock_run):
        """Falls back to EGG_REPO_PATH when repo_path is empty."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        _commit_non_llm_fix("lint", "")

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["cwd"] == "/env/repo"


class TestMatchJobEdgeCases:
    """Additional edge cases for _match_job."""

    def test_case_insensitive_match(self):
        """Matching is case-insensitive."""
        config = {"workflows": {"Lint": {"python": {}}}}
        workflow, job = _match_job("Python", config)
        assert workflow == "Lint"
        assert job == "python"

    def test_non_dict_jobs_skipped(self):
        """Non-dict job values are skipped without error."""
        config = {"workflows": {"Lint": "not_a_dict"}}
        workflow, job = _match_job("Python", config)
        assert workflow == ""
        assert job == ""

    def test_substring_match_precedence(self):
        """Exact match takes precedence over substring match."""
        config = {
            "workflows": {
                "Lint": {
                    "Python": {"non_llm_fix": "exact"},
                    "Python (3.12)": {"non_llm_fix": "substring"},
                }
            }
        }
        workflow, job = _match_job("Python", config)
        assert job == "Python"  # Exact match wins

    def test_multiple_workflows(self):
        """Search across multiple workflows."""
        config = {
            "workflows": {
                "Lint": {"Shell": {}},
                "Test": {"Python": {}},
            }
        }
        workflow, job = _match_job("Python", config)
        assert workflow == "Test"
        assert job == "Python"


class TestResolveConflictsEdgeCases:
    """Additional conflict resolution edge cases."""

    @patch("egg_babysit.steps.conflict.fetch_pr_state")
    @patch("egg_babysit.steps.conflict.run_fixer")
    def test_resolve_with_elapsed_time(self, mock_fixer, mock_fetch, config):
        """Elapsed time is passed through to the fixer."""
        pr = _make_pr_state(mergeable_state="dirty")
        mock_fixer.return_value = FixerResult(success=True, commit_sha="new")
        mock_fetch.return_value = _make_pr_state(mergeable_state="clean")

        result = resolve_conflicts(config, pr, elapsed=500.0)

        assert result.success is True
        # Verify elapsed was passed to run_fixer
        call_kwargs = mock_fixer.call_args
        assert call_kwargs.kwargs.get("elapsed") == 500.0 or call_kwargs[1].get("elapsed") == 500.0


class TestAddressFeedbackEdgeCases:
    """Additional feedback addressing edge cases."""

    @patch("egg_babysit.steps.feedback.run_fixer")
    def test_feedback_with_multiple_comments(self, mock_fixer, config):
        """Multiple review comments are all included in the prompt."""
        mock_fixer.return_value = FixerResult(success=True, commit_sha="abc")

        comments = ["Fix bug on line 10", "Add missing type hint", "Update docstring"]
        result = address_feedback(config, comments, round_number=1)

        assert result.success is True
        # Verify all comments appear in the prompt
        call_args = mock_fixer.call_args[0]
        prompt = call_args[0]
        for comment in comments:
            assert comment in prompt

    @patch("egg_babysit.steps.feedback.run_fixer")
    def test_feedback_round_boundary(self, mock_fixer, config):
        """Feedback at exactly max round succeeds, at max+1 escalates."""
        mock_fixer.return_value = FixerResult(success=True)

        # At max round (3) - should work
        result = address_feedback(config, ["comment"], round_number=3)
        assert result.success is True

        # At max+1 - should escalate
        result = address_feedback(config, ["comment"], round_number=4)
        assert result.escalate is True

    def test_feedback_zero_max_rounds(self):
        """Config with max_feedback_rounds=0 means first round escalates."""
        zero_config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_feedback_rounds=0,
        )

        result = address_feedback(zero_config, ["comment"], round_number=1)

        assert result.escalate is True


class TestRunReviewEdgeCases:
    """Additional review step edge cases."""

    @patch("egg_babysit.steps.review.run_reviewer")
    def test_review_pending_verdict(self, mock_reviewer, config):
        """PENDING verdict when review fails."""
        mock_reviewer.return_value = ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error="Timeout",
        )

        result = run_review(config)

        assert result.success is False
        assert result.verdict == ReviewVerdict.PENDING
        assert "Timeout" in result.message

    @patch("egg_babysit.steps.review.run_reviewer")
    def test_review_commented_verdict(self, mock_reviewer, config):
        """COMMENTED verdict is treated as successful."""
        mock_reviewer.return_value = ReviewResult(
            verdict=ReviewVerdict.COMMENTED,
            comments=["Looks interesting"],
        )

        result = run_review(config)

        assert result.success is True
        assert result.verdict == ReviewVerdict.COMMENTED
