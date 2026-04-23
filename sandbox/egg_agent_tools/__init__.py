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
lazily and only when the ``EGG_MCP_TOOLS`` environment variable is
truthy.  When the flag is unset the SDK wire-up is byte-identical to
today, so non-opt-in pipelines pay no cost.
"""

from __future__ import annotations

from egg_agent_tools.server import (  # noqa: F401
    SYSTEM_PROMPT_NUDGE,
    build_sandbox_mcp_server,
)
from egg_agent_tools.tools import (  # noqa: F401
    TOOL_LIST,
    TOOL_NAMESPACES,
)

__all__ = [
    "SYSTEM_PROMPT_NUDGE",
    "TOOL_LIST",
    "TOOL_NAMESPACES",
    "build_sandbox_mcp_server",
]
