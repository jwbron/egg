"""Gap tests for egg_babysit.pr_state — status mapping edge cases, rate limiting."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.pr_state import (
    _map_check_status,
    _run_gh,
    detect_head_sha_change,
    fetch_ci_checks,
    fetch_pr_state,
    get_full_pr_state,
)
from egg_babysit.types import CICheckStatus, PRState, ReviewVerdict


class TestMapCheckStatusAdditional:
    """Additional status mapping cases not covered by existing tests."""

    def test_error_conclusion(self):
        assert _map_check_status("completed", "ERROR") == CICheckStatus.FAILING

    def test_action_required_conclusion(self):
        assert _map_check_status("completed", "ACTION_REQUIRED") == CICheckStatus.FAILING

    def test_waiting_state(self):
        assert _map_check_status("WAITING", "") == CICheckStatus.PENDING

    def test_requested_state(self):
        assert _map_check_status("REQUESTED", "") == CICheckStatus.PENDING

    def test_empty_state_and_conclusion(self):
        """Both state and conclusion empty falls back to pending."""
        assert _map_check_status("", "") == CICheckStatus.PENDING

    def test_case_sensitivity(self):
        """Status mapping uses uppercase key."""
        # The function uppercases both, so lowercase should work
        assert _map_check_status("completed", "success") == CICheckStatus.PASSING
        assert _map_check_status("completed", "failure") == CICheckStatus.FAILING


class TestRunGh:
    """Test _run_gh helper."""

    @patch("egg_babysit.pr_state.subprocess.run")
    def test_run_gh_basic(self, mock_run):
        """Basic gh CLI call."""
        mock_run.return_value = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")

        result = _run_gh(["pr", "view", "--json", "number"])

        assert result == '{"ok": true}'
        mock_run.assert_called_once()

    @patch("egg_babysit.pr_state.subprocess.run")
    def test_run_gh_rate_limit_warning(self, mock_run):
        """Rate limit messages in stderr are logged as warnings."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="{}",
            stderr="API rate limit exceeded",
        )

        result = _run_gh(["pr", "view"])

        assert result == "{}"
        # Just verify it doesn't error — rate limit logging is best-effort

    @patch("egg_babysit.pr_state.subprocess.run")
    def test_run_gh_nonzero_exit(self, mock_run):
        """Non-zero exit raises CalledProcessError."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(subprocess.CalledProcessError):
            _run_gh(["pr", "view"])

    @patch("egg_babysit.pr_state.subprocess.run")
    def test_run_gh_timeout(self, mock_run):
        """Timeout raises TimeoutExpired."""
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 60)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_gh(["pr", "view"])

    @patch("egg_babysit.pr_state.subprocess.run")
    def test_run_gh_custom_timeout(self, mock_run):
        """Custom timeout is passed to subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        _run_gh(["pr", "view"], timeout=120)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["timeout"] == 120


class TestFetchCIChecksAdditional:
    """Additional fetch_ci_checks edge cases."""

    @patch("egg_babysit.pr_state._run_gh")
    def test_missing_fields_handled(self, mock_run_gh):
        """Missing fields in check data are handled with defaults."""
        data = [{"name": "lint"}]  # Missing state, conclusion, detailsUrl
        mock_run_gh.return_value = json.dumps(data)

        results = fetch_ci_checks(42, "owner/repo")

        assert len(results) == 1
        assert results[0].name == "lint"
        assert results[0].status == CICheckStatus.PENDING  # Default for empty state
        assert results[0].url == ""

    @patch("egg_babysit.pr_state._run_gh")
    def test_unknown_check_name(self, mock_run_gh):
        """Check with missing name gets 'unknown' default."""
        data = [{"state": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "http://url"}]
        mock_run_gh.return_value = json.dumps(data)

        results = fetch_ci_checks(42, "owner/repo")

        assert len(results) == 1
        assert results[0].name == "unknown"


