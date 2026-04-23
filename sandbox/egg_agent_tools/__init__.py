"""egg_agent_tools — in-process SDK MCP tool surface for sandbox agents.

This package exposes egg's agent-lifecycle capabilities (HITL decisions,
BRC consensus, phase context, progress signalling, task completion) as
first-class MCP tools that sandbox agents can call directly through the
Claude Agent SDK's in-process MCP server facility.

Layout
------

- ``handlers/`` — pure request→response functions (dict in, dict out).
  The same functions back the existing shell CLIs
  (``sandbox/egg_lib/contract_cli.py`` and
  ``sandbox/egg_lib/orch_cli.py``), so any behaviour change lands in one
  place.
- ``tools/``    — ``@tool``-decorated async wrappers that invoke the
  handlers from the SDK's in-process MCP server.
- ``server.py`` — ``build_sandbox_mcp_server`` factory.
- ``schemas.py``— argparse→JSON-schema helpers for tools with CLI
  counterparts.

Gating
------

``shared/egg_agent/client.py::run_agent_async`` imports this package
lazily.  The MCP surface is on by default (since #1942); set
``EGG_MCP_TOOLS`` to ``false`` / ``0`` / ``no`` / ``off`` to opt out,
in which case the SDK wire-up is byte-identical to the pre-#1765
path and the package is not imported.
"""

from __future__ import annotations

from egg_agent_tools.server import (  # noqa: F401
    SYSTEM_PROMPT_NUDGE,
    build_sandbox_mcp_server,
)
from egg_agent_tools.tools import (  # noqa: F401
    TOOL_LIST,
    TOOL_NAMESPACES,
    TOOL_REGISTRY,
    ToolRegistration,
)

__all__ = [
    "SYSTEM_PROMPT_NUDGE",
    "TOOL_LIST",
    "TOOL_NAMESPACES",
    "TOOL_REGISTRY",
    "ToolRegistration",
    "build_sandbox_mcp_server",
]
