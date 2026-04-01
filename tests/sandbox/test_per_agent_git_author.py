"""
Tests for per-agent git identity (#1481).

Validates that setup_git() uses role-aware identity:
- With EGG_AGENT_ROLE set: user.name = "egg ({role})", user.email = "{role}@egg.local"
- Without EGG_AGENT_ROLE: user.name = "egg", user.email = "egg@localhost" (backward compat)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Add shared + sandbox to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

import entrypoint


class TestPerAgentGitIdentity:
    """setup_git configures per-agent git identity based on EGG_AGENT_ROLE."""

    @pytest.fixture
    def config(self):
        cfg = MagicMock()
        cfg.runtime_uid = 1000
        cfg.runtime_gid = 1000
        cfg.github_token = None
        return cfg

    @pytest.fixture
    def logger(self):
        return MagicMock()

    @patch("entrypoint.run_cmd")
    def test_role_coder_identity(self, mock_run_cmd, config, logger, monkeypatch):
        """Coder role sets user.name='egg (coder)' and user.email='coder@egg.local'."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
        entrypoint.setup_git(config, logger)

        user_tuple = (1000, 1000)
        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        email_call = [c for c in calls if "user.email" in c.args[0]]

        assert len(name_call) >= 1
        assert name_call[0] == call(
            ["git", "config", "--global", "user.name", "egg (coder)"],
            as_user=user_tuple,
        )
        assert len(email_call) >= 1
        assert email_call[0] == call(
            ["git", "config", "--global", "user.email", "coder@egg.local"],
            as_user=user_tuple,
        )

    @patch("entrypoint.run_cmd")
    def test_role_tester_identity(self, mock_run_cmd, config, logger, monkeypatch):
        """Tester role gets appropriate identity."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
        entrypoint.setup_git(config, logger)

        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        assert name_call[0].args[0][-1] == "egg (tester)"

    @patch("entrypoint.run_cmd")
    def test_role_documenter_identity(self, mock_run_cmd, config, logger, monkeypatch):
        """Documenter role gets appropriate identity."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "documenter")
        entrypoint.setup_git(config, logger)

        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        email_call = [c for c in calls if "user.email" in c.args[0]]
        assert name_call[0].args[0][-1] == "egg (documenter)"
        assert email_call[0].args[0][-1] == "documenter@egg.local"

    @patch("entrypoint.run_cmd")
    def test_no_role_backward_compat(self, mock_run_cmd, config, logger, monkeypatch):
        """Without EGG_AGENT_ROLE, uses legacy identity (egg / egg@localhost)."""
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
        entrypoint.setup_git(config, logger)

        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        email_call = [c for c in calls if "user.email" in c.args[0]]

        assert name_call[0].args[0][-1] == "egg"
        assert email_call[0].args[0][-1] == "egg@localhost"

    @patch("entrypoint.run_cmd")
    def test_empty_role_backward_compat(self, mock_run_cmd, config, logger, monkeypatch):
        """Empty EGG_AGENT_ROLE also uses legacy identity."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "")
        entrypoint.setup_git(config, logger)

        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        email_call = [c for c in calls if "user.email" in c.args[0]]

        assert name_call[0].args[0][-1] == "egg"
        assert email_call[0].args[0][-1] == "egg@localhost"

    @patch("entrypoint.run_cmd")
    def test_reviewer_code_identity(self, mock_run_cmd, config, logger, monkeypatch):
        """Reviewer role with underscore works correctly."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer_code")
        entrypoint.setup_git(config, logger)

        calls = mock_run_cmd.call_args_list
        name_call = [c for c in calls if "user.name" in c.args[0]]
        email_call = [c for c in calls if "user.email" in c.args[0]]

        assert name_call[0].args[0][-1] == "egg (reviewer_code)"
        assert email_call[0].args[0][-1] == "reviewer_code@egg.local"
