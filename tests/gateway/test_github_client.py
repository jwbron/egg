"""Tests for gateway github_client module."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from github_client import (
    GitHubClient,
    GitHubResult,
    GitHubToken,
    extract_repo_from_gh_api_path,
    extract_repo_from_gh_command,
    get_github_client,
    get_user_mode_client,
    parse_gh_api_args,
    validate_gh_api_path,
)


class TestValidateGhApiPath:
    """Tests for validate_gh_api_path function."""

    def test_get_pr_list(self):
        """GET on PR list is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls")
        assert valid is True
        assert err == ""

    def test_get_specific_pr(self):
        """GET on specific PR is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls/123")
        assert valid is True

    def test_get_pr_comments(self):
        """GET on PR comments is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls/42/comments")
        assert valid is True

    def test_get_issue(self):
        """GET on issue is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1")
        assert valid is True

    def test_get_issue_comments(self):
        """GET on issue comments is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/5/comments")
        assert valid is True

    def test_get_repo_info(self):
        """GET on repo info is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo")
        assert valid is True

    def test_get_branches(self):
        """GET on branches is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/branches")
        assert valid is True

    def test_get_specific_branch(self):
        """GET on a specific branch is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/branches/main")
        assert valid is True

    def test_get_commits(self):
        """GET on commits is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/commits")
        assert valid is True

    def test_get_specific_commit(self):
        """GET on specific commit is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/commits/abc123def")
        assert valid is True

    def test_get_contents(self):
        """GET on file contents is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/contents/src/main.py")
        assert valid is True

    def test_get_releases(self):
        """GET on releases is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/releases")
        assert valid is True

    def test_get_latest_release(self):
        """GET on latest release is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/releases/latest")
        assert valid is True

    def test_get_user(self):
        """GET on user is allowed."""
        valid, err = validate_gh_api_path("user")
        assert valid is True

    def test_get_specific_user(self):
        """GET on specific user is allowed."""
        valid, err = validate_gh_api_path("users/octocat")
        assert valid is True

    def test_post_allowed(self):
        """POST method is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues", method="POST")
        assert valid is True

    def test_patch_allowed(self):
        """PATCH method is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1", method="PATCH")
        assert valid is True

    def test_delete_blocked(self):
        """DELETE method is blocked."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1", method="DELETE")
        assert valid is False
        assert "DELETE" in err

    def test_put_blocked(self):
        """PUT method is blocked."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1", method="PUT")
        assert valid is False

    def test_unknown_path_blocked(self):
        """Unknown API paths are blocked."""
        valid, err = validate_gh_api_path("orgs/myorg/teams")
        assert valid is False
        assert "not in allowlist" in err

    def test_leading_slash_stripped(self):
        """Leading slash is stripped before matching."""
        valid, err = validate_gh_api_path("/repos/owner/repo/pulls")
        assert valid is True

    def test_pr_reviews(self):
        """PR reviews endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls/1/reviews")
        assert valid is True

    def test_pr_files(self):
        """PR files endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls/1/files")
        assert valid is True

    def test_pr_commits(self):
        """PR commits endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/pulls/1/commits")
        assert valid is True

    def test_git_refs(self):
        """Git refs endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/git/refs/heads/main")
        assert valid is True

    def test_compare_commits(self):
        """Compare endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/compare/main...feature")
        assert valid is True

    def test_release_by_tag(self):
        """Release by tag is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/releases/tags/v1.0.0")
        assert valid is True

    def test_issue_labels(self):
        """Issue labels endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1/labels")
        assert valid is True

    def test_issue_events(self):
        """Issue events endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1/events")
        assert valid is True

    def test_issue_timeline(self):
        """Issue timeline endpoint is allowed."""
        valid, err = validate_gh_api_path("repos/owner/repo/issues/1/timeline")
        assert valid is True


