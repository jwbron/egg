"""Tests for egg_babysit.pr_state — PR state fetching and parsing."""

import json
import subprocess
from unittest.mock import patch

import pytest
from egg_babysit.pr_state import (
    _map_check_status,
    _parse_json,
    detect_head_sha_change,
    fetch_ci_checks,
    fetch_pr_state,
    fetch_review_comments,
    get_full_pr_state,
)
from egg_babysit.types import CICheckStatus, PRState, ReviewVerdict


class TestMapCheckStatus:
    """Test the _map_check_status helper."""

    def test_success_conclusion(self):
        assert _map_check_status("completed", "SUCCESS") == CICheckStatus.PASSING

    def test_failure_conclusion(self):
        assert _map_check_status("completed", "FAILURE") == CICheckStatus.FAILING

    def test_neutral_conclusion(self):
        assert _map_check_status("completed", "NEUTRAL") == CICheckStatus.PASSING

    def test_skipped_conclusion(self):
        assert _map_check_status("completed", "SKIPPED") == CICheckStatus.PASSING

    def test_cancelled_conclusion(self):
        assert _map_check_status("completed", "CANCELLED") == CICheckStatus.FAILING

    def test_timed_out_conclusion(self):
        assert _map_check_status("completed", "TIMED_OUT") == CICheckStatus.FAILING

    def test_pending_state_no_conclusion(self):
        assert _map_check_status("PENDING", "") == CICheckStatus.PENDING

    def test_in_progress_state(self):
        assert _map_check_status("IN_PROGRESS", "") == CICheckStatus.PENDING

    def test_queued_state(self):
        assert _map_check_status("QUEUED", "") == CICheckStatus.PENDING

    def test_stale_state(self):
        assert _map_check_status("STALE", "") == CICheckStatus.STALE

    def test_unknown_falls_back_to_pending(self):
        assert _map_check_status("UNKNOWN_STATE", "") == CICheckStatus.PENDING

    def test_conclusion_takes_precedence_over_state(self):
        """When both state and conclusion are present, conclusion is used."""
        assert _map_check_status("completed", "FAILURE") == CICheckStatus.FAILING

    def test_startup_failure(self):
        assert _map_check_status("completed", "STARTUP_FAILURE") == CICheckStatus.FAILING


class TestParseJson:
    """Test the _parse_json helper."""

    def test_valid_json(self):
        assert _parse_json('{"key": "value"}') == {"key": "value"}

    def test_valid_list(self):
        assert _parse_json("[1, 2, 3]") == [1, 2, 3]

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            _parse_json("not json", context="test")

    def test_invalid_json_with_context(self):
        with pytest.raises(ValueError, match="test context"):
            _parse_json("{bad", context="test context")


class TestFetchPRState:
    """Test fetch_pr_state with mocked subprocess."""

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_success(self, mock_run_gh, sample_pr_view_json):
        mock_run_gh.return_value = json.dumps(sample_pr_view_json)

        result = fetch_pr_state(42, "owner/repo")

        assert result.number == 42
        assert result.title == "Add feature X"
        assert result.state == "open"
        assert result.merged is False
        assert result.mergeable is True
        assert result.mergeable_state == "clean"
        assert result.head_sha == "abc123def456"
        assert result.base_branch == "main"
        assert result.head_branch == "feature-x"
        assert result.review_verdict == ReviewVerdict.PENDING

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_merged(self, mock_run_gh, sample_pr_view_merged_json):
        mock_run_gh.return_value = json.dumps(sample_pr_view_merged_json)

        result = fetch_pr_state(42, "owner/repo")

        assert result.merged is True
        assert result.state == "merged"

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_dirty(self, mock_run_gh, sample_pr_view_conflicting_json):
        mock_run_gh.return_value = json.dumps(sample_pr_view_conflicting_json)

        result = fetch_pr_state(42, "owner/repo")

        assert result.has_conflicts is True
        assert result.mergeable is False
        assert result.mergeable_state == "dirty"

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_approved(self, mock_run_gh, sample_pr_view_json):
        data = {**sample_pr_view_json, "reviewDecision": "APPROVED"}
        mock_run_gh.return_value = json.dumps(data)

        result = fetch_pr_state(42, "owner/repo")
        assert result.review_verdict == ReviewVerdict.APPROVED

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_changes_requested(self, mock_run_gh, sample_pr_view_json):
        data = {**sample_pr_view_json, "reviewDecision": "CHANGES_REQUESTED"}
        mock_run_gh.return_value = json.dumps(data)

        result = fetch_pr_state(42, "owner/repo")
        assert result.review_verdict == ReviewVerdict.CHANGES_REQUESTED

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_subprocess_error(self, mock_run_gh):
        mock_run_gh.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(subprocess.CalledProcessError):
            fetch_pr_state(42, "owner/repo")

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_pr_state_invalid_json(self, mock_run_gh):
        mock_run_gh.return_value = "not valid json"

        with pytest.raises(ValueError, match="Invalid JSON"):
            fetch_pr_state(42, "owner/repo")


