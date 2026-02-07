"""
Resilience utilities for SDLC pipeline external failure handling.

Provides:
- RateLimitHandler: Parse X-RateLimit-* headers and manage sleep/retry
- RetryWithBackoff: Exponential backoff with configurable parameters
- TimeoutCheckpoint: Monitor job time and checkpoint state before timeout

These utilities help the pipeline gracefully handle transient failures
and external service limitations.
"""

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

T = TypeVar("T")


# ============================================================
# Rate Limit Handling
# ============================================================


@dataclass
class RateLimitInfo:
    """Information about current rate limit status."""

    limit: int | None = None  # Maximum requests allowed
    remaining: int | None = None  # Requests remaining in window
    reset_at: datetime | None = None  # When the rate limit resets
    used: int | None = None  # Requests used in current window

    @property
    def is_limited(self) -> bool:
        """Check if currently rate limited."""
        return self.remaining is not None and self.remaining <= 0

    @property
    def seconds_until_reset(self) -> int:
        """Get seconds until rate limit resets."""
        if self.reset_at is None:
            return 0
        now = datetime.now(UTC)
        if now >= self.reset_at:
            return 0
        return int((self.reset_at - now).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "used": self.used,
            "is_limited": self.is_limited,
            "seconds_until_reset": self.seconds_until_reset,
        }


class RateLimitHandler:
    """
    Handle rate limiting from external APIs.

    Parses standard rate limit headers and provides retry logic.

    Supported header formats:
    - GitHub: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, X-RateLimit-Used
    - Generic: RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset
    - Retry-After header for 429 responses
    """

    # Header name mappings (case-insensitive)
    HEADER_MAPPINGS = {
        "limit": ["x-ratelimit-limit", "ratelimit-limit"],
        "remaining": ["x-ratelimit-remaining", "ratelimit-remaining"],
        "reset": ["x-ratelimit-reset", "ratelimit-reset"],
        "used": ["x-ratelimit-used"],
        "retry_after": ["retry-after"],
    }

    def __init__(self, headers: dict[str, str] | None = None):
        """
        Initialize with optional headers to parse.

        Args:
            headers: Response headers dictionary
        """
        self._headers = self._normalize_headers(headers or {})
        self._info: RateLimitInfo | None = None

    @staticmethod
    def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
        """Normalize header names to lowercase."""
        return {k.lower(): v for k, v in headers.items()}

    def _get_header(self, field: str) -> str | None:
        """Get a header value by field name."""
        for header_name in self.HEADER_MAPPINGS.get(field, []):
            if header_name in self._headers:
                return self._headers[header_name]
        return None

    def parse(self) -> RateLimitInfo:
        """
        Parse rate limit information from headers.

        Returns:
            RateLimitInfo with parsed values
        """
        if self._info is not None:
            return self._info

        # Parse limit
        limit_str = self._get_header("limit")
        limit = int(limit_str) if limit_str and limit_str.isdigit() else None

        # Parse remaining
        remaining_str = self._get_header("remaining")
        remaining = int(remaining_str) if remaining_str and remaining_str.isdigit() else None

        # Parse reset time (Unix timestamp)
        reset_str = self._get_header("reset")
        reset_at = None
        if reset_str and reset_str.isdigit():
            reset_at = datetime.fromtimestamp(int(reset_str), tz=UTC)

        # Parse used
        used_str = self._get_header("used")
        used = int(used_str) if used_str and used_str.isdigit() else None

        # Check for Retry-After header (takes precedence for reset time)
        retry_after = self._get_header("retry_after")
        if retry_after:
            try:
                # Retry-After can be seconds or HTTP-date
                seconds = int(retry_after)
                reset_at = datetime.now(UTC) + timedelta(seconds=seconds)
            except ValueError:
                pass  # Ignore invalid retry-after

        self._info = RateLimitInfo(
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            used=used,
        )
        return self._info

    def should_wait(self) -> bool:
        """Check if we should wait before making another request."""
        info = self.parse()
        return info.is_limited

    def wait_until_reset(self, max_wait_seconds: int = 300) -> bool:
        """
        Sleep until rate limit resets.

        Args:
            max_wait_seconds: Maximum time to wait

        Returns:
            True if we waited, False if no wait needed or exceeded max
        """
        info = self.parse()
        if not info.is_limited:
            return False

        wait_time = min(info.seconds_until_reset, max_wait_seconds)
        if wait_time <= 0:
            return False

        time.sleep(wait_time)
        return True


