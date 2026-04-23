"""Factory + system-prompt nudge for the in-process egg MCP server."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.tools import (
    NAMESPACE_DESCRIPTIONS,
    TOOL_LIST,
    TOOL_NAMESPACES,
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
        desc = NAMESPACE_DESCRIPTIONS.get(
            namespace, f"operations in the {namespace} namespace"
        )
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


def build_sandbox_mcp_server(
    *,
    name: str = "egg",
    version: str = "1.0.0",
    tools: list[Any] | None = None,
) -> Any:
    """Build the in-process SDK MCP server for sandbox agents.

    Imports :func:`claude_agent_sdk.create_sdk_mcp_server` lazily so
    host-side callers who don't have the SDK installed can still import
    this module (useful for tests).
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "claude-agent-sdk is required to build the sandbox MCP server. "
            "Install it inside the sandbox image (see sandbox/Dockerfile)."
        ) from exc

    effective_tools = tools if tools is not None else TOOL_LIST
    return create_sdk_mcp_server(name=name, version=version, tools=effective_tools)


__all__ = ["SYSTEM_PROMPT_NUDGE", "build_sandbox_mcp_server"]
