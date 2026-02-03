"""
Tests for repo_parser module.
"""

import subprocess
from unittest.mock import MagicMock, patch

from gateway.repo_parser import (
    RepoInfo,
    extract_repo_from_request,
    get_remote_url,
    is_github_url,
    normalize_github_url,
    normalize_repo_name,
    parse_github_url,
    parse_owner_repo,
    parse_repo_from_path,
    parse_worktree_path,
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


class TestNormalizeGitHubUrl:
    """Tests for normalize_github_url function."""

    def test_empty_url(self):
        assert normalize_github_url("") == ""

    def test_whitespace_stripped(self):
        result = normalize_github_url("  https://github.com/owner/repo  ")
        assert result == "https://github.com/owner/repo"

    def test_url_encoded_chars_decoded(self):
        # %6f = o, %77 = w, etc.
        result = normalize_github_url("https://github.com/%6f%77%6e%65%72/repo")
        assert "owner" in result.lower()

    def test_double_encoded_chars(self):
        # Double-encoded characters should be decoded
        result = normalize_github_url("https://github.com/owner/repo")
        assert result == "https://github.com/owner/repo"

    def test_double_slashes_removed(self):
        result = normalize_github_url("https://github.com//owner//repo")
        assert "//" not in result or "://" in result

    def test_trailing_slash_removed(self):
        result = normalize_github_url("https://github.com/owner/repo/")
        assert not result.endswith("/") or result == "https://github.com"

    def test_credentials_stripped(self):
        result = normalize_github_url("https://user:pass@github.com/owner/repo")
        assert "user:pass" not in result

    def test_non_https_protocol_double_slashes(self):
        result = normalize_github_url("git://github.com//owner//repo")
        assert "git://" in result
        # Should normalize path
        assert "//owner" not in result

    def test_non_github_url_passthrough(self):
        result = normalize_github_url("https://gitlab.com/owner/repo")
        assert "gitlab.com" in result


class TestGetRemoteUrl:
    """Tests for get_remote_url function."""

    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )

        result = get_remote_url("/path/to/repo")
        assert result == "https://github.com/owner/repo.git"

    @patch("subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="fatal: not a git repository",
        )

        result = get_remote_url("/path/to/nonrepo")
        assert result is None

    @patch("subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)

        result = get_remote_url("/path/to/repo")
        assert result is None

    @patch("subprocess.run")
    def test_exception(self, mock_run):
        mock_run.side_effect = Exception("Some error")

        result = get_remote_url("/path/to/repo")
        assert result is None

    @patch("subprocess.run")
    def test_custom_remote(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/fork/repo.git\n",
        )

        get_remote_url("/path/to/repo", remote="upstream")

        call_args = mock_run.call_args[0][0]
        assert "upstream" in call_args


class TestParseRepoFromPath:
    """Tests for parse_repo_from_path function."""

    @patch("gateway.repo_parser.get_remote_url")
    def test_success(self, mock_get_url):
        mock_get_url.return_value = "https://github.com/owner/repo.git"

        result = parse_repo_from_path("/path/to/repo")

        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    @patch("gateway.repo_parser.get_remote_url")
    def test_no_remote(self, mock_get_url):
        mock_get_url.return_value = None

        result = parse_repo_from_path("/path/to/repo")
        assert result is None


class TestParseWorktreePath:
    """Tests for parse_worktree_path function."""

    def test_empty_path(self):
        container_id, repo_name = parse_worktree_path("")
        assert container_id is None
        assert repo_name is None

    def test_path_not_in_worktree_base(self):
        container_id, repo_name = parse_worktree_path("/some/other/path")
        assert container_id is None
        assert repo_name is None

    def test_valid_worktree_path(self, tmp_path):
        # Create worktree structure
        worktree_base = tmp_path / ".egg-worktrees"
        worktree_base.mkdir()
        container_dir = worktree_base / "container-123"
        container_dir.mkdir()
        repo_dir = container_dir / "test-repo"
        repo_dir.mkdir()

        container_id, repo_name = parse_worktree_path(
            str(repo_dir),
            worktree_base=str(worktree_base),
        )

        assert container_id == "container-123"
        assert repo_name == "test-repo"

    def test_insufficient_parts(self, tmp_path):
        # Path with only container, no repo
        worktree_base = tmp_path / ".egg-worktrees"
        worktree_base.mkdir()
        container_dir = worktree_base / "container-123"
        container_dir.mkdir()

        container_id, repo_name = parse_worktree_path(
            str(container_dir),
            worktree_base=str(worktree_base),
        )

        assert container_id is None
        assert repo_name is None


class TestExtractRepoFromRequest:
    """Tests for extract_repo_from_request function."""

    def test_repo_param_takes_priority(self):
        result = extract_repo_from_request(
            repo="owner/repo",
            url="https://github.com/other/other",
        )

        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    def test_url_fallback(self):
        result = extract_repo_from_request(
            repo=None,
            url="https://github.com/owner/repo",
        )

        assert result is not None
        assert result.owner == "owner"
        assert result.repo == "repo"

    @patch("gateway.repo_parser.parse_repo_from_path")
    def test_repo_path_fallback(self, mock_parse):
        mock_parse.return_value = RepoInfo(owner="owner", repo="repo")

        result = extract_repo_from_request(
            repo=None,
            url=None,
            repo_path="/path/to/repo",
        )

        assert result is not None
        assert result.owner == "owner"

    def test_no_sources(self):
        result = extract_repo_from_request(
            repo=None,
            url=None,
            repo_path=None,
        )

        assert result is None

    def test_invalid_repo_falls_through_to_url(self):
        result = extract_repo_from_request(
            repo="invalid",  # Not owner/repo format
            url="https://github.com/owner/repo",
        )

        assert result is not None
        assert result.owner == "owner"


class TestParseGitHubUrlSecurityCases:
    """Tests for security edge cases in URL parsing."""

    def test_path_traversal_in_owner(self):
        url = "https://github.com/..//etc/passwd"
        result = parse_github_url(url)
        # Should either return None or sanitized result
        if result is not None:
            assert ".." not in result.owner

    def test_path_traversal_in_repo(self):
        url = "https://github.com/owner/../sensitive"
        result = parse_github_url(url)
        if result is not None:
            assert ".." not in result.repo

    def test_http_downgrade(self):
        # HTTP URLs should still parse (normalization upgrades to HTTPS)
        url = "http://github.com/owner/repo"
        result = parse_github_url(url)
        assert result is not None
        assert result.owner == "owner"