def parse_rate_limit_headers(headers: dict[str, str]) -> RateLimitInfo:
    """
    Convenience function to parse rate limit headers.

    Args:
        headers: Response headers dictionary

    Returns:
        RateLimitInfo with parsed values
    """
    return RateLimitHandler(headers).parse()


# ============================================================
# Retry with Backoff
# ============================================================


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True  # Add randomness to prevent thundering herd


class RetryableError(Exception):
    """Exception that indicates an operation should be retried."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def calculate_backoff_delay(
    attempt: int,
    config: RetryConfig | None = None,
) -> float:
    """
    Calculate delay for a given retry attempt.

    Args:
        attempt: Current attempt number (0-indexed)
        config: Retry configuration

    Returns:
        Delay in seconds
    """
    if config is None:
        config = RetryConfig()

    # Calculate exponential delay
    delay = config.initial_delay_seconds * (config.exponential_base**attempt)

    # Cap at max delay
    delay = min(delay, config.max_delay_seconds)

    # Add jitter if enabled (±25%)
    if config.jitter:
        import random

        jitter_factor = 0.75 + (random.random() * 0.5)
        delay *= jitter_factor

    return delay


def retry_with_backoff(
    config: RetryConfig | None = None,
    retryable_exceptions: tuple[type[Exception], ...] = (RetryableError,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for retrying operations with exponential backoff.

    Args:
        config: Retry configuration
        retryable_exceptions: Exception types that trigger retry

    Returns:
        Decorator function
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None

            for attempt in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt >= config.max_retries:
                        break

                    # Check for explicit retry-after
                    if isinstance(e, RetryableError) and e.retry_after:
                        delay = min(e.retry_after, config.max_delay_seconds)
                    else:
                        delay = calculate_backoff_delay(attempt, config)

                    time.sleep(delay)

            # Exhausted retries
            if last_exception:
                raise last_exception
            raise RuntimeError("Retry loop completed without result or exception")

        return wrapper

    return decorator


# ============================================================
# Timeout Checkpoint
# ============================================================


@dataclass
class CheckpointState:
    """State saved at a checkpoint."""

    timestamp: datetime
    job_start_time: datetime
    elapsed_seconds: int
    remaining_seconds: int
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "job_start_time": self.job_start_time.isoformat(),
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "data": self.data,
        }


class TimeoutCheckpoint:
    """
    Monitor job execution time and create checkpoints before timeout.

    Used in GitHub Actions to save state before the job times out,
    allowing graceful continuation on the next run.

    Default behavior:
    - Monitor against a 6-hour (360 minute) timeout
    - Create checkpoint when 10 minutes remain
    """

    DEFAULT_TIMEOUT_MINUTES = 360  # 6 hours
    DEFAULT_CHECKPOINT_MARGIN_MINUTES = 10  # Create checkpoint with this much time left

    def __init__(
        self,
        timeout_minutes: int | None = None,
        checkpoint_margin_minutes: int | None = None,
        job_start_time: datetime | None = None,
    ):
        """
        Initialize timeout checkpoint monitor.

        Args:
            timeout_minutes: Total job timeout
            checkpoint_margin_minutes: When to trigger checkpoint (before timeout)
            job_start_time: When the job started (defaults to now)
        """
        self.timeout_minutes = timeout_minutes or self.DEFAULT_TIMEOUT_MINUTES
        self.checkpoint_margin_minutes = (
            checkpoint_margin_minutes or self.DEFAULT_CHECKPOINT_MARGIN_MINUTES
        )
        self.job_start_time = job_start_time or datetime.now(UTC)
        self._checkpoints: list[CheckpointState] = []

    @property
    def deadline(self) -> datetime:
        """Get the job deadline."""
        return self.job_start_time + timedelta(minutes=self.timeout_minutes)

    @property
    def checkpoint_time(self) -> datetime:
        """Get the time when checkpoint should be created."""
        return self.deadline - timedelta(minutes=self.checkpoint_margin_minutes)

    @property
    def elapsed_seconds(self) -> int:
        """Get seconds elapsed since job start."""
        return int((datetime.now(UTC) - self.job_start_time).total_seconds())

    @property
    def remaining_seconds(self) -> int:
        """Get seconds remaining until timeout."""
        return int((self.deadline - datetime.now(UTC)).total_seconds())

    @property
    def should_checkpoint(self) -> bool:
        """Check if we should create a checkpoint now."""
        return datetime.now(UTC) >= self.checkpoint_time

    @property
    def is_near_timeout(self) -> bool:
        """Check if we're close to timeout (within margin)."""
        return self.should_checkpoint

    def check_time(self) -> tuple[bool, int]:
        """
        Check current time status.

        Returns:
            Tuple of (should_checkpoint, seconds_remaining)
        """
        return self.should_checkpoint, self.remaining_seconds

    def create_checkpoint(self, data: dict[str, Any]) -> CheckpointState:
        """
        Create a checkpoint with the given data.

        Args:
            data: State data to save

        Returns:
            CheckpointState with full context
        """
        state = CheckpointState(
            timestamp=datetime.now(UTC),
            job_start_time=self.job_start_time,
            elapsed_seconds=self.elapsed_seconds,
            remaining_seconds=self.remaining_seconds,
            data=data,
        )
        self._checkpoints.append(state)
        return state

    def get_latest_checkpoint(self) -> CheckpointState | None:
        """Get the most recent checkpoint."""
        return self._checkpoints[-1] if self._checkpoints else None

    def format_status(self) -> str:
        """Format current status as human-readable string."""
        elapsed_min = self.elapsed_seconds // 60
        remaining_min = self.remaining_seconds // 60
        return (
            f"Job time: {elapsed_min}m elapsed, {remaining_min}m remaining "
            f"(timeout: {self.timeout_minutes}m, checkpoint margin: {self.checkpoint_margin_minutes}m)"
        )


