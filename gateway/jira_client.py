"""
Jira REST API client for the gateway sidecar.

Provides a thin wrapper around the Atlassian Cloud REST API v3.  All traffic
originates from the gateway (never from the sandbox) and is authenticated
with Basic auth using credentials loaded from ``gateway/jira_credentials.py``.

Public surface (used by ``/api/v1/jira/*`` routes in ``gateway.py``):

Read verbs (v1 — issue #1556):

- ``JiraClient.get_ticket(key, fields=None)`` — ``GET /rest/api/3/issue/{key}``
  with ``expand=renderedBody,renderedFields`` by default so agents receive the
  Atlassian-rendered HTML alongside the raw Atlassian Document Format JSON.
- ``JiraClient.search(jql, fields=None, next_page_token=None, max_results=None)``
  — ``POST /rest/api/3/search/jql`` (cursor pagination via ``nextPageToken``).
- ``JiraClient.get_comments(key)`` — ``GET /rest/api/3/issue/{key}/comment``.
- ``JiraClient.execute_raw(method, path, query=None, body=None)`` — passthrough
  used by ``/api/v1/jira/execute`` for read-only API endpoints.

Write verbs (v1.1 — issue #1924):

- ``JiraClient.create_issue(...)`` — ``POST /rest/api/3/issue``.
- ``JiraClient.edit_issue(...)`` — ``PUT /rest/api/3/issue/{key}``.
- ``JiraClient.add_comment(...)`` — ``POST /rest/api/3/issue/{key}/comment``.
- ``JiraClient.create_issue_link(...)`` — ``POST /rest/api/3/issueLink``.

Path safety:

- ``validate_jira_api_path(path, method)`` enforces a regex allowlist of the
  read-only REST paths permitted via the ``/api/v1/jira/execute`` passthrough
  route — and **only** that route.  ``ALLOWED_METHODS`` stays
  ``frozenset({"GET"})`` so the passthrough remains read-only forever.  The
  write verbs (``create_issue`` / ``edit_issue`` / ``add_comment`` /
  ``create_issue_link``) call ``_request`` directly with hardcoded paths and
  do **not** consult ``validate_jira_api_path`` — they have their own narrow
  body schemas + project allowlist enforced at the route layer in
  ``gateway.py``.  Write verbs are still subject to the path-segment
  denylist below: even an admin who relaxes ``ALLOWED_METHODS`` can't reach
  ``transitions`` / ``worklog`` / ``attachments`` / ``watchers`` via
  ``/execute``.

429 handling (refine Q5, architect D7):

- GET requests retry at most once on HTTP 429, honoring ``Retry-After`` up to
  30s.  Write verbs never retry (future-safety + at-most-once semantics for
  upstream Atlassian).  Both paths emit the ``jira_upstream_rate_limited``
  audit event so operators see 429s on writes too (feedback Q1, decision-16
  symmetry).

404 envelope (refine Q8, architect D8):

- ``get_ticket`` and ``get_comments`` translate upstream 404 into a structured
  ``{"status": "not_found", "key": key, "upstream_status": 404}`` dict so the
  route returns HTTP 200 with a semantic body instead of a raw error.  Other
  endpoints still raise ``JiraUpstreamError``.

Field validation:

- ``validate_fields`` caps the list at 32 entries and requires each to match
  ``^[a-zA-Z_][a-zA-Z0-9_.-]*$`` — applied at the route layer before calling
  the client.
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
    from .jira_adf import is_adf_dict, wrap_text_as_adf
    from .jira_credentials import (
        JiraCredentials,
        JiraCredentialsUnavailable,
        get_jira_credentials,
    )
    from .jira_idempotency import get_or_run as _idempotency_get_or_run
except ImportError:
    # Flat-module test loading: ``gateway/tests/conftest.py`` strips
    # ``__package__`` so relative imports fail.  Make sure the gateway
    # directory is on ``sys.path`` so the absolute fallbacks below resolve
    # ``jira_adf.py`` / ``jira_idempotency.py`` even when conftest doesn't
    # preload them (the new modules in #1924 are NOT pre-registered there).
    _gateway_dir = str(Path(__file__).parent)
    if _gateway_dir not in sys.path:
        sys.path.insert(0, _gateway_dir)
    from jira_adf import (  # type: ignore[no-redef, import-untyped]
        is_adf_dict,
        wrap_text_as_adf,
    )
    from jira_credentials import (  # type: ignore[no-redef, import-untyped]
        JiraCredentials,
        JiraCredentialsUnavailable,
        get_jira_credentials,
    )
    from jira_idempotency import (  # type: ignore[no-redef, import-untyped]
        get_or_run as _idempotency_get_or_run,
    )

logger = get_logger("gateway.jira-client")


# -----------------------------------------------------------------------------
# Constants & validation helpers
# -----------------------------------------------------------------------------

# Allowed HTTP methods for the ``/api/v1/jira/execute`` passthrough route.
# Stays read-only **forever** — the dedicated write verbs (``create_issue``,
# ``edit_issue``, ``add_comment``, ``create_issue_link``) bypass
# ``validate_jira_api_path`` entirely and call ``_request`` directly with
# hardcoded paths.  Operators who want writes go through those routes;
# nothing should route POST/PUT/PATCH/DELETE through ``/execute``.
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
    # Issue #1557 slice-2 — read-only ``GET /rest/api/3/issue/{key}/
    # remotelink`` for the in-flight PR detection signal (decision-7
    # signal b). Stays inside the GET-only ``ALLOWED_METHODS`` plus
    # the ``JIRA_WRITE_VERBS_DENIED`` segment list, so POST / PUT /
    # DELETE on this path remain rejected.
    re.compile(rf"^issue/{_TICKET_KEY}/remotelink$"),
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

    Used **only** by the ``/api/v1/jira/execute`` passthrough route — the
    dedicated write verbs (``create_issue`` / ``edit_issue`` / ``add_comment``
    / ``create_issue_link``) call ``JiraClient._request`` directly with
    hardcoded paths and do not consult this validator.  The denylist still
    applies: even those write methods cannot reach a denied path segment
    (``transitions``, ``worklog``, ``attachments``, ``watchers``) because
    the gateway never composes such a path; and ``/execute`` will reject
    any request whose path or method touches the denylist.

    Normalizes the path (strips leading/trailing slashes, drops query string,
    rejects ``..`` segments, rejects duplicate slashes, rejects non-ASCII
    characters) before checking the regex allowlist.

    Args:
        path: REST path relative to ``/rest/api/3/`` (e.g. ``issue/FOO-1``).
        method: HTTP method (``GET`` is the only one allowed via execute).

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
        Atlassian returned on the first try — at-most-once semantics for
        write verbs.  All 429 responses (read **and** write) emit the
        ``jira_upstream_rate_limited`` audit event so operators see write
        rate-limit events even though writes don't auto-retry (refine
        feedback Q1).
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
            _emit_rate_limited_audit(
                path=path, method=method, attempt=attempt, retry_after=retry_after
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

    def get_remotelinks(self, key: str) -> dict[str, Any]:
        """Fetch the remote-link list for an issue (issue #1557 slice-2).

        Used by the reassess sweep's in-flight classifier (decision-7
        signal b) — a child epic ticket whose remote-link list
        includes a ``github.com/.../pull/<N>`` URL is treated as
        in-flight regardless of its Atlassian status. Same 404
        semantics as ``get_ticket`` / ``get_comments``.

        Atlassian returns a bare list at the top level for this
        endpoint; ``_safe_json`` re-wraps it as ``{"data": [...]}``
        for caller uniformity. We re-key the wrapper to
        ``{"remotelinks": [...]}`` so the gateway route emits a
        consistent envelope downstream agents and the reassess sweep
        consume.
        """
        response = self._request("GET", f"issue/{key}/remotelink")
        if response.status_code == 404:
            return _not_found_envelope(key)
        _raise_for_status(response, f"issue/{key}/remotelink")
        body = _safe_json(response, f"issue/{key}/remotelink")
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return {"remotelinks": body["data"]}
        if isinstance(body, list):  # pragma: no cover — _safe_json wraps lists
            return {"remotelinks": body}
        return body

    def transition_issue(
        self,
        key: str,
        *,
        transition_id: str | None = None,
        transition_name: str | None = None,
        comment_adf: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """``POST /rest/api/3/issue/{key}/transitions`` — issue #1557 slice-2.

        **Internal-only**: the public agent-facing surface continues to
        deny transitions via :data:`JIRA_WRITE_VERBS_DENIED`. The
        gateway's orchestrator-only ``/api/v1/jira/ticket/transition``
        route (added with loopback + shared-secret check) is the sole
        caller. The path is composed in-method so even if the regex
        allowlist is widened the agent-facing routes still can't
        compose this URL.

        Args:
            key: Atlassian issue key.
            transition_id: Numeric transition ID. Either this or
                ``transition_name`` must be supplied; ID wins.
            transition_name: Human-readable transition name (e.g.
                ``"Won't Do"``). The method looks up the matching
                transition ID by calling Atlassian's
                ``GET /issue/{key}/transitions`` first.
            comment_adf: Optional ADF comment body posted as part of
                the transition payload. Forwarded verbatim to
                Atlassian.

        Returns
        -------
        (status_code, body)
            Status code and decoded JSON body of the
            ``transitions`` POST. Atlassian returns 204 on success
            with an empty body.
        """
        if not transition_id and not transition_name:
            raise ValueError("transition_id or transition_name is required")
        resolved_id = transition_id
        if not resolved_id and transition_name:
            # Look up the transition ID by name.
            list_resp = self._request("GET", f"issue/{key}/transitions")
            _raise_for_status(list_resp, f"issue/{key}/transitions")
            list_body = _safe_json(list_resp, f"issue/{key}/transitions")
            target_norm = transition_name.strip().lower()
            transitions = list_body.get("transitions") if isinstance(list_body, dict) else None
            if not isinstance(transitions, list):
                raise JiraUpstreamError(
                    500, list_body, f"issue/{key}/transitions"
                )
            for entry in transitions:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name.strip().lower() == target_norm:
                    resolved_id = str(entry.get("id"))
                    break
            if not resolved_id:
                raise JiraUpstreamError(
                    404,
                    {"reason": f"transition {transition_name!r} not available on {key}"},
                    f"issue/{key}/transitions",
                )

        payload: dict[str, Any] = {
            "transition": {"id": str(resolved_id)},
        }
        if comment_adf is not None:
            payload["update"] = {"comment": [{"add": {"body": comment_adf}}]}

        response = self._request(
            "POST", f"issue/{key}/transitions", body=payload
        )
        if response.status_code in (200, 204):
            return response.status_code, {}
        _raise_for_status(response, f"issue/{key}/transitions")
        return response.status_code, _safe_json(
            response, f"issue/{key}/transitions"
        )

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

    # -- Write verbs (issue #1924) -------------------------------------------
    #
    # The four methods below bypass ``validate_jira_api_path`` entirely;
    # their target paths are hardcoded so an attacker can't smuggle a
    # different verb through them.  Body schemas are validated upstream by
    # the route handlers in ``gateway.py``; this layer is structural only.

    def create_issue(
        self,
        *,
        project_key: str,
        issuetype: dict[str, Any] | str,
        summary: str,
        description: str | dict[str, Any] | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
        epic_link: str | None = None,
        epic_link_field: str = "parent",
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any], bool]:
        """``POST /rest/api/3/issue`` — create a new ticket.

        Args:
            project_key: Atlassian project key (``"ENG"``).  Caller must have
                already verified this against the project allowlist.
            issuetype: Either a string (treated as ``{"name": value}``) or a
                pre-shaped dict (``{"id": "10001"}`` / ``{"name": "Task"}``).
            summary: Plain-text summary (≤ 255 chars — caller enforces).
            description: ``None``, plain text, or a pre-built ADF dict.
                Plain strings get wrapped via ``wrap_text_as_adf``.
            labels: Optional flat list of label strings.
            parent: Optional parent ticket key (e.g. for sub-tasks).  Mutually
                exclusive with ``epic_link`` at the route layer.
            epic_link: Optional epic ticket key.  Routed via
                ``epic_link_field`` (``"parent"`` for next-gen / company-
                managed projects, ``"customfield_10014"`` for classic /
                team-managed).
            epic_link_field: Which Atlassian field carries the epic link
                for this site — comes from ``JiraPolicy.epic_link_field``.
            idempotency_key: Optional opaque dedup key.  When set, the
                response is replayed for repeat calls within
                ``IDEMPOTENCY_TTL_SECONDS``.

        Returns:
            ``(status_code, response_json)`` — the route layer normalises
            this into the public envelope.
        """
        if isinstance(issuetype, str):
            issuetype_block = {"name": issuetype}
        elif isinstance(issuetype, dict):
            issuetype_block = dict(issuetype)
        else:
            raise ValueError("issuetype must be a string or dict")

        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": issuetype_block,
        }

        if description is not None:
            fields["description"] = (
                description if is_adf_dict(description) else wrap_text_as_adf(str(description))
            )

        if labels:
            fields["labels"] = list(labels)

        if parent and epic_link:
            # Defence in depth — the route layer rejects this, but if
            # something slips through we refuse rather than send Atlassian
            # an ambiguous body.
            raise ValueError("parent and epic_link are mutually exclusive")

        if parent:
            fields["parent"] = {"key": parent}
        elif epic_link:
            if epic_link_field == "parent":
                fields["parent"] = {"key": epic_link}
            else:
                fields[epic_link_field] = epic_link

        request_body: dict[str, Any] = {"fields": fields}

        def _do_request() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", "issue", body=request_body)
            _raise_for_status(response, "issue")
            return response.status_code, _safe_json(response, "issue")

        return _idempotency_get_or_run(
            "jira_ticket_create",
            project_key,
            idempotency_key,
            _do_request,
        )

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
    ) -> dict[str, Any]:
        """``PUT /rest/api/3/issue/{key}`` — partial update.

        Atlassian supports two label-modification modes which **cannot**
        coexist on a single request: a full ``fields.labels`` replace, or
        an incremental ``update.labels`` op-list of ``add``/``remove``.
        Callers that mix replace + incremental get a ``ValueError`` here
        (the route layer rejects with 400 first).

        ``notify_users=False`` (refine decision-5 default) sends
        ``?notifyUsers=false`` so an edit doesn't blast every watcher's
        inbox; pass ``True`` explicitly to opt in.

        Returns the upstream ``(status_code, response_json)`` tuple — the
        route layer normalises into the ``{status: "updated", key}``
        envelope.
        """
        replace_labels = labels is not None
        incremental_labels = bool(add_labels) or bool(remove_labels)
        if replace_labels and incremental_labels:
            raise ValueError(
                "labels (replace) and add_labels/remove_labels (incremental) are mutually exclusive"
            )

        fields: dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = (
                description if is_adf_dict(description) else wrap_text_as_adf(str(description))
            )
        if replace_labels:
            fields["labels"] = list(labels or [])

        update: dict[str, list[dict[str, str]]] = {}
        if incremental_labels:
            ops: list[dict[str, str]] = []
            for value in add_labels or []:
                ops.append({"add": value})
            for value in remove_labels or []:
                ops.append({"remove": value})
            update["labels"] = ops

        body: dict[str, Any] = {}
        if fields:
            body["fields"] = fields
        if update:
            body["update"] = update
        if not body:
            raise ValueError("edit_issue requires at least one field to change")

        query: dict[str, Any] | None = None
        if not notify_users:
            query = {"notifyUsers": "false"}

        response = self._request("PUT", f"issue/{key}", query=query, body=body)
        _raise_for_status(response, f"issue/{key}")
        # Atlassian returns 204 No Content on a successful edit; surface
        # an empty dict so route handlers always see a structured value.
        if response.status_code == 204 or not response.content:
            return {}
        return _safe_json(response, f"issue/{key}")

    def add_comment(
        self,
        *,
        key: str,
        body: str | dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any], bool]:
        """``POST /rest/api/3/issue/{key}/comment`` — append a comment.

        ``body`` accepts either a plain string (wrapped via ``wrap_text_as_adf``)
        or a pre-built ADF dict (passed through verbatim).

        ``idempotency_key`` keys the in-memory cache by
        ``(jira_comment_add, ticket_key, idempotency_key)`` so the same
        opaque key against two different tickets — even within the same
        project — is two distinct cache entries.  Project-only namespacing
        was wrong: two agents both choosing
        ``--idempotency-key bisect-start`` against ``ENG-1`` and ``ENG-2``
        would silently replay the first response to the second caller
        (reviewer_code_holistic cycle 1 finding #2, #1924).
        """
        adf_body = body if is_adf_dict(body) else wrap_text_as_adf(str(body))
        request_body = {"body": adf_body}

        def _do_request() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", f"issue/{key}/comment", body=request_body)
            _raise_for_status(response, f"issue/{key}/comment")
            return response.status_code, _safe_json(response, f"issue/{key}/comment")

        return _idempotency_get_or_run(
            "jira_comment_add",
            key,
            idempotency_key,
            _do_request,
        )

    def create_issue_link(
        self,
        *,
        link_type: str,
        inward_key: str,
        outward_key: str,
        comment: str | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any], bool]:
        """``POST /rest/api/3/issueLink`` — link two tickets.

        Atlassian does **not** dedupe identical ``(inward, outward, type)``
        triples — a transient-error retry would create a duplicate link
        (refine Open Q28).  The idempotency cache (decision-28) sidesteps
        this for caller-driven retries.

        The cache key namespaces the opaque ``idempotency_key`` by
        ``(jira_issue_link_create, "<inward>__<outward>__<type>", key)`` so
        the same opaque key against different triples produces distinct
        entries (test ``link_cache_aliasing``).
        """
        request_body: dict[str, Any] = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        if comment is not None:
            adf = comment if is_adf_dict(comment) else wrap_text_as_adf(str(comment))
            request_body["comment"] = {"body": adf}

        # Synthetic project tag — the triple is the policy boundary, not a
        # single project.  Sorting the inward/outward keys alphabetically
        # would dedupe genuine A->B vs B->A triples (different links!) so
        # we keep them in caller order.
        synthetic_project = f"{inward_key}__{outward_key}__{link_type}"

        def _do_request() -> tuple[int, dict[str, Any]]:
            response = self._request("POST", "issueLink", body=request_body)
            _raise_for_status(response, "issueLink")
            # Atlassian returns 201 Created with empty body for issueLink.
            if response.status_code == 201 and not response.content:
                return response.status_code, {}
            return response.status_code, _safe_json(response, "issueLink")

        return _idempotency_get_or_run(
            "jira_issue_link_create",
            synthetic_project,
            idempotency_key,
            _do_request,
        )


# -----------------------------------------------------------------------------
# Helpers & module-level singleton
# -----------------------------------------------------------------------------


def _not_found_envelope(key: str) -> dict[str, Any]:
    """Canonical ``not_found`` envelope used by ticket-read endpoints."""
    return {"status": "not_found", "key": key, "upstream_status": 404}


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


def _emit_rate_limited_audit(
    *,
    path: str,
    method: str,
    attempt: int,
    retry_after: int,
) -> None:
    """Emit the ``jira_upstream_rate_limited`` audit event.

    Lifted out of ``_request`` so it fires for **every** 429 the gateway
    sees — including write verbs that bypass the GET-only retry loop.
    Falls back to a structured log line when called outside a Flask
    request context (e.g. a future batch worker that reuses ``JiraClient``).
    """
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
                    "method": method.upper(),
                    "attempt": attempt,
                    "retry_after": retry_after,
                },
            )
            return
        except Exception:  # pragma: no cover – defensive
            logger.exception("audit_log failed in jira _request")

    logger.warning(
        "Jira upstream 429",
        path=path,
        method=method.upper(),
        attempt=attempt,
        retry_after=retry_after,
    )


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
    except TypeError, ValueError:
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
    "DEFAULT_MAX_RESULTS",
    "HARD_MAX_RESULTS",
    "JIRA_API_ALLOWED_PATHS",
    "JIRA_WRITE_VERBS_DENIED",
    "JiraClient",
    "JiraCredentials",
    "JiraCredentialsUnavailable",
    "JiraUpstreamError",
    "MAX_FIELDS",
    "get_jira_client",
    "is_adf_dict",
    "reset_jira_client",
    "validate_fields",
    "validate_jira_api_path",
    "wrap_text_as_adf",
]
