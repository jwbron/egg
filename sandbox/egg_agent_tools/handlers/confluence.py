"""Confluence handlers — gateway-backed reads for sandbox agents (#2994).

These mirror the verbs in the ``sandbox/scripts/confluence`` shell
wrapper one-for-one, POSTing to the same ``/api/v1/confluence/*`` gateway
routes with the session token :mod:`egg_agent_tools.handlers._gateway`
already resolves.  They hold **no Atlassian credentials** and add **no
new capability** — the gateway still enforces the space allowlist,
read-only policy, CQL scope extraction, response redaction, and the
private-network-mode gate.  This is a presentation layer that makes the
routes discoverable as ``mcp__confluence__*`` tools (the bash wrapper is
prose an agent has to recall; an MCP tool is in the manifest every turn).

Each handler accepts snake_case request keys (the convention every other
egg MCP tool uses) and translates them to the camelCase field names the
gateway expects.  None of these verbs has a Python ``egg-*`` CLI
counterpart the MCP↔CLI drift test can walk — the human-facing analog is
the bash ``confluence`` wrapper, not an argparse parser — so every
registration sets ``cli_command=None`` and each docstring records the
"no CLI" rationale required by the decision-13 drift gate.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers._gateway import gateway_data_request
from egg_agent_tools.handlers.errors import HandlerError


def _as_str_list(value: Any) -> list[str] | None:
    """Normalise a list-or-CSV-string into a clean ``list[str]`` (or None).

    The agent-facing schema asks for an array, but a model occasionally
    passes the gateway's comma-separated form; accept both so a stray
    ``"storage,view"`` doesn't 400 at the gateway.
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        raise HandlerError(f"expected a string or list of strings, got {type(value).__name__}")
    cleaned = [v for v in items if v]
    return cleaned or None


def _require_str(req: dict[str, Any], key: str, label: str) -> str:
    value = req.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HandlerError(f"{label} is required (pass '{key}')")
    return value.strip()


def confluence_page_get(req: dict[str, Any]) -> Any:
    """Fetch a Confluence page by numeric ``page_id`` (no CLI counterpart).

    Mirrors ``confluence page get`` → ``POST /api/v1/confluence/page/get``.
    Optional ``body_format`` (list, default ``storage`` server-side) and
    ``expand`` select the rendition and extra fields.
    """
    body: dict[str, Any] = {"pageId": _require_str(req, "page_id", "page_id")}
    body_format = _as_str_list(req.get("body_format"))
    if body_format:
        body["bodyFormat"] = body_format
    expand = _as_str_list(req.get("expand"))
    if expand:
        body["expand"] = expand
    return gateway_data_request("/api/v1/confluence/page/get", body=body)


def confluence_page_descendants(req: dict[str, Any]) -> Any:
    """List the descendants of a Confluence page (no CLI counterpart).

    Mirrors ``confluence page descendants`` →
    ``POST /api/v1/confluence/page/descendants``.  Paginated via ``limit``
    + opaque ``cursor``; ``depth`` bounds the tree walk.
    """
    body: dict[str, Any] = {"pageId": _require_str(req, "page_id", "page_id")}
    if req.get("depth") is not None:
        body["depth"] = req["depth"]
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    return gateway_data_request("/api/v1/confluence/page/descendants", body=body)


def confluence_page_footer_comments(req: dict[str, Any]) -> Any:
    """Fetch footer comments on a Confluence page (no CLI counterpart).

    Mirrors ``confluence page footer-comments`` →
    ``POST /api/v1/confluence/page/footer-comments``.  Set
    ``include_replies`` to inline threaded replies.
    """
    body: dict[str, Any] = {
        "pageId": _require_str(req, "page_id", "page_id"),
        "includeReplies": bool(req.get("include_replies", False)),
    }
    body_format = _as_str_list(req.get("body_format"))
    if body_format:
        body["bodyFormat"] = body_format
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    return gateway_data_request("/api/v1/confluence/page/footer-comments", body=body)


def confluence_page_inline_comments(req: dict[str, Any]) -> Any:
    """Fetch inline comments on a Confluence page (no CLI counterpart).

    Mirrors ``confluence page inline-comments`` →
    ``POST /api/v1/confluence/page/inline-comments``.  The gateway
    transparently retries against the v1 API when v2 returns 404 and
    flags ``used_fallback`` on the response.
    """
    body: dict[str, Any] = {"pageId": _require_str(req, "page_id", "page_id")}
    body_format = _as_str_list(req.get("body_format"))
    if body_format:
        body["bodyFormat"] = body_format
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    return gateway_data_request("/api/v1/confluence/page/inline-comments", body=body)


def confluence_space_pages(req: dict[str, Any]) -> Any:
    """List pages in a Confluence space (no CLI counterpart).

    Mirrors ``confluence space pages`` →
    ``POST /api/v1/confluence/space/pages``.  The space must be on the
    operator's allowlist or the gateway returns 403.
    """
    body: dict[str, Any] = {"spaceKey": _require_str(req, "space_key", "space_key")}
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    body_format = _as_str_list(req.get("body_format"))
    if body_format:
        body["bodyFormat"] = body_format
    return gateway_data_request("/api/v1/confluence/space/pages", body=body)


def confluence_space_list(req: dict[str, Any]) -> Any:
    """List the Confluence spaces visible to the agent (no CLI counterpart).

    Mirrors ``confluence space list`` →
    ``POST /api/v1/confluence/space/list``.  The response is filtered to
    the operator's allowlist — this is the verb that answers "which
    spaces can I read?" without guessing (the #2994 motivating failure).
    """
    body: dict[str, Any] = {}
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    return gateway_data_request("/api/v1/confluence/space/list", body=body)


def confluence_search(req: dict[str, Any]) -> Any:
    """Run a CQL query (no CLI counterpart).

    Mirrors ``confluence search`` → ``POST /api/v1/confluence/search``.
    The CQL must statically scope to allowlisted spaces; the gateway's
    scope extractor denies on ambiguity (any ``OR`` over ``space``).
    """
    body: dict[str, Any] = {"cql": _require_str(req, "cql", "cql")}
    if req.get("limit") is not None:
        body["limit"] = int(req["limit"])
    if req.get("cursor"):
        body["cursor"] = req["cursor"]
    return gateway_data_request("/api/v1/confluence/search", body=body)


def confluence_execute(req: dict[str, Any]) -> Any:
    """Raw read-only Confluence REST passthrough (no CLI counterpart).

    Mirrors ``confluence execute`` → ``POST /api/v1/confluence/execute``.
    GET-only escape hatch for routes without a dedicated verb; the
    gateway rejects non-GET methods and denied paths with 403.
    """
    body: dict[str, Any] = {
        "method": _require_str(req, "method", "method"),
        "path": _require_str(req, "path", "path"),
    }
    query = req.get("query")
    if query is not None:
        if not isinstance(query, dict):
            raise HandlerError("'query' must be an object of string key/value pairs")
        body["query"] = query
    request_body = req.get("body")
    if request_body is not None:
        body["body"] = request_body
    return gateway_data_request("/api/v1/confluence/execute", body=body)
