"""Tests for gateway/fork_policy.py."""

import os
from unittest.mock import patch

from gateway.fork_policy import (
    ForkPolicy,
    ForkPolicyResult,
    check_fork_allowed,
    get_fork_policy,
)


class TestForkPolicyResult:
    """Tests for ForkPolicyResult dataclass."""

    def test_basic_result(self):
        """Test creating a basic result."""
        result = ForkPolicyResult(
            allowed=True,
            reason="Test reason",
        )
        assert result.allowed is True
        assert result.reason == "Test reason"
        assert result.source_visibility is None
        assert result.target_visibility is None
        assert result.details is None

    def test_result_with_all_fields(self):
        """Test creating result with all fields."""
        result = ForkPolicyResult(
            allowed=False,
            reason="Fork blocked",
            source_visibility="public",
            target_visibility="public",
            details={"hint": "Use a private repo"},
        )
        assert result.allowed is False
        assert result.source_visibility == "public"
        assert result.target_visibility == "public"
        assert result.details["hint"] == "Use a private repo"

    def test_to_dict_minimal(self):
        """Test to_dict with minimal fields."""
        result = ForkPolicyResult(allowed=True, reason="OK")
        d = result.to_dict()
        assert d == {
            "allowed": True,
            "reason": "OK",
            "policy": "fork_policy",
        }

    def test_to_dict_with_all_fields(self):
        """Test to_dict with all fields."""
        result = ForkPolicyResult(
            allowed=False,
            reason="Blocked",
            source_visibility="private",
            target_visibility="public",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d == {
            "allowed": False,
            "reason": "Blocked",
            "policy": "fork_policy",
            "source_visibility": "private",
            "target_visibility": "public",
            "details": {"key": "value"},
        }


class TestForkPolicy:
    """Tests for ForkPolicy class."""

    def test_init_default_enabled(self):
        """Test initialization with default enabled state from env."""
        with patch.dict(os.environ, {"PRIVATE_MODE": "true"}):
            from gateway import private_repo_policy

            # Reset cached value
            private_repo_policy._policy = None
            policy = ForkPolicy()
            assert policy.enabled is True

    def test_init_explicitly_disabled(self):
        """Test initialization with explicitly disabled state."""
        policy = ForkPolicy(enabled=False)
        assert policy.enabled is False

    def test_init_explicitly_enabled(self):
        """Test initialization with explicitly enabled state."""
        policy = ForkPolicy(enabled=True)
        assert policy.enabled is True

    def test_check_fork_source_disabled_policy(self):
        """Test check_fork_source when policy is disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed is True
        assert "disabled" in result.reason

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_source_public_blocked(self, mock_visibility):
        """Test that forking from public repos is blocked."""
        mock_visibility.return_value = "public"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed is False
        assert result.source_visibility == "public"
        assert "public" in result.reason.lower() or "blocked" in result.reason.lower()

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_source_private_allowed(self, mock_visibility):
        """Test that forking from private repos is allowed."""
        mock_visibility.return_value = "private"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed is True
        assert result.source_visibility == "private"

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_source_internal_allowed(self, mock_visibility):
        """Test that forking from internal repos is allowed."""
        mock_visibility.return_value = "internal"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed is True
        assert result.source_visibility == "internal"

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_source_unknown_visibility_blocked(self, mock_visibility):
        """Test that unknown visibility blocks forking."""
        mock_visibility.return_value = None
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed is False
        assert "visibility" in result.reason.lower() or "unknown" in result.reason.lower()

    def test_check_fork_target_disabled_policy(self):
        """Test check_fork_target when policy is disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork_target("org", make_private=False)
        assert result.allowed is True

    def test_check_fork_target_public_blocked(self):
        """Test that forking to public visibility is blocked."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("org", make_private=False)
        assert result.allowed is False
        assert result.target_visibility == "public"

    def test_check_fork_target_private_allowed(self):
        """Test that forking to private visibility is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("org", make_private=True)
        assert result.allowed is True
        assert result.target_visibility == "private"

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_full_disabled_policy(self, mock_visibility):
        """Test check_fork when policy is disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork("owner", "repo", "org", make_private=False)
        assert result.allowed is True
        mock_visibility.assert_not_called()

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_full_source_blocked(self, mock_visibility):
        """Test check_fork blocks when source is public."""
        mock_visibility.return_value = "public"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", "org", make_private=True)
        assert result.allowed is False

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_full_target_blocked(self, mock_visibility):
        """Test check_fork blocks when target is public."""
        mock_visibility.return_value = "private"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", "org", make_private=False)
        assert result.allowed is False
        assert result.target_visibility == "public"

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_full_allowed(self, mock_visibility):
        """Test check_fork allows private to private."""
        mock_visibility.return_value = "private"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", "org", make_private=True)
        assert result.allowed is True
        assert result.source_visibility == "private"
        assert result.target_visibility == "private"

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_personal_target(self, mock_visibility):
        """Test check_fork with personal target (None org)."""
        mock_visibility.return_value = "private"
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", target_org=None, make_private=True)
        assert result.allowed is True
        assert "personal" in result.reason

    @patch("gateway.fork_policy.logger")
    @patch("gateway.fork_policy.get_repo_visibility")
    def test_log_policy_event_allowed(self, mock_visibility, mock_logger):
        """Test that allowed events are logged as info."""
        mock_visibility.return_value = "private"
        policy = ForkPolicy(enabled=True)
        policy.check_fork_source("owner", "repo")
        mock_logger.info.assert_called()

    @patch("gateway.fork_policy.logger")
    @patch("gateway.fork_policy.get_repo_visibility")
    def test_log_policy_event_denied(self, mock_visibility, mock_logger):
        """Test that denied events are logged as warning."""
        mock_visibility.return_value = "public"
        policy = ForkPolicy(enabled=True)
        policy.check_fork_source("owner", "repo")
        mock_logger.warning.assert_called()


class TestGetForkPolicy:
    """Tests for get_fork_policy singleton function."""

    def test_returns_singleton(self):
        """Test that get_fork_policy returns same instance."""
        import gateway.fork_policy as fp

        # Reset global
        fp._fork_policy = None

        policy1 = get_fork_policy()
        policy2 = get_fork_policy()
        assert policy1 is policy2

    def test_thread_safety(self):
        """Test thread-safe initialization."""
        import threading

        import gateway.fork_policy as fp

        fp._fork_policy = None
        instances = []

        def get_instance():
            instances.append(get_fork_policy())

        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should be the same instance
        assert all(i is instances[0] for i in instances)


class TestCheckForkAllowed:
    """Tests for check_fork_allowed convenience function."""

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_allowed_function(self, mock_visibility):
        """Test the convenience function works."""
        mock_visibility.return_value = "private"
        import gateway.fork_policy as fp

        fp._fork_policy = ForkPolicy(enabled=True)

        result = check_fork_allowed(
            source_owner="owner",
            source_repo="repo",
            target_org="org",
            make_private=True,
        )
        assert result.allowed is True

    @patch("gateway.fork_policy.get_repo_visibility")
    def test_check_fork_allowed_uses_defaults(self, mock_visibility):
        """Test that check_fork_allowed uses default target_org."""
        mock_visibility.return_value = "private"
        import gateway.fork_policy as fp

        fp._fork_policy = ForkPolicy(enabled=True)

        result = check_fork_allowed(
            source_owner="owner",
            source_repo="repo",
        )
        # Should succeed with defaults
        assert result.allowed is True
