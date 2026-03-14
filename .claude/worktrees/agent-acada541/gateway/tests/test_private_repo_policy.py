"""
Tests for private_repo_policy module.

The module controls repository visibility per-session:
- session_mode="private": Private repos only
- session_mode="public": Public repos only

Network lockdown (PRIVATE_MODE env var) is separate - it controls Squid proxy config
but not repo visibility. Repo visibility is determined per-session.
"""

import os
from unittest.mock import patch

# Import from conftest-loaded module
from private_repo_policy import (
    PRIVATE_MODE_VAR,
    PrivateRepoPolicy,
    PrivateRepoPolicyResult,
    check_private_repo_access,
    get_private_repo_policy,
    is_private_mode_enabled,
)


class TestPrivateRepoPolicyResult:
    """Tests for PrivateRepoPolicyResult dataclass."""

    def test_to_dict_allowed(self):
        result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test reason",
            visibility="private",
        )
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "Test reason"
        assert d["visibility"] == "private"
        assert d["policy"] == "private_mode"

    def test_to_dict_denied(self):
        result = PrivateRepoPolicyResult(
            allowed=False,
            reason="Access denied",
            visibility="public",
            details={"hint": "Use private repo"},
        )
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["reason"] == "Access denied"
        assert d["visibility"] == "public"
        assert d["details"]["hint"] == "Use private repo"

    def test_to_dict_with_session_mode(self):
        result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test reason",
            visibility="private",
            session_mode="private",
        )
        d = result.to_dict()
        assert d["session_mode"] == "private"


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled function.

    Note: This function checks the PRIVATE_MODE env var which controls
    network lockdown (Squid proxy config), NOT repo visibility.
    Repo visibility is determined per-session via session_mode.
    """

    def test_enabled_with_true(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: "true"}):
            assert is_private_mode_enabled() is True

    def test_enabled_with_1(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: "1"}):
            assert is_private_mode_enabled() is True

    def test_enabled_with_yes(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: "yes"}):
            assert is_private_mode_enabled() is True

    def test_enabled_case_insensitive(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: "TRUE"}):
            assert is_private_mode_enabled() is True

    def test_disabled_with_false(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: "false"}):
            assert is_private_mode_enabled() is False

    def test_disabled_when_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_private_mode_enabled() is False

    def test_disabled_with_empty_string(self):
        with patch.dict(os.environ, {PRIVATE_MODE_VAR: ""}):
            assert is_private_mode_enabled() is False


class TestPrivateRepoPolicyPrivateMode:
    """Tests for PrivateRepoPolicy when session_mode='private'."""

    @patch("private_repo_policy.get_repo_visibility")
    def test_allows_private_repo(self, mock_visibility):
        """Private mode allows private repos."""
        mock_visibility.return_value = "private"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="private-repo",
            session_mode="private",
        )
        assert result.allowed is True
        assert result.visibility == "private"
        assert result.details.get("private_mode") is True
        assert result.session_mode == "private"

    @patch("private_repo_policy.get_repo_visibility")
    def test_allows_internal_repo(self, mock_visibility):
        """Private mode allows internal repos."""
        mock_visibility.return_value = "internal"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="internal-repo",
            session_mode="private",
        )
        assert result.allowed is True
        assert result.visibility == "internal"

    @patch("private_repo_policy.get_repo_visibility")
    def test_denies_public_repo(self, mock_visibility):
        """Private mode denies public repos."""
        mock_visibility.return_value = "public"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="public-repo",
            session_mode="private",
        )
        assert result.allowed is False
        assert result.visibility == "public"
        assert result.details.get("private_mode") is True

    @patch("private_repo_policy.get_repo_visibility")
    def test_fail_closed_on_unknown_visibility(self, mock_visibility):
        """When visibility is unknown, should deny (fail closed)."""
        mock_visibility.return_value = None

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="unknown-repo",
            session_mode="private",
        )
        assert result.allowed is False
        assert result.visibility is None

    def test_fail_closed_on_unknown_repo(self):
        """When repo cannot be determined, should deny."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner=None,
            repo=None,
            session_mode="private",
        )
        assert result.allowed is False

    @patch("private_repo_policy.get_repo_visibility")
    def test_check_repository_access_for_write(self, mock_visibility):
        """for_write=True should be passed through."""
        mock_visibility.return_value = "private"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="repo",
            for_write=True,
            session_mode="private",
        )
        assert result.allowed is True

        # Verify for_write=True was used (stricter caching)
        mock_visibility.assert_called_once_with("owner", "repo", for_write=True)

    @patch("private_repo_policy.get_repo_visibility")
    def test_check_repository_access_for_read(self, mock_visibility):
        """for_write=False should be passed through."""
        mock_visibility.return_value = "private"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="owner",
            repo="repo",
            for_write=False,
            session_mode="private",
        )
        assert result.allowed is True

        # Verify for_write=False was used (read operation)
        mock_visibility.assert_called_once_with("owner", "repo", for_write=False)


