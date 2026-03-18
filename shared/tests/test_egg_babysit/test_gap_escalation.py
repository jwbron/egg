"""Gap tests for egg_babysit.escalation — Slack, orchestrator, edge cases."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.escalation import (
    _escalate_via_orchestrator,
    _escalate_via_slack,
    escalate,
    post_pr_comment,
)


@pytest.fixture
def config():
    return BabysitConfig(pr_number=42, repo="owner/repo")


@pytest.fixture
def config_with_orchestrator():
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        orchestrator_url="http://localhost:8080",
    )


class TestPostPRComment:
    """Test post_pr_comment edge cases."""

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_comment_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert post_pr_comment(42, "owner/repo", "Test comment") is True

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_comment_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Not found")

        assert post_pr_comment(42, "owner/repo", "Test comment") is False

    @patch("egg_babysit.escalation.subprocess.run")
    def test_post_comment_exception(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 30)

        assert post_pr_comment(42, "owner/repo", "Test comment") is False


class TestEscalateViaOrchestrator:
    """Test orchestrator escalation."""

    def test_skip_without_orchestrator_url(self, config):
        """No orchestrator URL → silently skips."""
        # Should not raise
        _escalate_via_orchestrator(config, "Test reason", "Test context")

    @patch("egg_babysit.escalation.subprocess.run")
    def test_creates_hitl_decision(self, mock_run, config_with_orchestrator):
        """Creates HITL decision via egg-contract."""
        mock_run.return_value = MagicMock(returncode=0)

        _escalate_via_orchestrator(config_with_orchestrator, "Test reason", "context")

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "egg-contract" in call_args
        assert "add-decision" in call_args

    @patch("egg_babysit.escalation.subprocess.run")
    def test_egg_contract_not_found(self, mock_run, config_with_orchestrator):
        """FileNotFoundError for egg-contract is handled."""
        mock_run.side_effect = FileNotFoundError("egg-contract not found")

        # Should not raise
        _escalate_via_orchestrator(config_with_orchestrator, "reason", "context")


class TestEscalateViaSlack:
    """Test Slack notification escalation."""

    def test_creates_notification_file(self, tmp_path, config, monkeypatch):
        """Creates a notification file in the notifications directory."""
        # _escalate_via_slack uses Path(os.path.expanduser("~/sharing/notifications"))
        # We monkeypatch expanduser to redirect ~ to tmp_path
        notifications_dir = tmp_path / "sharing" / "notifications"
        notifications_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / p.lstrip("~/")) if p.startswith("~") else p,
        )

        _escalate_via_slack(config, "Test escalation")

        # Check that a notification file was created
        files = list(notifications_dir.glob("*babysit*"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "42" in content
        assert "Test escalation" in content

    def test_missing_notifications_dir(self, config):
        """Missing notifications directory → silently skips."""
        with patch("os.path.expanduser", return_value="/nonexistent"):
            # Should not raise
            _escalate_via_slack(config, "Test reason")


class TestEscalateFullFlow:
    """Test the top-level escalate function."""

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_calls_all_channels(self, mock_comment, mock_orch, mock_slack, config):
        """Escalate attempts all three channels."""
        mock_comment.return_value = True

        escalate(config, "Test reason", "Test context")

        mock_comment.assert_called_once()
        mock_orch.assert_called_once()
        mock_slack.assert_called_once()

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_continues_on_comment_failure(
        self, mock_comment, mock_orch, mock_slack, config
    ):
        """If PR comment fails, other channels still attempted."""
        mock_comment.return_value = False

        escalate(config, "reason", "context")

        mock_orch.assert_called_once()
        mock_slack.assert_called_once()

    @patch("egg_babysit.escalation._escalate_via_slack")
    @patch("egg_babysit.escalation._escalate_via_orchestrator")
    @patch("egg_babysit.escalation.post_pr_comment")
    def test_escalate_comment_contains_reason(self, mock_comment, mock_orch, mock_slack, config):
        """Escalation PR comment includes the reason."""
        mock_comment.return_value = True

        escalate(config, "Max retries exceeded", "Lint job failed 3 times")

        call_args = mock_comment.call_args
        body = call_args[0][2]  # Third positional arg is body
        assert "Max retries exceeded" in body
        assert "Lint job failed 3 times" in body
