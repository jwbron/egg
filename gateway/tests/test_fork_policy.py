"""Tests for fork_policy.py - Fork-specific policy rules."""

from unittest.mock import patch

from fork_policy import (
    ForkPolicy,
    ForkPolicyResult,
    check_fork_allowed,
    get_fork_policy,
)


class TestForkPolicyResult:
    """Tests for ForkPolicyResult dataclass."""

    def test_to_dict_basic(self):
        """to_dict includes allowed, reason, and policy."""
        result = ForkPolicyResult(allowed=True, reason="test reason")
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "test reason"
        assert d["policy"] == "fork_policy"

    def test_to_dict_with_visibilities(self):
        """to_dict includes visibility when set."""
        result = ForkPolicyResult(
            allowed=False,
            reason="blocked",
            source_visibility="public",
            target_visibility="private",
        )
        d = result.to_dict()
        assert d["source_visibility"] == "public"
        assert d["target_visibility"] == "private"

    def test_to_dict_with_details(self):
        """to_dict includes details when set."""
        result = ForkPolicyResult(
            allowed=True,
            reason="ok",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d["details"] == {"key": "value"}

    def test_to_dict_without_optionals(self):
        """to_dict excludes None optional fields."""
        result = ForkPolicyResult(allowed=True, reason="ok")
        d = result.to_dict()
        assert "source_visibility" not in d
        assert "target_visibility" not in d
        assert "details" not in d


class TestForkPolicyDisabled:
    """Tests for ForkPolicy when disabled."""

    def test_disabled_check_fork_source(self):
        """Returns allowed when disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork_source("owner", "repo")
        assert result.allowed
        assert "disabled" in result.reason

    def test_disabled_check_fork_target(self):
        """Returns allowed when disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork_target("org")
        assert result.allowed

    def test_disabled_check_fork(self):
        """Returns allowed when disabled."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork("owner", "repo")
        assert result.allowed

    def test_enabled_property(self):
        """enabled property reflects init value."""
        assert ForkPolicy(enabled=True).enabled is True
        assert ForkPolicy(enabled=False).enabled is False


class TestForkPolicyEnabled:
    """Tests for ForkPolicy when enabled."""

    def test_check_fork_source_public_blocked(self):
        """Forking from public repo is blocked."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="public"):
            result = policy.check_fork_source("owner", "public-repo")
            assert not result.allowed
            assert result.source_visibility == "public"

    def test_check_fork_source_private_allowed(self):
        """Forking from private repo is allowed."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="private"):
            result = policy.check_fork_source("owner", "private-repo")
            assert result.allowed
            assert result.source_visibility == "private"

    def test_check_fork_source_internal_allowed(self):
        """Forking from internal repo is allowed."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="internal"):
            result = policy.check_fork_source("owner", "internal-repo")
            assert result.allowed
            assert result.source_visibility == "internal"

    def test_check_fork_source_unknown_visibility_blocked(self):
        """Unknown visibility is blocked."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value=None):
            result = policy.check_fork_source("owner", "unknown-repo")
            assert not result.allowed

    def test_check_fork_target_private_allowed(self):
        """Fork to private target is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("target-org", make_private=True)
        assert result.allowed
        assert result.target_visibility == "private"

    def test_check_fork_target_public_blocked(self):
        """Fork to public target is blocked."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("target-org", make_private=False)
        assert not result.allowed
        assert result.target_visibility == "public"

    def test_check_fork_full_private_to_private(self):
        """Full fork check: private to private is allowed."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="private"):
            result = policy.check_fork("owner", "repo", target_org="org", make_private=True)
            assert result.allowed
            assert result.source_visibility == "private"
            assert result.target_visibility == "private"

    def test_check_fork_full_public_source_blocked(self):
        """Full fork check: public source is blocked."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="public"):
            result = policy.check_fork("owner", "repo", target_org="org")
            assert not result.allowed

    def test_check_fork_full_public_target_blocked(self):
        """Full fork check: public target is blocked."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="private"):
            result = policy.check_fork("owner", "repo", target_org="org", make_private=False)
            assert not result.allowed

    def test_check_fork_personal_account(self):
        """Fork to personal account (no org) uses 'personal'."""
        policy = ForkPolicy(enabled=True)
        with patch("fork_policy.get_repo_visibility", return_value="private"):
            result = policy.check_fork("owner", "repo", target_org=None, make_private=True)
            assert result.allowed
            assert "personal" in result.reason


class TestGetForkPolicy:
    """Tests for get_fork_policy singleton."""

    def test_returns_fork_policy(self):
        """get_fork_policy returns a ForkPolicy instance."""
        import fork_policy as fp
        fp._fork_policy = None  # Reset global
        with patch("fork_policy.is_private_mode_enabled", return_value=False):
            policy = get_fork_policy()
            assert isinstance(policy, ForkPolicy)
        fp._fork_policy = None  # Cleanup

    def test_returns_same_instance(self):
        """get_fork_policy returns the same instance on repeated calls."""
        import fork_policy as fp
        fp._fork_policy = None
        with patch("fork_policy.is_private_mode_enabled", return_value=False):
            p1 = get_fork_policy()
            p2 = get_fork_policy()
            assert p1 is p2
        fp._fork_policy = None


class TestCheckForkAllowed:
    """Tests for check_fork_allowed convenience function."""

    def test_delegates_to_policy(self):
        """check_fork_allowed delegates to the global policy."""
        import fork_policy as fp
        fp._fork_policy = None
        with patch("fork_policy.is_private_mode_enabled", return_value=False):
            result = check_fork_allowed("owner", "repo")
            assert result.allowed
        fp._fork_policy = None

    def test_with_target_org(self):
        """check_fork_allowed passes target_org."""
        import fork_policy as fp
        fp._fork_policy = None
        with patch("fork_policy.is_private_mode_enabled", return_value=True):
            with patch("fork_policy.get_repo_visibility", return_value="private"):
                result = check_fork_allowed("owner", "repo", target_org="org", make_private=True)
                assert result.allowed
        fp._fork_policy = None
