"""
Tests for rate_limiter module.

Tests the sliding window rate limiting implementation.
"""

import threading
import time

import pytest

# Import from conftest-loaded module
from rate_limiter import (
    RateLimitResult,
    SlidingWindowRateLimiter,
    check_heartbeat_rate_limit,
    check_registration_rate_limit,
    failed_lookup_limiter,
    get_all_limiter_stats,
    heartbeat_limiter,
    record_failed_lookup,
    registration_limiter,
)


class TestRateLimitResult:
    """Tests for RateLimitResult dataclass."""

    def test_allowed_result(self):
        """Test allowed rate limit result."""
        result = RateLimitResult(allowed=True, remaining=5)
        assert result.allowed is True
        assert result.remaining == 5
        assert result.retry_after_seconds is None

    def test_denied_result(self):
        """Test denied rate limit result."""
        result = RateLimitResult(allowed=False, remaining=0, retry_after_seconds=30)
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds == 30

    def test_to_dict_allowed(self):
        """Test serialization of allowed result."""
        result = RateLimitResult(allowed=True, remaining=5)
        d = result.to_dict()
        assert d["allowed"] is True
        assert d["remaining"] == 5
        assert "retry_after_seconds" not in d

    def test_to_dict_denied(self):
        """Test serialization of denied result."""
        result = RateLimitResult(allowed=False, remaining=0, retry_after_seconds=30)
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["remaining"] == 0
        assert d["retry_after_seconds"] == 30


class TestSlidingWindowRateLimiter:
    """Tests for SlidingWindowRateLimiter class."""

    @pytest.fixture
    def limiter(self):
        """Create a rate limiter for testing."""
        return SlidingWindowRateLimiter(
            max_requests=5,
            window_seconds=60,
            name="test_limiter",
        )

    def test_allows_under_limit(self, limiter):
        """Test that requests under limit are allowed."""
        for i in range(5):
            result = limiter.is_allowed("test-key")
            assert result.allowed is True
            assert result.remaining == 5 - i - 1

    def test_denies_over_limit(self, limiter):
        """Test that requests over limit are denied."""
        # Use up the limit
        for _ in range(5):
            limiter.is_allowed("test-key")

        # Next request should be denied
        result = limiter.is_allowed("test-key")
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds > 0

    def test_separate_keys(self, limiter):
        """Test that different keys have separate limits."""
        # Use up limit for key1
        for _ in range(5):
            limiter.is_allowed("key1")

        # key1 should be denied
        result = limiter.is_allowed("key1")
        assert result.allowed is False

        # key2 should still be allowed
        result = limiter.is_allowed("key2")
        assert result.allowed is True

    def test_check_only_doesnt_count(self, limiter):
        """Test that check_only doesn't consume a request."""
        # Check without consuming
        result = limiter.check_only("test-key")
        assert result.allowed is True
        assert result.remaining == 5

        # Still should have 5 remaining
        result = limiter.check_only("test-key")
        assert result.remaining == 5

    def test_sliding_window_expiry(self):
        """Test that old requests expire from the window."""
        limiter = SlidingWindowRateLimiter(
            max_requests=3,
            window_seconds=1,  # 1 second window for fast testing
            name="short_window",
        )

        # Use up the limit
        for _ in range(3):
            limiter.is_allowed("test-key")

        # Should be denied
        result = limiter.is_allowed("test-key")
        assert result.allowed is False

        # Wait for window to expire
        time.sleep(1.1)

        # Should be allowed again
        result = limiter.is_allowed("test-key")
        assert result.allowed is True

    def test_reset_key(self, limiter):
        """Test resetting a specific key."""
        # Use up the limit
        for _ in range(5):
            limiter.is_allowed("test-key")

        # Should be denied
        result = limiter.is_allowed("test-key")
        assert result.allowed is False

        # Reset the key
        limiter.reset("test-key")

        # Should be allowed again
        result = limiter.is_allowed("test-key")
        assert result.allowed is True

    def test_reset_all(self, limiter):
        """Test resetting all keys."""
        # Use up limits for multiple keys
        for key in ["key1", "key2", "key3"]:
            for _ in range(5):
                limiter.is_allowed(key)

        # All should be denied
        for key in ["key1", "key2", "key3"]:
            result = limiter.is_allowed(key)
            assert result.allowed is False

        # Reset all
        count = limiter.reset_all()
        assert count == 3

        # All should be allowed again
        for key in ["key1", "key2", "key3"]:
            result = limiter.is_allowed(key)
            assert result.allowed is True

    def test_get_stats(self, limiter):
        """Test getting limiter statistics."""
        # Make some requests
        for _ in range(3):
            limiter.is_allowed("key1")
        for _ in range(2):
            limiter.is_allowed("key2")

        stats = limiter.get_stats()
        assert stats["name"] == "test_limiter"
        assert stats["max_requests"] == 5
        assert stats["window_seconds"] == 60
        assert stats["active_keys"] == 2
        assert stats["total_active_requests"] == 5


class TestSlidingWindowRateLimiterThreadSafety:
    """Tests for thread safety."""

    def test_concurrent_requests(self):
        """Test concurrent requests don't exceed limit."""
        limiter = SlidingWindowRateLimiter(
            max_requests=100,
            window_seconds=60,
            name="concurrent_test",
        )
        allowed_count = []
        errors = []

        def make_request():
            try:
                result = limiter.is_allowed("shared-key")
                allowed_count.append(result.allowed)
            except Exception as e:
                errors.append(e)

        # Make 200 concurrent requests
        threads = [threading.Thread(target=make_request) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Exactly 100 should be allowed
        assert sum(allowed_count) == 100


class TestPreConfiguredLimiters:
    """Tests for pre-configured rate limiters."""

    def test_registration_limiter_exists(self):
        """Test registration limiter is configured."""
        stats = registration_limiter.get_stats()
        assert stats["name"] == "session_registration"
        assert stats["max_requests"] == 10
        assert stats["window_seconds"] == 60

    def test_failed_lookup_limiter_exists(self):
        """Test failed lookup limiter is configured."""
        stats = failed_lookup_limiter.get_stats()
        assert stats["name"] == "failed_session_lookup"
        assert stats["max_requests"] == 10
        assert stats["window_seconds"] == 60

    def test_heartbeat_limiter_exists(self):
        """Test heartbeat limiter is configured."""
        stats = heartbeat_limiter.get_stats()
        assert stats["name"] == "session_heartbeat"
        assert stats["max_requests"] == 100
        assert stats["window_seconds"] == 3600


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_check_registration_rate_limit(self):
        """Test registration rate limit check."""
        # Reset to ensure clean state
        registration_limiter.reset("test-ip-1")
        result = check_registration_rate_limit("test-ip-1")
        assert result.allowed is True

    def test_record_failed_lookup(self):
        """Test failed lookup recording."""
        # Reset to ensure clean state
        failed_lookup_limiter.reset("test-ip-2")
        result = record_failed_lookup("test-ip-2")
        assert result.allowed is True

    def test_check_heartbeat_rate_limit(self):
        """Test heartbeat rate limit check."""
        # Reset to ensure clean state
        heartbeat_limiter.reset("test-session-1")
        result = check_heartbeat_rate_limit("test-session-1")
        assert result.allowed is True

    def test_get_all_limiter_stats(self):
        """Test getting all limiter stats."""
        stats = get_all_limiter_stats()
        assert "registration" in stats
        assert "failed_lookup" in stats
        assert "heartbeat" in stats
