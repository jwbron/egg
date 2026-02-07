"""
Rate Limiter - Thread-safe sliding window rate limiting.

Provides rate limiting infrastructure for the gateway sidecar to protect against:
- Session enumeration attacks (brute force guessing session tokens)
- Resource exhaustion from excessive heartbeat requests

Design decisions:
- In-memory rate limiting (NOT persisted) - gateway restart clears limits
- Thread-safe with fine-grained locking
- Sliding window algorithm for accurate rate tracking
- Separate limiters for different operations
"""

import sys
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.rate-limiter")


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    remaining: int
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
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
        """
        Initialize the rate limiter.

        Args:
            max_requests: Maximum number of requests allowed in the window
            window_seconds: Size of the sliding window in seconds
            name: Name for logging purposes
        """
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self.name = name

        # requests: key -> list of timestamps
        self._requests: dict[str, list[datetime]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> RateLimitResult:
        """
        Check if a request is allowed for the given key.

        If allowed, records the request. If not, returns retry info.

        Args:
            key: The key to rate limit on (e.g., IP address, session ID)

        Returns:
            RateLimitResult with allowed status and remaining count
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
        """
        Check rate limit without recording a request.

        Useful for checking status before performing expensive operations.

        Args:
            key: The key to check

        Returns:
            RateLimitResult (read-only check)
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
        """
        Reset rate limit for a specific key.

        Args:
            key: The key to reset
        """
        with self._lock:
            if key in self._requests:
                del self._requests[key]

    def reset_all(self) -> int:
        """
        Reset all rate limits.

        Returns:
            Number of keys that were reset
        """
        with self._lock:
            count = len(self._requests)
            self._requests.clear()
            return count

    def get_stats(self) -> dict[str, Any]:
        """
        Get statistics about rate limiter state.

        Returns:
            Dictionary with stats
        """
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


def record_failed_lookup(source_ip: str) -> RateLimitResult:
    """
    Record a failed session lookup and check rate limit.

    Called when an invalid session token is presented.

    Args:
        source_ip: The source IP address

    Returns:
        RateLimitResult (for future requests)
    """
    return failed_lookup_limiter.is_allowed(source_ip)


def check_heartbeat_rate_limit(session_id: str) -> RateLimitResult:
    """
    Check rate limit for explicit heartbeat requests.

    Args:
        session_id: The session ID (or token hash prefix)

    Returns:
        RateLimitResult
    """
    return heartbeat_limiter.is_allowed(session_id)


def get_all_limiter_stats() -> dict[str, Any]:
    """
    Get statistics for all rate limiters.

    Returns:
        Dictionary with stats for each limiter
    """
    return {
        "failed_lookup": failed_lookup_limiter.get_stats(),
        "heartbeat": heartbeat_limiter.get_stats(),
        "github_api": github_api_rate_tracker.get_status(),
    }


# =============================================================================
# GitHub API Rate Limit Tracking
# =============================================================================


@dataclass
class GitHubRateLimitInfo:
    """Information about GitHub API rate limit status."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    used: int | None = None
    resource: str = "core"  # core, search, graphql, etc.
    last_updated: datetime | None = None

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
        """Convert to dictionary for API response."""
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "used": self.used,
            "resource": self.resource,
            "is_limited": self.is_limited,
            "seconds_until_reset": self.seconds_until_reset,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


class GitHubAPIRateTracker:
    """
    Track GitHub API rate limits across requests.

    Parses X-RateLimit-* headers from GitHub API responses and tracks
    the current rate limit status for observability and smart retry logic.
    """

    def __init__(self) -> None:
        """Initialize the rate tracker."""
        self._lock = threading.Lock()
        # Track rate limits per resource type (core, search, graphql, etc.)
        self._limits: dict[str, GitHubRateLimitInfo] = {}
        # Track rate limit events for logging (use deque to avoid memory leak from list slicing)
        self._events: deque[dict[str, Any]] = deque(maxlen=100)

    def update_from_headers(
        self,
        headers: dict[str, str],
        resource: str = "core",
    ) -> GitHubRateLimitInfo:
        """
        Update rate limit info from response headers.

        Args:
            headers: Response headers dictionary (case-insensitive)
            resource: The rate limit resource (core, search, graphql)

        Returns:
            Updated GitHubRateLimitInfo
        """
        # Normalize headers to lowercase
        headers_lower = {k.lower(): v for k, v in headers.items()}

        # Parse standard GitHub rate limit headers
        limit_str = headers_lower.get("x-ratelimit-limit")
        remaining_str = headers_lower.get("x-ratelimit-remaining")
        reset_str = headers_lower.get("x-ratelimit-reset")
        used_str = headers_lower.get("x-ratelimit-used")

        # Also check for Retry-After header (takes precedence)
        retry_after = headers_lower.get("retry-after")

        with self._lock:
            # Get or create info for this resource
            info = self._limits.get(resource, GitHubRateLimitInfo(resource=resource))

            # Update fields if present
            if limit_str and limit_str.isdigit():
                info.limit = int(limit_str)
            if remaining_str and remaining_str.isdigit():
                info.remaining = int(remaining_str)
            if used_str and used_str.isdigit():
                info.used = int(used_str)

            # Parse reset time
            if reset_str and reset_str.isdigit():
                info.reset_at = datetime.fromtimestamp(int(reset_str), tz=UTC)

            # Retry-After overrides reset time calculation
            if retry_after:
                try:
                    seconds = int(retry_after)
                    info.reset_at = datetime.now(UTC) + timedelta(seconds=seconds)
                except ValueError:
                    pass  # Ignore invalid retry-after

            info.last_updated = datetime.now(UTC)
            info.resource = resource
            self._limits[resource] = info

            # Log if rate limited
            if info.is_limited:
                self._log_event("rate_limited", resource, info)

            return info

    def _log_event(
        self,
        event_type: str,
        resource: str,
        info: GitHubRateLimitInfo,
    ) -> None:
        """Log a rate limit event."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "resource": resource,
            "remaining": info.remaining,
            "reset_at": info.reset_at.isoformat() if info.reset_at else None,
            "seconds_until_reset": info.seconds_until_reset,
        }
        self._events.append(event)

        # Log warning
        logger.warning(
            "GitHub API rate limit event",
            event_type=event_type,
            resource=resource,
            remaining=info.remaining,
            seconds_until_reset=info.seconds_until_reset,
        )

    def get_info(self, resource: str = "core") -> GitHubRateLimitInfo | None:
        """
        Get current rate limit info for a resource.

        Args:
            resource: The rate limit resource

        Returns:
            GitHubRateLimitInfo copy or None if not tracked
        """
        with self._lock:
            info = self._limits.get(resource)
            if info is None:
                return None
            # Return a copy to prevent external mutation (thread safety)
            return GitHubRateLimitInfo(
                limit=info.limit,
                remaining=info.remaining,
                reset_at=info.reset_at,
                used=info.used,
                resource=info.resource,
                last_updated=info.last_updated,
            )

    def is_rate_limited(self, resource: str = "core") -> bool:
        """
        Check if a resource is currently rate limited.

        Args:
            resource: The rate limit resource

        Returns:
            True if rate limited
        """
        info = self.get_info(resource)
        return info.is_limited if info else False

    def get_retry_after(self, resource: str = "core") -> int | None:
        """
        Get retry-after seconds for a rate limited resource.

        Args:
            resource: The rate limit resource

        Returns:
            Seconds to wait, or None if not rate limited
        """
        info = self.get_info(resource)
        if info and info.is_limited:
            return info.seconds_until_reset
        return None

    def get_status(self) -> dict[str, Any]:
        """
        Get overall rate limit status for observability.

        Returns:
            Dictionary with status for all tracked resources
        """
        with self._lock:
            resources = {resource: info.to_dict() for resource, info in self._limits.items()}
            return {
                "resources": resources,
                "any_limited": any(info.is_limited for info in self._limits.values()),
                "recent_events": self._events[-10:] if self._events else [],
            }

    def reset(self) -> None:
        """Reset all tracked rate limits."""
        with self._lock:
            self._limits.clear()
            self._events.clear()


# Global GitHub API rate tracker instance
github_api_rate_tracker = GitHubAPIRateTracker()


def update_github_rate_limit(
    headers: dict[str, str],
    resource: str = "core",
) -> GitHubRateLimitInfo:
    """
    Update GitHub API rate limit from response headers.

    Convenience function for updating the global tracker.

    Args:
        headers: Response headers dictionary
        resource: The rate limit resource

    Returns:
        Updated GitHubRateLimitInfo
    """
    return github_api_rate_tracker.update_from_headers(headers, resource)


def get_github_rate_limit_status() -> dict[str, Any]:
    """
    Get current GitHub API rate limit status.

    Returns:
        Dictionary with rate limit status
    """
    return github_api_rate_tracker.get_status()


def should_retry_after_rate_limit(resource: str = "core") -> tuple[bool, int | None]:
    """
    Check if request should be retried after rate limit.

    Args:
        resource: The rate limit resource

    Returns:
        Tuple of (should_retry, seconds_to_wait)
    """
    is_limited = github_api_rate_tracker.is_rate_limited(resource)
    retry_after = github_api_rate_tracker.get_retry_after(resource) if is_limited else None
    return is_limited, retry_after
