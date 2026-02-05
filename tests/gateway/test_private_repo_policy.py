"""Tests for gateway private_repo_policy module."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from private_repo_policy import (
    PrivateRepoPolicy,
    PrivateRepoPolicyResult,
    is_private_mode_enabled,
)


class TestPrivateRepoPolicyResult:
    """Tests for PrivateRepoPolicyResult dataclass."""

    def test_allowed(self):
        """Allowed result."""
        r = PrivateRepoPolicyResult(
            allowed=True,
            reason="Private repo allowed",
            visibility="private",
        )
        d = r.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "Private repo allowed"
        assert d["policy"] == "private_mode"
        assert d["visibility"] == "private"

    def test_denied(self):
        """Denied result."""
        r = PrivateRepoPolicyResult(
            allowed=False,
            reason="Public repo blocked",
            visibility="public",
        )
        d = r.to_dict()
        assert d["allowed"] is False
        assert d["visibility"] == "public"

    def test_no_visibility(self):
        """Result without visibility."""
        r = PrivateRepoPolicyResult(allowed=True, reason="OK")
        d = r.to_dict()
        assert "visibility" not in d

    def test_with_session_mode(self):
        """Result with session mode."""
        r = PrivateRepoPolicyResult(
            allowed=True,
            reason="OK",
            session_mode="private",
        )
        d = r.to_dict()
        assert d["session_mode"] == "private"

    def test_with_details(self):
        """Result with details dict."""
        r = PrivateRepoPolicyResult(
            allowed=False,
            reason="blocked",
            details={"repo": "owner/repo"},
        )
        d = r.to_dict()
        assert d["details"]["repo"] == "owner/repo"


class TestIsPrivateModeEnabled:
    """Tests for is_private_mode_enabled function."""

    def test_env_true(self, monkeypatch):
        """PRIVATE_MODE=true enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "true")
        assert is_private_mode_enabled() is True

    def test_env_false(self, monkeypatch):
        """PRIVATE_MODE=false disables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "false")
        assert is_private_mode_enabled() is False

    def test_env_not_set(self, monkeypatch):
        """Unset PRIVATE_MODE defaults to false."""
        monkeypatch.delenv("PRIVATE_MODE", raising=False)
        assert is_private_mode_enabled() is False

    def test_env_one(self, monkeypatch):
        """PRIVATE_MODE=1 enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "1")
        assert is_private_mode_enabled() is True

    def test_env_yes(self, monkeypatch):
        """PRIVATE_MODE=yes enables private mode."""
        monkeypatch.setenv("PRIVATE_MODE", "yes")
        assert is_private_mode_enabled() is True


class TestPrivateRepoPolicy:
    """Tests for PrivateRepoPolicy class."""

    def test_no_session_mode_denied(self):
        """Requests without session_mode are denied."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            owner="org",
            repo="repo",
            session_mode=None,
        )
        assert result.allowed is False
        assert "session" in result.reason.lower()

    @patch("private_repo_policy.get_repo_visibility")
    def test_private_mode_allows_private_repo(self, mock_vis):
        """Private mode allows private repos."""
        mock_vis.return_value = "private"
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="private-repo",
            session_mode="private",
        )
        assert result.allowed is True

    @patch("private_repo_policy.get_repo_visibility")
    def test_private_mode_denies_public_repo(self, mock_vis):
        """Private mode denies public repos."""
        mock_vis.return_value = "public"
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="public-repo",
            session_mode="private",
        )
        assert result.allowed is False

    @patch("private_repo_policy.get_repo_visibility")
    def test_public_mode_allows_public_repo(self, mock_vis):
        """Public mode allows public repos."""
        mock_vis.return_value = "public"
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="public-repo",
            session_mode="public",
        )
        assert result.allowed is True

    @patch("private_repo_policy.get_repo_visibility")
    def test_public_mode_denies_private_repo(self, mock_vis):
        """Public mode denies private repos."""
        mock_vis.return_value = "private"
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="private-repo",
            session_mode="public",
        )
        assert result.allowed is False

    @patch("private_repo_policy.get_repo_visibility")
    def test_unknown_visibility_denied(self, mock_vis):
        """Unknown visibility (None) is denied (fail closed)."""
        mock_vis.return_value = None
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="mysterious-repo",
            session_mode="private",
        )
        assert result.allowed is False

    def test_no_repo_info_denied(self):
        """Requests with no identifiable repo are denied."""
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="push",
            session_mode="private",
        )
        assert result.allowed is False

    @patch("private_repo_policy.get_repo_visibility")
    def test_private_mode_allows_internal_repo(self, mock_vis):
        """Private mode allows internal repos."""
        mock_vis.return_value = "internal"
        policy = PrivateRepoPolicy()
        result = policy.check_repository_access(
            operation="fetch",
            owner="org",
            repo="internal-repo",
            session_mode="private",
        )
        assert result.allowed is True
