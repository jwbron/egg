"""
Tests for repo_parser module.
"""

# Import from conftest-loaded module
from repo_parser import (
    RepoInfo,
    is_github_url,
    normalize_repo_name,
    parse_github_url,
    parse_owner_repo,
)


class TestRepoInfo:
    """Tests for RepoInfo dataclass."""

    def test_full_name(self):
        info = RepoInfo(owner="owner", repo="repo")
        assert info.full_name == "owner/repo"

    def test_str(self):
        info = RepoInfo(owner="owner", repo="repo")
        assert str(info) == "owner/repo"


class TestParseGitHubUrl:
    """Tests for parse_github_url function."""

    def test_https_with_git_extension(self):
        url = "https://github.com/owner/repo.git"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_https_without_git_extension(self):
        url = "https://github.com/owner/repo"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_https_with_trailing_slash(self):
        url = "https://github.com/owner/repo/"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_ssh_colon_format(self):
        url = "git@github.com:owner/repo.git"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_ssh_without_git_extension(self):
        url = "git@github.com:owner/repo"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_ssh_protocol_format(self):
        url = "ssh://git@github.com/owner/repo.git"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_git_protocol_format(self):
        url = "git://github.com/owner/repo.git"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_empty_url(self):
        assert parse_github_url("") is None

    def test_none_url(self):
        assert parse_github_url(None) is None

    def test_non_github_url(self):
        url = "https://gitlab.com/owner/repo.git"
        assert parse_github_url(url) is None

    def test_whitespace_trimmed(self):
        url = "  https://github.com/owner/repo.git  "
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"


class TestParseOwnerRepo:
    """Tests for parse_owner_repo function."""

    def test_owner_repo_format(self):
        result = parse_owner_repo("owner/repo")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_with_url(self):
        result = parse_owner_repo("https://github.com/owner/repo.git")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_empty_string(self):
        assert parse_owner_repo("") is None

    def test_none(self):
        assert parse_owner_repo(None) is None

    def test_single_word(self):
        assert parse_owner_repo("repo") is None

    def test_whitespace_trimmed(self):
        result = parse_owner_repo("  owner/repo  ")
        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_complex_repo_name(self):
        result = parse_owner_repo("owner/repo-name_123")
        assert result is not None
        assert result.repo == "repo-name_123"


class TestIsGitHubUrl:
    """Tests for is_github_url function."""

    def test_https_url(self):
        assert is_github_url("https://github.com/owner/repo.git") is True

    def test_ssh_url(self):
        assert is_github_url("git@github.com:owner/repo.git") is True

    def test_non_github_url(self):
        assert is_github_url("https://gitlab.com/owner/repo.git") is False

    def test_empty_url(self):
        assert is_github_url("") is False

    def test_none(self):
        assert is_github_url(None) is False


class TestNormalizeRepoName:
    """Tests for normalize_repo_name function."""

    def test_with_git_suffix(self):
        assert normalize_repo_name("repo.git") == "repo"

    def test_without_git_suffix(self):
        assert normalize_repo_name("repo") == "repo"

    def test_git_in_name(self):
        # .git at end is removed, but "git" in middle is kept
        assert normalize_repo_name("mygitrepo.git") == "mygitrepo"
        assert normalize_repo_name("mygitrepo") == "mygitrepo"
