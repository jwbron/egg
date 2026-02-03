"""
Repository visibility checking with caching.

Provides GitHub API integration to check whether repositories are public,
private, or internal. Used by Private Repo Mode to restrict operations
to private repositories only.

Security Properties:
- Fail-closed: If visibility cannot be determined, assume public (deny access)
- Two-tier caching: Short TTL for reads, no caching for writes
- Thread-safe: Uses locks for cache access
"""

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import requests

from shared.egg_logging import get_logger

logger = get_logger("gateway.repo-visibility")

# GitHub API configuration
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# Default cache TTLs (in seconds)
DEFAULT_VISIBILITY_CACHE_TTL_READ = 60
DEFAULT_VISIBILITY_CACHE_TTL_WRITE = 0

# Type alias for visibility values
VisibilityType = Literal["public", "private", "internal"]

# Valid visibility values for validation
VALID_VISIBILITIES: frozenset[str] = frozenset({"public", "private", "internal"})


@dataclass
class CachedVisibility:
    """Cached repository visibility with TTL."""

    owner: str
    repo: str
    visibility: VisibilityType
    fetched_at: float

    def is_stale(self, ttl: int) -> bool:
        """Check if cache entry is stale based on TTL."""
        if ttl <= 0:
            return True
        return (datetime.now(UTC).timestamp() - self.fetched_at) > ttl


