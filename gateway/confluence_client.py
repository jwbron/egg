"""
Confluence REST API client for the gateway sidecar.

Provides a thin, read-only wrapper around the Atlassian Cloud Confluence
REST API.  All traffic originates from the gateway (never from the sandbox)
and is authenticated with Basic auth using credentials loaded from
``gateway/confluence_credentials.py``.

Per-verb endpoint pinning (decision B1):

The wrapper is v2-first hybrid (refine decision #2): every public verb is
pinned to a specific Atlassian API version, and known v2 bugs (inline-
comment 404, footer-comment nested-reply gap) fall back transparently to
v1.  Each fall-back emits a structured ``confluence_v1_fallback`` audit
entry so operators can monitor whether Atlassian has fixed the v2 bugs and
the fallback can be retired.

Public surface (used by ``/api/v1/confluence/*`` routes in ``gateway.py``):

- ``ConfluenceClient.get_page(page_id, body_format=("storage",), expand=None)``
  → ``GET /wiki/api/v2/pages/{id}``
- ``ConfluenceClient.get_page_descendants(page_id, depth=None, limit=None,
  cursor=None)`` → ``GET /wiki/api/v2/pages/{id}/descendants``
- ``ConfluenceClient.get_page_footer_comments(page_id, body_format=("storage",),
  include_replies=False, limit=None, cursor=None)`` →
  ``GET /wiki/api/v2/pages/{id}/footer-comments`` (+ nested-reply pull when
  requested)
- ``ConfluenceClient.get_page_inline_comments(page_id, body_format=("storage",),
  limit=None, cursor=None)`` → ``GET /wiki/api/v2/pages/{id}/inline-comments``
  with v1 fallback on 404
- ``ConfluenceClient.list_spaces(allowed_spaces, limit=None, cursor=None)``
  → ``GET /wiki/api/v2/spaces`` filtered to ``allowed_spaces``
- ``ConfluenceClient.get_space_pages(space_id, limit=None, cursor=None,
  body_format=("storage",))`` → ``GET /wiki/api/v2/spaces/{space-id}/pages``
- ``ConfluenceClient.search_cql(cql, limit=None, cursor=None)`` →
  ``GET /wiki/rest/api/search`` (v1-only — there is no v2 CQL endpoint)
- ``ConfluenceClient.execute_raw(method, path, query=None, body=None)`` →
  passthrough used by ``/api/v1/confluence/execute`` for read-only paths.

Path safety:

- ``validate_confluence_api_path(path, method)`` enforces a regex allowlist
  of the read-only REST paths permitted by v1.  Write verbs and path
  fragments in ``CONFLUENCE_DENIED_VERBS`` (``restrictions``,
  ``permissions``, ``space.admin``, ``users``, ``attachments``) are rejected
  unconditionally.

429 handling (Q2, risk R10):

- GET requests retry at most once on HTTP 429, honoring ``Retry-After`` up
  to 30s.  Both attempts emit ``confluence_upstream_rate_limited`` audit
  entries.

404 envelope (architect D8):

- ``get_page``, ``get_page_descendants``, ``get_page_footer_comments``,
  ``get_page_inline_comments``, and ``get_space_pages`` translate upstream
  404 into a structured ``{"status": "not_found", "id": ...,
  "upstream_status": 404}`` dict.  ``search_cql`` and ``execute_raw`` still
  raise ``ConfluenceUpstreamError`` for 404.

403 envelope (Q7, risk R15):

- All read methods raise ``ConfluenceUpstreamForbidden`` on upstream 403
  so the route layer can audit it as ``confluence_upstream_403`` (distinct
  from generic upstream errors).

Response redaction (decision 10):

- ``redact_response`` walks every JSON response and strips ``accountId``,
  ``emailAddress``, and user-profile ``_links.webui`` URLs before returning
  to the route layer.  Page / space ``_links.webui`` URLs are preserved
  because they are addressable by the agent.

Payload-size cap (risk R7):

- Responses larger than ``CONFLUENCE_RESPONSE_MAX_BYTES`` (5 MiB) raise
  ``ConfluenceResponseTooLarge`` so the route layer can return HTTP 413.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

try:
    from .confluence_credentials import (
        ConfluenceCredentials,
        ConfluenceCredentialsUnavailable,
        get_confluence_credentials,
    )
except ImportError:
    from confluence_credentials import (  # type: ignore[no-redef, import-untyped]
        ConfluenceCredentials,
        ConfluenceCredentialsUnavailable,
        get_confluence_credentials,
    )

logger = get_logger("gateway.confluence-client")


# -----------------------------------------------------------------------------
# Constants & validation helpers
# -----------------------------------------------------------------------------

# Allowed HTTP methods for Confluence REST calls in v1.  Read-only fence.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET"})

# Path / verb segments and HTTP methods permanently out of scope for the
# Confluence wrapper.  See decisions 12 (attachments) and the broader read-
# only stance of v1.  Even if a future maintainer widens ALLOWED_METHODS,
# the gateway will still refuse these.
CONFLUENCE_DENIED_VERBS: frozenset[str] = frozenset(
    {
        # Path-segment denylist.
        "restrictions",
        "permissions",
        "space.admin",
        "users",
        "attachments",
        # HTTP-method denylist (also enforced by ALLOWED_METHODS).
        "DELETE",
        "PUT",
        "PATCH",
        "POST",
    }
)

# Allowed body-format tokens passed to the v2 ``body-format=`` query param.
# Everything else is rejected at validation time.
ALLOWED_BODY_FORMATS: frozenset[str] = frozenset(
    {"storage", "atlas_doc_format", "view", "export_view"}
)

# Default body format (decision-5 tweak): single body shape on the wire by
# default to keep payloads small; planners that need ADF tree traversal
# pass the override per-call.
DEFAULT_BODY_FORMAT: tuple[str, ...] = ("storage",)


# Regex allowlist for /api/v1/confluence/execute.  GET only; intentionally
# narrow.  Confluence Cloud v2 reads live under ``api/v2/...``, search and
# the v1 fallback live under ``rest/api/...``.
_PAGE_ID = r"\d+"
_SPACE_ID = r"\d+"

# Anti-bypass invariant (reviewer_code 9ae21669 + reviewer_security ec5985ff
# cycle-3 NACK on issue #1931): the /execute path allowlist must NOT include
# any path family that a narrow route already covers, because routing those
# through /execute skips the route-level safeguards.  The original removed
# paths and their bypass shapes:
#
# - ``rest/api/search``       — bypasses extract_search_spaces (CQL extractor)
# - ``api/v2/spaces``         — bypasses list_spaces' allowlist filter
# - ``api/v2/footer-comments`` (flat) — page-id-in-query, no upstream
#                              spaceKey filter; post-fetch space-allowlist
#                              check cannot resolve the targeted page
# - ``api/v2/inline-comments`` (flat) — same flat-endpoint shape
#
# Additionally, the page-scoped descendant / comment subpaths
# (``api/v2/pages/{id}/descendants`` etc.) are intentionally NOT in the
# allowlist either: their response body has no top-level ``spaceId``, so
# ``_check_post_fetch_space_allowlist`` always returns ``(False, None)`` and
# the route always emits ``confluence_space_denied`` — i.e. they're
# unusable via /execute.  Agents reach those endpoints through the
# dedicated /api/v1/confluence/page/* routes, which fetch the parent page
# and resolve spaceKey before the comment/descendant body ships.
#
# These paths remain reachable INTERNALLY (the client methods construct them
# directly without going through validate_confluence_api_path) for the
# include_replies side-call inside get_page_footer_comments and the v2-bug
# fallback inside get_page_inline_comments.  They are simply not exposed to
# the agent via the /execute escape hatch.  Mirrors gateway/jira_client.py's
# permanent denylist of search/jql + bare project for the same anti-bypass
# reason (PR #1964).
CONFLUENCE_API_ALLOWED_PATHS: list[re.Pattern[str]] = [
    re.compile(rf"^api/v2/pages/{_PAGE_ID}$"),
    re.compile(rf"^api/v2/spaces/{_SPACE_ID}/pages$"),
]

# CQL search has a 200-result hard upper bound at Atlassian.  We clamp at
# 100 to match Jira's defaults and keep transcripts predictable.
DEFAULT_LIMIT: int = 25
HARD_MAX_LIMIT: int = 100

# Hard payload cap on responses returned to the sandbox (risk R7).  5 MiB.
CONFLUENCE_RESPONSE_MAX_BYTES: int = 5 * 1024 * 1024

# 429 retry policy.
_RETRY_AFTER_CAP_SECONDS: int = 30
_DEFAULT_RETRY_AFTER_SECONDS: int = 1

# Single-request timeout for upstream Confluence calls.
_DEFAULT_TIMEOUT_SECONDS: float = 30.0

# spaceId ↔ spaceKey LRU cache (architect Q2).  Populated by list_spaces and
# get_page; consumed by routes that need to translate one to the other
# without double-fetching.
_SPACE_CACHE_TTL_SECONDS: float = 60.0
_SPACE_CACHE_MAX_ENTRIES: int = 256


class ConfluenceUpstreamError(RuntimeError):
    """Raised when Atlassian returns a non-2xx response that isn't a 404 on
    the endpoints where 404 is modelled as a ``not_found`` envelope.
    """

    def __init__(self, status_code: int, body: Any, path: str):
        super().__init__(f"Confluence upstream returned {status_code} for {path}")
        self.status_code = status_code
        self.body = body
        self.path = path


class ConfluenceUpstreamForbidden(RuntimeError):
    """Raised when Atlassian returns HTTP 403 for a read endpoint.

    The route layer translates this to a ``confluence_upstream_403`` audit
    event so operators can distinguish bot-account permission denials from
    space-allowlist denials and other upstream errors (Q7, risk R15).
    """

    def __init__(self, status_code: int, body: Any, path: str):
        super().__init__(f"Confluence upstream returned 403 for {path}")
        self.status_code = status_code
        self.body = body
        self.path = path


class ConfluenceResponseTooLarge(RuntimeError):
    """Raised when an upstream response exceeds ``CONFLUENCE_RESPONSE_MAX_BYTES``.

    The route layer translates this to HTTP 413 so the agent can request a
    narrower scope (different bodyFormat, smaller limit, etc.).
    """

    def __init__(self, size_bytes: int, path: str):
        super().__init__(f"Confluence response too large: {size_bytes} bytes from {path}")
        self.size_bytes = size_bytes
        self.path = path


def validate_confluence_api_path(path: str, method: str) -> tuple[bool, str]:
    """Validate a Confluence REST API path + method against the allowlist.

    Normalizes the path (strips leading/trailing slashes, drops query string,
    rejects ``..`` segments, rejects duplicate slashes, rejects non-ASCII
    characters) before checking the regex allowlist.

    Args:
        path: REST path relative to the Confluence base
            (e.g. ``api/v2/pages/12345`` or ``rest/api/search``).
        method: HTTP method (``GET`` is the only one allowed in v1).

    Returns:
        ``(True, "")`` if allowed; ``(False, reason)`` otherwise.
    """
    method_upper = (method or "").upper()
    if method_upper not in ALLOWED_METHODS:
        return False, f"HTTP method '{method_upper}' not allowed for Confluence"

    if method_upper in CONFLUENCE_DENIED_VERBS:
        return False, f"HTTP method '{method_upper}' is permanently denied"

    if not isinstance(path, str) or not path:
        return False, "path is required"

    # Reject non-ASCII / unicode (homoglyph guard).
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False, "path contains non-ASCII characters"

    # Strip query string before any other normalisation.
    path_no_query = path.split("?", 1)[0].split("#", 1)[0]

    # Reject path traversal and duplicate slashes (catch ``//foo`` BEFORE
    # stripping leading/trailing slashes).
    if ".." in path_no_query.split("/"):
        return False, "path contains '..' segment"
    if "//" in path_no_query:
        return False, "path contains duplicate slashes"
    stripped = path_no_query.strip("/")
    if not stripped:
        return False, "path is empty after normalisation"

    for segment in stripped.split("/"):
        if segment in CONFLUENCE_DENIED_VERBS:
            return False, f"path segment '{segment}' is permanently denied"

    for pattern in CONFLUENCE_API_ALLOWED_PATHS:
        if pattern.fullmatch(stripped):
            return True, ""

    return False, f"path '{stripped}' not in allowlist"


def _validate_body_format(body_format: Any) -> list[str]:
    """Validate and normalise a body-format list.

    Returns a list of validated tokens (empty if ``body_format`` is None).
    Raises ``ValueError`` on invalid input.
    """
    if body_format is None:
        return []
    if isinstance(body_format, str):
        # Accept a single string for ergonomic callers.
        body_format = [body_format]
    if not isinstance(body_format, (list, tuple)):
        raise ValueError("body_format must be a string or list of strings")
    cleaned: list[str] = []
    for entry in body_format:
        if not isinstance(entry, str):
            raise ValueError("body_format entries must be strings")
        if entry not in ALLOWED_BODY_FORMATS:
            raise ValueError(
                f"invalid body_format: {entry!r} (allowed: {sorted(ALLOWED_BODY_FORMATS)})"
            )
        cleaned.append(entry)
    return cleaned


def _validate_page_id(page_id: Any) -> str:
    """Validate that page_id is a numeric string.  Raises ValueError on miss."""
    if not isinstance(page_id, str) or not page_id:
        raise ValueError("page_id must be a non-empty string")
    if not page_id.isdigit():
        raise ValueError(f"page_id must be numeric, got: {page_id!r}")
    return page_id


def _validate_space_id(space_id: Any) -> str:
    """Validate that space_id is a numeric string.  Raises ValueError on miss."""
    if not isinstance(space_id, str) or not space_id:
        raise ValueError("space_id must be a non-empty string")
    if not space_id.isdigit():
        raise ValueError(f"space_id must be numeric, got: {space_id!r}")
    return space_id


# -----------------------------------------------------------------------------
# Response redaction
# -----------------------------------------------------------------------------

# Canonical sentinel substituted in for redacted user-identifying fields.
_REDACTED_VALUE: str = "<redacted>"

# Keys to scrub at any depth in any JSON response body.
_REDACTED_KEYS: frozenset[str] = frozenset({"accountId", "emailAddress"})

# user-profile ``_links.webui`` URL detector.  Confluence uses
# ``/wiki/people/<accountId>`` for user profiles; we redact those but leave
# space / page ``webui`` URLs alone (they're addressable by the agent).
_USER_PROFILE_WEBUI_RE = re.compile(r"(^|/)(?:wiki/)?people/")


def _is_user_profile_link(value: Any) -> bool:
    """Return True if a ``_links.webui`` value points at a user profile."""
    if not isinstance(value, str) or not value:
        return False
    return _USER_PROFILE_WEBUI_RE.search(value) is not None


# v2 user objects expose ``_links.self`` pointing at
# ``/wiki/api/v2/users/{accountId}``.  Strip them defensively so a future
# Atlassian schema change that drops the ``accountId`` field but keeps
# ``_links.self`` doesn't silently start leaking identifiers (reviewer_security
# non-blocking note, issue #1931).
_USER_PROFILE_SELF_RE = re.compile(r"/api/v\d+/users/")


def _is_user_profile_self_link(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return _USER_PROFILE_SELF_RE.search(value) is not None


def redact_response(payload: Any) -> Any:
    """Walk ``payload`` and strip user-identifying fields in-place.

    - ``accountId`` / ``emailAddress`` keys at any depth → ``"<redacted>"``
    - ``_links.webui`` URLs that look like user profile links →
      ``"<redacted>"``
    - ``_links.self`` URLs that point at ``/api/vN/users/...`` →
      ``"<redacted>"`` (defense-in-depth against future schema drift).
    - Page / space ``_links.webui`` URLs are preserved.

    The walker mutates dicts in place and returns the same object (for
    callers that want to chain the call).  Lists are walked recursively.
    """
    if isinstance(payload, dict):
        for key, value in list(payload.items()):
            if key in _REDACTED_KEYS:
                payload[key] = _REDACTED_VALUE
                continue
            if key == "_links" and isinstance(value, dict):
                webui = value.get("webui")
                if _is_user_profile_link(webui):
                    value["webui"] = _REDACTED_VALUE
                self_link = value.get("self")
                if _is_user_profile_self_link(self_link):
                    value["self"] = _REDACTED_VALUE
                # Walk into the rest of _links so nested user-profile
                # references inside ``self`` etc. still get scrubbed.
                redact_response(value)
                continue
            redact_response(value)
    elif isinstance(payload, list):
        for item in payload:
            redact_response(item)
    return payload


# -----------------------------------------------------------------------------
# Space cache (spaceId ↔ spaceKey, 60s TTL) — architect Q2
# -----------------------------------------------------------------------------


@dataclass
class _SpaceCacheEntry:
    space_id: str
    space_key: str
    expires_at: float


class _SpaceCache:
    """Tiny LRU-with-TTL cache mapping space_id ↔ space_key.

    Populated by ``list_spaces`` and ``get_page``; consumed by route helpers
    that need to translate one to the other without re-fetching.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _SPACE_CACHE_TTL_SECONDS,
        max_entries: int = _SPACE_CACHE_MAX_ENTRIES,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._by_id: OrderedDict[str, _SpaceCacheEntry] = OrderedDict()
        self._by_key: OrderedDict[str, _SpaceCacheEntry] = OrderedDict()

    def put(self, space_id: str, space_key: str) -> None:
        if not space_id or not space_key:
            return
        entry = _SpaceCacheEntry(
            space_id=str(space_id),
            space_key=str(space_key),
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._by_id[entry.space_id] = entry
            self._by_id.move_to_end(entry.space_id)
            self._by_key[entry.space_key] = entry
            self._by_key.move_to_end(entry.space_key)
            self._evict()

    def key_for_id(self, space_id: str) -> str | None:
        with self._lock:
            entry = self._by_id.get(space_id)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                self._drop(entry)
                return None
            self._by_id.move_to_end(space_id)
            return entry.space_key

    def id_for_key(self, space_key: str) -> str | None:
        with self._lock:
            entry = self._by_key.get(space_key)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                self._drop(entry)
                return None
            self._by_key.move_to_end(space_key)
            return entry.space_id

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_key.clear()

    def _evict(self) -> None:
        while len(self._by_id) > self._max:
            _, evicted = self._by_id.popitem(last=False)
            self._by_key.pop(evicted.space_key, None)

    def _drop(self, entry: _SpaceCacheEntry) -> None:
        self._by_id.pop(entry.space_id, None)
        self._by_key.pop(entry.space_key, None)


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------


@dataclass
class ConfluenceClient:
    """Thin REST-API wrapper around Atlassian Cloud Confluence.

    The client is class-shaped so v1.1 multi-site support is a single-file
    drop-in: wire a second instance with its own ``creds_provider`` /
    ``http_client`` and the route layer can pick between them without
    refactoring the read paths.
    """

    creds_provider: Any = get_confluence_credentials
    http_client: httpx.Client | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    space_cache: _SpaceCache = field(default_factory=_SpaceCache)
    _logged_default_body_format: bool = field(default=False, init=False, repr=False)
    _http_client_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def _client(self) -> httpx.Client:
        # Concurrent first requests must not each construct (and leak) an
        # httpx.Client.  Double-check under the lock so the hot path stays
        # lock-free once the client is initialised.
        if self.http_client is None:
            with self._http_client_lock:
                if self.http_client is None:
                    self.http_client = httpx.Client(timeout=self.timeout_seconds)
        return self.http_client

    def _build_url(self, creds: ConfluenceCredentials, path: str) -> str:
        """Compose the full Atlassian Confluence URL for a relative path.

        ``path`` is relative to the Confluence base — the base already
        includes ``/wiki`` (see ``confluence_credentials.py``), so we just
        append the API segment.
        """
        return f"{creds.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a single REST call with Basic auth + one 429-retry.

        Retry is GET-only.  Both attempts emit a structured
        ``confluence_upstream_rate_limited`` audit entry so operators see
        whether the retry succeeded.
        """
        creds = self.creds_provider()
        headers = {
            "Authorization": creds.basic_auth_header(),
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        url = self._build_url(creds, path)
        client = self._client()
        retryable = method.upper() == "GET"

        response: httpx.Response | None = None
        for attempt in (0, 1):
            response = client.request(
                method=method,
                url=url,
                params=query,
                json=body,
                headers=headers,
            )
            if response.status_code != 429:
                return response

            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            _audit_rate_limited(path=path, attempt=attempt, retry_after=retry_after)

            if attempt == 1 or not retryable:
                return response
            time.sleep(retry_after)

        # Defensive — loop always returns above.
        assert response is not None
        return response  # pragma: no cover

    # -- Public verbs ---------------------------------------------------------

    def get_page(
        self,
        page_id: str,
        body_format: Any = None,
        expand: Any = None,
    ) -> dict[str, Any]:
        """Fetch a single Confluence page (v2-first).

        Returns the parsed (and redacted) JSON body on 2xx, or a 404 envelope
        when the page does not exist.  Default ``body-format=storage``;
        callers may override to any subset of ``ALLOWED_BODY_FORMATS``.
        """
        page_id = _validate_page_id(page_id)
        formats = _validate_body_format(body_format) or list(DEFAULT_BODY_FORMAT)
        self._log_default_body_format(formats)

        query: dict[str, Any] = {"body-format": ",".join(formats)}
        if expand is not None:
            if isinstance(expand, (list, tuple)):
                query["expand"] = ",".join(str(v) for v in expand)
            elif isinstance(expand, str):
                query["expand"] = expand
            else:
                raise ValueError("expand must be a string or list of strings")

        path = f"api/v2/pages/{page_id}"
        response = self._request("GET", path, query=query)
        if response.status_code == 404:
            return _not_found_envelope(page_id)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        body_json = _safe_json(response, path)

        # Populate the space cache opportunistically — get_page returns
        # ``spaceId`` at the top level in v2.
        space_id = body_json.get("spaceId")
        if isinstance(space_id, (str, int)):
            # The space key isn't on the page response; we only cache the id
            # mapping here when we can pair it with a key (handled by
            # list_spaces).  Skip silently if we don't have a key.
            pass

        return _finalize_response(body_json, path)

    def get_page_descendants(
        self,
        page_id: str,
        depth: Any = None,
        limit: Any = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the descendants of a Confluence page (v2)."""
        page_id = _validate_page_id(page_id)
        query: dict[str, Any] = {}
        if depth is not None:
            query["depth"] = depth
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = f"api/v2/pages/{page_id}/descendants"
        response = self._request("GET", path, query=query or None)
        if response.status_code == 404:
            return _not_found_envelope(page_id)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        return _finalize_response(_safe_json(response, path), path)

    def get_page_footer_comments(
        self,
        page_id: str,
        body_format: Any = None,
        include_replies: bool = False,
        limit: Any = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch footer comments on a Confluence page.

        Per decision D1, when ``include_replies`` is set the wrapper makes
        a second call to the v1 ``/wiki/api/v2/footer-comments`` endpoint
        with ``page-id={page_id}&depth=all`` to fill in nested replies the
        v2 page-scoped endpoint omits.
        """
        page_id = _validate_page_id(page_id)
        formats = _validate_body_format(body_format) or list(DEFAULT_BODY_FORMAT)
        self._log_default_body_format(formats)
        query: dict[str, Any] = {"body-format": ",".join(formats)}
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = f"api/v2/pages/{page_id}/footer-comments"
        response = self._request("GET", path, query=query)
        if response.status_code == 404:
            return _not_found_envelope(page_id)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        primary = _safe_json(response, path)

        if include_replies:
            replies_path = "api/v2/footer-comments"
            replies_query: dict[str, Any] = {
                "page-id": page_id,
                "depth": "all",
                "body-format": ",".join(formats),
            }
            replies_response = self._request("GET", replies_path, query=replies_query)
            # On 2xx, fold replies into the primary envelope under a
            # normalized key.  On non-2xx for the replies side-call, we
            # *don't* fail the primary read — log and emit the v1-fallback
            # audit so operators see the gap.
            if 200 <= replies_response.status_code < 300:
                primary["_replies"] = _safe_json(replies_response, replies_path)
                _audit_v1_fallback(
                    endpoint="footer_comments_nested",
                    v2_status=replies_response.status_code,
                    page_id=page_id,
                )
            else:
                logger.warning(
                    "Footer-comment nested-reply fetch failed",
                    page_id=page_id,
                    status=replies_response.status_code,
                )

        return _finalize_response(primary, path)

    def get_page_inline_comments(
        self,
        page_id: str,
        body_format: Any = None,
        limit: Any = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch inline comments on a Confluence page.

        v2-first with v1 fallback (decision D1).  When the v2 endpoint
        returns 404 — Atlassian's known inline-comment bug — the wrapper
        retries against the v1 endpoint
        ``rest/api/content/{page_id}/child/comment?location=inline&expand=body.view``.
        """
        page_id = _validate_page_id(page_id)
        formats = _validate_body_format(body_format) or list(DEFAULT_BODY_FORMAT)
        self._log_default_body_format(formats)
        query: dict[str, Any] = {"body-format": ",".join(formats)}
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = f"api/v2/pages/{page_id}/inline-comments"
        response = self._request("GET", path, query=query)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        if response.status_code == 404:
            # v1 fallback (decision D1).
            v1_path = f"rest/api/content/{page_id}/child/comment"
            v1_query = {"location": "inline", "expand": "body.view"}
            v1_response = self._request("GET", v1_path, query=v1_query)
            _audit_v1_fallback(
                endpoint="inline_comments",
                v2_status=404,
                page_id=page_id,
            )
            if v1_response.status_code == 404:
                envelope = _not_found_envelope(page_id)
                envelope["used_fallback"] = True
                return envelope
            if v1_response.status_code == 403:
                raise ConfluenceUpstreamForbidden(403, _safe_response_body(v1_response), v1_path)
            _raise_for_status(v1_response, v1_path)
            v1_body = _safe_json(v1_response, v1_path)
            v1_body["used_fallback"] = True
            return _finalize_response(v1_body, v1_path)

        _raise_for_status(response, path)
        return _finalize_response(_safe_json(response, path), path)

    def list_spaces(
        self,
        allowed_spaces: frozenset[str],
        limit: Any = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List Confluence spaces, filtered to ``allowed_spaces``.

        Per decision 11, agents cannot enumerate the full tenant space set
        — the response only contains spaces whose ``key`` is in the
        operator's allowlist.  The cursor is preserved (minus filtered
        entries) so callers can paginate.
        """
        query: dict[str, Any] = {}
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = "api/v2/spaces"
        response = self._request("GET", path, query=query or None)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        body_json = _safe_json(response, path)

        self._populate_cache_from_spaces_payload(body_json)
        results = body_json.get("results")
        if isinstance(results, list):
            kept: list[Any] = []
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("key")
                if isinstance(key, str) and key in allowed_spaces:
                    kept.append(entry)
            body_json["results"] = kept

        return _finalize_response(body_json, path)

    def populate_space_cache(self, *, max_pages: int = 4) -> None:
        """Walk ``GET /wiki/api/v2/spaces`` pagination to fill the cache.

        Routes that translate ``spaceKey``↔``spaceId`` rely on the cache; if
        the operator's tenant has more spaces than fit on a single v2 page,
        a target sitting on page 2+ would otherwise look unresolvable and
        the call would fail-closed.  Walks at most ``max_pages`` pages so a
        very large tenant cannot pin the gateway on a slow upstream.

        The 403 / non-2xx error shapes mirror ``list_spaces`` so callers can
        catch the same exception types.
        """
        path = "api/v2/spaces"
        cursor: str | None = None
        for _ in range(max_pages):
            query: dict[str, Any] = {}
            if cursor:
                query["cursor"] = cursor
            response = self._request("GET", path, query=query or None)
            if response.status_code == 403:
                raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
            _raise_for_status(response, path)
            body_json = _safe_json(response, path)
            self._populate_cache_from_spaces_payload(body_json)
            cursor = _extract_next_cursor(body_json)
            if not cursor:
                return

    def _populate_cache_from_spaces_payload(self, body_json: dict[str, Any]) -> None:
        """Insert every ``{id, key}`` pair from a ``/wiki/api/v2/spaces`` page."""
        results = body_json.get("results")
        if not isinstance(results, list):
            return
        for entry in results:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            space_id = entry.get("id")
            if isinstance(space_id, (str, int)) and isinstance(key, str):
                self.space_cache.put(str(space_id), key)

    def get_space_pages(
        self,
        space_id: str,
        limit: Any = None,
        cursor: str | None = None,
        body_format: Any = None,
    ) -> dict[str, Any]:
        """List pages in a Confluence space (by numeric space id, v2)."""
        space_id = _validate_space_id(space_id)
        formats = _validate_body_format(body_format) or list(DEFAULT_BODY_FORMAT)
        self._log_default_body_format(formats)
        query: dict[str, Any] = {"body-format": ",".join(formats)}
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = f"api/v2/spaces/{space_id}/pages"
        response = self._request("GET", path, query=query)
        if response.status_code == 404:
            return _not_found_envelope(space_id)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        return _finalize_response(_safe_json(response, path), path)

    def search_cql(
        self,
        cql: str,
        limit: Any = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Run a CQL query (v1-only — there is no v2 CQL endpoint)."""
        if not isinstance(cql, str) or not cql.strip():
            raise ValueError("cql is required")
        query: dict[str, Any] = {"cql": cql}
        if limit is not None:
            query["limit"] = limit
        if cursor:
            query["cursor"] = cursor

        path = "rest/api/search"
        response = self._request("GET", path, query=query)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        return _finalize_response(_safe_json(response, path), path)

    def execute_raw(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pass-through for the ``/api/v1/confluence/execute`` route.

        Callers must have already validated ``path``/``method`` via
        ``validate_confluence_api_path``.  Raises ``ConfluenceUpstreamError``
        on any non-2xx status.
        """
        response = self._request(method, path, query=query, body=body)
        if response.status_code == 403:
            raise ConfluenceUpstreamForbidden(403, _safe_response_body(response), path)
        _raise_for_status(response, path)
        return _finalize_response(_safe_json(response, path), path)

    def _log_default_body_format(self, formats: list[str]) -> None:
        if self._logged_default_body_format:
            return
        if formats == list(DEFAULT_BODY_FORMAT):
            logger.info(
                "Confluence default body-format active",
                body_format=",".join(formats),
            )
            self._logged_default_body_format = True


# -----------------------------------------------------------------------------
# Helpers & module-level singleton
# -----------------------------------------------------------------------------


def _not_found_envelope(identifier: str) -> dict[str, Any]:
    """Canonical ``not_found`` envelope used by read endpoints."""
    return {"status": "not_found", "id": identifier, "upstream_status": 404}


def _raise_for_status(response: httpx.Response, path: str) -> None:
    """Raise ``ConfluenceUpstreamError`` if the response is not a 2xx."""
    if 200 <= response.status_code < 300:
        return
    body: Any
    try:
        body = response.json()
    except Exception:
        body = response.text
    raise ConfluenceUpstreamError(response.status_code, body, path)


def _safe_response_body(response: httpx.Response) -> Any:
    """Best-effort JSON-or-text body for upstream-error envelopes."""
    try:
        return response.json()
    except Exception:
        return response.text


def _safe_json(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a 2xx JSON response, wrapping non-dict shapes for callers."""
    try:
        data = response.json()
    except Exception as exc:  # pragma: no cover — Atlassian always returns JSON
        raise ConfluenceUpstreamError(response.status_code, response.text, path) from exc
    if not isinstance(data, dict):
        return {"data": data}
    return data


def _finalize_response(body: dict[str, Any], path: str) -> dict[str, Any]:
    """Apply redaction + payload-size cap before returning to the route layer."""
    redact_response(body)
    # Size check — JSON-serialise once.  We'd rather allocate the bytes here
    # than ship an oversized payload to the sandbox.
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except Exception:  # pragma: no cover — defensive
        size = 0
    if size > CONFLUENCE_RESPONSE_MAX_BYTES:
        raise ConfluenceResponseTooLarge(size, path)
    return body


def _extract_next_cursor(body_json: dict[str, Any]) -> str | None:
    """Return the ``cursor`` value from ``_links.next`` if Atlassian set one.

    The v2 API returns the next page as a relative URL with a ``cursor=...``
    query parameter; absence of a cursor (or absence of ``_links.next``) means
    the caller has reached the last page.
    """
    links = body_json.get("_links")
    if not isinstance(links, dict):
        return None
    next_url = links.get("next")
    if not isinstance(next_url, str) or not next_url:
        return None
    try:
        parsed = urlparse(next_url)
    except ValueError:
        return None
    # parse_qs defaults to keep_blank_values=False, so ``cursor=`` (empty
    # value) yields no entry and the caller's pagination loop terminates —
    # the desired fail-safe behaviour.  The ``cursor or None`` guard at the
    # bottom of this function would also cover the empty-string case under
    # ``keep_blank_values=True``, but verify and add explicit coverage if a
    # future change flips that flag.
    qs = parse_qs(parsed.query)
    cursor_values = qs.get("cursor")
    if not cursor_values:
        return None
    cursor = cursor_values[0]
    return cursor or None


def _parse_retry_after(value: str | None) -> int:
    """Parse a ``Retry-After`` header value to an integer number of seconds."""
    if value is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    if parsed <= 0:
        return _DEFAULT_RETRY_AFTER_SECONDS
    return min(parsed, _RETRY_AFTER_CAP_SECONDS)


def _audit_rate_limited(*, path: str, attempt: int, retry_after: int) -> None:
    """Emit a ``confluence_upstream_rate_limited`` audit entry, if possible."""
    try:
        from flask import has_request_context
    except ImportError:  # pragma: no cover

        def has_request_context() -> bool:
            return False

    try:
        from .gateway import audit_log
    except ImportError:
        try:
            from gateway import audit_log  # type: ignore[no-redef, attr-defined]
        except ImportError:
            audit_log = None  # type: ignore[assignment]

    if audit_log is not None and has_request_context():
        try:
            audit_log(
                "confluence_upstream_rate_limited",
                "confluence_request",
                success=False,
                details={
                    "path": path,
                    "attempt": attempt,
                    "retry_after": retry_after,
                },
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("audit_log failed in confluence _request")
            return
        return

    logger.warning(
        "Confluence upstream 429",
        path=path,
        attempt=attempt,
        retry_after=retry_after,
    )


def _audit_v1_fallback(*, endpoint: str, v2_status: int, page_id: str) -> None:
    """Emit a ``confluence_v1_fallback`` audit entry."""
    try:
        from flask import has_request_context
    except ImportError:  # pragma: no cover

        def has_request_context() -> bool:
            return False

    try:
        from .gateway import audit_log
    except ImportError:
        try:
            from gateway import audit_log  # type: ignore[no-redef, attr-defined]
        except ImportError:
            audit_log = None  # type: ignore[assignment]

    if audit_log is not None and has_request_context():
        try:
            audit_log(
                "confluence_v1_fallback",
                "confluence_request",
                success=True,
                details={
                    "endpoint": endpoint,
                    "v2_status": v2_status,
                    "page_id": page_id,
                },
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("audit_log failed in confluence v1 fallback")
            return
        return

    logger.info(
        "Confluence v1 fallback exercised",
        endpoint=endpoint,
        v2_status=v2_status,
        page_id=page_id,
    )


# Module-level singleton — mirrors ``jira_client.get_jira_client``.
_confluence_client: ConfluenceClient | None = None
_confluence_client_lock = threading.Lock()


def get_confluence_client() -> ConfluenceClient:
    """Return the process-wide ``ConfluenceClient`` singleton."""
    global _confluence_client
    with _confluence_client_lock:
        if _confluence_client is None:
            _confluence_client = ConfluenceClient()
        return _confluence_client


def reset_confluence_client() -> None:
    """Drop the module-level singleton (test helper)."""
    global _confluence_client
    with _confluence_client_lock:
        _confluence_client = None


__all__ = [
    "ALLOWED_BODY_FORMATS",
    "ALLOWED_METHODS",
    "CONFLUENCE_API_ALLOWED_PATHS",
    "CONFLUENCE_DENIED_VERBS",
    "CONFLUENCE_RESPONSE_MAX_BYTES",
    "ConfluenceClient",
    "ConfluenceCredentials",
    "ConfluenceCredentialsUnavailable",
    "ConfluenceResponseTooLarge",
    "ConfluenceUpstreamError",
    "ConfluenceUpstreamForbidden",
    "DEFAULT_BODY_FORMAT",
    "DEFAULT_LIMIT",
    "HARD_MAX_LIMIT",
    "get_confluence_client",
    "redact_response",
    "reset_confluence_client",
    "validate_confluence_api_path",
]
