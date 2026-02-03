"""Tests for gateway policy module.

Tests the PolicyEngine class and policy checking functions.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gateway.policy import (
    BoundedCache,
    CachedPRInfo,
    PolicyEngine,
    PolicyResult,
    extract_branch_from_refspec,
    extract_repo_from_remote,
)


class TestPolicyResult:
    """Tests for PolicyResult dataclass."""

    def test_to_dict_allowed(self):
        """Test to_dict for allowed result."""
        result = PolicyResult(allowed=True, reason="Test reason")
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "Test reason"
        assert "details" not in d

    def test_to_dict_denied_with_details(self):
        """Test to_dict for denied result with details."""
        result = PolicyResult(
            allowed=False,
            reason="Denied",
            details={"author": "someone"},
        )
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["details"] == {"author": "someone"}


class TestBoundedCache:
    """Tests for BoundedCache class."""

    def test_respects_max_size(self):
        """Test that cache respects max size."""
        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["d"] = 4  # Should evict 'a'

        assert len(cache) == 3
        assert "a" not in cache
        assert "d" in cache

    def test_updates_move_to_end(self):
        """Test that updates move items to end."""
        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3

        # Update 'a' - moves it to end
        cache["a"] = 10

        # Add new item - should evict 'b', not 'a'
        cache["d"] = 4

        assert "a" in cache
        assert "b" not in cache


class TestCachedPRInfo:
    """Tests for CachedPRInfo class."""

    def test_is_stale_fresh_entry(self):
        """Test that a fresh entry is not stale."""
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="OPEN",
            head_branch="feature",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert not info.is_stale

    def test_is_stale_old_entry(self):
        """Test that an old entry (>5 min) is stale."""
        # 10 minutes ago
        old_time = datetime.now(UTC).timestamp() - 600
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="OPEN",
            head_branch="feature",
            fetched_at=old_time,
        )
        assert info.is_stale


class TestPolicyEngine:
    """Tests for PolicyEngine class."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a PolicyEngine instance for testing."""
        return PolicyEngine(
            github_client=mock_github_client,
            bot_name="egg",
            branch_prefix="egg/",
            protected_branches=["main", "master"],
        )

    def test_check_pr_create_allowed_bot_mode(self, policy_engine):
        """Test that PR creation is allowed in bot mode."""
        result = policy_engine.check_pr_create_allowed("owner/repo", auth_mode="bot")
        assert result.allowed is True
        assert "bot mode" in result.reason.lower()

    def test_check_pr_create_blocked_user_mode(self, policy_engine):
        """Test that PR creation is blocked in user mode."""
        result = policy_engine.check_pr_create_allowed("owner/repo", auth_mode="user")
        assert result.allowed is False
        assert "user mode" in result.reason.lower()
        assert result.details["auth_mode"] == "user"

    def test_check_branch_ownership_protected_branch(self, policy_engine):
        """Test that protected branches are blocked."""
        result = policy_engine.check_branch_ownership("owner/repo", "main")
        assert result.allowed is False
        assert "protected" in result.reason.lower()

    def test_check_branch_ownership_prefixed_branch(self, policy_engine):
        """Test that egg-prefixed branches are allowed."""
        result = policy_engine.check_branch_ownership("owner/repo", "egg/feature-123")
        assert result.allowed is True
        assert "prefix" in result.reason.lower()

    def test_check_branch_ownership_egg_dash_prefix(self, policy_engine):
        """Test that egg- prefixed branches are also allowed."""
        result = policy_engine.check_branch_ownership("owner/repo", "egg-feature-123")
        assert result.allowed is True

    def test_check_pr_ownership_bot_author(self, policy_engine, mock_github_client):
        """Test that bot-authored PRs are owned."""
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "egg[bot]"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 42)
        assert result.allowed is True
        assert "owned by" in result.reason.lower()

    def test_check_pr_ownership_not_bot_author(self, policy_engine, mock_github_client):
        """Test that non-bot authored PRs are not owned."""
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "some-user"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 42)
        assert result.allowed is False
        assert "not owned" in result.reason.lower()

    def test_check_pr_ownership_pr_not_found(self, policy_engine, mock_github_client):
        """Test handling when PR is not found."""
        mock_github_client.get_pr_info.return_value = None

        result = policy_engine.check_pr_ownership("owner/repo", 999)
        assert result.allowed is False
        assert "not found" in result.reason.lower()

    def test_check_pr_comment_allowed(self, policy_engine, mock_github_client):
        """Test that comments are allowed on any PR."""
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "any-user"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_comment_allowed("owner/repo", 42)
        assert result.allowed is True
        assert "allowed" in result.reason.lower()

    def test_check_pr_comment_pr_not_found(self, policy_engine, mock_github_client):
        """Test that comment fails when PR doesn't exist."""
        mock_github_client.get_pr_info.return_value = None

        result = policy_engine.check_pr_comment_allowed("owner/repo", 999)
        assert result.allowed is False

    def test_check_merge_always_blocked(self, policy_engine):
        """Test that merge is always blocked."""
        result = policy_engine.check_merge_allowed("owner/repo", 42)
        assert result.allowed is False
        assert "human" in result.reason.lower()

    def test_check_branch_ownership_with_auth_mode_bot(self, policy_engine):
        """Test that auth_mode is included in bot mode result."""
        result = policy_engine.check_branch_ownership("owner/repo", "egg/feature", auth_mode="bot")
        assert result.allowed is True
        assert result.details.get("auth_mode") == "bot"

    def test_check_branch_ownership_user_mode_no_github_client(self, policy_engine):
        """Test user mode fails without GitHub client for branch check."""
        # PolicyEngine with no github client
        policy_engine.github = None
        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert result.allowed is False
        assert "github client" in result.reason.lower()

    def test_check_branch_ownership_user_mode_new_branch(self, policy_engine, mock_github_client):
        """Test user mode allows push to new branch."""
        mock_github_client.branch_exists.return_value = False

        result = policy_engine.check_branch_ownership("owner/repo", "new-feature", auth_mode="user")
        assert result.allowed is True
        assert "new branch" in result.reason.lower()
        assert result.details.get("auth_mode") == "user"
        assert result.details.get("reason") == "new_branch"

    def test_check_branch_ownership_user_mode_existing_no_pr(
        self, policy_engine, mock_github_client
    ):
        """Test user mode blocks existing branch without authorized PR."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = []

        result = policy_engine.check_branch_ownership(
            "owner/repo", "existing-branch", auth_mode="user"
        )
        assert result.allowed is False
        assert result.details.get("auth_mode") == "user"

    def test_check_branch_ownership_user_mode_existing_with_bot_pr(
        self, policy_engine, mock_github_client
    ):
        """Test user mode allows existing branch with bot PR."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 123,
                "author": {"login": "egg[bot]"},
                "state": "OPEN",
                "headRefName": "existing",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "egg[bot]"},
            "state": "OPEN",
            "headRefName": "existing",
        }

        result = policy_engine.check_branch_ownership(
            "owner/repo", "existing-branch", auth_mode="user"
        )
        assert result.allowed is True
        assert result.details.get("reason") == "bot_pr"

    def test_check_branch_ownership_user_mode_api_error(self, policy_engine, mock_github_client):
        """Test user mode fails closed on API error."""
        mock_github_client.branch_exists.return_value = None  # API error

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert result.allowed is False
        assert "api error" in result.reason.lower()

    def test_check_pr_ownership_with_auth_mode(self, policy_engine, mock_github_client):
        """Test that auth_mode is passed through to PR ownership check."""
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "egg[bot]"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 42, auth_mode="user")
        assert result.allowed is True
        assert result.details.get("auth_mode") == "user"

    def test_check_branch_ownership_configured_user_pr(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Test that configured user's PRs are allowed in bot mode."""
        # Mock the configured user
        monkeypatch.setattr(
            "gateway.policy.PolicyEngine._get_configured_user",
            lambda self: "configured-user",
        )

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 123,
                "author": {"login": "configured-user"},
                "state": "OPEN",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "configured-user"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership(
            "owner/repo", "feature-branch", auth_mode="bot"
        )
        assert result.allowed is True
        assert result.details.get("reason") == "configured_user_pr"

    def test_check_pr_ownership_configured_user(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Test that configured user's PRs are recognized as owned."""
        # Mock the configured user
        monkeypatch.setattr(
            "gateway.policy.PolicyEngine._get_configured_user",
            lambda self: "configured-user",
        )

        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "configured-user"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 42)
        assert result.allowed is True
        assert "configured user" in result.reason.lower()

    def test_check_branch_ownership_user_mode_existing_with_configured_user_pr(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Test user mode allows existing branch with configured user's PR."""
        # Mock the configured user
        monkeypatch.setattr(
            "gateway.policy.PolicyEngine._get_configured_user",
            lambda self: "configured-user",
        )

        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "configured-user"},
                "state": "OPEN",
                "headRefName": "feature-branch",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "configured-user"},
            "state": "OPEN",
            "headRefName": "feature-branch",
        }

        result = policy_engine.check_branch_ownership(
            "owner/repo", "feature-branch", auth_mode="user"
        )
        assert result.allowed is True
        assert result.details.get("auth_mode") == "user"
        assert result.details.get("reason") == "configured_user_pr"
        assert result.details.get("configured_user") == "configured-user"

    def test_check_pr_ownership_case_insensitive(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Test that configured user matching is case-insensitive."""
        # Mock with lowercase configured user
        monkeypatch.setattr(
            "gateway.policy.PolicyEngine._get_configured_user",
            lambda self: "configureduser",
        )

        # PR author has different case
        mock_github_client.get_pr_info.return_value = {
            "author": {"login": "ConfiguredUser"},
            "state": "OPEN",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 42)
        assert result.allowed is True
        assert "configured user" in result.reason.lower()


class TestExtractRepoFromRemote:
    """Tests for extract_repo_from_remote function."""

    def test_https_url(self):
        """Test extracting from HTTPS URL."""
        result = extract_repo_from_remote("https://github.com/owner/repo.git")
        assert result == "owner/repo"

    def test_https_url_no_git_suffix(self):
        """Test extracting from HTTPS URL without .git suffix."""
        result = extract_repo_from_remote("https://github.com/owner/repo")
        assert result == "owner/repo"

    def test_ssh_url(self):
        """Test extracting from SSH URL."""
        result = extract_repo_from_remote("git@github.com:owner/repo.git")
        assert result == "owner/repo"

    def test_ssh_url_no_git_suffix(self):
        """Test extracting from SSH URL without .git suffix."""
        result = extract_repo_from_remote("git@github.com:owner/repo")
        assert result == "owner/repo"

    def test_invalid_url(self):
        """Test that invalid URL returns None."""
        result = extract_repo_from_remote("not-a-valid-url")
        assert result is None

    def test_non_github_url(self):
        """Test that non-GitHub URLs return None."""
        result = extract_repo_from_remote("https://gitlab.com/owner/repo.git")
        assert result is None


class TestExtractBranchFromRefspec:
    """Tests for extract_branch_from_refspec function."""

    def test_simple_branch_name(self):
        """Test extracting simple branch name."""
        result = extract_branch_from_refspec("feature-branch")
        assert result == "feature-branch"

    def test_refs_heads_prefix(self):
        """Test stripping refs/heads/ prefix."""
        result = extract_branch_from_refspec("refs/heads/main")
        assert result == "main"

    def test_local_remote_format(self):
        """Test local:remote format."""
        result = extract_branch_from_refspec("local-branch:remote-branch")
        assert result == "remote-branch"

    def test_force_push_indicator(self):
        """Test stripping + force push indicator."""
        result = extract_branch_from_refspec("+feature")
        assert result == "feature"

    def test_full_refspec(self):
        """Test full refspec with refs/heads/ on both sides."""
        refspec = "+refs/heads/local:refs/heads/remote"
        result = extract_branch_from_refspec(refspec)
        assert result == "remote"

    def test_empty_refspec(self):
        """Test empty refspec returns None."""
        result = extract_branch_from_refspec("")
        assert result is None
