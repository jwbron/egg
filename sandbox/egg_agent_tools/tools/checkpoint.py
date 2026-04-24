"""Checkpoint-namespace @tool wrappers (list, show, search).

All three wrappers forward to handlers in
``egg_agent_tools.handlers.checkpoint``.  The CLI equivalent is
``egg-checkpoint list/show/search`` — the drift test asserts both
paths dispatch through the same handlers by walking the CLI parser.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import checkpoint as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "checkpoint"

_COMMON_FILTERS: dict[str, Any] = {
    "issue": {"type": "integer", "description": "Filter by issue number"},
    "pr": {"type": "integer", "description": "Filter by PR number"},
    "branch": {"type": "string", "description": "Filter by branch name"},
    "session": {"type": "string", "description": "Filter by session ID"},
    "trigger": {"type": "string", "description": "Filter by trigger type"},
    "status": {"type": "string", "description": "Filter by session status"},
    "agent_type": {
        "type": "string",
        "description": (
            "Filter by agent type (base types or composite BRC reviewer "
            "roles: reviewer_code, reviewer_contract, reviewer_refine, etc.)"
        ),
    },
    "phase": {
        "type": "string",
        "enum": ["refine", "plan", "implement", "pr"],
        "description": "Filter by pipeline phase",
    },
    "pipeline": {"type": "string", "description": "Filter by pipeline ID"},
    "repo": {"type": "string", "description": "Filter by source repo (owner/repo)"},
    "upstream_limit": {
        "type": "integer",
        "description": (
            "Optional cap on the raw index scan (equivalent to "
            "`egg-checkpoint list --limit N`). Usually leave unset; use "
            "`limit` + `cursor` for MCP-level pagination."
        ),
    },
    "repo_path": {"type": "string", "description": "Override repo path"},
    "checkpoint_repo": {"type": "string", "description": "External checkpoint repo (owner/repo)"},
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_COMMON_FILTERS,
        "limit": {
            "type": "integer",
            "default": 100,
            "description": "Page size (default 100, max 500)",
        },
        "cursor": {"type": "string", "description": "Opaque pagination token"},
    },
}

_SHOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identifier": {
            "type": "string",
            "description": "Checkpoint ID (ckpt-…) or commit SHA",
        },
        "repo_path": {"type": "string"},
        "checkpoint_repo": {"type": "string"},
    },
    "required": ["identifier"],
}

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_COMMON_FILTERS,
        "text": {
            "type": "string",
            "description": "Case-insensitive substring to search for in transcripts",
        },
        "limit": {
            "type": "integer",
            "default": 100,
            "description": "Page size (default 100, max 500)",
        },
        "cursor": {"type": "string", "description": "Opaque pagination token"},
    },
    "required": ["text"],
}


@tool(
    "list",
    "List checkpoints matching filters (issue, pr, branch, agent_type, …). "
    "Paginated via `limit` + opaque `cursor`. Prefer this over 'egg-checkpoint list'.",
    _LIST_SCHEMA,
)
async def checkpoint_list(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.checkpoint_list, args)


@tool(
    "show",
    "Load a single checkpoint by ID (ckpt-…) or commit SHA. Prefer this over "
    "'egg-checkpoint show'.",
    _SHOW_SCHEMA,
)
async def checkpoint_show(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.checkpoint_show, args)


@tool(
    "search",
    "Search checkpoint transcripts for matching text. Paginated via `limit` + "
    "opaque `cursor`. Prefer this over 'egg-checkpoint search'.",
    _SEARCH_SCHEMA,
)
async def checkpoint_search(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.checkpoint_search, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__checkpoint__list",
        namespace=NAMESPACE,
        handler=handlers.checkpoint_list,
        sdk_tool=checkpoint_list,
        cli_command=("egg-checkpoint", "list"),
    ),
    ToolRegistration(
        name="mcp__checkpoint__show",
        namespace=NAMESPACE,
        handler=handlers.checkpoint_show,
        sdk_tool=checkpoint_show,
        cli_command=("egg-checkpoint", "show"),
    ),
    ToolRegistration(
        name="mcp__checkpoint__search",
        namespace=NAMESPACE,
        handler=handlers.checkpoint_search,
        sdk_tool=checkpoint_search,
        cli_command=("egg-checkpoint", "search"),
    ),
]
