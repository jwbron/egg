"""
Rate Limiter - Thread-safe sliding window rate limiting.

Provides rate limiting infrastructure for the gateway sidecar to protect against:
- Session enumeration attacks (brute force guessing session tokens)
- DoS attacks on session registration and other endpoints
- Resource exhaustion from excessive requests

Design decisions:
- In-memory rate limiting (NOT persisted) - gateway restart clears limits
- Thread-safe with fine-grained locking
- Sliding window algorithm for accurate rate tracking
- Separate limiters for different operations
"""

import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shared.egg_logging import get_logger

logger = get_logger("gateway.rate-limiter")


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    remaining: int
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        result: dict = {
            "allowed": self.allowed,
            "remaining": self.remaining,
        }
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result


class SlidingWindowRateLimiter:
    """
    Thread-safe sliding window rate limiter.

    Uses a sliding window algorithm where each request is timestamped.
    Old requests outside the window are pruned on each check.
    """

    def __init__(self, max_requests: int, window_seconds: int, name: str = "default"):
        """Initialize the rate limiter."""
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self.name = name

        # requests: key -> list of timestamps
        self._requests: dict[str, list[datetime]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> RateLimitResult:
        """Check if a request is allowed for the given key.

        If allowed, records the request. If not, returns retry info.
        """
        now = datetime.now(UTC)
        cutoff = now - self.window

        with self._lock:
            # Prune old entries
            self._requests[key] = [t for t in self._requests[key] if t > cutoff]

            current_count = len(self._requests[key])
            remaining = self.max_requests - current_count

            if current_count >= self.max_requests:
                # Calculate retry after (time until oldest request expires)
                if self._requests[key]:
                    oldest = min(self._requests[key])
                    retry_after = int((oldest + self.window - now).total_seconds()) + 1
                else:
                    retry_after = int(self.window.total_seconds())

                logger.warning(
                    "Rate limit exceeded",
                    limiter=self.name,
                    key=key,
                    max_requests=self.max_requests,
                    window_seconds=int(self.window.total_seconds()),
                )

                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(1, retry_after),
                )

            # Record this request
            self._requests[key].append(now)

            return RateLimitResult(
                allowed=True,
                remaining=remaining - 1,  # -1 because we just used one
            )

    def check_only(self, key: str) -> RateLimitResult:
        """Check rate limit without recording a request.

        Useful for checking status before performing expensive operations.
        """
        now = datetime.now(UTC)
        cutoff = now - self.window

        with self._lock:
            # Prune old entries (but don't save)
            current = [t for t in self._requests[key] if t > cutoff]
            current_count = len(current)
            remaining = self.max_requests - current_count

            if current_count >= self.max_requests:
                if current:
                    oldest = min(current)
                    retry_after = int((oldest + self.window - now).total_seconds()) + 1
                else:
                    retry_after = int(self.window.total_seconds())

                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(1, retry_after),
                )

            return RateLimitResult(
                allowed=True,
                remaining=remaining,
            )

    def reset(self, key: str) -> None:
        """Reset rate limit for a specific key."""
        with self._lock:
            if key in self._requests:
                del self._requests[key]

    def reset_all(self) -> int:
        """Reset all rate limits."""
        with self._lock:
            count = len(self._requests)
            self._requests.clear()
            return count

    def get_stats(self) -> dict:
        """Get statistics about rate limiter state."""
        now = datetime.now(UTC)
        cutoff = now - self.window

        with self._lock:
            active_keys = 0
            total_requests = 0

            for _key, timestamps in self._requests.items():
                # Count only non-expired requests
                active = [t for t in timestamps if t > cutoff]
                if active:
                    active_keys += 1
                    total_requests += len(active)

            return {
                "name": self.name,
                "max_requests": self.max_requests,
                "window_seconds": int(self.window.total_seconds()),
                "active_keys": active_keys,
                "total_active_requests": total_requests,
            }


# Pre-configured rate limiters for different operations
# These are module-level singletons created on first import

# Session registration: 10 registrations per minute per source IP
# Prevents bulk session creation attacks
registration_limiter = SlidingWindowRateLimiter(
    max_requests=10,
    window_seconds=60,
    name="session_registration",
)

# Failed session lookups: 10 failures per minute per source IP
# Prevents session enumeration/brute force attacks
failed_lookup_limiter = SlidingWindowRateLimiter(
    max_requests=10,
    window_seconds=60,
    name="failed_session_lookup",
)

# Explicit heartbeat endpoint: 100 per hour per session
# Prevents DoS on the dedicated heartbeat endpoint
# (Note: implicit heartbeats via request handling are not rate limited)
heartbeat_limiter = SlidingWindowRateLimiter(
    max_requests=100,
    window_seconds=3600,
    name="session_heartbeat",
)


def check_registration_rate_limit(source_ip: str) -> RateLimitResult:
    """Check rate limit for session registration."""
    return registration_limiter.is_allowed(source_ip)


def record_failed_lookup(source_ip: str) -> RateLimitResult:
    """Record a failed session lookup and check rate limit.

    Called when an invalid session token is presented.
    """
    return failed_lookup_limiter.is_allowed(source_ip)


def check_heartbeat_rate_limit(session_id: str) -> RateLimitResult:
    """Check rate limit for explicit heartbeat requests."""
    return heartbeat_limiter.is_allowed(session_id)


def get_all_limiter_stats() -> dict:
    """Get statistics for all rate limiters."""
    return {
        "registration": registration_limiter.get_stats(),
        "failed_lookup": failed_lookup_limiter.get_stats(),
        "heartbeat": heartbeat_limiter.get_stats(),
    }
