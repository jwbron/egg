"""Tests for egg_babysit.escalation — HITL escalation logic."""

from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.escalation import escalate, post_pr_comment


@pytest.fixture
def escalation_config():
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        orchestrator_url="http://localhost:9999",
        pipeline_id="pr-42",
    )


@pytest.fixture
def minimal_config():
    return BabysitConfig(pr_number=42, repo="owner/repo")


class TestPostPrComment:
    """Test post_pr_comment function."""

    @patch("egg_babysit.escalation.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        result = post_pr_comment(42, "owner/repo", "Test body")

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "gh" in call_args[0][0]
        assert "42" in call_args[0][0]

    @patch("egg_babysit.escalation.subprocess.run")
    def test_failure_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Error")

        result = post_pr_comment(42, "owner/repo", "Test body")

        assert result is False

    @patch("egg_babysit.escalation.subprocess.run")
    def test_exception_handled(self, mock_run):
        mock_run.side_effect = Exception("Network error")

        result = post_pr_comment(42, "owner/repo", "Test body")

        assert result is False


class TestEscalate:
    """Test escalate function multi-channel attempts."""

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_calls_all_channels(self, mock_comment, mock_orch, mock_slack, escalation_config):
        mock_comment.return_value = True

        escalate(escalation_config, "Test reason", "Test context")

        mock_comment.assert_called_once()
        mock_orch.assert_called_once()
        mock_slack.assert_called_once()

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_comment_body_contains_reason(
        self, mock_comment, mock_orch, mock_slack, escalation_config
    ):
        mock_comment.return_value = True

        escalate(escalation_config, "Merge conflicts", "Cannot resolve")

        call_args = mock_comment.call_args
        body = call_args[0][2]
        assert "Merge conflicts" in body
        assert "Cannot resolve" in body

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_comment_failure_doesnt_block_other_channels(
        self, mock_comment, mock_orch, mock_slack, escalation_config
    ):
        mock_comment.return_value = False

        escalate(escalation_config, "reason", "context")

        # Other channels still called despite comment failure.
        mock_orch.assert_called_once()
        mock_slack.assert_called_once()


class TestEscalateViaOrchestrator:
    """Test _escalate_via_orchestrator."""

    @patch("egg_babysit.escalation.subprocess.run")
    def test_skips_when_no_orchestrator(self, mock_run, minimal_config):
        from egg_babysit.escalation import _escalate_via_orchestrator

        _escalate_via_orchestrator(minimal_config, "reason", "context")

        mock_run.assert_not_called()

    @patch("egg_babysit.escalation.subprocess.run")
    def test_calls_egg_contract(self, mock_run, escalation_config):
        from egg_babysit.escalation import _escalate_via_orchestrator

        mock_run.return_value = MagicMock(returncode=0)

        _escalate_via_orchestrator(escalation_config, "reason", "context")

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "egg-contract" in call_args

    @patch("egg_babysit.escalation.subprocess.run")
    def test_handles_file_not_found(self, mock_run, escalation_config):
        from egg_babysit.escalation import _escalate_via_orchestrator

        mock_run.side_effect = FileNotFoundError("egg-contract not found")

        # Should not raise.
        _escalate_via_orchestrator(escalation_config, "reason", "context")


class TestEscalateViaSlack:
    """Test _escalate_via_slack."""

    def test_creates_notification_file(self, tmp_path, escalation_config):
        from egg_babysit.escalation import _escalate_via_slack

        notifications_dir = tmp_path / "notifications"
        notifications_dir.mkdir()

        with patch("os.path.expanduser", return_value=str(notifications_dir)):
            _escalate_via_slack(escalation_config, "Test reason")

        # Check that a file was created.
        files = list(notifications_dir.glob("*-babysit-escalation.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "PR #42" in content
        assert "owner/repo" in content
        assert "Test reason" in content

    def test_skips_when_no_notifications_dir(self, escalation_config):
        from egg_babysit.escalation import _escalate_via_slack

        with patch("os.path.expanduser", return_value="/nonexistent/path"):
            # Should not raise.
            _escalate_via_slack(escalation_config, "reason")
