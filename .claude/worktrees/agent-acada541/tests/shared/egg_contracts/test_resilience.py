"""Tests for egg_contracts.resilience module."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from egg_contracts.resilience import (
    CheckpointState,
    RateLimitHandler,
    RateLimitInfo,
    RetryableError,
    RetryConfig,
    TimeoutCheckpoint,
    calculate_backoff_delay,
    create_timeout_checkpoint,
    parse_rate_limit_headers,
    retry_with_backoff,
    should_checkpoint_now,
)


class TestRateLimitInfo:
    """Tests for RateLimitInfo dataclass."""

    def test_not_limited(self):
        """Test when not rate limited."""
        info = RateLimitInfo(
            limit=5000,
            remaining=4500,
            reset_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert info.is_limited is False
        assert info.seconds_until_reset > 0

    def test_is_limited(self):
        """Test when rate limited."""
        info = RateLimitInfo(
            limit=5000,
            remaining=0,
            reset_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        assert info.is_limited is True

    def test_no_remaining(self):
        """Test when remaining is None."""
        info = RateLimitInfo(limit=5000)
        assert info.is_limited is False

    def test_reset_passed(self):
        """Test when reset time has passed."""
        info = RateLimitInfo(
            limit=5000,
            remaining=0,
            reset_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert info.seconds_until_reset == 0

    def test_to_dict(self):
        """Test conversion to dictionary."""
        info = RateLimitInfo(
            limit=5000,
            remaining=4500,
            reset_at=datetime.now(UTC) + timedelta(hours=1),
            used=500,
        )
        data = info.to_dict()
        assert data["limit"] == 5000
        assert data["remaining"] == 4500
        assert data["used"] == 500
        assert data["is_limited"] is False


class TestRateLimitHandler:
    """Tests for RateLimitHandler."""

    def test_parse_github_headers(self):
        """Test parsing GitHub rate limit headers."""
        reset_time = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4500",
            "X-RateLimit-Reset": str(reset_time),
            "X-RateLimit-Used": "500",
        }
        handler = RateLimitHandler(headers)
        info = handler.parse()

        assert info.limit == 5000
        assert info.remaining == 4500
        assert info.used == 500
        assert info.reset_at is not None

    def test_parse_lowercase_headers(self):
        """Test parsing lowercase headers."""
        headers = {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "500",
        }
        handler = RateLimitHandler(headers)
        info = handler.parse()

        assert info.limit == 1000
        assert info.remaining == 500

    def test_parse_retry_after(self):
        """Test parsing Retry-After header."""
        headers = {
            "Retry-After": "60",
            "X-RateLimit-Remaining": "0",
        }
        handler = RateLimitHandler(headers)
        info = handler.parse()

        assert info.remaining == 0
        assert info.reset_at is not None
        # Reset should be roughly 60 seconds from now
        assert 55 <= info.seconds_until_reset <= 65

    def test_should_wait(self):
        """Test should_wait method."""
        headers = {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())),
        }
        handler = RateLimitHandler(headers)
        assert handler.should_wait() is True

    def test_should_not_wait(self):
        """Test when no waiting is needed."""
        headers = {
            "X-RateLimit-Remaining": "100",
        }
        handler = RateLimitHandler(headers)
        assert handler.should_wait() is False

    def test_empty_headers(self):
        """Test with empty headers."""
        handler = RateLimitHandler({})
        info = handler.parse()

        assert info.limit is None
        assert info.remaining is None


class TestParseRateLimitHeaders:
    """Tests for parse_rate_limit_headers convenience function."""

    def test_convenience_function(self):
        """Test that convenience function works."""
        headers = {"X-RateLimit-Limit": "5000"}
        info = parse_rate_limit_headers(headers)
        assert info.limit == 5000


class TestRetryConfig:
    """Tests for RetryConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.initial_delay_seconds == 1.0
        assert config.max_delay_seconds == 30.0
        assert config.exponential_base == 2.0
        assert config.jitter is True


class TestCalculateBackoffDelay:
    """Tests for calculate_backoff_delay."""

    def test_exponential_growth(self):
        """Test that delay grows exponentially."""
        config = RetryConfig(jitter=False)

        delay0 = calculate_backoff_delay(0, config)
        delay1 = calculate_backoff_delay(1, config)
        delay2 = calculate_backoff_delay(2, config)

        assert delay0 == 1.0
        assert delay1 == 2.0
        assert delay2 == 4.0

    def test_max_delay_cap(self):
        """Test that delay is capped at max."""
        config = RetryConfig(max_delay_seconds=10.0, jitter=False)

        delay = calculate_backoff_delay(10, config)  # Would be 1024 without cap
        assert delay == 10.0

    def test_jitter_varies_delay(self):
        """Test that jitter adds variance."""
        config = RetryConfig(jitter=True)

        delays = [calculate_backoff_delay(2, config) for _ in range(10)]

        # With jitter, delays should vary
        assert len(set(delays)) > 1  # Not all the same


