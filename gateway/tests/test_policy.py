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

    def test_branch_ownership_with_egg_pr(self, policy_engine, mock_github_client):
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

    def test_pr_ownership_egg_author(self, policy_engine, mock_github_client):
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

    def test_pr_ownership_egg_bot_author(self, policy_engine, mock_github_client):
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

    def test_pr_comment_allowed_on_egg_pr(self, policy_engine, mock_github_client):
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


class TestProtectedBranches:
    """Tests for protected branch enforcement."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_push_to_main_blocked_bot_mode(self, policy_engine):
        """Push to main branch is always blocked in bot mode."""
        result = policy_engine.check_branch_ownership("owner/repo", "main", auth_mode="bot")
        assert not result.allowed
        assert "protected" in result.reason.lower()
        assert result.details is not None
        assert result.details.get("branch") == "main"

    def test_push_to_main_blocked_user_mode(self, policy_engine):
        """Push to main branch is always blocked in user mode."""
        result = policy_engine.check_branch_ownership("owner/repo", "main", auth_mode="user")
        assert not result.allowed
        assert "protected" in result.reason.lower()

    def test_push_to_master_blocked(self, policy_engine):
        """Push to master branch is always blocked."""
        result = policy_engine.check_branch_ownership("owner/repo", "master")
        assert not result.allowed
        assert "protected" in result.reason.lower()

    def test_protected_branch_hint_suggests_feature_branch(self, policy_engine):
        """Protected branch denial provides helpful hint."""
        result = policy_engine.check_branch_ownership("owner/repo", "main")
        assert not result.allowed
        assert result.details is not None
        assert "hint" in result.details
        assert "feature branch" in result.details["hint"].lower()


class TestUserModeBranchOwnership:
    """Tests for user mode branch ownership logic."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client, monkeypatch):
        """Create a policy engine with mocked GitHub client and configured user."""
        engine = PolicyEngine(github_client=mock_github_client)
        monkeypatch.setattr(engine, "_get_configured_user", lambda: "testuser")
        return engine

    def test_user_mode_new_branch_allowed(self, policy_engine, mock_github_client):
        """User mode allows push to new branch (doesn't exist upstream)."""
        mock_github_client.branch_exists.return_value = False

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert result.allowed
        assert "new branch" in result.reason.lower()
        assert result.details is not None
        assert result.details.get("reason") == "new_branch"

    def test_user_mode_existing_branch_no_pr_denied(self, policy_engine, mock_github_client):
        """User mode denies push to existing branch with no PR."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = []

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert not result.allowed
        assert "no open pr" in result.reason.lower()

    def test_user_mode_existing_branch_with_bot_pr_allowed(self, policy_engine, mock_github_client):
        """User mode allows push to branch with bot's PR."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {"number": 123, "author": {"login": "egg"}, "state": "open", "headRefName": "feature"}
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert result.allowed
        assert result.details is not None
        assert result.details.get("reason") == "bot_pr"

    def test_user_mode_existing_branch_with_configured_user_pr_allowed(
        self, policy_engine, mock_github_client
    ):
        """User mode allows push to branch with configured user's PR."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 456,
                "author": {"login": "testuser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 456,
            "author": {"login": "testuser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert result.allowed
        assert result.details is not None
        assert result.details.get("reason") == "configured_user_pr"

    def test_user_mode_api_error_fails_closed(self, policy_engine, mock_github_client):
        """User mode fails closed when branch existence check fails (API error)."""
        mock_github_client.branch_exists.return_value = None  # API error

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert not result.allowed
        assert "could not verify" in result.reason.lower()
        assert "api error" in result.reason.lower()

    def test_user_mode_unrelated_pr_denied(self, policy_engine, mock_github_client):
        """User mode denies push when PR exists but by unrelated author."""
        mock_github_client.branch_exists.return_value = True
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 789,
                "author": {"login": "randomuser"},
                "state": "open",
                "headRefName": "feature",
            }
        ]
        mock_github_client.get_pr_info.return_value = {
            "number": 789,
            "author": {"login": "randomuser"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_branch_ownership("owner/repo", "feature", auth_mode="user")
        assert not result.allowed
        assert result.details is not None
        assert "hint" in result.details


class TestBotAuthorFormats:
    """Tests for different author data formats (string vs dict)."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_author_as_dict_with_login(self, policy_engine, mock_github_client):
        """Author provided as dict with login key is handled correctly."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert result.allowed

    def test_author_as_string(self, policy_engine, mock_github_client):
        """Author provided as string is handled correctly."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": "egg",  # String format
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        # Should handle string author gracefully
        # The implementation handles both formats via isinstance check
        assert result.allowed

    def test_author_dict_empty_login(self, policy_engine, mock_github_client):
        """Author dict with empty login is handled correctly."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": ""},
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert not result.allowed

    def test_author_case_insensitive_matching(self, policy_engine, mock_github_client):
        """Author matching is case insensitive."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "EGG"},  # Uppercase
            "state": "open",
            "headRefName": "feature",
        }

        result = policy_engine.check_pr_ownership("owner/repo", 123)
        assert result.allowed

    def test_bot_suffix_variants(self, policy_engine, mock_github_client):
        """All bot suffix variants are recognized."""
        variants = ["egg", "egg[bot]", "app/egg", "apps/egg"]

        for variant in variants:
            mock_github_client.get_pr_info.return_value = {
                "number": 123,
                "author": {"login": variant},
                "state": "open",
                "headRefName": "feature",
            }
            result = policy_engine.check_pr_ownership("owner/repo", 123)
            assert result.allowed, f"Variant '{variant}' should be recognized as bot"


