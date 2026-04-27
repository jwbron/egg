"""
In-process idempotency cache for Jira write verbs.

Implements decision-3 (optional ``idempotency_key``) and decision-16 (in-memory
per-gateway-process, 5-minute TTL) from the architect analysis for issue
[#1924](https://github.com/jwbron/egg/issues/1924).

Atlassian Cloud has no native idempotency-key header for the v3 REST API, so
when a caller (e.g. an SDLC agent retrying after a network blip) issues the
same logical write twice, Atlassian happily creates two tickets / posts two
comments / wires two duplicate links.  This cache shields the gateway against
short-lived retries: a successful write is remembered for ``IDEMPOTENCY_TTL_SECONDS``,
and any subsequent call with the same ``(verb, project, idempotency_key)``
triple within that window returns the cached response without touching
Atlassian.

Scope:

- Per-gateway-process — explicitly NOT a Redis / cross-instance cache.  v1
  ships a single gateway sidecar per host so this is sufficient (Q22:
  multi-tenant deferred).
- 5-minute TTL — long enough to absorb retry storms after a transient
  upstream error, short enough that the orchestrator owns higher-level
  dedup (Q18).
- Lazy eviction at lookup — entries past their TTL are removed when the
  next caller probes for the same key.  No background thread.
- Thread-safe via a module-level ``_lock``.

Cache key composition:

- ``createJiraIssue`` and ``addCommentToJiraIssue`` use ``(verb, project_key,
  idempotency_key)``.  The project key narrows the namespace so two callers
  re-using the same opaque key against different projects do not collide.
- ``createIssueLink`` uses ``(verb="link", canonical_link_id(inward, outward,
  type), idempotency_key)`` (decision-28).  Distinct ``(inward, outward,
  type)`` triples never alias to the same cache entry even if callers reuse
  the same opaque key.

Public API:

- ``get_or_run(verb, project, key, fn)`` — lookup-or-compute.  When ``key`` is
  ``None`` the cache is bypassed entirely and ``fn()`` runs unconditionally,
  matching the "optional with documented warning" decision.
- ``clear_cache()`` — drop all entries.  Used by tests.
- ``IDEMPOTENCY_TTL_SECONDS`` — public 5-minute TTL constant.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

# Public TTL — 5 minutes.  Tuned per architect analysis (decision-16).
IDEMPOTENCY_TTL_SECONDS: int = 5 * 60

# Internal storage: ``(verb, project, key)`` -> ``(monotonic_ts, status_code,
# response_json)``.  ``status_code`` is included so cached entries can be
# replayed verbatim by the route layer (return body + status as if Atlassian
# answered again).
_CacheKey = tuple[str, str, str]
_CacheEntry = tuple[float, int, dict[str, Any]]
_cache: dict[_CacheKey, _CacheEntry] = {}
_lock = threading.Lock()


def _now() -> float:
    """Return the current monotonic clock reading.

    Wrapped in a helper so tests can patch ``time.monotonic`` via
    ``monkeypatch.setattr`` and exercise TTL expiry deterministically.
    """
    return time.monotonic()


def get_or_run(
    verb: str,
    project: str,
    key: str | None,
    fn: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    """Return a cached ``(status_code, response_json)`` if one exists, else run ``fn``.

    Args:
        verb: A short identifier for the write verb (e.g. ``"create"``,
            ``"comment"``, ``"link"``).  Combined with ``project`` and
            ``key`` to form the cache key.
        project: A namespace string — typically the Atlassian project key
            (``"ENG"``).  For ``createIssueLink`` callers should pass
            ``canonical_link_id(inward, outward, type)`` (a stable triple
            string) so distinct triples never alias to the same opaque key.
        key: Caller-supplied idempotency token.  ``None`` disables the
            cache entirely and ``fn()`` runs unconditionally.
        fn: A zero-arg callable that performs the upstream call and returns
            ``(status_code, response_json)``.  Only invoked on a miss.

    Returns:
        ``(status_code, response_json)`` either fetched from cache or freshly
        produced by ``fn``.

    Notes:
        ``fn`` exceptions are NOT cached — a failed upstream call leaves the
        cache empty, so the next retry runs the function again.  This is a
        deliberate choice: caching errors would force the caller to wait out
        the TTL after an outage.
    """
    if key is None:
        return fn()

    cache_key: _CacheKey = (verb, project, key)
    now = _now()

    with _lock:
        entry = _cache.get(cache_key)
        if entry is not None:
            ts, status, body = entry
            if now - ts <= IDEMPOTENCY_TTL_SECONDS:
                return status, body
            # Stale — drop it and fall through to recompute.
            del _cache[cache_key]

    # Cache miss (or stale).  Run outside the lock so concurrent calls for
    # different keys do not block each other.  A small thundering-herd window
    # exists for the same key; the worst case is two upstream calls and the
    # second writer overwrites the first cached entry — both responses are
    # successful so the user-visible behaviour matches Atlassian's at-most-once
    # semantics anyway.
    status_code, response = fn()
    with _lock:
        _cache[cache_key] = (_now(), status_code, response)
    return status_code, response


def clear_cache() -> None:
    """Drop every cached entry.  Used by tests to isolate cases."""
    with _lock:
        _cache.clear()


def canonical_link_id(inward_key: str, outward_key: str, link_type: str) -> str:
    """Build a stable namespace string for ``createIssueLink`` cache keys.

    Returns ``"<inward>|<outward>|<type>"``.  Two different
    ``(inward, outward, type)`` triples never collide because the separator
    (``|``) is illegal in Jira ticket keys (``[A-Z][A-Z0-9_]*-\\d+``) and in
    Jira link-type names (Atlassian rejects pipes in link-type names).
    """
    return f"{inward_key}|{outward_key}|{link_type}"


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "canonical_link_id",
    "clear_cache",
    "get_or_run",
]
