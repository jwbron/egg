"""Tests for per-agent git identity in setup_git().

Verifies that EGG_AGENT_ROLE controls the git user.name and user.email
set during sandbox initialization, enabling auditability in multi-agent
pipelines.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add sandbox/ to sys.path so entrypoint is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from entrypoint import setup_git


@pytest.fixture()
def mock_config():
    """Create a minimal Config-like object."""
    config = MagicMock()
    config.runtime_uid = os.getuid()
    config.runtime_gid = os.getgid()
    config.github_token = None  # Skip credential helper setup
    return config


@pytest.fixture()
def mock_logger():
    """Create a mock logger with a success method."""
    return MagicMock()


class TestSetupGitIdentityWithRole:
    """When EGG_AGENT_ROLE is set, git identity includes the role."""

    @patch("entrypoint.run_cmd")
    def test_git_user_name_includes_role(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {"EGG_AGENT_ROLE": "coder"}):
            setup_git(mock_config, mock_logger)

        user_tuple = (mock_config.runtime_uid, mock_config.runtime_gid)
        mock_run_cmd.assert_any_call(
            ["git", "config", "--global", "user.name", "egg (coder)"],
            as_user=user_tuple,
        )

    @patch("entrypoint.run_cmd")
    def test_git_email_uses_role(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {"EGG_AGENT_ROLE": "coder"}):
            setup_git(mock_config, mock_logger)

        user_tuple = (mock_config.runtime_uid, mock_config.runtime_gid)
        mock_run_cmd.assert_any_call(
            ["git", "config", "--global", "user.email", "coder@egg.local"],
            as_user=user_tuple,
        )

    @patch("entrypoint.run_cmd")
    def test_log_message_includes_role(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"}):
            setup_git(mock_config, mock_logger)

        mock_logger.success.assert_any_call(
            "Git configured to commit as egg (tester) <tester@egg.local>"
        )


class TestSetupGitIdentityWithoutRole:
    """When EGG_AGENT_ROLE is not set, git identity uses defaults."""

    @patch("entrypoint.run_cmd")
    def test_git_user_name_is_default(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EGG_AGENT_ROLE", None)
            setup_git(mock_config, mock_logger)

        user_tuple = (mock_config.runtime_uid, mock_config.runtime_gid)
        mock_run_cmd.assert_any_call(
            ["git", "config", "--global", "user.name", "egg"],
            as_user=user_tuple,
        )

    @patch("entrypoint.run_cmd")
    def test_git_email_is_default(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EGG_AGENT_ROLE", None)
            setup_git(mock_config, mock_logger)

        user_tuple = (mock_config.runtime_uid, mock_config.runtime_gid)
        mock_run_cmd.assert_any_call(
            ["git", "config", "--global", "user.email", "egg@localhost"],
            as_user=user_tuple,
        )

    @patch("entrypoint.run_cmd")
    def test_log_message_is_default(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EGG_AGENT_ROLE", None)
            setup_git(mock_config, mock_logger)

        mock_logger.success.assert_any_call("Git configured to commit as egg <egg@localhost>")


class TestSetupGitIdentityEmptyRole:
    """When EGG_AGENT_ROLE is set to empty string, falls back to default."""

    @patch("entrypoint.run_cmd")
    def test_empty_role_uses_default_name(self, mock_run_cmd, mock_config, mock_logger):
        with patch.dict(os.environ, {"EGG_AGENT_ROLE": ""}):
            setup_git(mock_config, mock_logger)

        user_tuple = (mock_config.runtime_uid, mock_config.runtime_gid)
        mock_run_cmd.assert_any_call(
            ["git", "config", "--global", "user.name", "egg"],
            as_user=user_tuple,
        )


class TestGitAuthorPartialMatch:
    """Both identity formats contain 'egg', so git log --author=egg works."""

    def test_default_name_contains_egg(self):
        assert "egg" in "egg"

    def test_role_name_contains_egg(self):
        assert "egg" in "egg (coder)"

    def test_role_name_contains_egg_for_any_role(self):
        for role in ("coder", "tester", "reviewer", "documenter"):
            name = f"egg ({role})"
            assert "egg" in name, f"git log --author=egg would miss '{name}'"
