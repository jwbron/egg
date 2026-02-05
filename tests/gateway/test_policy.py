"""Tests for gateway policy module."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from policy import (
    BoundedCache,
    CachedPRInfo,
    PolicyEngine,
    PolicyResult,
    _load_trusted_users,
    _reset_bot_config_caches,
    get_bot_branch_prefixes,
    get_bot_identities,
)


class TestPolicyResult:
    """Tests for PolicyResult dataclass."""

    def test_allowed_result(self):
        """Allowed result."""
        r = PolicyResult(allowed=True, reason="OK")
        d = r.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "OK"
        assert "details" not in d

    def test_denied_result(self):
        """Denied result."""
        r = PolicyResult(allowed=False, reason="Blocked")
        d = r.to_dict()
        assert d["allowed"] is False
        assert d["reason"] == "Blocked"

    def test_with_details(self):
        """Result with details."""
        r = PolicyResult(
            allowed=True,
            reason="OK",
            details={"branch": "main", "pr": 42},
        )
        d = r.to_dict()
        assert d["details"]["branch"] == "main"
        assert d["details"]["pr"] == 42


class TestCachedPRInfo:
    """Tests for CachedPRInfo dataclass."""

    def test_fresh(self):
        """Fresh cache entry."""
        info = CachedPRInfo(
            pr_number=1,
            author="user",
            state="open",
            head_branch="feature",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        assert info.is_stale is False

    def test_stale(self):
        """Stale cache entry (over 5 minutes)."""
        info = CachedPRInfo(
            pr_number=1,
            author="user",
            state="open",
            head_branch="feature",
            fetched_at=datetime.now(UTC).timestamp() - 600,  # 10 min ago
        )
        assert info.is_stale is True

    def test_fields(self):
        """Field access."""
        info = CachedPRInfo(
            pr_number=42,
            author="alice",
            state="open",
            head_branch="fix/bug",
            fetched_at=1234567890.0,
        )
        assert info.pr_number == 42
        assert info.author == "alice"
        assert info.state == "open"
        assert info.head_branch == "fix/bug"


class TestBoundedCache:
    """Tests for BoundedCache ordered dict."""

    def test_basic_operations(self):
        """Basic set/get."""
        cache = BoundedCache(max_size=5)
        cache["a"] = 1
        cache["b"] = 2
        assert cache["a"] == 1
        assert cache["b"] == 2
        assert len(cache) == 2

    def test_eviction(self):
        """Oldest entries are evicted when over max_size."""
        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["d"] = 4  # Should evict "a"
        assert "a" not in cache
        assert len(cache) == 3
        assert cache["d"] == 4

    def test_update_moves_to_end(self):
        """Updating existing key moves it to end."""
        cache = BoundedCache(max_size=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["a"] = 10  # Update, moves "a" to end
        cache["d"] = 4  # Should evict "b" (now oldest)
        assert "a" in cache
        assert "b" not in cache

    def test_max_size_1(self):
        """Cache with max_size of 1."""
        cache = BoundedCache(max_size=1)
        cache["a"] = 1
        assert len(cache) == 1
        cache["b"] = 2
        assert len(cache) == 1
        assert "a" not in cache
        assert cache["b"] == 2


class TestGetBotIdentities:
    """Tests for get_bot_identities function."""

    def test_returns_identities(self, monkeypatch):
        """Returns identity variants."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_NAME", "egg-bot")
        identities = get_bot_identities()
        assert "egg-bot" in identities
        assert "egg-bot[bot]" in identities
        assert "app/egg-bot" in identities
        assert "apps/egg-bot" in identities
        _reset_bot_config_caches()

    def test_missing_bot_name(self, monkeypatch):
        """Raises ValueError when GATEWAY_BOT_NAME not set."""
        _reset_bot_config_caches()
        monkeypatch.delenv("GATEWAY_BOT_NAME", raising=False)
        with pytest.raises(ValueError, match="GATEWAY_BOT_NAME"):
            get_bot_identities()
        _reset_bot_config_caches()

    def test_case_insensitive(self, monkeypatch):
        """Bot name is lowercased."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_NAME", "MyBot")
        identities = get_bot_identities()
        assert "mybot" in identities
        _reset_bot_config_caches()

    def test_cached(self, monkeypatch):
        """Second call uses cached value."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_NAME", "test-bot")
        ids1 = get_bot_identities()
        ids2 = get_bot_identities()
        assert ids1 is ids2
        _reset_bot_config_caches()


