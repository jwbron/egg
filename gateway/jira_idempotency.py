"""
In-process idempotency cache for Jira write verbs.

Atlassian's REST API does **not** dedupe identical writes natively — a retry
after a transient 5xx will create a duplicate ticket / comment / issue link.
v1 (refine decision-16) keeps things simple by caching the response of a
write call locally, keyed by ``(verb, project, key)``, with a 5-minute TTL
that covers the typical transient-error retry window.

Public surface:

- ``get_or_run(verb, project, key, fn)`` — look the triple up in the cache
  and replay the cached ``(status_code, response_json)`` if present;
  otherwise invoke ``fn()`` (which must return ``(status_code, response_json)``)
  and cache the result before returning it.  When ``key`` is ``None`` the
  cache is bypassed entirely (i.e. the caller doesn't want dedup).
- ``clear_cache()`` — wipe the cache (test helper / config-reload hook).
- ``IDEMPOTENCY_TTL_SECONDS`` — module constant.

Implementation notes:

- The cache is module-level (per-gateway-process) so it is **not** durable
  across restarts and **not** shared across replicas.  That is intentional
  — refine decision-16 explicitly chose in-memory.  Operators who run
  multi-replica gateways must pin Jira-write traffic to a single replica,
  or accept the (small) duplication risk on retries that hit different
  replicas inside the TTL window.
- Eviction is lazy at lookup time — we don't run a background reaper
  thread.  Stale entries are dropped when their key is queried again, and
  the cache rarely grows beyond a few entries in practice (one per
  outstanding write within the 5-minute window).
- A ``threading.Lock`` guards mutations.  Lookups also take the lock so
  concurrent callers see a consistent view.

The ``fn`` callable runs **outside** the lock — holding the lock across an
HTTP round-trip would serialise unrelated writes.  This means two concurrent
calls with the same key may both miss the cache and both invoke ``fn``;
whichever one writes its result last wins.  That's acceptable: the value
landing in the cache is still a valid response for the same logical
operation.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

logger = get_logger("gateway.jira-idempotency")


# 5 minutes — chosen to cover Atlassian's typical transient-error window
# (refine decision-16).  Longer would risk replaying stale responses to a
# request that has since changed in user-facing meaning; shorter wouldn't
# cover slow upstream incidents.
IDEMPOTENCY_TTL_SECONDS: int = 300


# Module-level cache.  Keys are ``(verb, project, key)`` tuples (all
# strings).  Values are ``(monotonic_seconds, status_code, response_json)``.
_CacheKey = tuple[str, str, str]
_CacheEntry = tuple[float, int, dict[str, Any]]
_cache: dict[_CacheKey, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _is_fresh(entry: _CacheEntry, now: float) -> bool:
    """Return True iff the cache entry is still within its TTL."""
    inserted_at, _status, _body = entry
    return (now - inserted_at) < IDEMPOTENCY_TTL_SECONDS


def get_or_run(
    verb: str,
    project: str,
    key: str | None,
    fn: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    """Replay a cached response for ``(verb, project, key)`` or call ``fn``.

    Args:
        verb: A short identifier for the upstream verb (e.g.
            ``"jira_ticket_create"``).  Verbs share key-space within a
            project, so distinct verbs sharing the same opaque key
            **do not** collide.
        project: The Jira project key the operation targets (e.g.
            ``"ENG"``).  For verbs whose targets span projects (e.g.
            ``createIssueLink``), pass a canonical synthetic project tag
            built by the caller (see ``JiraClient.create_issue_link``).
        key: The caller-supplied opaque idempotency key.  ``None`` bypasses
            the cache entirely; the caller doesn't want dedup.
        fn: A zero-arg callable that performs the upstream work and returns
            ``(status_code, response_json)``.  Called only on cache miss.

    Returns:
        ``(status_code, response_json)`` either replayed from the cache or
        produced by ``fn``.
    """
    if not key:
        return fn()

    cache_key: _CacheKey = (verb, project, key)
    now = time.monotonic()

    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            if _is_fresh(entry, now):
                _inserted_at, status, body = entry
                logger.info(
                    "Jira idempotency cache hit",
                    verb=verb,
                    project=project,
                )
                return status, body
            # Stale — drop it now so concurrent callers don't re-replay it.
            _cache.pop(cache_key, None)

    # Run outside the lock so concurrent unrelated writes aren't serialised
    # behind a slow upstream call.
    status, body = fn()

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), status, body)

    return status, body


def clear_cache() -> None:
    """Wipe the idempotency cache (test helper / config-reload hook)."""
    with _cache_lock:
        _cache.clear()


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "clear_cache",
    "get_or_run",
]
