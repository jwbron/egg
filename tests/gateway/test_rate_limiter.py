"""Tests for gateway rate_limiter module."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from rate_limiter import (
    RateLimitResult,
    SlidingWindowRateLimiter,
    check_heartbeat_rate_limit,
    check_registration_rate_limit,
    get_all_limiter_stats,
    record_failed_lookup,
)


class TestRateLimitResult:
    """Tests for RateLimitResult dataclass."""

    def test_allowed(self):
        """Allowed result."""
        r = RateLimitResult(allowed=True, remaining=5)
        d = r.to_dict()
        assert d["allowed"] is True
        assert d["remaining"] == 5
        assert "retry_after_seconds" not in d

    def test_denied_with_retry(self):
        """Denied result with retry info."""
        r = RateLimitResult(allowed=False, remaining=0, retry_after_seconds=30)
        d = r.to_dict()
        assert d["allowed"] is False
        assert d["remaining"] == 0
        assert d["retry_after_seconds"] == 30


class TestSlidingWindowRateLimiter:
    """Tests for SlidingWindowRateLimiter class."""

    def test_basic_allow(self):
        """Requests within limit are allowed."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60, name="test")
        result = limiter.is_allowed("key1")
        assert result.allowed is True
        assert result.remaining == 4

    def test_multiple_requests(self):
        """Multiple requests decrement remaining."""
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60, name="test")
        r1 = limiter.is_allowed("key1")
        r2 = limiter.is_allowed("key1")
        r3 = limiter.is_allowed("key1")
        assert r1.remaining == 2
        assert r2.remaining == 1
        assert r3.remaining == 0

    def test_exceed_limit(self):
        """Requests exceeding limit are denied."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60, name="test")
        limiter.is_allowed("key1")
        limiter.is_allowed("key1")
        result = limiter.is_allowed("key1")
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds > 0

    def test_different_keys_independent(self):
        """Different keys have independent limits."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, name="test")
        r1 = limiter.is_allowed("key1")
        r2 = limiter.is_allowed("key2")
        assert r1.allowed is True
        assert r2.allowed is True

    def test_check_only_does_not_consume(self):
        """check_only doesn't consume a request."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, name="test")
        check = limiter.check_only("key1")
        assert check.allowed is True
        assert check.remaining == 1
        # Should still allow an actual request
        result = limiter.is_allowed("key1")
        assert result.allowed is True

    def test_check_only_over_limit(self):
        """check_only shows denied when over limit."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, name="test")
        limiter.is_allowed("key1")
        check = limiter.check_only("key1")
        assert check.allowed is False
        assert check.remaining == 0

    def test_reset_key(self):
        """Reset clears a specific key."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, name="test")
        limiter.is_allowed("key1")
        limiter.reset("key1")
        result = limiter.is_allowed("key1")
        assert result.allowed is True

    def test_reset_nonexistent_key(self):
        """Reset on nonexistent key is safe."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60, name="test")
        limiter.reset("nonexistent")  # Should not raise

    def test_reset_all(self):
        """Reset all clears everything."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, name="test")
        limiter.is_allowed("key1")
        limiter.is_allowed("key2")
        count = limiter.reset_all()
        assert count == 2
        # Should be allowed again
        assert limiter.is_allowed("key1").allowed is True

    def test_get_stats(self):
        """Get statistics."""
        limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60, name="test-stats")
        limiter.is_allowed("key1")
        limiter.is_allowed("key1")
        limiter.is_allowed("key2")
        stats = limiter.get_stats()
        assert stats["name"] == "test-stats"
        assert stats["max_requests"] == 10
        assert stats["window_seconds"] == 60
        assert stats["active_keys"] == 2
        assert stats["total_active_requests"] == 3

    def test_get_stats_empty(self):
        """Stats with no requests."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60, name="empty")
        stats = limiter.get_stats()
        assert stats["active_keys"] == 0
        assert stats["total_active_requests"] == 0


class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""

    def test_check_registration_rate_limit(self):
        """Registration rate limit function works."""
        result = check_registration_rate_limit("10.0.0.1")
        assert result.allowed is True

    def test_record_failed_lookup(self):
        """Failed lookup recording works."""
        result = record_failed_lookup("10.0.0.2")
        assert result.allowed is True

    def test_check_heartbeat_rate_limit(self):
        """Heartbeat rate limit function works."""
        result = check_heartbeat_rate_limit("session-123")
        assert result.allowed is True

    def test_get_all_limiter_stats(self):
        """Get all limiter stats."""
        stats = get_all_limiter_stats()
        assert "registration" in stats
        assert "failed_lookup" in stats
        assert "heartbeat" in stats
        assert stats["registration"]["name"] == "session_registration"
