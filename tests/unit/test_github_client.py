"""Tests for gateway github_client module.

Tests for user mode validation and token handling.
"""

import pytest

from gateway.github_client import (
    BLOCKED_GH_COMMANDS,
    GH_API_ALLOWED_PATHS,
    READONLY_GH_COMMANDS,
    GitHubClient,
    GitHubResult,
    validate_gh_api_path,
)


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


class TestValidateGhApiPath:
    """Tests for validate_gh_api_path function."""

    def test_pr_list_allowed(self):
        """Test that PR listing is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls", "GET")
        assert allowed is True
        assert msg == ""

    def test_pr_view_allowed(self):
        """Test that viewing a specific PR is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123", "GET")
        assert allowed is True

    def test_pr_comments_allowed(self):
        """Test that PR comments are allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123/comments", "GET")
        assert allowed is True

    def test_pr_comments_post_allowed(self):
        """Test that POST to PR comments is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123/comments", "POST")
        assert allowed is True

    def test_issue_list_allowed(self):
        """Test that issue listing is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues", "GET")
        assert allowed is True

    def test_issue_view_allowed(self):
        """Test that viewing a specific issue is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues/456", "GET")
        assert allowed is True

    def test_issue_comments_allowed(self):
        """Test that issue comments are allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues/123/comments", "GET")
        assert allowed is True

    def test_repo_view_allowed(self):
        """Test that repo info is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo", "GET")
        assert allowed is True

    def test_branches_allowed(self):
        """Test that branch listing is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/branches", "GET")
        assert allowed is True

    def test_user_info_allowed(self):
        """Test that user info is allowed."""
        allowed, msg = validate_gh_api_path("user", "GET")
        assert allowed is True

    def test_specific_user_allowed(self):
        """Test that specific user info is allowed."""
        allowed, msg = validate_gh_api_path("users/testuser", "GET")
        assert allowed is True

    def test_releases_allowed(self):
        """Test that releases endpoint is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/releases", "GET")
        assert allowed is True

    def test_latest_release_allowed(self):
        """Test that latest release is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/releases/latest", "GET")
        assert allowed is True

    def test_leading_slash_stripped(self):
        """Test that leading slash is stripped."""
        allowed, msg = validate_gh_api_path("/repos/owner/repo/pulls", "GET")
        assert allowed is True

    def test_delete_method_blocked(self):
        """Test that DELETE method is blocked."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123", "DELETE")
        assert allowed is False
        assert "DELETE" in msg

    def test_put_method_blocked(self):
        """Test that PUT method is blocked."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123", "PUT")
        assert allowed is False

    def test_patch_method_allowed(self):
        """Test that PATCH method is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues/123", "PATCH")
        assert allowed is True

    def test_unknown_path_blocked(self):
        """Test that unknown paths are blocked."""
        allowed, msg = validate_gh_api_path("admin/settings", "GET")
        assert allowed is False
        assert "not in allowlist" in msg

    def test_orgs_blocked(self):
        """Test that org endpoints are blocked."""
        allowed, msg = validate_gh_api_path("orgs/myorg/members", "GET")
        assert allowed is False

    def test_pr_review_comments_allowed(self):
        """Test that PR review comments are allowed."""
        path = "repos/owner/repo/pulls/123/reviews/456/comments"
        allowed, msg = validate_gh_api_path(path, "GET")
        assert allowed is True

    def test_issue_events_allowed(self):
        """Test that issue events are allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues/123/events", "GET")
        assert allowed is True

    def test_issue_timeline_allowed(self):
        """Test that issue timeline is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/issues/123/timeline", "GET")
        assert allowed is True

    def test_commit_comments_allowed(self):
        """Test that commit comments are allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/commits/abc123def/comments", "GET")
        assert allowed is True


class TestGitHubConstants:
    """Tests for GitHub client constants."""

    def test_readonly_commands_frozen(self):
        """Test that readonly commands are immutable."""
        assert isinstance(READONLY_GH_COMMANDS, frozenset)

    def test_blocked_commands_frozen(self):
        """Test that blocked commands are immutable."""
        assert isinstance(BLOCKED_GH_COMMANDS, frozenset)

    def test_pr_merge_blocked(self):
        """Test that pr merge is blocked."""
        assert "pr merge" in BLOCKED_GH_COMMANDS

    def test_repo_delete_blocked(self):
        """Test that repo delete is blocked."""
        assert "repo delete" in BLOCKED_GH_COMMANDS

    def test_pr_view_readonly(self):
        """Test that pr view is readonly."""
        assert "pr view" in READONLY_GH_COMMANDS

    def test_api_allowed_paths_has_patterns(self):
        """Test that API allowed paths has patterns."""
        assert len(GH_API_ALLOWED_PATHS) > 0


class TestGitHubResult:
    """Tests for GitHubResult dataclass."""

    def test_success_result(self):
        """Test creating a successful result."""
        result = GitHubResult(
            success=True,
            stdout="output",
            stderr="",
            returncode=0,
        )
        assert result.success is True
        assert result.returncode == 0

    def test_failure_result(self):
        """Test creating a failed result."""
        result = GitHubResult(
            success=False,
            stdout="",
            stderr="error message",
            returncode=1,
        )
        assert result.success is False
        assert "error message" in result.stderr