class TestFetchPRStateAdditional:
    """Additional fetch_pr_state edge cases."""

    @patch("egg_babysit.pr_state._run_gh")
    def test_review_required_maps_to_pending(self, mock_run_gh):
        """REVIEW_REQUIRED review decision maps to PENDING."""
        data = {
            "number": 42,
            "title": "PR",
            "state": "OPEN",
            "merged": False,
            "mergeable": "MERGEABLE",
            "mergeableState": "clean",
            "headRefOid": "abc",
            "baseRefName": "main",
            "headRefName": "feature",
            "reviewDecision": "REVIEW_REQUIRED",
        }
        mock_run_gh.return_value = json.dumps(data)

        result = fetch_pr_state(42, "owner/repo")

        assert result.review_verdict == ReviewVerdict.PENDING

    @patch("egg_babysit.pr_state._run_gh")
    def test_missing_mergeable_state(self, mock_run_gh):
        """Missing mergeableState defaults to 'unknown'."""
        data = {
            "number": 42,
            "title": "PR",
            "state": "OPEN",
            "merged": False,
            "mergeable": "UNKNOWN",
            "mergeableState": None,
            "headRefOid": "abc",
            "baseRefName": "main",
            "headRefName": "feature",
            "reviewDecision": "",
        }
        mock_run_gh.return_value = json.dumps(data)

        result = fetch_pr_state(42, "owner/repo")

        assert result.mergeable_state == "unknown"

    @patch("egg_babysit.pr_state._run_gh")
    def test_unknown_mergeable_is_not_mergeable(self, mock_run_gh):
        """UNKNOWN mergeable state maps to mergeable=False."""
        data = {
            "number": 42,
            "title": "PR",
            "state": "OPEN",
            "merged": False,
            "mergeable": "UNKNOWN",
            "mergeableState": "unknown",
            "headRefOid": "abc",
            "baseRefName": "main",
            "headRefName": "feature",
            "reviewDecision": "",
        }
        mock_run_gh.return_value = json.dumps(data)

        result = fetch_pr_state(42, "owner/repo")

        assert result.mergeable is False


class TestGetFullPRStateEdgeCases:
    """Additional get_full_pr_state edge cases."""

    @patch("egg_babysit.pr_state.fetch_review_comments")
    @patch("egg_babysit.pr_state.fetch_ci_checks")
    @patch("egg_babysit.pr_state.fetch_pr_state")
    def test_ci_error_propagates(self, mock_pr, mock_ci, mock_comments):
        """CI check fetch errors propagate."""
        mock_pr.return_value = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc",
            base_branch="main",
            head_branch="feature",
        )
        mock_ci.side_effect = subprocess.CalledProcessError(1, "gh")

        with pytest.raises(subprocess.CalledProcessError):
            get_full_pr_state(42, "owner/repo")

    @patch("egg_babysit.pr_state.fetch_review_comments")
    @patch("egg_babysit.pr_state.fetch_ci_checks")
    @patch("egg_babysit.pr_state.fetch_pr_state")
    def test_mutates_pr_state_in_place(self, mock_pr, mock_ci, mock_comments):
        """get_full_pr_state mutates the PRState object in place."""
        pr = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="abc",
            base_branch="main",
            head_branch="feature",
        )
        mock_pr.return_value = pr
        mock_ci.return_value = []
        mock_comments.return_value = ["comment"]

        result = get_full_pr_state(42, "owner/repo")

        assert result is pr  # Same object
        assert result.review_comments == ["comment"]


class TestDetectHeadShaChangeEdgeCases:
    """Edge cases for detect_head_sha_change."""

    def test_very_long_sha(self):
        """Long SHA strings are compared correctly."""
        pr = PRState(
            number=42,
            title="Test",
            state="open",
            merged=False,
            mergeable=True,
            mergeable_state="clean",
            head_sha="a" * 40,
            base_branch="main",
            head_branch="feature",
        )
        assert detect_head_sha_change("b" * 40, pr) is True
        assert detect_head_sha_change("a" * 40, pr) is False