class TestPrivateRepoPolicyPublicMode:
    """Tests for PrivateRepoPolicy when session_mode='public'."""

    @patch("private_repo_policy.get_repo_visibility")
    def test_allows_public_repo(self, mock_visibility):
        """Public mode allows public repos."""
        mock_visibility.return_value = "public"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="public-repo",
            session_mode="public",
        )
        assert result.allowed is True
        assert result.visibility == "public"
        assert result.details.get("private_mode") is False
        assert result.session_mode == "public"

    @patch("private_repo_policy.get_repo_visibility")
    def test_denies_private_repo(self, mock_visibility):
        """Public mode denies private repos."""
        mock_visibility.return_value = "private"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="private-repo",
            session_mode="public",
        )
        assert result.allowed is False
        assert result.visibility == "private"
        assert "public" in result.reason.lower()
        assert result.details.get("private_mode") is False

    @patch("private_repo_policy.get_repo_visibility")
    def test_denies_internal_repo(self, mock_visibility):
        """Public mode denies internal repos."""
        mock_visibility.return_value = "internal"

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="internal-repo",
            session_mode="public",
        )
        assert result.allowed is False
        assert result.visibility == "internal"

    @patch("private_repo_policy.get_repo_visibility")
    def test_fail_closed_on_unknown_visibility(self, mock_visibility):
        """When visibility is unknown, should deny (fail closed)."""
        mock_visibility.return_value = None

        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="unknown-repo",
            session_mode="public",
        )
        assert result.allowed is False
        assert result.visibility is None

    def test_fail_closed_on_unknown_repo(self):
        """When repo cannot be determined, should deny."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner=None,
            repo=None,
            session_mode="public",
        )
        assert result.allowed is False


class TestPrivateRepoPolicyNoSessionMode:
    """Tests for PrivateRepoPolicy when session_mode is not provided."""

    def test_denies_when_session_mode_missing(self):
        """Operations without session_mode should be denied."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="owner",
            repo="some-repo",
            session_mode=None,
        )
        assert result.allowed is False
        assert "session mode" in result.reason.lower()
        assert result.details.get("error") == "Missing session mode"

    def test_denies_without_session_mode_explicit(self):
        """Explicitly passing None for session_mode should be denied."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="owner",
            repo="repo",
            # session_mode not passed (defaults to None)
        )
        assert result.allowed is False
        assert "session mode" in result.reason.lower()


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_private_repo_policy_returns_singleton(self):
        """get_private_repo_policy should return a singleton."""
        # Reset the global
        import private_repo_policy

        private_repo_policy._policy = None

        policy1 = get_private_repo_policy()
        policy2 = get_private_repo_policy()
        assert policy1 is policy2

    @patch("private_repo_policy.get_repo_visibility")
    def test_check_private_repo_access_with_session_mode(self, mock_visibility):
        """check_private_repo_access convenience function should work with session_mode."""
        mock_visibility.return_value = "private"

        # Reset singleton
        import private_repo_policy

        private_repo_policy._policy = None

        result = check_private_repo_access(
            operation="push",
            owner="owner",
            repo="repo",
            session_mode="private",
        )
        assert result.allowed is True

    def test_check_private_repo_access_without_session_mode(self):
        """check_private_repo_access without session_mode should deny."""
        # Reset singleton
        import private_repo_policy

        private_repo_policy._policy = None

        result = check_private_repo_access(
            operation="push",
            owner="owner",
            repo="repo",
            # No session_mode
        )
        assert result.allowed is False
        assert "session mode" in result.reason.lower()