class RepoVisibilityChecker:
    """Repository visibility checker with caching.

    Uses GitHub API to check repository visibility and caches results
    to reduce API calls. Supports two-tier caching with different TTLs
    for read and write operations.
    """

    def __init__(
        self,
        read_ttl: int | None = None,
        write_ttl: int | None = None,
        get_tokens_fn: Callable[[], list[tuple[str, str]]] | None = None,
    ):
        """Initialize the visibility checker.

        Args:
            read_ttl: Cache TTL for read operations (seconds)
            write_ttl: Cache TTL for write operations (seconds, 0 = no cache)
            get_tokens_fn: Function to get tokens [(token, source), ...]
        """
        self._read_ttl = read_ttl if read_ttl is not None else self._get_read_ttl()
        self._write_ttl = write_ttl if write_ttl is not None else self._get_write_ttl()
        self._get_tokens_fn = get_tokens_fn

        # Cache: (owner, repo) -> CachedVisibility
        self._cache: dict[tuple[str, str], CachedVisibility] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _get_read_ttl() -> int:
        """Get read cache TTL from environment or default."""
        return int(os.environ.get("VISIBILITY_CACHE_TTL_READ", DEFAULT_VISIBILITY_CACHE_TTL_READ))

    @staticmethod
    def _get_write_ttl() -> int:
        """Get write cache TTL from environment or default."""
        return int(os.environ.get("VISIBILITY_CACHE_TTL_WRITE", DEFAULT_VISIBILITY_CACHE_TTL_WRITE))

    def _get_tokens(self) -> list[tuple[str, str]]:
        """Get all available tokens for visibility queries."""
        if self._get_tokens_fn:
            return self._get_tokens_fn()

        tokens = []

        # Try to get bot token from token_refresher
        try:
            from .token_refresher import get_bot_token

            bot_token, _source = get_bot_token()
            if bot_token:
                tokens.append((bot_token, "bot"))
        except ImportError:
            pass

        # User token fallback
        user_token = os.environ.get("EGG_GITHUB_USER_TOKEN", "").strip()
        if user_token:
            tokens.append((user_token, "user"))

        return tokens

    def _fetch_visibility_with_token(
        self, owner: str, repo: str, token: str, source: str
    ) -> VisibilityType | None:
        """Fetch repository visibility using a specific token."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                visibility = data.get("visibility", "public")

                if visibility not in VALID_VISIBILITIES:
                    logger.warning(
                        "Invalid visibility value from GitHub API",
                        owner=owner,
                        repo=repo,
                        visibility=visibility,
                        token_source=source,
                    )
                    return None

                logger.debug(
                    "Fetched repository visibility",
                    owner=owner,
                    repo=repo,
                    visibility=visibility,
                    token_source=source,
                )
                # Cast to VisibilityType since we validated it above
                assert visibility in VALID_VISIBILITIES
                if visibility == "public":
                    return "public"
                elif visibility == "private":
                    return "private"
                else:
                    return "internal"

            elif response.status_code == 404:
                logger.debug(
                    "Token cannot access repository (404)",
                    owner=owner,
                    repo=repo,
                    token_source=source,
                )
                return None

            elif response.status_code == 403:
                logger.warning(
                    "GitHub API forbidden/rate-limited",
                    owner=owner,
                    repo=repo,
                    status_code=403,
                    token_source=source,
                )
                return None

            else:
                logger.warning(
                    "GitHub API unexpected status",
                    owner=owner,
                    repo=repo,
                    status_code=response.status_code,
                    token_source=source,
                )
                return None

        except requests.Timeout:
            logger.warning(
                "GitHub API timeout",
                owner=owner,
                repo=repo,
                token_source=source,
            )
            return None
        except requests.RequestException as e:
            logger.warning(
                "GitHub API request failed",
                owner=owner,
                repo=repo,
                error=str(e),
                token_source=source,
            )
            return None

    def _fetch_visibility(self, owner: str, repo: str) -> VisibilityType | None:
        """Fetch repository visibility, trying all available tokens."""
        tokens = self._get_tokens()

        if not tokens:
            logger.warning("No GitHub tokens available for visibility check")
            return None

        for token, source in tokens:
            visibility = self._fetch_visibility_with_token(owner, repo, token, source)
            if visibility is not None:
                return visibility

        logger.warning(
            "All tokens failed visibility check",
            owner=owner,
            repo=repo,
            tokens_tried=[source for _, source in tokens],
        )
        return None

    def get_visibility(
        self,
        owner: str,
        repo: str,
        for_write: bool = False,
    ) -> VisibilityType | None:
        """Get repository visibility with tiered caching."""
        cache_key = (owner.lower(), repo.lower())
        ttl = self._write_ttl if for_write else self._read_ttl

        # Check cache
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and not cached.is_stale(ttl):
                logger.debug(
                    "Cache hit for visibility",
                    owner=owner,
                    repo=repo,
                    visibility=cached.visibility,
                    for_write=for_write,
                )
                return cached.visibility

        # Fetch from API
        visibility = self._fetch_visibility(owner, repo)

        # Cache the result if we got a valid response
        if visibility:
            with self._cache_lock:
                self._cache[cache_key] = CachedVisibility(
                    owner=owner.lower(),
                    repo=repo.lower(),
                    visibility=visibility,
                    fetched_at=datetime.now(UTC).timestamp(),
                )

        return visibility

    def is_private(
        self,
        owner: str,
        repo: str,
        for_write: bool = False,
    ) -> bool | None:
        """Check if a repository is private (or internal)."""
        visibility = self.get_visibility(owner, repo, for_write=for_write)
        if visibility is None:
            return None
        return visibility in ("private", "internal")

    def clear_cache(self) -> None:
        """Clear the visibility cache."""
        with self._cache_lock:
            self._cache.clear()

    def invalidate(self, owner: str, repo: str) -> None:
        """Invalidate cache entry for a specific repository."""
        cache_key = (owner.lower(), repo.lower())
        with self._cache_lock:
            self._cache.pop(cache_key, None)


# Global visibility checker instance with thread-safe initialization
_checker: RepoVisibilityChecker | None = None
_checker_lock = threading.Lock()


def get_visibility_checker() -> RepoVisibilityChecker:
    """Get the global visibility checker instance (thread-safe)."""
    global _checker
    if _checker is None:
        with _checker_lock:
            if _checker is None:
                _checker = RepoVisibilityChecker()
    return _checker


def get_repo_visibility(
    owner: str,
    repo: str,
    for_write: bool = False,
) -> VisibilityType | None:
    """Get repository visibility (convenience function)."""
    return get_visibility_checker().get_visibility(owner, repo, for_write=for_write)


def is_repo_private(
    owner: str,
    repo: str,
    for_write: bool = False,
) -> bool | None:
    """Check if a repository is private (convenience function)."""
    return get_visibility_checker().is_private(owner, repo, for_write=for_write)
