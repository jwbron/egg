"""Factories + system-prompt nudge for the in-process egg MCP servers.

Claude's MCP client renders every tool as ``mcp__<server_key>__<raw_name>``
where ``<server_key>`` is the key under
``ClaudeAgentOptions.mcp_servers`` and ``<raw_name>`` is the string
passed to the SDK's ``@tool`` decorator.  To land the decision-7
semantic names (``mcp__sdlc__register_open_question``, etc.) we use
**one server per namespace** — server key ``sdlc`` + raw name
``register_open_question`` → visible ``mcp__sdlc__register_open_question``.

A single aggregate ``egg`` server would double-prefix
(``mcp__egg__mcp__sdlc__register_open_question``) which breaks the
nudge, the docs, and decision-7.

``build_sandbox_mcp_server()`` therefore returns a ``{namespace: server}``
dict ready to drop into ``ClaudeAgentOptions.mcp_servers``.  A single-
server form is kept as ``build_aggregate_mcp_server()`` for niche
callers (tests).
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.tools import (
    NAMESPACE_DESCRIPTIONS,
    TOOL_LIST,
    TOOL_NAMESPACES,
    TOOL_REGISTRY,
)


def _render_nudge() -> str:
    """Generate the bootstrap system-prompt paragraph from TOOL_NAMESPACES.

    Rendered at import time so adding/renaming a namespace automatically
    updates the nudge — no parallel string to edit.  The output stays
    under 200 words.
    """
    lines: list[str] = [
        "You have first-class MCP tools for the agent-lifecycle operations "
        "you perform in every phase. Prefer them over shelling out to "
        "`egg-orch` / `egg-contract` via Bash.",
        "",
        "Available tool namespaces:",
    ]
    for namespace in sorted(TOOL_NAMESPACES):
        desc = NAMESPACE_DESCRIPTIONS.get(namespace, f"operations in the {namespace} namespace")
        lines.append(f"- `mcp__{namespace}__*` — {desc}.")
    lines.append("")
    lines.append(
        "Call the MCP tool directly; do not run `egg-orch consensus "
        "propose`, `egg-contract add-decision`, etc. through Bash when "
        "an `mcp__*` tool covers the same capability. The shell CLIs "
        "remain available for other tooling but are slower and less "
        "reliable for agent use."
    )
    return "\n".join(lines)


SYSTEM_PROMPT_NUDGE: str = _render_nudge()


def _tools_for_namespace(namespace: str) -> list[Any]:
    """Collect the SDK tool objects registered under a namespace."""
    return [reg.sdk_tool for reg in TOOL_REGISTRY.values() if reg.namespace == namespace]


def build_sandbox_mcp_server(
    *,
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build the in-process SDK MCP servers for sandbox agents.

    Returns a dict keyed by namespace (``sdlc``/``brc``/``phase``/
    ``progress``/``task``) ready to drop into
    ``ClaudeAgentOptions.mcp_servers``.  Using per-namespace server keys
    yields the decision-7 visible names
    (``mcp__<namespace>__<verb>``) rather than the double-prefix an
    aggregate server would produce.

    Lazily imports :func:`claude_agent_sdk.create_sdk_mcp_server` so
    host-side callers (tests, docs tooling) can import this module
    without the SDK installed.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "claude-agent-sdk is required to build the sandbox MCP server. "
            "Install it inside the sandbox image (see sandbox/Dockerfile)."
        ) from exc

    servers: dict[str, Any] = {}
    for namespace in TOOL_NAMESPACES:
        servers[namespace] = create_sdk_mcp_server(
            name=namespace,
            version=version,
            tools=_tools_for_namespace(namespace),
        )
    return servers


def build_aggregate_mcp_server(
    *,
    name: str = "egg",
    version: str = "1.0.0",
    tools: list[Any] | None = None,
) -> Any:
    """Build a single aggregate MCP server with every tool under one key.

    Exists for tests and niche callers that prefer a single server.
    Note: the Claude-visible names will be ``mcp__<name>__<raw_name>``.
    The default raw names drop the namespace prefix, so with
    ``name='egg'`` tools look like ``mcp__egg__register_open_question``
    — NOT the decision-7 ``mcp__sdlc__register_open_question``.  Use
    :func:`build_sandbox_mcp_server` for the canonical wire-up.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "claude-agent-sdk is required to build the aggregate MCP server."
        ) from exc

    effective = tools if tools is not None else TOOL_LIST
    return create_sdk_mcp_server(name=name, version=version, tools=effective)


__all__ = [
    "SYSTEM_PROMPT_NUDGE",
    "build_aggregate_mcp_server",
    "build_sandbox_mcp_server",
]
