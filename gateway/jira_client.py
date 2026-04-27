"""
Jira REST API client for the gateway sidecar.

Provides a thin, policy-enforced wrapper around the Atlassian Cloud REST API
v3.  All traffic originates from the gateway (never from the sandbox) and is
authenticated with Basic auth using credentials loaded from
``gateway/jira_credentials.py``.

Public surface (used by ``/api/v1/jira/*`` routes in ``gateway.py``):

Read verbs (issue [#1556](https://github.com/jwbron/egg/issues/1556) — v1):

- ``JiraClient.get_ticket(key, fields=None)`` — ``GET /rest/api/3/issue/{key}``
  with ``expand=renderedBody,renderedFields`` by default so agents receive the
  Atlassian-rendered HTML alongside the raw Atlassian Document Format JSON.
- ``JiraClient.search(jql, fields=None, next_page_token=None, max_results=None)``
  — ``POST /rest/api/3/search/jql`` (cursor pagination via ``nextPageToken``).
- ``JiraClient.get_comments(key)`` — ``GET /rest/api/3/issue/{key}/comment``.
- ``JiraClient.execute_raw(method, path, query=None, body=None)`` — passthrough
  used by ``/api/v1/jira/execute`` for read-only API endpoints.

Write verbs (issue [#1924](https://github.com/jwbron/egg/issues/1924) — v1.1):

- ``JiraClient.create_issue(...)`` — ``POST /rest/api/3/issue``.
- ``JiraClient.edit_issue(...)`` — ``PUT /rest/api/3/issue/{key}``.
- ``JiraClient.add_comment(...)`` — ``POST /rest/api/3/issue/{key}/comment``.
- ``JiraClient.create_issue_link(...)`` — ``POST /rest/api/3/issueLink``.

Path safety:

- ``validate_jira_api_path(path, method)`` enforces a regex allowlist of the
  read-only REST paths permitted by ``/api/v1/jira/execute``.  Its
  ``ALLOWED_METHODS={"GET"}`` constraint applies **only** to that
  passthrough — it is not consulted by the write methods listed above,
  whose paths are hardcoded inside the methods (so the path-segment
  denylist cannot be reached by construction).  Write verbs
  (``DELETE``/``PUT``/``PATCH``) on ``/execute`` and path fragments in
  ``JIRA_WRITE_VERBS_DENIED`` (``transitions``, ``worklog``,
  ``attachments``, ``watchers``) are rejected unconditionally — see refine
  / architect cycle.

429 handling:

- GET requests retry at most once on HTTP 429, honoring ``Retry-After`` up to
  30s.  Write verbs never retry (future-safety: a retried POST can create
  duplicate Jira issues).
- ``jira_upstream_rate_limited`` is emitted on every 429 — including writes
  — so the audit trail covers operator-visible rate-limit incidents.  The
  retry loop only retries GETs; the audit emit is unconditional.

404 envelope:

- ``get_ticket`` and ``get_comments`` translate upstream 404 into a structured
  ``{"status": "not_found", "key": key, "upstream_status": 404}`` dict so the
  route returns HTTP 200 with a semantic body instead of a raw error.  Other
  endpoints still raise ``JiraUpstreamError``.

Field validation:

- ``validate_fields`` caps the list at 32 entries and requires each to match
  ``^[a-zA-Z_][a-zA-Z0-9_.-]*$`` — applied at the route layer before calling
  the client.

Idempotency (write verbs):

- ``create_issue``, ``add_comment``, and ``create_issue_link`` consult the
  in-process cache in ``gateway/jira_idempotency.py`` when the caller passes
  ``idempotency_key``.  ``edit_issue`` is naturally idempotent and bypasses
  the cache.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

try:
    from .jira_credentials import (
        JiraCredentials,
        JiraCredentialsUnavailable,
        get_jira_credentials,
    )
except ImportError:
    from jira_credentials import (  # type: ignore[no-redef, import-untyped]
        JiraCredentials,
        JiraCredentialsUnavailable,
        get_jira_credentials,
    )

# Idempotency cache + ADF wrapper helpers — added in #1924.  The conftest in
# ``gateway/tests`` loads modules with absolute imports, so we mirror the
# ``jira_credentials`` two-step import dance here.
try:
    from . import jira_adf, jira_idempotency
except ImportError:
    import jira_adf  # type: ignore[no-redef, import-untyped]
    import jira_idempotency  # type: ignore[no-redef, import-untyped]

logger = get_logger("gateway.jira-client")


# -----------------------------------------------------------------------------
# Constants & validation helpers
# -----------------------------------------------------------------------------

# Allowed HTTP methods for the ``/api/v1/jira/execute`` passthrough.  Stays
# GET-only forever — the write verbs added in
# [#1924](https://github.com/jwbron/egg/issues/1924) live on dedicated routes
# (``/api/v1/jira/ticket/create``, ``ticket/edit``, ``ticket/comment/add``,
# ``issue-link/create``) whose paths are hardcoded inside their corresponding
# ``JiraClient`` write methods.  Those methods bypass
# ``validate_jira_api_path`` because their paths are construction-time
# constants — there is no caller-supplied path that could smuggle in a
# denied verb segment (``transitions``, ``worklog``, ``attachments``,
# ``watchers``).  Adding new write verbs in the future means adding a new
# narrow route + ``JiraClient`` method — *not* widening this set.
ALLOWED_METHODS: frozenset[str] = frozenset({"GET"})

# Paths / HTTP verbs that are permanently out of scope for the wrapper.
# Even if a future maintainer widens ALLOWED_METHODS, the gateway will still
# refuse these — they're the escape hatch that turns read-only audit trails
# into real Jira mutations, and the refine phase explicitly blocked them.
JIRA_WRITE_VERBS_DENIED: frozenset[str] = frozenset(
    {
        # Path-segment denylist (checked against individual segments of the
        # normalised path).
        "transitions",
        "worklog",
        "attachments",
        "watchers",
        # HTTP-method denylist (checked against the request method).
        "DELETE",
        "PUT",
        "PATCH",
    }
)

# Regex allowlist mirroring ``validate_gh_api_path`` in ``github_client.py``.
# GET only; intentionally narrow.  Extend by adding a compiled pattern, never
# by relaxing the shape.
#
# Project keys follow Atlassian's rule: uppercase ASCII letter, then
# letters/digits/underscore (``[A-Z][A-Z0-9_]*``).  Ticket keys are
# ``<PROJECT>-<digits>``.
_PROJECT_KEY = r"[A-Z][A-Z0-9_]*"
_TICKET_KEY = rf"{_PROJECT_KEY}-\d+"

JIRA_API_ALLOWED_PATHS: list[re.Pattern[str]] = [
    re.compile(rf"^issue/{_TICKET_KEY}$"),
    re.compile(rf"^issue/{_TICKET_KEY}/comment$"),
    # ``search/jql`` is intentionally NOT in this allowlist.  ``/api/v1/jira/
    # search`` MUST go through the dedicated route so the JQL project-scope
    # extractor (gateway/jira_search.py) runs before anything touches
    # Atlassian.  Allowing ``search/jql`` through ``/api/v1/jira/execute``
    # would bypass that extractor and let an agent read issues from any
    # project (reviewer_code cycle 1 finding #3).
    #
    # ``^project$`` (bare, no key suffix) is intentionally excluded:
    # ``GET /rest/api/3/project`` returns ALL projects visible to the API
    # token, bypassing the project allowlist.  Agents should use
    # ``project/<KEY>`` for specific, allowlisted projects only.
    re.compile(rf"^project/{_PROJECT_KEY}$"),
]

_FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")

# Sanity cap on client-side field lists passed as ``fields=...``.  Matches
# architect guidance; Atlassian itself accepts larger lists but 32 is plenty
# for any reasonable agent query and keeps log lines bounded.
MAX_FIELDS: int = 32

# Default expand parameters for issue reads.  Gives agents both the raw
# Atlassian Document Format JSON and the server-rendered HTML in a single
# request so they don't need to re-fetch with different expand values
# (risk R6, architect Q4).
DEFAULT_EXPAND: tuple[str, ...] = ("renderedBody", "renderedFields")

# Default ``maxResults`` when the caller doesn't pass one.  Capped at 100 by
# the route layer (architect).
DEFAULT_MAX_RESULTS: int = 50

# Hard upper bound on ``maxResults``.  Enforced in ``search()``; routes clamp
# their own input but we double-check here so direct callers (tests, future
# orchestrator hooks) can't smuggle in a larger value.
HARD_MAX_RESULTS: int = 100

# 429 retry policy.
_RETRY_AFTER_CAP_SECONDS: int = 30
_DEFAULT_RETRY_AFTER_SECONDS: int = 1

# Single-request timeout for upstream Jira calls.
_DEFAULT_TIMEOUT_SECONDS: float = 30.0

# -----------------------------------------------------------------------------
# Write-verb body caps (issue #1924, Q15 default)
# -----------------------------------------------------------------------------
#
# These bounds keep audit-log entries small and protect against memory
# exhaustion if a misbehaving caller sends a 100 MiB description.  They are
# enforced at the route layer (``gateway/gateway.py``) so the rejection
# happens before we touch the client; the constants are exported here so
# tests and callers can refer to them by name.

# Atlassian summary field hard limit.
JIRA_SUMMARY_MAX_CHARS: int = 255

# Description / comment body — generous enough for plan-apply commentary,
# small enough to bound audit-log size.  Atlassian publishes no comment-body
# limit in the v3 docs but mirrors the description constraint server-side
# under Cloud's modern editor.
JIRA_DESCRIPTION_MAX_CHARS: int = 32 * 1024
JIRA_COMMENT_MAX_CHARS: int = 32 * 1024

# Labels — Atlassian rejects labels above 255 chars; 30 distinct labels per
# write covers the realistic "list every component this ticket touches"
# case without giving a misbehaving caller free rein.
JIRA_LABEL_MAX_COUNT: int = 30
JIRA_LABEL_MAX_CHARS: int = 255

# Default link-type allowlist (decision-4).  Operators may extend this list
# via ``config/context-filters.yaml jira.link_types: [...]``; the default
# covers the most common SDLC use cases (blocks / relates).
DEFAULT_JIRA_LINK_TYPES: tuple[str, ...] = ("Blocks", "Relates")


class JiraUpstreamError(RuntimeError):
    """Raised when Atlassian returns a non-2xx response that isn't a 404 on
    the endpoints where 404 is modelled as a ``not_found`` envelope.
    """

    def __init__(self, status_code: int, body: Any, path: str):
        super().__init__(f"Jira upstream returned {status_code} for {path}")
        self.status_code = status_code
        self.body = body
        self.path = path


def validate_jira_api_path(path: str, method: str) -> tuple[bool, str]:
    """Validate a Jira REST API path + method against the allowlist.

    Normalizes the path (strips leading/trailing slashes, drops query string,
    rejects ``..`` segments, rejects duplicate slashes, rejects non-ASCII
    characters) before checking the regex allowlist.

    Args:
        path: REST path relative to ``/rest/api/3/`` (e.g. ``issue/FOO-1``).
        method: HTTP method (``GET`` is the only one allowed in v1).

    Returns:
        ``(True, "")`` if the request is allowed; ``(False, reason)`` otherwise.
    """
    method_upper = (method or "").upper()
    if method_upper not in ALLOWED_METHODS:
        return False, f"HTTP method '{method_upper}' not allowed for Jira"

    # Explicit write-verb denylist on the method (belt-and-braces — already
    # excluded from ALLOWED_METHODS above, but kept so future maintainers see
    # the intent).
    if method_upper in JIRA_WRITE_VERBS_DENIED:
        return False, f"HTTP method '{method_upper}' is permanently denied"

    if not isinstance(path, str) or not path:
        return False, "path is required"

    # Reject non-ASCII / unicode (covers homoglyph keys like Cyrillic 'A').
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return False, "path contains non-ASCII characters"

    # Strip query string before any other normalisation.
    path_no_query = path.split("?", 1)[0].split("#", 1)[0]

    # Reject path traversal and duplicate slashes.
    if ".." in path_no_query.split("/"):
        return False, "path contains '..' segment"
    # Catch duplicate slashes BEFORE stripping leading/trailing ones so
    # ``//issue/FOO-1`` — which would normalise to a valid path — is still
    # rejected.
    if "//" in path_no_query:
        return False, "path contains duplicate slashes"
    stripped = path_no_query.strip("/")
    if not stripped:
        return False, "path is empty after normalisation"

    # Reject any path segment that matches a denied write verb.
    for segment in stripped.split("/"):
        if segment in JIRA_WRITE_VERBS_DENIED:
            return False, f"path segment '{segment}' is a denied write verb"

    for pattern in JIRA_API_ALLOWED_PATHS:
        if pattern.fullmatch(stripped):
            return True, ""

    return False, f"path '{stripped}' not in allowlist"


def validate_fields(fields: Any) -> list[str]:
    """Validate and normalise a ``fields`` list.

    Args:
        fields: Either ``None`` (callers treat as empty) or a list/tuple of
            Jira field names.

    Returns:
        A list of validated field strings (empty if ``fields`` was ``None``).

    Raises:
        ValueError: If the list exceeds ``MAX_FIELDS`` entries or any entry
            fails the ``_FIELD_NAME_RE`` regex.
    """
    if fields is None:
        return []
    if not isinstance(fields, (list, tuple)):
        raise ValueError("fields must be a list of strings")
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"fields exceeds maximum of {MAX_FIELDS} entries")
    cleaned: list[str] = []
    for entry in fields:
        if not isinstance(entry, str):
            raise ValueError("fields entries must be strings")
        if not _FIELD_NAME_RE.fullmatch(entry):
            raise ValueError(f"invalid field name: {entry!r}")
        cleaned.append(entry)
    return cleaned


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------


@dataclass
class JiraClient:
    """Thin REST-API wrapper around Atlassian Cloud.

    The client is deliberately class-shaped (and not a bag of module-level
    helpers) so that v1.1 multi-site support (refine decision #10) is a
    single-file drop-in: wire a second instance with its own
    ``creds_provider`` / ``http_client`` and the route layer can pick between
    them without refactoring the read paths.
    """

    creds_provider: Any = get_jira_credentials
    http_client: httpx.Client | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def _client(self) -> httpx.Client:
        """Return the underlying httpx client, creating one on first use."""
        if self.http_client is None:
            self.http_client = httpx.Client(timeout=self.timeout_seconds)
        return self.http_client

    def _build_url(self, creds: JiraCredentials, path: str) -> str:
        """Compose the full Atlassian REST URL for a relative path."""
        return f"{creds.base_url}/rest/api/3/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a single REST call with Basic auth + one 429-retry.

        Retry is GET-only.  For any non-GET method the caller gets whatever
        Atlassian returned on the first try — preserving future-safety if
        someone widens ``ALLOWED_METHODS``.
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
            # Import lazily — audit_log lives in gateway.py which imports us.
            # ``audit_log`` dereferences ``flask.request``, so we can only call
            # it inside a request context; a future batch/worker use of this
            # client (outside Flask) must not crash here.
            try:
                from flask import has_request_context
            except ImportError:  # pragma: no cover — flask is a hard dep

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
                        "jira_upstream_rate_limited",
                        "jira_request",
                        success=False,
                        details={
                            "path": path,
                            "attempt": attempt,
                            "retry_after": retry_after,
                        },
                    )
                except Exception:  # pragma: no cover – defensive
                    logger.exception("audit_log failed in jira _request")
            else:
                logger.warning(
                    "Jira upstream 429",
                    path=path,
                    attempt=attempt,
                    retry_after=retry_after,
                )

            if attempt == 1 or not retryable:
                return response
            time.sleep(retry_after)

        # Defensive — loop always returns above.
        return response  # pragma: no cover

    # -- Public verbs ---------------------------------------------------------

    def get_ticket(
        self,
        key: str,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch a single Jira issue.

        Returns the parsed JSON body on 2xx, or a 404 envelope
        (``{"status": "not_found", ...}``) when the issue does not exist.
        """
        query: dict[str, Any] = {}
        expand_values: list[str] = (
            list(DEFAULT_EXPAND) if expand is None else [str(v) for v in expand]
        )
        if expand_values:
            query["expand"] = ",".join(expand_values)
        if fields:
            query["fields"] = ",".join(fields)
        response = self._request("GET", f"issue/{key}", query=query or None)
        if response.status_code == 404:
            return _not_found_envelope(key)
        _raise_for_status(response, f"issue/{key}")
        return _safe_json(response, f"issue/{key}")

    def get_comments(self, key: str) -> dict[str, Any]:
        """Fetch the comment list for an issue (renderedBody included).

        Same 404 semantics as ``get_ticket``.
        """
        response = self._request(
            "GET",
            f"issue/{key}/comment",
            query={"expand": "renderedBody"},
        )
        if response.status_code == 404:
            return _not_found_envelope(key)
        _raise_for_status(response, f"issue/{key}/comment")
        return _safe_json(response, f"issue/{key}/comment")

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        next_page_token: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Run a JQL query via ``POST /rest/api/3/search/jql``.

        Uses Atlassian's cursor pagination: pass ``next_page_token`` from the
        previous response to fetch the next page.
        """
        if not isinstance(jql, str) or not jql.strip():
            raise ValueError("jql is required")
        effective_max = (
            DEFAULT_MAX_RESULTS if max_results is None else min(int(max_results), HARD_MAX_RESULTS)
        )
        if effective_max <= 0:
            effective_max = DEFAULT_MAX_RESULTS
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": effective_max,
        }
        if fields:
            body["fields"] = list(fields)
        if next_page_token:
            body["nextPageToken"] = next_page_token

        response = self._request("POST", "search/jql", body=body)
        _raise_for_status(response, "search/jql")
        return _safe_json(response, "search/jql")

    def execute_raw(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pass-through for the ``/api/v1/jira/execute`` route.

        Callers must have already validated ``path``/``method`` via
        ``validate_jira_api_path``.  Raises ``JiraUpstreamError`` on any
        non-2xx status (including 404 — execute is not a resource-lookup
        endpoint, so "not found" is a real error here).
        """
        response = self._request(method, path, query=query, body=body)
        _raise_for_status(response, path)
        return _safe_json(response, path)

    # -- Write verbs (issue #1924) ------------------------------------------
    #
    # These methods bypass ``validate_jira_api_path`` — their REST paths are
    # construction-time constants (``"issue"``, ``"issue/<key>"``,
    # ``"issue/<key>/comment"``, ``"issueLink"``) so the path-segment
    # denylist (``transitions``, ``worklog``, ``attachments``, ``watchers``)
    # cannot be reached by construction.  They build the Atlassian-shaped
    # body dict, ADF-wrap rich-text fields, and consult
    # ``jira_idempotency.get_or_run`` when the caller passes
    # ``idempotency_key``.

    def create_issue(
        self,
        *,
        project_key: str,
        issuetype: str | int,
        summary: str,
        description: str | dict[str, Any] | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
        epic_link: str | None = None,
        epic_link_field: str = "parent",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Jira issue.

        Builds the Atlassian REST body — ``fields.project.key=project_key``,
        ``fields.issuetype.{name|id}``, ``fields.summary``,
        ``fields.description=ADF`` (text wrapped, dict passed through),
        ``fields.labels``, and either ``fields.parent.key`` or
        ``fields.customfield_10014`` for epic-link dispatch (decision-2
        default).

        Args:
            project_key: Atlassian project key (e.g. ``"ENG"``) the new
                ticket belongs to.
            issuetype: Either a name (``"Task"``) or numeric ID
                (``10001``).  Strings that parse as ints are treated as
                IDs, matching Atlassian's wire shape (``{"id": "10001"}``).
            summary: Required, ≤ ``JIRA_SUMMARY_MAX_CHARS`` chars (route
                layer enforces; method does not re-check).
            description: Optional rich-text body.  Plain strings are wrapped
                via ``jira_adf.wrap_text_as_adf``; dicts that pass
                ``jira_adf.is_adf_dict`` go through unchanged.  ``None``
                omits the field entirely.
            labels: Optional list of label strings.  ``None`` omits.
            parent: Optional parent ticket key.  When set, emits
                ``fields.parent={"key": parent}``.
            epic_link: Optional epic ticket key.  When set, routed via
                ``epic_link_field`` (decision-2): ``"parent"`` →
                ``fields.parent.key``; ``"customfield_10014"`` →
                ``fields.customfield_10014``.  Never both.
            epic_link_field: ``"parent"`` (next-gen / company-managed
                projects, default) or ``"customfield_10014"`` (classic /
                team-managed).  Loaded from ``JiraPolicy.epic_link_field``
                at the route layer.
            idempotency_key: Optional caller-supplied dedup token.  When
                present, the cache in ``jira_idempotency`` is consulted
                before issuing the upstream call.

        Returns:
            Atlassian's parsed response dict (typically
            ``{"id", "key", "self"}`` — the route layer wraps it in the
            ``{key, id, browse_url, status: "created"}`` envelope).

        Raises:
            JiraUpstreamError: On any non-2xx upstream status.
            ValueError: If ``epic_link_field`` is unknown.
        """
        if epic_link_field not in ("parent", "customfield_10014"):
            raise ValueError(
                f"epic_link_field must be 'parent' or 'customfield_10014', "
                f"got {epic_link_field!r}"
            )

        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": _build_issuetype_field(issuetype),
            "summary": summary,
        }
        if description is not None:
            fields["description"] = _coerce_adf(description)
        if labels:
            fields["labels"] = list(labels)

        # Epic-link dispatch.  ``parent`` provided explicitly always wins
        # for ``fields.parent`` — the route layer rejects calls that pass
        # both ``parent`` AND ``epic_link`` so we never end up emitting two
        # conflicting fields here.
        if parent is not None:
            fields["parent"] = {"key": parent}
        if epic_link is not None:
            if epic_link_field == "parent":
                fields["parent"] = {"key": epic_link}
            else:  # customfield_10014
                fields["customfield_10014"] = epic_link

        request_body: dict[str, Any] = {"fields": fields}

        def _do_call() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", "issue", body=request_body)
            _raise_for_status(response, "issue")
            return response.status_code, _safe_json(response, "issue")

        _, body = jira_idempotency.get_or_run(
            "create",
            project_key,
            idempotency_key,
            _do_call,
        )
        return body

    def edit_issue(
        self,
        *,
        key: str,
        summary: str | None = None,
        description: str | dict[str, Any] | None = None,
        labels: list[str] | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        notify_users: bool = False,
    ) -> None:
        """Edit an existing Jira issue (``PUT /rest/api/3/issue/{key}``).

        Two label modes — mutually exclusive at the method level:

        - **Replace mode**: pass ``labels=[...]`` to overwrite the entire
          list.  Emits ``{"fields": {"labels": [...]}}``.
        - **Incremental mode**: pass ``add_labels=[...]`` and / or
          ``remove_labels=[...]`` to mutate the existing list.  Emits
          ``{"update": {"labels": [{"add": "x"}, ..., {"remove": "y"},
          ...]}}``.

        Combining ``labels`` with either ``add_labels`` / ``remove_labels``
        raises ``ValueError`` — the route layer (Phase 3) returns 400 for
        the same case before calling this method.

        Atlassian defaults ``notifyUsers`` to ``true``; we omit the query
        parameter entirely in that case to keep the URL clean.  When
        ``notify_users=False`` we explicitly send ``notifyUsers=false`` —
        decision-5 default for SDLC writes.

        Args:
            key: Ticket key to edit.
            summary: New summary, or ``None`` to leave unchanged.
            description: New description (text or ADF dict), or ``None`` to
                leave unchanged.
            labels: Replace-mode label list.
            add_labels: Incremental add list.
            remove_labels: Incremental remove list.
            notify_users: Whether Atlassian should email watchers
                (default: ``False`` per decision-5).

        Returns:
            ``None`` — Atlassian responds with HTTP 204 No Content on
            success.

        Raises:
            ValueError: If both replace-mode and incremental-mode label
                arguments are supplied.
            JiraUpstreamError: On any non-2xx upstream status.
        """
        replace_mode = labels is not None
        incremental_mode = (add_labels is not None) or (remove_labels is not None)
        if replace_mode and incremental_mode:
            raise ValueError(
                "edit_issue: pass either 'labels' (replace mode) OR "
                "'add_labels' / 'remove_labels' (incremental mode), not both"
            )

        body: dict[str, Any] = {}
        fields_block: dict[str, Any] = {}
        if summary is not None:
            fields_block["summary"] = summary
        if description is not None:
            fields_block["description"] = _coerce_adf(description)
        if replace_mode:
            fields_block["labels"] = list(labels) if labels else []
        if fields_block:
            body["fields"] = fields_block

        if incremental_mode:
            label_ops: list[dict[str, str]] = []
            for label in add_labels or []:
                label_ops.append({"add": label})
            for label in remove_labels or []:
                label_ops.append({"remove": label})
            body["update"] = {"labels": label_ops}

        # Empty body — caller asked for a no-op edit.  We could short-circuit
        # but Atlassian itself rejects empty edits with 400, so let the
        # upstream surface the error rather than silently succeeding.

        query: dict[str, Any] | None = None
        if not notify_users:
            query = {"notifyUsers": "false"}

        response = self._request("PUT", f"issue/{key}", query=query, body=body)
        _raise_for_status(response, f"issue/{key}")
        # 204 No Content on success — no body to parse.

    def add_comment(
        self,
        *,
        key: str,
        body: str | dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add a comment to an existing Jira issue.

        ``body`` can be plain text (auto-wrapped to ADF) or a pre-built ADF
        dict (passed through).  Visibility (``visibility.type``,
        ``visibility.value``) is intentionally not exposed — decision-6
        default for v1.

        Args:
            key: Ticket key the comment lands on.
            body: Comment body — plain string or ADF dict.
            idempotency_key: Optional dedup token; cached in
                ``jira_idempotency`` under
                ``("comment", key, idempotency_key)``.

        Returns:
            Atlassian's parsed response dict for the new comment (id,
            author, created, body, etc.).

        Raises:
            JiraUpstreamError: On any non-2xx upstream status.
        """
        request_body = {"body": _coerce_adf(body)}

        def _do_call() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", f"issue/{key}/comment", body=request_body)
            _raise_for_status(response, f"issue/{key}/comment")
            return response.status_code, _safe_json(response, f"issue/{key}/comment")

        # Cache key namespace = ticket key.  Two callers re-using the same
        # opaque idempotency key against different tickets do not collide.
        _, response_body = jira_idempotency.get_or_run(
            "comment",
            key,
            idempotency_key,
            _do_call,
        )
        return response_body

    def create_issue_link(
        self,
        *,
        link_type: str,
        inward_key: str,
        outward_key: str,
        comment: str | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue link between two tickets.

        Atlassian does NOT dedupe identical ``(inward, outward, type)``
        triples — the same call repeated five times creates five identical
        links.  Decision-28 mandates we extend the idempotency cache to
        this verb so the gateway-side cache is the dedup boundary.

        Args:
            link_type: Atlassian link-type name (``"Blocks"``, ``"Relates"``,
                etc.).  The route-layer schema validates this against the
                operator-configurable allowlist (decision-4).
            inward_key: Ticket key on the inward side of the link.
            outward_key: Ticket key on the outward side of the link.
            comment: Optional comment to attach to the link, surfaced via
                Atlassian's ``comment`` field on ``issueLink`` (decision-23
                — single round-trip with idempotency).
            idempotency_key: Optional dedup token; cached under
                ``("link", canonical_link_id(inward, outward, type),
                idempotency_key)`` so two callers re-using the same opaque
                key against different triples never alias.

        Returns:
            Atlassian's parsed response dict.  In practice
            ``POST /rest/api/3/issueLink`` returns 201 with an empty body;
            ``_safe_json`` wraps that as ``{}`` so the route layer can
            still build a structured envelope.

        Raises:
            JiraUpstreamError: On any non-2xx upstream status.
        """
        body: dict[str, Any] = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        if comment is not None:
            body["comment"] = {"body": _coerce_adf(comment)}

        def _do_call() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", "issueLink", body=body)
            _raise_for_status(response, "issueLink")
            # ``POST /rest/api/3/issueLink`` returns 201 with an empty body;
            # ``_safe_json`` raises on empty responses, so handle that here.
            if not response.content:
                return response.status_code, {}
            return response.status_code, _safe_json(response, "issueLink")

        cache_namespace = jira_idempotency.canonical_link_id(
            inward_key, outward_key, link_type
        )
        _, response_body = jira_idempotency.get_or_run(
            "link",
            cache_namespace,
            idempotency_key,
            _do_call,
        )
        return response_body


# -----------------------------------------------------------------------------
# Helpers & module-level singleton
# -----------------------------------------------------------------------------


def _not_found_envelope(key: str) -> dict[str, Any]:
    """Canonical ``not_found`` envelope used by ticket-read endpoints."""
    return {"status": "not_found", "key": key, "upstream_status": 404}


def _coerce_adf(value: str | dict[str, Any]) -> dict[str, Any]:
    """Return ADF — wrap a plain string, pass through a valid ADF dict.

    Anything else raises ``ValueError``; the route layer body-validates
    these inputs first, so this is a defence-in-depth check.
    """
    if isinstance(value, str):
        return jira_adf.wrap_text_as_adf(value)
    if jira_adf.is_adf_dict(value):
        return value
    raise ValueError(
        "rich-text body must be a string or a valid ADF document dict"
    )


def _build_issuetype_field(issuetype: str | int) -> dict[str, str]:
    """Translate an issuetype into the Atlassian wire shape.

    - Integer (or all-digit string) → ``{"id": "<n>"}``.
    - Non-numeric string → ``{"name": <s>}``.

    Raises ``ValueError`` for empty / wrong-typed inputs so the route
    layer sees a clean 400 instead of a 500 from Atlassian.
    """
    if isinstance(issuetype, bool):
        # ``bool`` is a subclass of ``int`` — explicitly reject before the
        # numeric branch below claims it.
        raise ValueError("issuetype must be a string name or numeric id")
    if isinstance(issuetype, int):
        return {"id": str(issuetype)}
    if isinstance(issuetype, str):
        cleaned = issuetype.strip()
        if not cleaned:
            raise ValueError("issuetype must not be empty")
        if cleaned.isdigit():
            return {"id": cleaned}
        return {"name": cleaned}
    raise ValueError("issuetype must be a string name or numeric id")


def _raise_for_status(response: httpx.Response, path: str) -> None:
    """Raise ``JiraUpstreamError`` if the response is not a 2xx."""
    if 200 <= response.status_code < 300:
        return
    body: Any
    try:
        body = response.json()
    except Exception:
        body = response.text
    raise JiraUpstreamError(response.status_code, body, path)


def _safe_json(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a 2xx JSON response or raise a structured upstream error."""
    try:
        data = response.json()
    except Exception as exc:  # pragma: no cover — Atlassian always returns JSON
        raise JiraUpstreamError(response.status_code, response.text, path) from exc
    if not isinstance(data, dict):
        # Jira v3 always returns an object at the top; wrap anything else so
        # callers can rely on a dict.
        return {"data": data}
    return data


def _parse_retry_after(value: str | None) -> int:
    """Parse a ``Retry-After`` header value to an integer number of seconds.

    Falls back to ``_DEFAULT_RETRY_AFTER_SECONDS`` for missing / malformed
    inputs, and clamps the upper end to ``_RETRY_AFTER_CAP_SECONDS`` so a
    pathological header can't block a gateway worker for minutes.
    """
    if value is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    if parsed <= 0:
        return _DEFAULT_RETRY_AFTER_SECONDS
    return min(parsed, _RETRY_AFTER_CAP_SECONDS)


# Module-level singleton — mirrors ``github_client.get_github_client``.
_jira_client: JiraClient | None = None
_jira_client_lock = threading.Lock()


def get_jira_client() -> JiraClient:
    """Return the process-wide ``JiraClient`` singleton."""
    global _jira_client
    with _jira_client_lock:
        if _jira_client is None:
            _jira_client = JiraClient()
        return _jira_client


def reset_jira_client() -> None:
    """Drop the module-level singleton (test helper)."""
    global _jira_client
    with _jira_client_lock:
        _jira_client = None


# Re-export for convenience — callers can ``from jira_client import ...``.
__all__ = [
    "ALLOWED_METHODS",
    "DEFAULT_EXPAND",
    "DEFAULT_JIRA_LINK_TYPES",
    "DEFAULT_MAX_RESULTS",
    "HARD_MAX_RESULTS",
    "JIRA_API_ALLOWED_PATHS",
    "JIRA_COMMENT_MAX_CHARS",
    "JIRA_DESCRIPTION_MAX_CHARS",
    "JIRA_LABEL_MAX_CHARS",
    "JIRA_LABEL_MAX_COUNT",
    "JIRA_SUMMARY_MAX_CHARS",
    "JIRA_WRITE_VERBS_DENIED",
    "JiraClient",
    "JiraCredentials",
    "JiraCredentialsUnavailable",
    "JiraUpstreamError",
    "MAX_FIELDS",
    "get_jira_client",
    "reset_jira_client",
    "validate_fields",
    "validate_jira_api_path",
]
