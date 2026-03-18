"""Tests for egg_babysit.reviewer — reviewer agent spawner."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.reviewer import ReviewResult, _extract_review_comments, run_reviewer
from egg_babysit.types import ReviewVerdict


@pytest.fixture
def config():
    return BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=600)


class TestExtractReviewComments:
    """Test _extract_review_comments helper."""

    def test_empty_stdout(self):
        assert _extract_review_comments("") == []

    def test_whitespace_only(self):
        assert _extract_review_comments("   \n  ") == []

    def test_single_comment(self):
        result = _extract_review_comments("Looks good overall")
        assert result == ["Looks good overall"]

    def test_multiline_treated_as_single(self):
        result = _extract_review_comments("Line 1\nLine 2\nLine 3")
        assert len(result) == 1
        assert "Line 1" in result[0]


class TestRunReviewer:
    """Test run_reviewer with mocked subprocess."""

    @patch("egg_babysit.reviewer.fetch_pr_state")
    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_approved(self, mock_build, mock_run, mock_fetch, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="LGTM", stderr="")
        from egg_babysit.types import PRState

        mock_fetch.return_value = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc",
            base_branch="main",
            head_branch="feature",
            review_verdict=ReviewVerdict.APPROVED,
        )

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.APPROVED
        assert result.error is None
        assert len(result.comments) > 0

    @patch("egg_babysit.reviewer.fetch_pr_state")
    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_changes_requested(self, mock_build, mock_run, mock_fetch, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="Fix the bug", stderr="")
        from egg_babysit.types import PRState

        mock_fetch.return_value = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc",
            base_branch="main",
            head_branch="feature",
            review_verdict=ReviewVerdict.CHANGES_REQUESTED,
        )

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.CHANGES_REQUESTED
        assert result.error is None

    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_agent_error(self, mock_build, mock_run, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Agent crashed")

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.PENDING
        assert result.error is not None
        assert "crashed" in result.error.lower() or "1" in result.error

    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_timeout(self, mock_build, mock_run, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.PENDING
        assert result.error is not None
        assert "timed out" in result.error.lower()

    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_unexpected_exception(self, mock_build, mock_run, config):
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.side_effect = OSError("Something broke")

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.PENDING
        assert result.error is not None

    @patch("egg_babysit.reviewer.fetch_pr_state")
    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.build_agent_command")
    def test_run_reviewer_fetch_verdict_fails(self, mock_build, mock_run, mock_fetch, config):
        """If fetching PR state for verdict fails, defaults to COMMENTED."""
        mock_build.return_value = ["claude", "--print", "prompt"]
        mock_run.return_value = MagicMock(returncode=0, stdout="review output", stderr="")
        mock_fetch.side_effect = Exception("API error")

        result = run_reviewer("Review this PR", config)

        assert result.verdict == ReviewVerdict.COMMENTED
        assert result.error is None


class TestReviewResult:
    """Test ReviewResult dataclass."""

    def test_basic_creation(self):
        result = ReviewResult(
            verdict=ReviewVerdict.APPROVED,
            comments=["LGTM"],
        )
        assert result.verdict == ReviewVerdict.APPROVED
        assert result.comments == ["LGTM"]
        assert result.error is None

    def test_with_error(self):
        result = ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error="Failed to run",
        )
        assert result.error == "Failed to run"
