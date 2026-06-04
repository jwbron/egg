"""Jira-namespace @tool wrappers (#2994).

Exposes the gateway-backed ``jira`` verbs as ``mcp__jira__*`` MCP tools
so the routes are discoverable in the agent's tool manifest every turn.
Every wrapper forwards to a handler in ``egg_agent_tools.handlers.jira``;
the gateway enforces all policy (project allowlist, the read vs. four-
write-route split, JQL scope, private-mode gate), so this layer carries
no credentials and adds no capability.

The namespace key is ``jira`` so the Claude-visible names are
``mcp__jira__<verb>`` — intentionally matching the *host* MCP namespace
(not present in the sandbox), so planner-authored task text referencing
``mcp__jira__*`` resolves to the restricted sandbox tools.  These verbs
have no ``egg-*`` CLI counterpart the drift test can walk, so every
``ToolRegistration`` sets ``cli_command=None`` (see #2994 and the
handler module docstring).
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import jira as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "jira"

_FIELDS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Field names to return (omit for Atlassian's default set).",
}
_LABELS = {"type": "array", "items": {"type": "string"}, "description": "Label names."}
_IDEMPOTENCY = {
    "type": "string",
    "description": "Idempotency key — makes a retried write safe (no duplicate).",
}

_TICKET_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "description": "Ticket key, e.g. ENG-123."},
        "fields": _FIELDS,
    },
    "required": ["ticket"],
}

_TICKET_KEY_ONLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "description": "Ticket key, e.g. ENG-123."},
    },
    "required": ["ticket"],
}

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "jql": {
            "type": "string",
            "description": (
                "JQL query. Must statically scope to allowlisted projects "
                "(e.g. 'project = ENG AND status = Open'); an OR over `project` is denied."
            ),
        },
        "max_results": {"type": "integer", "description": "Max issues to return."},
        "fields": _FIELDS,
        "next_page_token": {
            "type": "string",
            "description": "Opaque pagination token from a prior response.",
        },
    },
    "required": ["jql"],
}

_TICKET_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "description": "Project key (must be allowlisted)."},
        "issue_type": {"type": "string", "description": "Issue type, e.g. Task / Bug / Story."},
        "summary": {"type": "string", "description": "Ticket summary / title."},
        "description": {"type": "string", "description": "Ticket description (plain text)."},
        "labels": _LABELS,
        "parent": {"type": "string", "description": "Parent issue key (for sub-tasks)."},
        "epic_link": {"type": "string", "description": "Epic key to link under."},
        "idempotency_key": _IDEMPOTENCY,
    },
    "required": ["project", "issue_type", "summary"],
}

_TICKET_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "description": "Ticket key to edit, e.g. ENG-123."},
        "summary": {"type": "string", "description": "New summary."},
        "description": {"type": "string", "description": "New description (plain text)."},
        "labels": {
            **_LABELS,
            "description": "Replace all labels (mutually exclusive with add/remove_labels).",
        },
        "add_labels": {**_LABELS, "description": "Labels to add (incremental)."},
        "remove_labels": {**_LABELS, "description": "Labels to remove (incremental)."},
        "notify_users": {
            "type": "boolean",
            "description": (
                "Send Jira notifications (default true, matching the "
                "sandbox/scripts/jira wrapper; pass false to suppress)."
            ),
        },
    },
    "required": ["ticket"],
}

_TICKET_COMMENT_ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "description": "Ticket key, e.g. ENG-123."},
        "body": {"type": "string", "description": "Comment body (plain text)."},
        "idempotency_key": _IDEMPOTENCY,
    },
    "required": ["ticket", "body"],
}

_LINK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "link_type": {"type": "string", "description": "Jira link name, e.g. Blocks / Relates."},
        "inward_issue": {"type": "string", "description": "Inward issue key, e.g. FOO-1."},
        "outward_issue": {"type": "string", "description": "Outward issue key, e.g. FOO-2."},
        "comment": {"type": "string", "description": "Optional comment to attach to the link."},
        "idempotency_key": _IDEMPOTENCY,
    },
    "required": ["link_type", "inward_issue", "outward_issue"],
}

_EXECUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "description": "HTTP method — GET only (others 403)."},
        "path": {"type": "string", "description": "Jira REST path (allowlisted)."},
        "query": {"type": "object", "description": "Query-string key/value pairs."},
    },
    "required": ["method", "path"],
}


@tool(
    "ticket_get",
    "Fetch a Jira ticket by key (e.g. ENG-123) via the gateway "
    "(project-allowlisted, read). Optional `fields` narrows the result.",
    _TICKET_GET_SCHEMA,
)
async def jira_ticket_get(args: dict[str, Any]) -> dict[str, Any]:
    # A ticket with `expand=renderedBody,renderedFields` (the gateway default)
    # can cross 1 MB on a long-running issue. Spill oversized payloads via
    # the `spill=True` surface in ``tools/_common.py`` so the agent can
    # Read/grep the full payload from disk.
    return await invoke_handler(handlers.jira_ticket_get, args, spill=True)


@tool(
    "ticket_comments",
    "Fetch the comments on a Jira ticket via the gateway.",
    _TICKET_KEY_ONLY_SCHEMA,
)
async def jira_ticket_comments(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_ticket_comments, args)


@tool(
    "ticket_remotelinks",
    "Fetch the remote links on a Jira ticket via the gateway (surfaces "
    "PRs humans opened against the ticket).",
    _TICKET_KEY_ONLY_SCHEMA,
)
async def jira_ticket_remotelinks(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_ticket_remotelinks, args)


@tool(
    "search",
    "Search Jira issues with JQL via the gateway. JQL must statically "
    "scope to allowlisted projects (an OR over `project` is denied). "
    "Paginate with `next_page_token`.",
    _SEARCH_SCHEMA,
)
async def jira_search(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_search, args)


@tool(
    "ticket_create",
    "Create a new Jira ticket via the gateway (project-allowlisted, "
    "private-mode). State-machine effect: creates a new issue. Pass "
    "`idempotency_key` to make a retry safe.",
    _TICKET_CREATE_SCHEMA,
)
async def jira_ticket_create(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_ticket_create, args)


@tool(
    "ticket_edit",
    "Edit a Jira ticket via the gateway. `labels` (replace) is mutually "
    "exclusive with `add_labels`/`remove_labels` (incremental). "
    "State-machine effect: mutates the issue fields in place.",
    _TICKET_EDIT_SCHEMA,
)
async def jira_ticket_edit(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_ticket_edit, args)


@tool(
    "ticket_comment_add",
    "Add a comment to a Jira ticket via the gateway. Pass `idempotency_key` to make a retry safe.",
    _TICKET_COMMENT_ADD_SCHEMA,
)
async def jira_ticket_comment_add(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_ticket_comment_add, args)


@tool(
    "link_create",
    "Link two Jira tickets via the gateway (both projects must be "
    "allowlisted). State-machine effect: creates an issue link.",
    _LINK_CREATE_SCHEMA,
)
async def jira_link_create(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_link_create, args)


@tool(
    "execute",
    "Raw read-only Jira REST passthrough via the gateway (GET-only escape "
    "hatch for routes without a dedicated verb; non-GET returns 403).",
    _EXECUTE_SCHEMA,
)
async def jira_execute(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.jira_execute, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__jira__ticket_get",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_get,
        sdk_tool=jira_ticket_get,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__ticket_comments",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_comments,
        sdk_tool=jira_ticket_comments,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__ticket_remotelinks",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_remotelinks,
        sdk_tool=jira_ticket_remotelinks,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__search",
        namespace=NAMESPACE,
        handler=handlers.jira_search,
        sdk_tool=jira_search,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__ticket_create",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_create,
        sdk_tool=jira_ticket_create,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__ticket_edit",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_edit,
        sdk_tool=jira_ticket_edit,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__ticket_comment_add",
        namespace=NAMESPACE,
        handler=handlers.jira_ticket_comment_add,
        sdk_tool=jira_ticket_comment_add,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__link_create",
        namespace=NAMESPACE,
        handler=handlers.jira_link_create,
        sdk_tool=jira_link_create,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__jira__execute",
        namespace=NAMESPACE,
        handler=handlers.jira_execute,
        sdk_tool=jira_execute,
        cli_command=None,
    ),
]
