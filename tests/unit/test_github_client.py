"""Tests for gateway github_client module.

Tests for user mode validation and token handling.
"""

import time

import pytest

from gateway.github_client import (
    BLOCKED_GH_COMMANDS,
    GH_API_ALLOWED_PATHS,
    READONLY_GH_COMMANDS,
    GitHubClient,
    GitHubResult,
    GitHubToken,
    extract_repo_from_gh_api_path,
    extract_repo_from_gh_command,
    parse_gh_api_args,
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


class TestParseGhApiArgs:
    """Tests for parse_gh_api_args function."""

    def test_extracts_api_path(self):
        """Test extracting API path from args."""
        path, method = parse_gh_api_args(["repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_extracts_method_with_dash_x(self):
        """Test extracting method with -X flag."""
        path, method = parse_gh_api_args(["-X", "POST", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "POST"

    def test_extracts_method_with_long_flag(self):
        """Test extracting method with --method flag."""
        path, method = parse_gh_api_args(["--method", "PATCH", "repos/owner/repo/issues/1"])
        assert path == "repos/owner/repo/issues/1"
        assert method == "PATCH"

    def test_extracts_method_with_equals_syntax(self):
        """Test extracting method with -X=POST syntax."""
        path, method = parse_gh_api_args(["-X=POST", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "POST"

    def test_extracts_method_with_method_equals_syntax(self):
        """Test extracting method with --method=POST syntax."""
        path, method = parse_gh_api_args(["--method=PATCH", "repos/owner/repo/issues/1"])
        assert path == "repos/owner/repo/issues/1"
        assert method == "PATCH"

    def test_handles_flags_with_values(self):
        """Test handling flags that take values."""
        path, method = parse_gh_api_args(["-H", "Accept: application/json", "user"])
        assert path == "user"
        assert method == "GET"

    def test_handles_flags_without_values(self):
        """Test handling flags that don't take values."""
        path, method = parse_gh_api_args(["--paginate", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_handles_unknown_flag(self):
        """Test handling unknown flags."""
        path, method = parse_gh_api_args(["--unknown-flag", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_returns_none_for_empty_args(self):
        """Test returns None for empty args."""
        path, method = parse_gh_api_args([])
        assert path is None
        assert method == "GET"

    def test_handles_method_flag_at_end(self):
        """Test handling -X flag at end without value."""
        path, method = parse_gh_api_args(["repos/owner/repo", "-X"])
        assert path == "repos/owner/repo"
        assert method == "GET"


class TestExtractRepoFromGhApiPath:
    """Tests for extract_repo_from_gh_api_path function."""

    def test_extracts_repo_from_pulls_path(self):
        """Test extracting repo from pulls path."""
        repo = extract_repo_from_gh_api_path("repos/owner/repo/pulls")
        assert repo == "owner/repo"

    def test_extracts_repo_from_path_with_leading_slash(self):
        """Test extracting repo from path with leading slash."""
        repo = extract_repo_from_gh_api_path("/repos/owner/repo/issues")
        assert repo == "owner/repo"

    def test_returns_none_for_user_path(self):
        """Test returns None for user path."""
        repo = extract_repo_from_gh_api_path("user")
        assert repo is None

    def test_returns_none_for_non_repos_path(self):
        """Test returns None for non-repos path."""
        repo = extract_repo_from_gh_api_path("orgs/myorg/repos")
        assert repo is None

    def test_returns_none_for_incomplete_path(self):
        """Test returns None for incomplete repos path."""
        repo = extract_repo_from_gh_api_path("repos/owner")
        assert repo is None

    def test_returns_none_for_dash_prefixed_parts(self):
        """Test returns None when owner/repo starts with dash."""
        repo = extract_repo_from_gh_api_path("repos/-owner/repo")
        assert repo is None


class TestExtractRepoFromGhCommand:
    """Tests for extract_repo_from_gh_command function."""

    def test_extracts_repo_from_repo_flag(self):
        """Test extracting repo from --repo flag."""
        repo = extract_repo_from_gh_command(["pr", "list", "--repo", "owner/repo"])
        assert repo == "owner/repo"

    def test_extracts_repo_from_r_flag(self):
        """Test extracting repo from -R flag."""
        repo = extract_repo_from_gh_command(["issue", "view", "-R", "owner/repo", "123"])
        assert repo == "owner/repo"

    def test_extracts_repo_from_repo_view(self):
        """Test extracting repo from repo view command."""
        repo = extract_repo_from_gh_command(["repo", "view", "owner/repo"])
        assert repo == "owner/repo"

    def test_extracts_repo_from_repo_clone(self):
        """Test extracting repo from repo clone command."""
        repo = extract_repo_from_gh_command(["repo", "clone", "owner/repo"])
        assert repo == "owner/repo"

    def test_extracts_repo_from_api_command(self):
        """Test extracting repo from api command."""
        repo = extract_repo_from_gh_command(["api", "repos/owner/repo/pulls"])
        assert repo == "owner/repo"

    def test_returns_none_for_empty_args(self):
        """Test returns None for empty args."""
        repo = extract_repo_from_gh_command([])
        assert repo is None

    def test_returns_none_for_repo_without_slash(self):
        """Test returns None when repo arg doesn't contain slash."""
        repo = extract_repo_from_gh_command(["repo", "view", "localrepo"])
        assert repo is None

    def test_returns_none_for_repo_flag_at_end(self):
        """Test returns None when --repo flag has no value."""
        repo = extract_repo_from_gh_command(["pr", "list", "--repo"])
        assert repo is None

    def test_returns_none_for_flag_like_repo_arg(self):
        """Test returns None when repo arg looks like a flag."""
        repo = extract_repo_from_gh_command(["repo", "view", "--help"])
        assert repo is None


class TestValidateGhApiPath:
    """Tests for validate_gh_api_path function."""

    def test_allows_user_path(self):
        """Test allows user path."""
        valid, msg = validate_gh_api_path("user")
        assert valid is True
        assert msg == ""

    def test_allows_pulls_path(self):
        """Test allows pulls path."""
        valid, msg = validate_gh_api_path("repos/owner/repo/pulls")
        assert valid is True

    def test_allows_issues_path(self):
        """Test allows issues path."""
        valid, msg = validate_gh_api_path("repos/owner/repo/issues/123")
        assert valid is True

    def test_allows_post_method(self):
        """Test allows POST method."""
        valid, msg = validate_gh_api_path("repos/owner/repo/pulls", method="POST")
        assert valid is True

    def test_allows_patch_method(self):
        """Test allows PATCH method."""
        valid, msg = validate_gh_api_path("repos/owner/repo/issues/1", method="PATCH")
        assert valid is True

    def test_rejects_delete_method(self):
        """Test rejects DELETE method."""
        valid, msg = validate_gh_api_path("repos/owner/repo/pulls/1", method="DELETE")
        assert valid is False
        assert "DELETE" in msg

    def test_rejects_put_method(self):
        """Test rejects PUT method."""
        valid, msg = validate_gh_api_path("repos/owner/repo/pulls/1", method="PUT")
        assert valid is False
        assert "PUT" in msg

    def test_rejects_unknown_path(self):
        """Test rejects unknown path."""
        valid, msg = validate_gh_api_path("admin/users")
        assert valid is False
        assert "not in allowlist" in msg

    def test_strips_leading_slash(self):
        """Test strips leading slash before validation."""
        valid, msg = validate_gh_api_path("/user")
        assert valid is True

    def test_pr_comments_allowed(self):
        """Test that PR comments are allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123/comments", "GET")
        assert allowed is True

    def test_pr_comments_post_allowed(self):
        """Test that POST to PR comments is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/pulls/123/comments", "POST")
        assert allowed is True

    def test_branches_allowed(self):
        """Test that branch listing is allowed."""
        allowed, msg = validate_gh_api_path("repos/owner/repo/branches", "GET")
        assert allowed is True

    def test_specific_user_allowed(self):
        """Test that specific user info is allowed."""
        allowed, msg = validate_gh_api_path("users/testuser", "GET")
        assert allowed is True

    def test_orgs_blocked(self):
        """Test that org endpoints are blocked."""
        allowed, msg = validate_gh_api_path("orgs/myorg/members", "GET")
        assert allowed is False

    def test_pr_review_comments_allowed(self):
        """Test that PR review comments are allowed."""
        path = "repos/owner/repo/pulls/123/reviews/456/comments"
        allowed, msg = validate_gh_api_path(path, "GET")
        assert allowed is True


class TestGitHubToken:
    """Tests for GitHubToken dataclass."""

    def test_is_expired_returns_false_for_future_token(self):
        """Test is_expired returns False for token expiring in future."""
        future_time = time.time() + 3600  # 1 hour from now
        token = GitHubToken(
            token="test_token",
            expires_at_unix=future_time,
            expires_at="2099-01-01T00:00:00Z",
            generated_at="2024-01-01T00:00:00Z",
        )
        assert token.is_expired is False

    def test_is_expired_returns_true_for_past_token(self):
        """Test is_expired returns True for token that expired."""
        past_time = time.time() - 3600  # 1 hour ago
        token = GitHubToken(
            token="test_token",
            expires_at_unix=past_time,
            expires_at="2020-01-01T00:00:00Z",
            generated_at="2020-01-01T00:00:00Z",
        )
        assert token.is_expired is True

    def test_is_expired_includes_buffer(self):
        """Test is_expired includes 5 minute buffer."""
        # Token expires in 4 minutes - should be considered expired due to buffer
        near_future = time.time() + 240  # 4 minutes from now
        token = GitHubToken(
            token="test_token",
            expires_at_unix=near_future,
            expires_at="soon",
            generated_at="now",
        )
        assert token.is_expired is True

    def test_minutes_until_expiry(self):
        """Test minutes_until_expiry calculation."""
        future_time = time.time() + 1800  # 30 minutes from now
        token = GitHubToken(
            token="test_token",
            expires_at_unix=future_time,
            expires_at="future",
            generated_at="now",
        )
        # Allow some tolerance for timing
        assert 29 < token.minutes_until_expiry < 31


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

    def test_to_dict(self):
        """Test to_dict returns correct dictionary."""
        result = GitHubResult(
            success=True,
            stdout="output",
            stderr="error",
            returncode=0,
        )
        d = result.to_dict()
        assert d == {
            "success": True,
            "stdout": "output",
            "stderr": "error",
            "returncode": 0,
        }

    def test_to_dict_with_failure(self):
        """Test to_dict with failed result."""
        result = GitHubResult(
            success=False,
            stdout="",
            stderr="command failed",
            returncode=1,
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["returncode"] == 1
