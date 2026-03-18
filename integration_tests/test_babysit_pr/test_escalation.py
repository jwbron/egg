"""Integration tests for babysit escalation mechanisms."""

from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.escalation import escalate, post_pr_comment


@pytest.mark.integration
class TestPostPRComment:
    """Test post_pr_comment with mocked gh CLI."""

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_pr_comment_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = post_pr_comment(42, "owner/repo", "Test comment")

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "gh" in cmd
        assert "pr" in cmd
        assert "comment" in cmd
        assert "42" in cmd

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_pr_comment_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Not found")

        result = post_pr_comment(42, "owner/repo", "Test comment")

        assert result is False

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_pr_comment_exception(self, mock_run):
        mock_run.side_effect = OSError("Command not found")

        result = post_pr_comment(42, "owner/repo", "Test comment")

        assert result is False


@pytest.mark.integration
class TestEscalateWithOrchestrator:
    """Test escalation via orchestrator (mocked)."""

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_calls_all_channels(self, mock_comment, mock_orch, mock_slack):
        """escalate() attempts all notification channels."""
        mock_comment.return_value = True

        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            orchestrator_url="http://localhost:9800",
            pipeline_id="pr-42",
        )

        escalate(config, "Test reason", "Test context")

        mock_comment.assert_called_once()
        mock_orch.assert_called_once()
        mock_slack.assert_called_once()

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_continues_on_failure(self, mock_comment, mock_orch, mock_slack):
        """If one channel fails, others are still attempted."""
        mock_comment.return_value = False  # PR comment fails
        mock_orch.return_value = None
        mock_slack.return_value = None

        config = BabysitConfig(pr_number=42, repo="owner/repo")

        # Should not raise even if PR comment fails
        escalate(config, "Test reason", "Test context")

        # All channels should be attempted regardless
        mock_comment.assert_called_once()
        mock_orch.assert_called_once()
        mock_slack.assert_called_once()

    @patch("egg_babysit.escalation.subprocess.run")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_comment_body_format(self, mock_comment, mock_subprocess):
        """Escalation comment includes reason and context."""
        mock_comment.return_value = True

        config = BabysitConfig(pr_number=42, repo="owner/repo")
        escalate(config, "CI keeps failing", "lint job failed 3 times")

        call_args = mock_comment.call_args
        body = call_args[0][2]  # Third positional arg is body
        assert "CI keeps failing" in body
        assert "lint job failed 3 times" in body
        assert "Babysit Escalation" in body
