"""Tests for gateway fork_policy module."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from fork_policy import (
    ForkPolicy,
    ForkPolicyResult,
    check_fork_allowed,
    get_fork_policy,
)


class TestForkPolicyResult:
    """Tests for ForkPolicyResult dataclass."""

    def test_allowed_result(self):
        """Allowed result serialization."""
        result = ForkPolicyResult(
            allowed=True,
            reason="All good",
            source_visibility="private",
            target_visibility="private",
        )
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "All good"
        assert d["policy"] == "fork_policy"
        assert d["source_visibility"] == "private"
        assert d["target_visibility"] == "private"

    def test_denied_result(self):
        """Denied result serialization."""
        result = ForkPolicyResult(
            allowed=False,
            reason="Public repo blocked",
        )
        d = result.to_dict()
        assert d["allowed"] is False
        assert "source_visibility" not in d
        assert "target_visibility" not in d

    def test_with_details(self):
        """Result with details dict."""
        result = ForkPolicyResult(
            allowed=False,
            reason="blocked",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d["details"] == {"key": "value"}

    def test_without_optional_fields(self):
        """Result without optional fields."""
        result = ForkPolicyResult(allowed=True, reason="ok")
        d = result.to_dict()
        assert "source_visibility" not in d
        assert "target_visibility" not in d
        assert "details" not in d


class TestForkPolicyDisabled:
    """Tests for ForkPolicy when disabled."""

    def test_disabled_allows_everything(self):
        """Disabled policy allows all forks."""
        policy = ForkPolicy(enabled=False)
        assert policy.enabled is False

        result = policy.check_fork_source("owner", "public-repo")
        assert result.allowed is True
        assert "disabled" in result.reason.lower()

    def test_disabled_check_fork_target(self):
        """Disabled policy allows all targets."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork_target("myorg", make_private=False)
        assert result.allowed is True

    def test_disabled_check_fork(self):
        """Disabled policy allows full fork operation."""
        policy = ForkPolicy(enabled=False)
        result = policy.check_fork("owner", "repo", "target-org", make_private=False)
        assert result.allowed is True


class TestForkPolicyEnabled:
    """Tests for ForkPolicy when enabled."""

    @patch("fork_policy.get_repo_visibility", return_value="public")
    def test_public_source_blocked(self, mock_vis):
        """Fork from public repo is blocked."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "public-repo")
        assert result.allowed is False
        assert result.source_visibility is None or "public" in str(result.reason).lower()

    @patch("fork_policy.get_repo_visibility", return_value="private")
    def test_private_source_allowed(self, mock_vis):
        """Fork from private repo is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "private-repo")
        assert result.allowed is True
        assert result.source_visibility == "private"

    @patch("fork_policy.get_repo_visibility", return_value="internal")
    def test_internal_source_allowed(self, mock_vis):
        """Fork from internal repo is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "internal-repo")
        assert result.allowed is True

    @patch("fork_policy.get_repo_visibility", return_value=None)
    def test_unknown_visibility_blocked(self, mock_vis):
        """Unknown visibility is blocked."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_source("owner", "unknown-repo")
        assert result.allowed is False

    def test_target_private_allowed(self):
        """Fork to private target is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("myorg", make_private=True)
        assert result.allowed is True
        assert result.target_visibility == "private"

    def test_target_public_blocked(self):
        """Fork to public target is blocked."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork_target("myorg", make_private=False)
        assert result.allowed is False
        assert result.target_visibility == "public"

    @patch("fork_policy.get_repo_visibility", return_value="private")
    def test_full_fork_private_to_private(self, mock_vis):
        """Full fork from private to private is allowed."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", "target-org", make_private=True)
        assert result.allowed is True
        assert result.source_visibility == "private"
        assert result.target_visibility == "private"

    @patch("fork_policy.get_repo_visibility", return_value="public")
    def test_full_fork_public_source_blocked(self, mock_vis):
        """Full fork from public is blocked at source check."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "pub-repo", "target-org", make_private=True)
        assert result.allowed is False

    @patch("fork_policy.get_repo_visibility", return_value="private")
    def test_full_fork_public_target_blocked(self, mock_vis):
        """Full fork to public target is blocked at target check."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", "target-org", make_private=False)
        assert result.allowed is False

    @patch("fork_policy.get_repo_visibility", return_value="private")
    def test_full_fork_personal_account(self, mock_vis):
        """Fork to personal account (no target_org)."""
        policy = ForkPolicy(enabled=True)
        result = policy.check_fork("owner", "repo", target_org=None, make_private=True)
        assert result.allowed is True
        assert "personal" in result.reason


class TestGetForkPolicy:
    """Tests for get_fork_policy singleton."""

    def test_returns_fork_policy(self):
        """Returns a ForkPolicy instance."""
        import fork_policy

        fork_policy._fork_policy = None
        with patch("fork_policy.is_private_mode_enabled", return_value=False):
            policy = get_fork_policy()
            assert isinstance(policy, ForkPolicy)

    def test_singleton_behavior(self):
        """Returns same instance on subsequent calls."""
        import fork_policy

        fork_policy._fork_policy = None
        with patch("fork_policy.is_private_mode_enabled", return_value=False):
            p1 = get_fork_policy()
            p2 = get_fork_policy()
            assert p1 is p2


class TestCheckForkAllowed:
    """Tests for check_fork_allowed convenience function."""

    @patch("fork_policy.is_private_mode_enabled", return_value=False)
    def test_convenience_function(self, mock_mode):
        """Convenience function delegates to policy."""
        import fork_policy

        fork_policy._fork_policy = None
        result = check_fork_allowed("owner", "repo")
        assert result.allowed is True
