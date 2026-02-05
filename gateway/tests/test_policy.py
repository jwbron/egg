"""Tests for policy enforcement logic."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

# conftest.py loads the modules via importlib
# Import from the loaded policy module
from policy import (
    TRUSTED_BRANCH_OWNERS,
    CachedPRInfo,
    PolicyEngine,
    PolicyResult,
    _reset_bot_config_caches,
    extract_branch_from_refspec,
    extract_repo_from_remote,
    get_bot_branch_prefixes,
    get_bot_identities,
)


class TestExtractRepoFromRemote:
    """Tests for extract_repo_from_remote function."""

    def test_https_url_with_git_suffix(self):
        url = "https://github.com/owner/repo.git"
        assert extract_repo_from_remote(url) == "owner/repo"

    def test_https_url_without_git_suffix(self):
        url = "https://github.com/owner/repo"
        assert extract_repo_from_remote(url) == "owner/repo"

    def test_ssh_url(self):
        url = "git@github.com:owner/repo.git"
        assert extract_repo_from_remote(url) == "owner/repo"

    def test_ssh_url_without_git_suffix(self):
        url = "git@github.com:owner/repo"
        assert extract_repo_from_remote(url) == "owner/repo"

    def test_invalid_url(self):
        url = "not-a-valid-url"
        assert extract_repo_from_remote(url) is None

    def test_non_github_url(self):
        url = "https://gitlab.com/owner/repo.git"
        assert extract_repo_from_remote(url) is None


class TestExtractBranchFromRefspec:
    """Tests for extract_branch_from_refspec function."""

    def test_simple_branch(self):
        assert extract_branch_from_refspec("main") == "main"

    def test_refs_heads_prefix(self):
        assert extract_branch_from_refspec("refs/heads/feature") == "feature"

    def test_local_remote_format(self):
        assert extract_branch_from_refspec("local:remote") == "remote"

    def test_full_refspec(self):
        refspec = "+refs/heads/local:refs/heads/remote"
        assert extract_branch_from_refspec(refspec) == "remote"

    def test_empty_refspec(self):
        assert extract_branch_from_refspec("") is None

    def test_force_push_prefix(self):
        assert extract_branch_from_refspec("+main") == "main"


class TestBotIdentities:
    """Tests for bot identity checking.

    Bot identities are loaded from GATEWAY_BOT_NAME env var (REQUIRED).
    Branch prefixes are loaded from GATEWAY_BOT_BRANCH_PREFIX env var (REQUIRED).

    Note: conftest.py sets these env vars for tests.
    """

    def test_bot_identities_include_configured_variants(self):
        """Test that configured bot name variants are included."""
        # conftest.py sets GATEWAY_BOT_NAME=egg for tests
        identities = get_bot_identities()
        assert "egg" in identities
        assert "egg[bot]" in identities
        assert "app/egg" in identities
        assert "apps/egg" in identities

    def test_bot_branch_prefixes_configured(self):
        """Test that configured branch prefixes are supported."""
        # conftest.py sets GATEWAY_BOT_BRANCH_PREFIX=egg for tests
        prefixes = get_bot_branch_prefixes()
        assert "egg-" in prefixes
        assert "egg/" in prefixes

    def test_bot_identities_raises_without_config(self, monkeypatch):
        """Test that missing GATEWAY_BOT_NAME raises ValueError."""
        _reset_bot_config_caches()
        monkeypatch.delenv("GATEWAY_BOT_NAME", raising=False)
        with pytest.raises(ValueError, match="GATEWAY_BOT_NAME.*required"):
            get_bot_identities()
        # Restore for other tests
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        _reset_bot_config_caches()

    def test_bot_branch_prefixes_raises_without_config(self, monkeypatch):
        """Test that missing GATEWAY_BOT_BRANCH_PREFIX raises ValueError."""
        _reset_bot_config_caches()
        monkeypatch.delenv("GATEWAY_BOT_BRANCH_PREFIX", raising=False)
        with pytest.raises(ValueError, match="GATEWAY_BOT_BRANCH_PREFIX.*required"):
            get_bot_branch_prefixes()
        # Restore for other tests
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        _reset_bot_config_caches()

    def test_different_bot_name_configuration(self, monkeypatch):
        """Test that a different bot name generates correct identities."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
        identities = get_bot_identities()
        assert "james-in-a-box" in identities
        assert "james-in-a-box[bot]" in identities
        assert "app/james-in-a-box" in identities
        assert "apps/james-in-a-box" in identities
        # Restore for other tests
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg")
        _reset_bot_config_caches()

    def test_different_branch_prefix_configuration(self, monkeypatch):
        """Test that a different branch prefix generates correct prefixes."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james")
        prefixes = get_bot_branch_prefixes()
        assert "james-" in prefixes
        assert "james/" in prefixes
        # Restore for other tests
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        _reset_bot_config_caches()


class TestCachedPRInfo:
    """Tests for CachedPRInfo class."""

    def test_is_stale_fresh_entry(self):
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="open",
            head_branch="feature",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert not info.is_stale

    def test_is_stale_old_entry(self):
        # 10 minutes ago
        old_time = datetime.now(UTC).timestamp() - 600
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="open",
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
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    # Branch ownership tests

    def test_branch_ownership_egg_prefix_dash(self, policy_engine):
        """egg- prefixed branches are always owned by egg."""
        result = policy_engine.check_branch_ownership("owner/repo", "egg-feature")
        assert result.allowed
        assert "bot-prefixed" in result.reason

    def test_branch_ownership_egg_prefix_slash(self, policy_engine):
        """egg/ prefixed branches are always owned by egg."""
        result = policy_engine.check_branch_ownership("owner/repo", "egg/feature")
        assert result.allowed
        assert "bot-prefixed" in result.reason

    def test_branch_ownership_with_jib_pr(self, policy_engine, mock_github_client):
        """Branch with open egg-authored PR is owned by egg."""
        # Mock PR list
        mock_github_client.list_prs_for_branch.return_value = [
            {"number": 123, "author": {"login": "egg"}, "state": "open", "headRefName": "feature"}
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert result.allowed
        assert "PR #123" in result.reason

    def test_branch_ownership_no_pr(self, policy_engine, mock_github_client):
        """Branch without PR is not owned by egg."""
        mock_github_client.list_prs_for_branch.return_value = []

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert not result.allowed
        assert "not owned by egg" in result.reason

    def test_branch_ownership_other_author_pr(self, policy_engine, mock_github_client):
        """Branch with PR by non-egg author is not owned by egg."""
        mock_github_client.list_prs_for_branch.return_value = [
            {"number": 123, "author": {"login": "human"}, "state": "open", "headRefName": "feature"}
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "human"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert not result.allowed

    # PR ownership tests

    def test_pr_ownership_jib_author(self, policy_engine, mock_github_client):
        """PR authored by egg is owned by egg."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert result.allowed
        assert "owned by egg" in result.reason

    def test_pr_ownership_jib_bot_author(self, policy_engine, mock_github_client):
        """PR authored by egg[bot] is owned by egg."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg[bot]"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert result.allowed

    def test_pr_ownership_other_author(self, policy_engine, mock_github_client):
        """PR authored by non-egg, non-configured user is not owned."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "human"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert not result.allowed
        assert "not owned by egg or configured user" in result.reason

    def test_pr_ownership_not_found(self, policy_engine, mock_github_client):
        """PR that doesn't exist returns not allowed."""
        mock_github_client.get_pr_info.return_value = None

        result = policy_engine.check_pr_ownership("owner/repo", 999)
        assert not result.allowed
        assert "not found" in result.reason

    # Merge policy tests

    def test_merge_always_blocked(self, policy_engine):
        """Merge operations are always blocked."""
        result = policy_engine.check_merge_allowed("owner/repo", 123)
        assert not result.allowed
        assert "not supported" in result.reason
        assert "Human must merge" in result.reason

    # PR comment tests

    def test_pr_comment_allowed_on_jib_pr(self, policy_engine, mock_github_client):
        """Comments are allowed on PRs owned by egg."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_comment_allowed("owner/repo", 123)
        assert result.allowed
        assert "allowed" in result.reason.lower()

    def test_pr_comment_allowed_on_other_pr(self, policy_engine, mock_github_client):
        """Comments are allowed on PRs owned by others."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "human"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_comment_allowed("owner/repo", 123)
        assert result.allowed
        assert "allowed" in result.reason.lower()

    def test_pr_comment_not_found(self, policy_engine, mock_github_client):
        """Comments not allowed if PR doesn't exist."""
        mock_github_client.get_pr_info.return_value = None

        result = policy_engine.check_pr_comment_allowed("owner/repo", 999)
        assert not result.allowed
        assert "not found" in result.reason