class TestParseGhApiArgs:
    """Tests for parse_gh_api_args function."""

    def test_simple_path(self):
        """Parse simple API path."""
        path, method = parse_gh_api_args(["repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_method_flag_short(self):
        """Parse -X method flag."""
        path, method = parse_gh_api_args(["-X", "PATCH", "repos/owner/repo/pulls/1"])
        assert path == "repos/owner/repo/pulls/1"
        assert method == "PATCH"

    def test_method_flag_long(self):
        """Parse --method flag."""
        path, method = parse_gh_api_args(["--method", "POST", "repos/owner/repo/issues"])
        assert path == "repos/owner/repo/issues"
        assert method == "POST"

    def test_method_equals_format(self):
        """Parse --method=PATCH format."""
        path, method = parse_gh_api_args(["--method=PATCH", "repos/owner/repo/pulls/1"])
        assert method == "PATCH"
        assert path == "repos/owner/repo/pulls/1"

    def test_x_equals_format(self):
        """Parse -X=POST format."""
        path, method = parse_gh_api_args(["-X=POST", "repos/owner/repo/issues"])
        assert method == "POST"

    def test_header_flag(self):
        """Skip -H header flags."""
        path, method = parse_gh_api_args(
            ["-H", "Accept: application/json", "repos/owner/repo/pulls"]
        )
        assert path == "repos/owner/repo/pulls"
        assert method == "GET"

    def test_field_flag(self):
        """Skip -f field flags."""
        path, method = parse_gh_api_args(
            ["-X", "PATCH", "repos/owner/repo/pulls/1", "-f", "base=main"]
        )
        assert path == "repos/owner/repo/pulls/1"
        assert method == "PATCH"

    def test_jq_flag(self):
        """Skip -q/--jq flags."""
        path, method = parse_gh_api_args(["repos/owner/repo/pulls", "--jq", ".[].number"])
        assert path == "repos/owner/repo/pulls"

    def test_paginate_flag(self):
        """Skip --paginate boolean flag."""
        path, method = parse_gh_api_args(["--paginate", "repos/owner/repo/issues"])
        assert path == "repos/owner/repo/issues"

    def test_silent_flag(self):
        """Skip --silent boolean flag."""
        path, method = parse_gh_api_args(["repos/owner/repo/pulls/1", "--silent"])
        assert path == "repos/owner/repo/pulls/1"

    def test_empty_args(self):
        """Empty args returns None path."""
        path, method = parse_gh_api_args([])
        assert path is None
        assert method == "GET"

    def test_only_flags(self):
        """Only flags with no path returns None."""
        path, method = parse_gh_api_args(["-X", "POST"])
        assert path is None

    def test_method_flag_without_value(self):
        """Method flag without value is skipped."""
        path, method = parse_gh_api_args(["-X"])
        assert path is None
        assert method == "GET"

    def test_unknown_flag_skipped(self):
        """Unknown flags starting with - are skipped."""
        path, method = parse_gh_api_args(["--unknown-flag", "repos/owner/repo/pulls"])
        assert path == "repos/owner/repo/pulls"

    def test_flag_with_equals(self):
        """Unknown flags with = are skipped."""
        path, method = parse_gh_api_args(["-f=key=value", "repos/owner/repo/issues"])
        assert path == "repos/owner/repo/issues"

    def test_method_case_insensitive(self):
        """Method is uppercased."""
        path, method = parse_gh_api_args(["-X", "post", "repos/owner/repo/issues"])
        assert method == "POST"

    def test_complex_example(self):
        """Complex real-world example."""
        path, method = parse_gh_api_args(
            [
                "-X",
                "POST",
                "-H",
                "Accept: application/vnd.github+json",
                "repos/owner/repo/issues/1/comments",
                "-f",
                "body=Hello",
            ]
        )
        assert path == "repos/owner/repo/issues/1/comments"
        assert method == "POST"


class TestExtractRepoFromGhApiPath:
    """Tests for extract_repo_from_gh_api_path function."""

    def test_simple_repo_path(self):
        """Extract owner/repo from repos path."""
        assert extract_repo_from_gh_api_path("repos/owner/repo/pulls") == "owner/repo"

    def test_just_repo(self):
        """Extract from minimal repos path."""
        assert extract_repo_from_gh_api_path("repos/owner/repo") == "owner/repo"

    def test_leading_slash(self):
        """Handle leading slash."""
        assert extract_repo_from_gh_api_path("/repos/owner/repo/issues/123") == "owner/repo"

    def test_non_repo_path(self):
        """Return None for non-repo paths."""
        assert extract_repo_from_gh_api_path("user") is None

    def test_orgs_path(self):
        """Return None for orgs path."""
        assert extract_repo_from_gh_api_path("orgs/myorg/repos") is None

    def test_too_few_parts(self):
        """Return None when not enough path parts."""
        assert extract_repo_from_gh_api_path("repos/owner") is None

    def test_dash_prefix_owner_blocked(self):
        """Reject owner starting with dash."""
        assert extract_repo_from_gh_api_path("repos/-badowner/repo") is None

    def test_dash_prefix_repo_blocked(self):
        """Reject repo starting with dash."""
        assert extract_repo_from_gh_api_path("repos/owner/-badrepo") is None

    def test_empty_string(self):
        """Handle empty string."""
        assert extract_repo_from_gh_api_path("") is None

    def test_deeply_nested_path(self):
        """Extract from deeply nested path."""
        assert (
            extract_repo_from_gh_api_path("repos/myorg/myrepo/pulls/42/comments") == "myorg/myrepo"
        )


class TestExtractRepoFromGhCommand:
    """Tests for extract_repo_from_gh_command function."""

    def test_repo_flag_short(self):
        """Extract via -R flag."""
        result = extract_repo_from_gh_command(["pr", "view", "123", "-R", "owner/repo"])
        assert result == "owner/repo"

    def test_repo_flag_long(self):
        """Extract via --repo flag."""
        result = extract_repo_from_gh_command(
            ["pr", "list", "--repo", "owner/repo", "--state", "open"]
        )
        assert result == "owner/repo"

    def test_repo_view_command(self):
        """Extract from gh repo view."""
        result = extract_repo_from_gh_command(["repo", "view", "owner/repo"])
        assert result == "owner/repo"

    def test_repo_clone_command(self):
        """Extract from gh repo clone."""
        result = extract_repo_from_gh_command(["repo", "clone", "owner/repo"])
        assert result == "owner/repo"

    def test_repo_fork_command(self):
        """Extract from gh repo fork."""
        result = extract_repo_from_gh_command(["repo", "fork", "owner/repo"])
        assert result == "owner/repo"

    def test_api_command(self):
        """Extract from gh api path."""
        result = extract_repo_from_gh_command(["api", "repos/owner/repo/pulls"])
        assert result == "owner/repo"

    def test_api_with_flags(self):
        """Extract from gh api with flags."""
        result = extract_repo_from_gh_command(["api", "-X", "GET", "repos/owner/repo/issues"])
        assert result == "owner/repo"

    def test_empty_args(self):
        """Empty args returns None."""
        assert extract_repo_from_gh_command([]) is None

    def test_no_repo_determinable(self):
        """Return None when repo not determinable."""
        assert extract_repo_from_gh_command(["pr", "list"]) is None

    def test_repo_subcommand_with_flag_arg(self):
        """Reject flag-like repo argument."""
        result = extract_repo_from_gh_command(["repo", "view", "--json", "name"])
        assert result is None

    def test_repo_subcommand_no_slash(self):
        """Reject repo argument without slash."""
        result = extract_repo_from_gh_command(["repo", "view", "just-a-name"])
        assert result is None

    def test_api_non_repo_path(self):
        """Return None for api with non-repo path."""
        result = extract_repo_from_gh_command(["api", "user"])
        assert result is None

    def test_repo_flag_has_priority(self):
        """--repo flag takes priority over positional args."""
        result = extract_repo_from_gh_command(
            ["repo", "view", "other/repo", "--repo", "priority/repo"]
        )
        assert result == "priority/repo"


class TestGitHubToken:
    """Tests for GitHubToken dataclass."""

    def test_not_expired(self):
        """Token far in the future is not expired."""
        from datetime import UTC, datetime

        future_ts = datetime.now(UTC).timestamp() + 3600  # 1 hour ahead
        token = GitHubToken(
            token="ghs_test123",
            expires_at_unix=future_ts,
            expires_at="2099-01-01T00:00:00Z",
            generated_at="2024-01-01T00:00:00Z",
        )
        assert token.is_expired is False
        assert token.minutes_until_expiry > 50

    def test_expired(self):
        """Token in the past is expired."""
        from datetime import UTC, datetime

        past_ts = datetime.now(UTC).timestamp() - 3600  # 1 hour ago
        token = GitHubToken(
            token="ghs_test123",
            expires_at_unix=past_ts,
            expires_at="2020-01-01T00:00:00Z",
            generated_at="2020-01-01T00:00:00Z",
        )
        assert token.is_expired is True
        assert token.minutes_until_expiry < 0

    def test_expired_within_buffer(self):
        """Token expiring within 5-minute buffer is considered expired."""
        from datetime import UTC, datetime

        # Expires in 3 minutes (within 5-minute buffer)
        near_ts = datetime.now(UTC).timestamp() + 180
        token = GitHubToken(
            token="ghs_test",
            expires_at_unix=near_ts,
            expires_at="soon",
            generated_at="now",
        )
        assert token.is_expired is True


class TestGitHubResult:
    """Tests for GitHubResult dataclass."""

    def test_success_result(self):
        """Successful result."""
        result = GitHubResult(success=True, stdout="output", stderr="", returncode=0)
        assert result.success is True
        d = result.to_dict()
        assert d["success"] is True
        assert d["stdout"] == "output"
        assert d["returncode"] == 0

    def test_failure_result(self):
        """Failed result."""
        result = GitHubResult(success=False, stdout="", stderr="error", returncode=1)
        assert result.success is False
        d = result.to_dict()
        assert d["stderr"] == "error"
        assert d["returncode"] == 1


class TestGitHubClient:
    """Tests for GitHubClient class."""

    def test_init_default_mode(self):
        """Default mode is bot."""
        client = GitHubClient()
        assert client.mode == "bot"

    def test_init_user_mode(self):
        """User mode initialization."""
        client = GitHubClient(mode="user")
        assert client.mode == "user"

    def test_get_user_token_from_env(self, monkeypatch):
        """Get user token from environment."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_testtoken123")
        client = GitHubClient(mode="user")
        assert client.get_user_token() == "ghp_testtoken123"

    def test_get_user_token_missing(self, monkeypatch):
        """Missing user token returns None."""
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        client = GitHubClient(mode="user")
        client._cached_user_token = None
        assert client.get_user_token() is None

    def test_get_user_token_cached(self, monkeypatch):
        """Cached user token is returned."""
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        client = GitHubClient(mode="user")
        client._cached_user_token = "cached_token"
        assert client.get_user_token() == "cached_token"

    def test_is_user_token_valid(self, monkeypatch):
        """User token validity check."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        client = GitHubClient(mode="user")
        assert client.is_user_token_valid() is True

    def test_is_user_token_invalid(self, monkeypatch):
        """User token invalid when missing."""
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        client = GitHubClient(mode="user")
        client._cached_user_token = None
        assert client.is_user_token_valid() is False

    def test_execute_no_token_bot_mode(self):
        """Execute without token returns error in bot mode."""
        client = GitHubClient(mode="bot")
        client._cached_token = None
        with patch.object(client, "get_token", return_value=None):
            result = client.execute(["pr", "list"])
            assert result.success is False
            assert "not available" in result.stderr

    def test_execute_no_token_user_mode(self, monkeypatch):
        """Execute without token returns error in user mode."""
        monkeypatch.delenv("GITHUB_USER_TOKEN", raising=False)
        client = GitHubClient(mode="user")
        client._cached_user_token = None
        result = client.execute(["pr", "list"])
        assert result.success is False
        assert "GITHUB_USER_TOKEN" in result.stderr

    @patch("subprocess.run")
    def test_execute_success(self, mock_run, monkeypatch):
        """Successful command execution."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        mock_run.return_value = MagicMock(returncode=0, stdout="output data", stderr="")
        client = GitHubClient(mode="user")
        result = client.execute(["pr", "list"])
        assert result.success is True
        assert result.stdout == "output data"

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run, monkeypatch):
        """Failed command execution."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        client = GitHubClient(mode="user")
        result = client.execute(["pr", "view", "999"])
        assert result.success is False
        assert result.returncode == 1

    @patch("subprocess.run")
    def test_execute_timeout(self, mock_run, monkeypatch):
        """Timeout during execution."""
        import subprocess

        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=60)
        client = GitHubClient(mode="user")
        result = client.execute(["pr", "list"], timeout=60)
        assert result.success is False
        assert "timed out" in result.stderr

    @patch("subprocess.run")
    def test_execute_exception(self, mock_run, monkeypatch):
        """Exception during execution."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        mock_run.side_effect = OSError("Command not found")
        client = GitHubClient(mode="user")
        result = client.execute(["pr", "list"])
        assert result.success is False
        assert "Command not found" in result.stderr

    @patch("subprocess.run")
    def test_execute_rate_limit_error(self, mock_run, monkeypatch):
        """Rate limit error is logged specifically."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_test")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="API rate limit exceeded")
        client = GitHubClient(mode="user")
        result = client.execute(["api", "user"])
        assert result.success is False

    def test_get_token_for_mode_user(self, monkeypatch):
        """Get token for user mode."""
        monkeypatch.setenv("GITHUB_USER_TOKEN", "ghp_user_token")
        client = GitHubClient()
        token = client.get_token_for_mode("user")
        assert token == "ghp_user_token"

    def test_get_token_for_mode_bot_no_token(self):
        """Get token for bot mode when no token available."""
        client = GitHubClient()
        client._cached_token = None
        with patch.object(client, "get_token", return_value=None):
            token = client.get_token_for_mode("bot")
            assert token is None


class TestGetGitHubClient:
    """Tests for module-level client accessor functions."""

    def test_get_github_client_caches(self):
        """Client instances are cached by mode."""
        import github_client as gc

        gc._clients.clear()
        client1 = get_github_client("bot")
        client2 = get_github_client("bot")
        assert client1 is client2

    def test_get_github_client_different_modes(self):
        """Different modes get different instances."""
        import github_client as gc

        gc._clients.clear()
        bot = get_github_client("bot")
        user = get_github_client("user")
        assert bot is not user
        assert bot.mode == "bot"
        assert user.mode == "user"

    def test_get_user_mode_client(self):
        """get_user_mode_client returns user mode client."""
        import github_client as gc

        gc._clients.clear()
        client = get_user_mode_client()
        assert client.mode == "user"