class TestGetBotBranchPrefixes:
    """Tests for get_bot_branch_prefixes function."""

    def test_returns_prefixes(self, monkeypatch):
        """Returns branch prefix variants."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        prefixes = get_bot_branch_prefixes()
        assert "egg-" in prefixes
        assert "egg/" in prefixes
        _reset_bot_config_caches()

    def test_missing_prefix(self, monkeypatch):
        """Raises ValueError when GATEWAY_BOT_BRANCH_PREFIX not set."""
        _reset_bot_config_caches()
        monkeypatch.delenv("GATEWAY_BOT_BRANCH_PREFIX", raising=False)
        with pytest.raises(ValueError, match="GATEWAY_BOT_BRANCH_PREFIX"):
            get_bot_branch_prefixes()
        _reset_bot_config_caches()

    def test_cached(self, monkeypatch):
        """Second call uses cached value."""
        _reset_bot_config_caches()
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "test")
        p1 = get_bot_branch_prefixes()
        p2 = get_bot_branch_prefixes()
        assert p1 is p2
        _reset_bot_config_caches()


class TestLoadTrustedUsers:
    """Tests for _load_trusted_users function."""

    def test_with_users(self, monkeypatch):
        """Load comma-separated users."""
        monkeypatch.setenv("GATEWAY_TRUSTED_USERS", "alice,bob,charlie")
        users = _load_trusted_users()
        assert users == frozenset({"alice", "bob", "charlie"})

    def test_empty(self, monkeypatch):
        """Empty env returns empty frozenset."""
        monkeypatch.delenv("GATEWAY_TRUSTED_USERS", raising=False)
        users = _load_trusted_users()
        assert users == frozenset()

    def test_whitespace_handling(self, monkeypatch):
        """Whitespace around names is stripped."""
        monkeypatch.setenv("GATEWAY_TRUSTED_USERS", " alice , bob ")
        users = _load_trusted_users()
        assert "alice" in users
        assert "bob" in users

    def test_lowercased(self, monkeypatch):
        """Names are lowercased."""
        monkeypatch.setenv("GATEWAY_TRUSTED_USERS", "Alice,BOB")
        users = _load_trusted_users()
        assert "alice" in users
        assert "bob" in users


class TestPolicyEngine:
    """Tests for PolicyEngine class."""

    def _make_engine(self, monkeypatch):
        """Create a PolicyEngine with test config."""
        monkeypatch.setenv("GATEWAY_BOT_NAME", "test-bot")
        monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "egg")
        _reset_bot_config_caches()
        mock_client = MagicMock()
        return PolicyEngine(github_client=mock_client)

    def test_is_bot_author_string(self, monkeypatch):
        """Bot author check with string."""
        engine = self._make_engine(monkeypatch)
        assert engine._is_bot_author("test-bot") is True
        assert engine._is_bot_author("test-bot[bot]") is True
        assert engine._is_bot_author("other-user") is False
        _reset_bot_config_caches()

    def test_is_bot_author_dict(self, monkeypatch):
        """Bot author check with dict."""
        engine = self._make_engine(monkeypatch)
        assert engine._is_bot_author({"login": "test-bot"}) is True
        assert engine._is_bot_author({"login": "other"}) is False
        _reset_bot_config_caches()

    def test_is_bot_branch(self, monkeypatch):
        """Bot branch detection."""
        engine = self._make_engine(monkeypatch)
        assert engine._is_bot_branch("egg-fix-bug") is True
        assert engine._is_bot_branch("egg/feature") is True
        assert engine._is_bot_branch("main") is False
        assert engine._is_bot_branch("feature/something") is False
        _reset_bot_config_caches()

    def test_is_trusted_author(self, monkeypatch):
        """Trusted author check."""
        monkeypatch.setenv("GATEWAY_TRUSTED_USERS", "trusted-user")
        import policy

        policy.TRUSTED_BRANCH_OWNERS = _load_trusted_users()
        engine = self._make_engine(monkeypatch)
        assert engine._is_trusted_author("trusted-user") is True
        assert engine._is_trusted_author("untrusted") is False
        policy.TRUSTED_BRANCH_OWNERS = frozenset()
        _reset_bot_config_caches()

    def test_is_trusted_author_dict(self, monkeypatch):
        """Trusted author check with dict."""
        monkeypatch.setenv("GATEWAY_TRUSTED_USERS", "alice")
        import policy

        policy.TRUSTED_BRANCH_OWNERS = _load_trusted_users()
        engine = self._make_engine(monkeypatch)
        assert engine._is_trusted_author({"login": "alice"}) is True
        assert engine._is_trusted_author({"login": "bob"}) is False
        policy.TRUSTED_BRANCH_OWNERS = frozenset()
        _reset_bot_config_caches()

    def test_is_configured_user_author(self, monkeypatch):
        """Configured user author check."""
        engine = self._make_engine(monkeypatch)
        assert engine._is_configured_user_author("myuser", "myuser") is True
        assert engine._is_configured_user_author("MyUser", "myuser") is True
        assert engine._is_configured_user_author("other", "myuser") is False
        _reset_bot_config_caches()

    def test_is_configured_user_author_dict(self, monkeypatch):
        """Configured user author check with dict."""
        engine = self._make_engine(monkeypatch)
        assert engine._is_configured_user_author({"login": "myuser"}, "myuser") is True
        assert engine._is_configured_user_author({"login": "other"}, "myuser") is False
        _reset_bot_config_caches()

    def test_get_pr_info_cache_hit(self, monkeypatch):
        """PR info from cache."""
        engine = self._make_engine(monkeypatch)
        cached = CachedPRInfo(
            pr_number=1,
            author="test-bot",
            state="open",
            head_branch="egg-fix",
            fetched_at=datetime.now(UTC).timestamp(),
        )
        engine._pr_cache[("owner/repo", 1)] = cached
        result = engine._get_pr_info("owner/repo", 1)
        assert result is not None
        assert result.pr_number == 1
        assert result.author == "test-bot"
        _reset_bot_config_caches()

    def test_get_pr_info_cache_stale(self, monkeypatch):
        """Stale cache triggers fetch."""
        engine = self._make_engine(monkeypatch)
        stale = CachedPRInfo(
            pr_number=1,
            author="test-bot",
            state="open",
            head_branch="egg-fix",
            fetched_at=datetime.now(UTC).timestamp() - 600,  # 10 min ago
        )
        engine._pr_cache[("owner/repo", 1)] = stale
        engine.github.get_pr_info.return_value = {
            "author": {"login": "test-bot"},
            "state": "open",
            "headRefName": "egg-fix",
        }
        result = engine._get_pr_info("owner/repo", 1)
        assert result is not None
        engine.github.get_pr_info.assert_called_once()
        _reset_bot_config_caches()

    def test_get_pr_info_not_found(self, monkeypatch):
        """PR not found returns None."""
        engine = self._make_engine(monkeypatch)
        engine.github.get_pr_info.return_value = None
        result = engine._get_pr_info("owner/repo", 999)
        assert result is None
        _reset_bot_config_caches()
