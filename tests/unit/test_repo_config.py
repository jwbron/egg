"""Tests for gateway repo_config module.

Tests the repository configuration for authentication modes.
"""

import pytest

from gateway.repo_config import (
    RepoConfig,
    get_auth_mode,
    get_bot_username,
    get_github_username,
    get_user_mode_config,
    is_user_mode_repo,
    reset_config,
)


class TestRepoConfig:
    """Tests for RepoConfig class."""

    @pytest.fixture(autouse=True)
    def reset_global_config(self):
        """Reset global config before and after each test."""
        reset_config()
        yield
        reset_config()

    def test_default_mode_is_bot(self):
        """Test that default mode is bot."""
        config = RepoConfig()
        assert config.default_mode == "bot"

    def test_default_mode_from_env(self, monkeypatch):
        """Test that default mode can be set via environment."""
        monkeypatch.setenv("EGG_AUTH_MODE", "user")
        config = RepoConfig()
        assert config.default_mode == "user"

    def test_invalid_mode_falls_back_to_bot(self, monkeypatch):
        """Test that invalid mode falls back to bot."""
        monkeypatch.setenv("EGG_AUTH_MODE", "invalid")
        config = RepoConfig()
        assert config.default_mode == "bot"

    def test_user_mode_repos_from_env(self, monkeypatch):
        """Test that user mode repos can be set via environment."""
        monkeypatch.setenv("EGG_USER_MODE_REPOS", "owner/repo1,owner/repo2")
        config = RepoConfig()
        assert config.get_auth_mode("owner/repo1") == "user"
        assert config.get_auth_mode("owner/repo2") == "user"
        assert config.get_auth_mode("owner/other") == "bot"

    def test_user_mode_repos_owner_pattern(self, monkeypatch):
        """Test that owner/* pattern works for user mode repos."""
        monkeypatch.setenv("EGG_USER_MODE_REPOS", "myuser/*")
        config = RepoConfig()
        assert config.get_auth_mode("myuser/repo1") == "user"
        assert config.get_auth_mode("myuser/repo2") == "user"
        assert config.get_auth_mode("otheruser/repo") == "bot"


class TestGetAuthMode:
    """Tests for get_auth_mode convenience function."""

    @pytest.fixture(autouse=True)
    def reset_global_config(self):
        """Reset global config before and after each test."""
        reset_config()
        yield
        reset_config()

    def test_default_returns_bot(self):
        """Test that default repo returns bot mode."""
        assert get_auth_mode("owner/repo") == "bot"

    def test_none_returns_default(self):
        """Test that None returns default mode."""
        assert get_auth_mode(None) == "bot"


class TestIsUserModeRepo:
    """Tests for is_user_mode_repo function."""

    @pytest.fixture(autouse=True)
    def reset_global_config(self):
        """Reset global config before and after each test."""
        reset_config()
        yield
        reset_config()

    def test_returns_false_for_bot_mode(self):
        """Test that bot mode repos return False."""
        assert is_user_mode_repo("owner/repo") is False

    def test_returns_true_for_user_mode(self, monkeypatch):
        """Test that user mode repos return True."""
        monkeypatch.setenv("EGG_USER_MODE_REPOS", "owner/repo")
        reset_config()
        assert is_user_mode_repo("owner/repo") is True


class TestGetUserModeConfig:
    """Tests for get_user_mode_config function."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        """Clean environment variables before each test."""
        monkeypatch.delenv("EGG_USER_MODE_GITHUB_USER", raising=False)
        monkeypatch.delenv("EGG_USER_MODE_GIT_NAME", raising=False)
        monkeypatch.delenv("EGG_USER_MODE_GIT_EMAIL", raising=False)

    def test_returns_empty_when_not_configured(self):
        """Test that empty config is returned when not configured."""
        config = get_user_mode_config()
        assert config["github_user"] == ""
        assert config["git_name"] == ""
        assert config["git_email"] == ""

    def test_loads_from_env(self, monkeypatch):
        """Test that config loads from environment variables."""
        monkeypatch.setenv("EGG_USER_MODE_GITHUB_USER", "testuser")
        monkeypatch.setenv("EGG_USER_MODE_GIT_NAME", "Test User")
        monkeypatch.setenv("EGG_USER_MODE_GIT_EMAIL", "test@example.com")

        config = get_user_mode_config()
        assert config["github_user"] == "testuser"
        assert config["git_name"] == "Test User"
        assert config["git_email"] == "test@example.com"


class TestGetBotUsername:
    """Tests for get_bot_username function."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        """Clean environment variables before each test."""
        monkeypatch.delenv("EGG_BOT_USERNAME", raising=False)

    def test_default_is_egg(self):
        """Test that default bot username is egg."""
        assert get_bot_username() == "egg"

    def test_loads_from_env(self, monkeypatch):
        """Test that bot username loads from environment."""
        monkeypatch.setenv("EGG_BOT_USERNAME", "custom-bot")
        assert get_bot_username() == "custom-bot"


class TestGetGitHubUsername:
    """Tests for get_github_username function."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        """Clean environment variables before each test."""
        monkeypatch.delenv("EGG_GITHUB_USERNAME", raising=False)

    def test_returns_none_when_not_configured(self):
        """Test that None is returned when not configured."""
        assert get_github_username() is None

    def test_loads_from_env(self, monkeypatch):
        """Test that username loads from environment."""
        monkeypatch.setenv("EGG_GITHUB_USERNAME", "testuser")
        assert get_github_username() == "testuser"
