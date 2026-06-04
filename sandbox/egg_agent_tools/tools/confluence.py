"""Confluence-namespace @tool wrappers (#2994).

Exposes the gateway-backed ``confluence`` verbs as ``mcp__confluence__*``
MCP tools so the routes are discoverable in the agent's tool manifest
every turn — instead of prose in ``environment.md`` an agent has to
recall.  Every wrapper forwards to a handler in
``egg_agent_tools.handlers.confluence``; the gateway enforces all policy
(space allowlist, read-only, CQL scope, redaction, private-mode gate),
so this layer carries no credentials and adds no capability.

The namespace key is ``confluence`` so the Claude-visible names are
``mcp__confluence__<verb>`` — intentionally matching the *host* MCP
namespace (which is not present in the sandbox), so planner-authored
task text that references ``mcp__confluence__*`` resolves to the
restricted sandbox tools.  These verbs have no ``egg-*`` CLI counterpart
the drift test can walk, so every ``ToolRegistration`` sets
``cli_command=None`` (see #2994 and the handler module docstring).
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import confluence as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "confluence"

_BODY_FORMAT = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Renditions to return, e.g. ['storage'] (default, XHTML-like), "
        "['atlas_doc_format'] (ADF JSON), or ['view'] (rendered HTML)."
    ),
}
_LIMIT = {"type": "integer", "description": "Page size for pagination."}
_CURSOR = {"type": "string", "description": "Opaque pagination cursor from a prior response."}

_PAGE_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {"type": "string", "description": "Numeric Confluence pageId."},
        "body_format": _BODY_FORMAT,
        "expand": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra fields to expand (e.g. ['version','ancestors']).",
        },
    },
    "required": ["page_id"],
}

_PAGE_DESCENDANTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {"type": "string", "description": "Numeric Confluence pageId."},
        "depth": {
            "type": ["integer", "string"],
            "description": "How deep to walk the descendant tree (e.g. 1 or 'all').",
        },
        "limit": _LIMIT,
        "cursor": _CURSOR,
    },
    "required": ["page_id"],
}

_PAGE_FOOTER_COMMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {"type": "string", "description": "Numeric Confluence pageId."},
        "include_replies": {
            "type": "boolean",
            "description": "Inline threaded replies (default false).",
        },
        "body_format": _BODY_FORMAT,
        "limit": _LIMIT,
        "cursor": _CURSOR,
    },
    "required": ["page_id"],
}

_PAGE_INLINE_COMMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {"type": "string", "description": "Numeric Confluence pageId."},
        "body_format": _BODY_FORMAT,
        "limit": _LIMIT,
        "cursor": _CURSOR,
    },
    "required": ["page_id"],
}

_SPACE_PAGES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "space_key": {"type": "string", "description": "Confluence space key (e.g. 'ENG')."},
        "limit": _LIMIT,
        "cursor": _CURSOR,
        "body_format": _BODY_FORMAT,
    },
    "required": ["space_key"],
}

_SPACE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": _LIMIT,
        "cursor": _CURSOR,
    },
}

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cql": {
            "type": "string",
            "description": (
                "CQL query. Must statically scope to allowlisted spaces "
                "(e.g. \"space = ENG AND text ~ 'RFC'\"); an OR over `space` is denied."
            ),
        },
        "limit": _LIMIT,
        "cursor": _CURSOR,
    },
    "required": ["cql"],
}

_EXECUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "description": "HTTP method — GET only (others 403)."},
        "path": {"type": "string", "description": "Confluence REST path (allowlisted)."},
        "query": {"type": "object", "description": "Query-string key/value pairs."},
        "body": {"type": "object", "description": "Request body (rarely needed for reads)."},
    },
    "required": ["method", "path"],
}


@tool(
    "page_get",
    "Fetch a Confluence page by numeric pageId via the gateway "
    "(space-allowlisted, read-only). Use instead of guessing page content.",
    _PAGE_GET_SCHEMA,
)
async def confluence_page_get(args: dict[str, Any]) -> dict[str, Any]:
    # Wiki-heavy pages can cross 1 MB once `body.storage.value` plus
    # expansions are inlined. Spill oversized payloads to a file the agent
    # can Read/grep, matching the checkpoint_show precedent.
    return await invoke_handler(handlers.confluence_page_get, args, spill=True)


@tool(
    "page_descendants",
    "List the descendants of a Confluence page via the gateway. "
    "Paginated via `limit` + opaque `cursor`.",
    _PAGE_DESCENDANTS_SCHEMA,
)
async def confluence_page_descendants(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_page_descendants, args)


@tool(
    "page_footer_comments",
    "Fetch footer comments on a Confluence page via the gateway.",
    _PAGE_FOOTER_COMMENTS_SCHEMA,
)
async def confluence_page_footer_comments(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_page_footer_comments, args)


@tool(
    "page_inline_comments",
    "Fetch inline comments on a Confluence page via the gateway "
    "(transparently falls back to the v1 API if v2 returns 404).",
    _PAGE_INLINE_COMMENTS_SCHEMA,
)
async def confluence_page_inline_comments(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_page_inline_comments, args)


@tool(
    "space_pages",
    "List pages in a Confluence space via the gateway "
    "(space must be allowlisted). Paginated via `limit` + `cursor`.",
    _SPACE_PAGES_SCHEMA,
)
async def confluence_space_pages(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_space_pages, args)


@tool(
    "space_list",
    "List the Confluence spaces visible to the agent via the gateway, "
    "filtered to the operator's allowlist. Use this to discover which "
    "spaces are readable — do NOT guess space keys.",
    _SPACE_LIST_SCHEMA,
)
async def confluence_space_list(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_space_list, args)


@tool(
    "search",
    "Run a CQL query via the gateway. CQL must statically scope to "
    "allowlisted spaces (an OR over `space` is denied). Paginated via "
    "`limit` + `cursor`.",
    _SEARCH_SCHEMA,
)
async def confluence_search(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_search, args)


@tool(
    "execute",
    "Raw read-only Confluence REST passthrough via the gateway (GET-only "
    "escape hatch for routes without a dedicated verb; non-GET returns 403).",
    _EXECUTE_SCHEMA,
)
async def confluence_execute(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.confluence_execute, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__confluence__page_get",
        namespace=NAMESPACE,
        handler=handlers.confluence_page_get,
        sdk_tool=confluence_page_get,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__page_descendants",
        namespace=NAMESPACE,
        handler=handlers.confluence_page_descendants,
        sdk_tool=confluence_page_descendants,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__page_footer_comments",
        namespace=NAMESPACE,
        handler=handlers.confluence_page_footer_comments,
        sdk_tool=confluence_page_footer_comments,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__page_inline_comments",
        namespace=NAMESPACE,
        handler=handlers.confluence_page_inline_comments,
        sdk_tool=confluence_page_inline_comments,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__space_pages",
        namespace=NAMESPACE,
        handler=handlers.confluence_space_pages,
        sdk_tool=confluence_space_pages,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__space_list",
        namespace=NAMESPACE,
        handler=handlers.confluence_space_list,
        sdk_tool=confluence_space_list,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__search",
        namespace=NAMESPACE,
        handler=handlers.confluence_search,
        sdk_tool=confluence_search,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__confluence__execute",
        namespace=NAMESPACE,
        handler=handlers.confluence_execute,
        sdk_tool=confluence_execute,
        cli_command=None,
    ),
]