class TestFetchCIChecks:
    """Test fetch_ci_checks with mocked subprocess."""

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_all_passing(self, mock_run_gh, sample_pr_checks_all_pass_json):
        mock_run_gh.return_value = json.dumps(sample_pr_checks_all_pass_json)

        results = fetch_ci_checks(42, "owner/repo")

        assert len(results) == 2
        assert all(c.status == CICheckStatus.PASSING for c in results)
        assert results[0].name == "lint"
        assert results[1].name == "test"

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_some_failing(self, mock_run_gh, sample_pr_checks_failing_json):
        mock_run_gh.return_value = json.dumps(sample_pr_checks_failing_json)

        results = fetch_ci_checks(42, "owner/repo")

        assert len(results) == 2
        failing = [c for c in results if c.status == CICheckStatus.FAILING]
        assert len(failing) == 1
        assert failing[0].name == "lint"

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_pending(self, mock_run_gh, sample_pr_checks_pending_json):
        mock_run_gh.return_value = json.dumps(sample_pr_checks_pending_json)

        results = fetch_ci_checks(42, "owner/repo")

        assert len(results) == 2
        assert all(c.status == CICheckStatus.PENDING for c in results)

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_empty_list(self, mock_run_gh):
        mock_run_gh.return_value = "[]"

        results = fetch_ci_checks(42, "owner/repo")
        assert results == []

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_non_list_response(self, mock_run_gh):
        """Returns empty list when response is not a list."""
        mock_run_gh.return_value = '{"error": "something"}'

        results = fetch_ci_checks(42, "owner/repo")
        assert results == []

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_ci_checks_preserves_url(self, mock_run_gh):
        data = [
            {
                "name": "lint",
                "state": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://example.com/run/1",
            },
        ]
        mock_run_gh.return_value = json.dumps(data)

        results = fetch_ci_checks(42, "owner/repo")
        assert results[0].url == "https://example.com/run/1"


class TestFetchReviewComments:
    """Test fetch_review_comments with mocked subprocess."""

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_success(self, mock_run_gh):
        mock_run_gh.return_value = '["Good work!", "Needs some changes"]'

        comments = fetch_review_comments(42, "owner/repo")

        assert len(comments) == 2
        assert "Good work!" in comments
        assert "Needs some changes" in comments

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_empty(self, mock_run_gh):
        mock_run_gh.return_value = "[]"

        comments = fetch_review_comments(42, "owner/repo")
        assert comments == []

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_filters_blank_lines(self, mock_run_gh):
        mock_run_gh.return_value = '["comment1", "", "  ", "comment2"]'

        comments = fetch_review_comments(42, "owner/repo")
        assert len(comments) == 2

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_multiline(self, mock_run_gh):
        """Multi-line review bodies are preserved as single comments."""
        mock_run_gh.return_value = (
            '["Fix the SQL injection on line 42.\\nAlso update the docstring.", "LGTM"]'
        )

        comments = fetch_review_comments(42, "owner/repo")
        assert len(comments) == 2
        assert "Fix the SQL injection on line 42.\nAlso update the docstring." in comments
        assert "LGTM" in comments

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_subprocess_error(self, mock_run_gh):
        mock_run_gh.side_effect = subprocess.CalledProcessError(1, "gh")

        comments = fetch_review_comments(42, "owner/repo")
        assert comments == []

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_invalid_json(self, mock_run_gh):
        """Invalid JSON returns empty list instead of propagating."""
        mock_run_gh.return_value = "not valid json"

        comments = fetch_review_comments(42, "owner/repo")
        assert comments == []

    @patch("egg_babysit.pr_state._run_gh")
    def test_fetch_review_comments_empty_string(self, mock_run_gh):
        """Empty string returns empty list instead of propagating."""
        mock_run_gh.return_value = ""

        comments = fetch_review_comments(42, "owner/repo")
        assert comments == []


class TestDetectHeadShaChange:
    """Test detect_head_sha_change."""

    def test_no_old_sha(self):
        """Empty old SHA should return False (first poll)."""
        pr_state = PRState(
            number=42,
            title="",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
        )
        assert detect_head_sha_change("", pr_state) is False

    def test_same_sha(self):
        pr_state = PRState(
            number=42,
            title="",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
        )
        assert detect_head_sha_change("abc123", pr_state) is False

    def test_different_sha(self):
        pr_state = PRState(
            number=42,
            title="",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="def456",
            base_branch="main",
            head_branch="feature",
        )
        assert detect_head_sha_change("abc123", pr_state) is True


class TestGetFullPRState:
    """Test get_full_pr_state composition."""

    @patch("egg_babysit.pr_state.fetch_review_comments")
    @patch("egg_babysit.pr_state.fetch_ci_checks")
    @patch("egg_babysit.pr_state.fetch_pr_state")
    def test_combines_all_sources(self, mock_pr, mock_ci, mock_comments):
        mock_pr.return_value = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc123",
            base_branch="main",
            head_branch="feature",
        )
        from egg_babysit.types import CICheckResult

        mock_ci.return_value = [
            CICheckResult(name="lint", status=CICheckStatus.PASSING, conclusion="SUCCESS"),
        ]
        mock_comments.return_value = ["Looks good"]

        result = get_full_pr_state(42, "owner/repo")

        assert result.number == 42
        assert len(result.ci_checks) == 1
        assert result.review_comments == ["Looks good"]
