"""Tests for gateway github_client module.

Tests for user mode validation and token handling.
"""

import pytest

from gateway.github_client import GitHubClient, GitHubResult


class TestValidateUserModeConfig:
    """Tests for validate_user_mode_config method."""

    @pytest.fixture
    def mock_client(self):
        """Create a GitHubClient with mocked methods."""
        client = GitHubClient.__new__(GitHubClient)
        client.mode = "bot"
        client._token = None
        client._token_expiry = None
        return client

    def test_returns_false_when_no_user_token(self, mock_client, monkeypatch):
        """Test validation fails when user token is not configured."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: False)

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is False
        assert "not configured" in message.lower()

    def test_returns_false_when_token_invalid(self, mock_client, monkeypatch):
        """Test validation fails when token cannot authenticate."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: True)
        monkeypatch.setattr(mock_client, "get_authenticated_user", lambda mode: None)

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is False
        assert "invalid" in message.lower() or "expired" in message.lower()

    def test_returns_false_when_username_mismatch(self, mock_client, monkeypatch):
        """Test validation fails when token username doesn't match configured user."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: True)
        monkeypatch.setattr(mock_client, "get_authenticated_user", lambda mode: "actual-user")

        # Mock repo_config module before it's imported inside the method
        import gateway.repo_config as repo_config_module

        monkeypatch.setattr(
            repo_config_module, "get_user_mode_config", lambda: {"github_user": "expected-user"}
        )

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is False
        assert "actual-user" in message
        assert "expected-user" in message

    def test_returns_true_when_username_matches(self, mock_client, monkeypatch):
        """Test validation succeeds when token username matches configured user."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: True)
        monkeypatch.setattr(mock_client, "get_authenticated_user", lambda mode: "testuser")

        # Mock repo_config module
        import gateway.repo_config as repo_config_module

        monkeypatch.setattr(
            repo_config_module, "get_user_mode_config", lambda: {"github_user": "testuser"}
        )

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is True
        assert message == ""

    def test_case_insensitive_username_match(self, mock_client, monkeypatch):
        """Test that username comparison is case-insensitive."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: True)
        monkeypatch.setattr(mock_client, "get_authenticated_user", lambda mode: "TestUser")

        # Mock repo_config with different case
        import gateway.repo_config as repo_config_module

        monkeypatch.setattr(
            repo_config_module, "get_user_mode_config", lambda: {"github_user": "testuser"}
        )

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is True

    def test_returns_true_when_no_configured_user(self, mock_client, monkeypatch):
        """Test validation succeeds when no github_user is configured."""
        monkeypatch.setattr(mock_client, "is_user_token_valid", lambda: True)
        monkeypatch.setattr(mock_client, "get_authenticated_user", lambda mode: "anyuser")

        # Mock repo_config with empty github_user
        import gateway.repo_config as repo_config_module

        monkeypatch.setattr(repo_config_module, "get_user_mode_config", lambda: {"github_user": ""})

        is_valid, message = mock_client.validate_user_mode_config()

        assert is_valid is True


class TestIsUserTokenValid:
    """Tests for is_user_token_valid method."""

    @pytest.fixture
    def mock_client(self):
        """Create a GitHubClient with mocked methods."""
        client = GitHubClient.__new__(GitHubClient)
        client.mode = "bot"
        return client

    def test_returns_false_when_no_token(self, mock_client, monkeypatch):
        """Test returns False when no user token exists."""
        monkeypatch.setattr(mock_client, "get_user_token", lambda: None)
        assert mock_client.is_user_token_valid() is False

    def test_returns_false_when_empty_token(self, mock_client, monkeypatch):
        """Test returns False when user token is empty."""
        monkeypatch.setattr(mock_client, "get_user_token", lambda: "")
        assert mock_client.is_user_token_valid() is False

    def test_returns_true_when_token_exists(self, mock_client, monkeypatch):
        """Test returns True when user token is set."""
        monkeypatch.setattr(mock_client, "get_user_token", lambda: "ghp_valid_token")
        assert mock_client.is_user_token_valid() is True


class TestGetAuthenticatedUser:
    """Tests for get_authenticated_user method."""

    @pytest.fixture
    def mock_client(self):
        """Create a GitHubClient with mocked execute method."""
        client = GitHubClient.__new__(GitHubClient)
        client.mode = "bot"
        return client

    def test_returns_username_on_success(self, mock_client, monkeypatch):
        """Test returns username when API call succeeds."""
        result = GitHubResult(
            success=True,
            stdout="testuser\n",
            stderr="",
            returncode=0,
        )
        monkeypatch.setattr(mock_client, "execute", lambda args, mode: result)

        username = mock_client.get_authenticated_user(mode="user")
        assert username == "testuser"

    def test_returns_none_on_failure(self, mock_client, monkeypatch):
        """Test returns None when API call fails."""
        result = GitHubResult(
            success=False,
            stdout="",
            stderr="error: not authenticated",
            returncode=1,
        )
        monkeypatch.setattr(mock_client, "execute", lambda args, mode: result)

        username = mock_client.get_authenticated_user(mode="user")
        assert username is None

    def test_returns_none_on_empty_response(self, mock_client, monkeypatch):
        """Test returns None when API returns empty response."""
        result = GitHubResult(
            success=True,
            stdout="",
            stderr="",
            returncode=0,
        )
        monkeypatch.setattr(mock_client, "execute", lambda args, mode: result)

        username = mock_client.get_authenticated_user(mode="user")
        assert username is None
