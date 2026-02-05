"""Tests for gateway repo_parser module."""

import sys
from pathlib import Path

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from repo_parser import (
    RepoInfo,
    extract_repo_from_request,
    is_github_url,
    normalize_github_url,
    normalize_repo_name,
    parse_github_url,
    parse_owner_repo,
    parse_worktree_path,
)


class TestRepoInfo:
    """Tests for RepoInfo dataclass."""

    def test_full_name(self):
        """full_name combines owner and repo."""
        info = RepoInfo(owner="myorg", repo="myrepo")
        assert info.full_name == "myorg/myrepo"

    def test_str(self):
        """String representation is full_name."""
        info = RepoInfo(owner="owner", repo="repo")
        assert str(info) == "owner/repo"


class TestNormalizeGitHubUrl:
    """Tests for normalize_github_url function."""

    def test_empty_url(self):
        """Empty URL returns empty string."""
        assert normalize_github_url("") == ""

    def test_whitespace_stripped(self):
        """Whitespace is stripped."""
        result = normalize_github_url("  https://github.com/owner/repo  ")
        assert result == "https://github.com/owner/repo"

    def test_url_decode(self):
        """URL-encoded characters are decoded."""
        result = normalize_github_url("https://github.com/%6f%77%6e%65%72/repo")
        assert "owner" in result

    def test_double_slash_normalized(self):
        """Double slashes in path are normalized."""
        result = normalize_github_url("https://github.com//owner//repo")
        assert "//" not in result.split("://")[1]

    def test_trailing_slash_removed(self):
        """Trailing slash is removed."""
        result = normalize_github_url("https://github.com/owner/repo/")
        assert not result.endswith("/")

    def test_credentials_stripped(self):
        """User credentials in URL are stripped."""
        result = normalize_github_url("https://user:pass@github.com/owner/repo")
        assert "user:pass" not in result

    def test_ssh_url_passthrough(self):
        """SSH URLs pass through with minimal changes."""
        url = "git@github.com:owner/repo.git"
        result = normalize_github_url(url)
        assert "github.com" in result

    def test_non_github_url(self):
        """Non-GitHub HTTP URLs are returned as-is."""
        url = "https://gitlab.com/owner/repo"
        result = normalize_github_url(url)
        # gitlab.com is not github.com, so less normalization
        assert "gitlab.com" in result


class TestParseGitHubUrl:
    """Tests for parse_github_url function."""

    def test_https_url(self):
        """Parse HTTPS URL."""
        result = parse_github_url("https://github.com/owner/repo")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_https_url_with_git(self):
        """Parse HTTPS URL with .git suffix."""
        result = parse_github_url("https://github.com/owner/repo.git")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_ssh_url(self):
        """Parse SSH URL."""
        result = parse_github_url("git@github.com:owner/repo.git")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_ssh_protocol_url(self):
        """Parse SSH protocol URL."""
        result = parse_github_url("ssh://git@github.com/owner/repo.git")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_git_protocol_url(self):
        """Parse git protocol URL."""
        result = parse_github_url("git://github.com/owner/repo.git")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_empty_url(self):
        """Empty URL returns None."""
        assert parse_github_url("") is None

    def test_none_url(self):
        """None-like empty URL returns None."""
        assert parse_github_url("") is None

    def test_non_github_url(self):
        """Non-GitHub URL returns None."""
        assert parse_github_url("https://gitlab.com/owner/repo") is None

    def test_invalid_url(self):
        """Invalid URL returns None."""
        assert parse_github_url("not a url") is None

    def test_path_traversal_blocked(self):
        """Path traversal in owner/repo is blocked."""
        result = parse_github_url("https://github.com/../etc/passwd")
        assert result is None

    def test_https_trailing_slash(self):
        """HTTPS URL with trailing slash."""
        result = parse_github_url("https://github.com/owner/repo/")
        assert result is not None
        assert result.repo == "repo"

    def test_http_url(self):
        """HTTP (not HTTPS) URL."""
        result = parse_github_url("http://github.com/owner/repo")
        assert result is not None
        assert result.owner == "owner"

    def test_ssh_without_git_suffix(self):
        """SSH URL without .git suffix."""
        result = parse_github_url("git@github.com:owner/repo")
        assert result is not None
        assert result.repo == "repo"


class TestParseOwnerRepo:
    """Tests for parse_owner_repo function."""

    def test_simple_owner_repo(self):
        """Parse simple owner/repo."""
        result = parse_owner_repo("owner/repo")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_empty_string(self):
        """Empty string returns None."""
        assert parse_owner_repo("") is None

    def test_url_fallback(self):
        """Falls back to URL parsing."""
        result = parse_owner_repo("https://github.com/owner/repo")
        assert result is not None
        assert result.owner == "owner"

    def test_whitespace_stripped(self):
        """Whitespace is stripped."""
        result = parse_owner_repo("  owner/repo  ")
        assert result is not None
        assert result.owner == "owner"

    def test_invalid_format(self):
        """Invalid format returns None."""
        assert parse_owner_repo("just-a-name") is None

    def test_too_many_slashes(self):
        """Too many path segments returns None (or URL parse)."""
        result = parse_owner_repo("a/b/c")
        assert result is None


class TestParseWorktreePath:
    """Tests for parse_worktree_path function."""

    def test_empty_path(self):
        """Empty path returns None/None."""
        assert parse_worktree_path("") == (None, None)

    def test_non_worktree_path(self):
        """Non-worktree path returns None/None."""
        assert parse_worktree_path("/tmp/some/path") == (None, None)


class TestExtractRepoFromRequest:
    """Tests for extract_repo_from_request function."""

    def test_from_repo_param(self):
        """Extract from repo parameter."""
        result = extract_repo_from_request(repo="owner/repo")
        assert result is not None
        assert result.full_name == "owner/repo"

    def test_from_url(self):
        """Extract from URL."""
        result = extract_repo_from_request(url="https://github.com/org/project")
        assert result is not None
        assert result.full_name == "org/project"

    def test_repo_takes_priority(self):
        """Repo parameter takes priority over URL."""
        result = extract_repo_from_request(
            repo="correct/repo",
            url="https://github.com/wrong/repo",
        )
        assert result is not None
        assert result.full_name == "correct/repo"

    def test_nothing_provided(self):
        """No parameters returns None."""
        assert extract_repo_from_request() is None


class TestIsGitHubUrl:
    """Tests for is_github_url function."""

    def test_valid_github_url(self):
        """Valid GitHub URL."""
        assert is_github_url("https://github.com/owner/repo") is True

    def test_invalid_url(self):
        """Invalid URL."""
        assert is_github_url("not a url") is False

    def test_empty_url(self):
        """Empty URL."""
        assert is_github_url("") is False

    def test_non_github(self):
        """Non-GitHub URL."""
        assert is_github_url("https://gitlab.com/owner/repo") is False

    def test_ssh_github(self):
        """SSH GitHub URL."""
        assert is_github_url("git@github.com:owner/repo.git") is True


class TestNormalizeRepoName:
    """Tests for normalize_repo_name function."""

    def test_with_git_suffix(self):
        """Remove .git suffix."""
        assert normalize_repo_name("repo.git") == "repo"

    def test_without_git_suffix(self):
        """No change without .git suffix."""
        assert normalize_repo_name("repo") == "repo"

    def test_double_git(self):
        """Only removes trailing .git."""
        assert normalize_repo_name("repo.git.git") == "repo.git"