class TestTrustedBranchOwners:
    """Tests for trusted branch owners functionality."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_trusted_users_loaded_from_env(self, monkeypatch):
        """TRUSTED_BRANCH_OWNERS is loaded from environment."""
        # This tests the module-level loading which happens at import time
        # The actual value depends on whether GATEWAY_TRUSTED_USERS was set
        # We just verify it's a frozenset
        assert isinstance(TRUSTED_BRANCH_OWNERS, frozenset)

    def test_branch_ownership_trusted_user_pr(self, policy_engine, mock_github_client, monkeypatch):
        """Branch with open PR by trusted user allows push."""
        # Patch the module-level TRUSTED_BRANCH_OWNERS
        import policy

        monkeypatch.setattr(policy, "TRUSTED_BRANCH_OWNERS", frozenset({"trusteduser"}))

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "trusteduser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 456,
            "author": {"login": "trusteduser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert result.allowed
        assert "trusted user" in result.reason.lower()

    def test_branch_ownership_trusted_user_case_insensitive(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Trusted user check is case insensitive."""
        import policy

        monkeypatch.setattr(policy, "TRUSTED_BRANCH_OWNERS", frozenset({"trusteduser"}))

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "TrustedUser"},  # Different case
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 456,
            "author": {"login": "TrustedUser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert result.allowed

    def test_branch_ownership_untrusted_user_pr(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Branch with open PR by non-trusted user denies push."""
        import policy

        monkeypatch.setattr(policy, "TRUSTED_BRANCH_OWNERS", frozenset({"trusteduser"}))

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "randomuser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 456,
            "author": {"login": "randomuser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert not result.allowed

    def test_branch_ownership_no_trusted_users_configured(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """With no trusted users configured, only egg can push."""
        import policy

        monkeypatch.setattr(policy, "TRUSTED_BRANCH_OWNERS", frozenset())

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "anyuser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 456,
            "author": {"login": "anyuser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature")
        assert not result.allowed


class TestConfiguredUser:
    """Tests for configured user (user mode) functionality in both modes."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_branch_ownership_configured_user_pr_bot_mode(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Bot mode allows push to branch with PR by configured user."""
        # Mock _get_configured_user to return a configured user
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 789,
                "author": {"login": "configureduser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 789,
            "author": {"login": "configureduser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="bot")
        assert result.allowed
        assert "configured user" in result.reason.lower()

    def test_branch_ownership_configured_user_case_insensitive(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Configured user check is case insensitive."""
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 789,
                "author": {"login": "ConfiguredUser"},  # Different case
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 789,
            "author": {"login": "ConfiguredUser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="bot")
        assert result.allowed

    def test_pr_ownership_configured_user_bot_mode(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Bot mode allows PR ownership by configured user."""
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "configureduser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123, auth_mode="bot")
        assert result.allowed
        assert "configured user" in result.reason.lower()

    def test_pr_ownership_configured_user_user_mode(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """User mode allows PR ownership by configured user."""
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "configureduser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123, auth_mode="user")
        assert result.allowed
        assert "configured user" in result.reason.lower()

    def test_pr_ownership_egg_with_configured_user_set(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """Egg PRs are still allowed when configured user is set."""
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123, auth_mode="bot")
        assert result.allowed
        assert "owned by egg" in result.reason.lower()

    def test_user_mode_denial_does_not_mention_trusted_users(
        self, policy_engine, mock_github_client, monkeypatch
    ):
        """User mode denial message should not mention trusted users."""
        monkeypatch.setattr(policy_engine, "_get_configured_user", lambda: "configureduser")

        # Branch exists with PR by unrelated user
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 999,
                "author": {"login": "randomuser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 999,
            "author": {"login": "randomuser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert not result.allowed
        # User mode should only mention egg and configured user, not trusted users
        assert "trusted" not in result.reason.lower()
        assert "egg" in result.reason.lower()
        assert "configureduser" in result.reason.lower()


class TestPolicyResult:
    """Tests for PolicyResult class."""

    def test_to_dict_allowed(self):
        result = PolicyResult(allowed=True, reason="Test reason")
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "Test reason"
        assert "details" not in d

    def test_to_dict_with_details(self):
        result = PolicyResult(
            allowed=False,
            reason="Test reason",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["details"] == {"key": "value"}