class TestBoundedCache:
    """Tests for BoundedCache behavior."""

    def test_cache_evicts_oldest_when_full(self):
        """BoundedCache evicts oldest entries when max size exceeded."""
        from policy import BoundedCache

        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["d"] = 4  # Should evict "a"

        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache
        assert "d" in cache

    def test_cache_update_moves_to_end(self):
        """Updating existing key moves it to end (LRU behavior)."""
        from policy import BoundedCache

        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["a"] = 10  # Update "a", moves to end
        cache["d"] = 4  # Should evict "b" (now oldest)

        assert "a" in cache
        assert "b" not in cache
        assert "c" in cache
        assert "d" in cache

    def test_cache_respects_max_size(self):
        """Cache never exceeds max size."""
        from policy import BoundedCache

        cache = BoundedCache(max_size=5)
        for i in range(100):
            cache[f"key_{i}"] = i

        assert len(cache) == 5


class TestCachedPRInfoStaleness:
    """Tests for CachedPRInfo staleness detection."""

    def test_fresh_entry_not_stale(self):
        """Recently fetched entry is not stale."""
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="open",
            head_branch="feature",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert not info.is_stale

    def test_entry_becomes_stale_after_5_minutes(self):
        """Entry becomes stale after 5 minutes."""
        # 5 minutes + 1 second ago
        old_time = datetime.now(UTC).timestamp() - 301
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="open",
            head_branch="feature",
            fetched_at=old_time,
        )
        assert info.is_stale

    def test_entry_just_under_5_minutes_not_stale(self):
        """Entry just under 5 minutes is not yet stale."""
        # 4 minutes 59 seconds ago (299 seconds)
        boundary_time = datetime.now(UTC).timestamp() - 299
        info = CachedPRInfo(
            pr_number=1,
            author="egg",
            state="open",
            head_branch="feature",
            fetched_at=boundary_time,
        )
        assert not info.is_stale


class TestPRCacheBehavior:
    """Tests for policy engine PR caching behavior."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create a policy engine with mocked GitHub client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_pr_info_cached_on_fetch(self, policy_engine, mock_github_client):
        """PR info is cached after fetching from GitHub."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        # First call - should fetch from GitHub
        policy_engine.check_pr_ownership("owner/repo", 123)
        assert mock_github_client.get_pr_info.call_count == 1

        # Second call - should use cache
        policy_engine.check_pr_ownership("owner/repo", 123)
        assert mock_github_client.get_pr_info.call_count == 1  # No additional calls

    def test_stale_cache_refetches(self, policy_engine, mock_github_client):
        """Stale cache entries trigger refetch."""
        mock_github_client.get_pr_info.return_value = {
            "number": 123,
            "author": {"login": "egg"},
            "state": "open",
            "headRefName": "feature",
        }

        # First call
        policy_engine.check_pr_ownership("owner/repo", 123)

        # Manually stale the cache entry
        cache_key = ("owner/repo", 123)
        if cache_key in policy_engine._pr_cache:
            cached = policy_engine._pr_cache[cache_key]
            # Set fetched_at to 10 minutes ago
            policy_engine._pr_cache[cache_key] = CachedPRInfo(
                pr_number=cached.pr_number,
                author=cached.author,
                state=cached.state,
                head_branch=cached.head_branch,
                fetched_at=datetime.now(UTC).timestamp() - 600,
            )

        # Second call - should refetch due to stale cache
        policy_engine.check_pr_ownership("owner/repo", 123)
        assert mock_github_client.get_pr_info.call_count == 2

    def test_branch_pr_cache_populates_pr_cache(self, policy_engine, mock_github_client):
        """Fetching PRs for a branch also populates the PR info cache."""
        mock_github_client.list_prs_for_branch.return_value = [
            {
                "number": 123,
                "author": {"login": "egg"},
                "state": "open",
                "headRefName": "feature",
            },
            {
                "number": 456,
                "author": {"login": "other"},
                "state": "open",
                "headRefName": "feature",
            },
        ]

        # Trigger branch ownership check which fetches PRs
        policy_engine.check_branch_ownership("owner/repo", "feature")

        # Both PRs should now be in the cache
        assert ("owner/repo", 123) in policy_engine._pr_cache
        assert ("owner/repo", 456) in policy_engine._pr_cache


class TestPRCreatePolicy:
    """Tests for PR creation policy."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client."""
        return MagicMock()

    @pytest.fixture
    def policy_engine(self, mock_github_client, monkeypatch):
        """Create a policy engine with mocked GitHub client."""
        engine = PolicyEngine(github_client=mock_github_client)
        monkeypatch.setattr(engine, "_get_configured_user", lambda: "testuser")
        return engine

    def test_pr_create_allowed_bot_mode(self, policy_engine):
        """PR creation is allowed in bot mode."""
        result = policy_engine.check_pr_create_allowed("owner/repo", auth_mode="bot")
        assert result.allowed
        assert "bot mode" in result.reason.lower()

    def test_pr_create_blocked_user_mode(self, policy_engine):
        """PR creation is blocked in user mode."""
        result = policy_engine.check_pr_create_allowed("owner/repo", auth_mode="user")
        assert not result.allowed
        assert "user mode" in result.reason.lower()
        assert "github ui" in result.reason.lower()

    def test_pr_create_user_mode_provides_hint(self, policy_engine):
        """PR creation denial in user mode provides helpful hint."""
        result = policy_engine.check_pr_create_allowed("owner/repo", auth_mode="user")
        assert not result.allowed
        assert result.details is not None
        assert "hint" in result.details