class TestRetryWithBackoff:
    """Tests for retry_with_backoff decorator."""

    def test_no_retry_on_success(self):
        """Test that successful calls don't retry."""
        call_count = 0

        @retry_with_backoff()
        def succeeds():
            nonlocal call_count
            call_count += 1
            return "success"

        result = succeeds()
        assert result == "success"
        assert call_count == 1

    def test_retries_on_retryable_error(self):
        """Test that RetryableError triggers retry."""
        call_count = 0

        @retry_with_backoff(RetryConfig(max_retries=2, initial_delay_seconds=0.01))
        def fails_then_succeeds():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableError("Temporary failure")
            return "success"

        result = fails_then_succeeds()
        assert result == "success"
        assert call_count == 3

    def test_exhausts_retries(self):
        """Test that retries are exhausted."""
        call_count = 0

        @retry_with_backoff(RetryConfig(max_retries=2, initial_delay_seconds=0.01))
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise RetryableError("Always fails")

        with pytest.raises(RetryableError):
            always_fails()

        assert call_count == 3  # Initial + 2 retries

    def test_uses_retry_after(self):
        """Test that retry_after from error is respected."""

        @retry_with_backoff(RetryConfig(max_retries=1, initial_delay_seconds=0.01))
        def fails():
            raise RetryableError("Rate limited", retry_after=1)

        start = time.time()
        with pytest.raises(RetryableError):
            fails()
        elapsed = time.time() - start

        # Should have waited at least 1 second
        assert elapsed >= 0.9


class TestCheckpointState:
    """Tests for CheckpointState dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        state = CheckpointState(
            timestamp=datetime.now(UTC),
            job_start_time=datetime.now(UTC) - timedelta(hours=5),
            elapsed_seconds=18000,
            remaining_seconds=3600,
            data={"current_task": "task-1"},
        )
        data = state.to_dict()

        assert data["elapsed_seconds"] == 18000
        assert data["remaining_seconds"] == 3600
        assert data["data"]["current_task"] == "task-1"


class TestTimeoutCheckpoint:
    """Tests for TimeoutCheckpoint."""

    def test_default_values(self):
        """Test default configuration."""
        checkpoint = TimeoutCheckpoint()
        assert checkpoint.timeout_minutes == 360
        assert checkpoint.checkpoint_margin_minutes == 10

    def test_deadline_calculation(self):
        """Test deadline is calculated correctly."""
        start = datetime.now(UTC)
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            job_start_time=start,
        )

        expected_deadline = start + timedelta(minutes=60)
        # Allow 1 second variance
        assert abs((checkpoint.deadline - expected_deadline).total_seconds()) < 1

    def test_checkpoint_time(self):
        """Test checkpoint time is calculated correctly."""
        start = datetime.now(UTC)
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            checkpoint_margin_minutes=10,
            job_start_time=start,
        )

        expected = start + timedelta(minutes=50)  # 60 - 10 = 50
        assert abs((checkpoint.checkpoint_time - expected).total_seconds()) < 1

    def test_elapsed_seconds(self):
        """Test elapsed time calculation."""
        start = datetime.now(UTC) - timedelta(minutes=30)
        checkpoint = TimeoutCheckpoint(job_start_time=start)

        elapsed = checkpoint.elapsed_seconds
        assert 1795 <= elapsed <= 1810  # About 30 minutes

    def test_remaining_seconds(self):
        """Test remaining time calculation."""
        start = datetime.now(UTC)
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            job_start_time=start,
        )

        remaining = checkpoint.remaining_seconds
        assert 3595 <= remaining <= 3605  # About 60 minutes

    def test_should_checkpoint_false(self):
        """Test should_checkpoint when plenty of time remains."""
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            checkpoint_margin_minutes=10,
            job_start_time=datetime.now(UTC),
        )
        assert checkpoint.should_checkpoint is False

    def test_should_checkpoint_true(self):
        """Test should_checkpoint when near timeout."""
        # Start 55 minutes ago with 60 minute timeout and 10 minute margin
        start = datetime.now(UTC) - timedelta(minutes=55)
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            checkpoint_margin_minutes=10,
            job_start_time=start,
        )
        assert checkpoint.should_checkpoint is True

    def test_create_checkpoint(self):
        """Test creating a checkpoint."""
        checkpoint = TimeoutCheckpoint()
        state = checkpoint.create_checkpoint({"task": "current"})

        assert state.data == {"task": "current"}
        assert state.timestamp is not None

    def test_get_latest_checkpoint(self):
        """Test getting latest checkpoint."""
        checkpoint = TimeoutCheckpoint()

        assert checkpoint.get_latest_checkpoint() is None

        checkpoint.create_checkpoint({"first": True})
        checkpoint.create_checkpoint({"second": True})

        latest = checkpoint.get_latest_checkpoint()
        assert latest is not None
        assert latest.data == {"second": True}

    def test_format_status(self):
        """Test status formatting."""
        checkpoint = TimeoutCheckpoint(
            timeout_minutes=60,
            checkpoint_margin_minutes=10,
        )
        status = checkpoint.format_status()

        assert "60m" in status or "360m" in status
        assert "elapsed" in status
        assert "remaining" in status


class TestCreateTimeoutCheckpoint:
    """Tests for create_timeout_checkpoint convenience function."""

    def test_creates_checkpoint(self):
        """Test convenience function."""
        checkpoint = create_timeout_checkpoint(
            timeout_minutes=120,
            margin_minutes=15,
        )
        assert checkpoint.timeout_minutes == 120
        assert checkpoint.checkpoint_margin_minutes == 15


class TestShouldCheckpointNow:
    """Tests for should_checkpoint_now convenience function."""

    def test_not_near_timeout(self):
        """Test when not near timeout."""
        result = should_checkpoint_now(
            job_start_time=datetime.now(UTC),
            timeout_minutes=60,
            margin_minutes=10,
        )
        assert result is False

    def test_near_timeout(self):
        """Test when near timeout."""
        start = datetime.now(UTC) - timedelta(minutes=55)
        result = should_checkpoint_now(
            job_start_time=start,
            timeout_minutes=60,
            margin_minutes=10,
        )
        assert result is True