def create_timeout_checkpoint(
    timeout_minutes: int = TimeoutCheckpoint.DEFAULT_TIMEOUT_MINUTES,
    margin_minutes: int = TimeoutCheckpoint.DEFAULT_CHECKPOINT_MARGIN_MINUTES,
) -> TimeoutCheckpoint:
    """
    Create a new timeout checkpoint monitor.

    Args:
        timeout_minutes: Total job timeout
        margin_minutes: When to trigger checkpoint

    Returns:
        TimeoutCheckpoint instance
    """
    return TimeoutCheckpoint(
        timeout_minutes=timeout_minutes,
        checkpoint_margin_minutes=margin_minutes,
    )


def should_checkpoint_now(
    job_start_time: datetime,
    timeout_minutes: int = TimeoutCheckpoint.DEFAULT_TIMEOUT_MINUTES,
    margin_minutes: int = TimeoutCheckpoint.DEFAULT_CHECKPOINT_MARGIN_MINUTES,
) -> bool:
    """
    Quick check if we should create a checkpoint.

    Args:
        job_start_time: When the job started
        timeout_minutes: Total job timeout
        margin_minutes: Checkpoint margin

    Returns:
        True if checkpoint should be created
    """
    checkpoint = TimeoutCheckpoint(
        timeout_minutes=timeout_minutes,
        checkpoint_margin_minutes=margin_minutes,
        job_start_time=job_start_time,
    )
    return checkpoint.should_checkpoint
