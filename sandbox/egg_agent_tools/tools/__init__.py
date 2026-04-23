"""SDK ``@tool``-decorated wrappers for egg_agent_tools.

Every wrapper is a thin call to :func:`invoke_handler` in
``tools/_common.py``; actual behaviour lives in ``handlers/*.py``.

This module exposes:

- ``TOOL_LIST`` — list of ``SdkMcpTool`` objects suitable for
  :func:`claude_agent_sdk.create_sdk_mcp_server`.
- ``TOOL_NAMESPACES`` — ``{namespace: [tool_name, ...]}`` mapping used
  to render the bootstrap system-prompt nudge.
- ``TOOL_REGISTRY`` — ``{tool_name: ToolRegistration(...)}`` consumed
  by the drift test to assert every tool with a declared
  ``cli_command`` resolves to the handler the CLI dispatches.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.tools import brc as _brc_tools
from egg_agent_tools.tools import phase as _phase_tools
from egg_agent_tools.tools import progress as _progress_tools
from egg_agent_tools.tools import sdlc as _sdlc_tools
from egg_agent_tools.tools import task as _task_tools
from egg_agent_tools.tools._registry import ToolRegistration

TOOL_REGISTRY: dict[str, ToolRegistration] = {}


def _register_all() -> None:
    """Populate TOOL_REGISTRY from the per-namespace modules."""
    for module in (_sdlc_tools, _brc_tools, _phase_tools, _progress_tools, _task_tools):
        for reg in module.REGISTRATIONS:
            TOOL_REGISTRY[reg.name] = reg


_register_all()


TOOL_LIST: list[Any] = [reg.sdk_tool for reg in TOOL_REGISTRY.values()]


def _group_by_namespace() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for reg in TOOL_REGISTRY.values():
        groups.setdefault(reg.namespace, []).append(reg.name)
    return groups


TOOL_NAMESPACES: dict[str, list[str]] = _group_by_namespace()

NAMESPACE_DESCRIPTIONS: dict[str, str] = {
    "sdlc": (
        "register a HITL decision, request open-ended feedback, "
        "and check for human answers"
    ),
    "brc": (
        "drive Broadcast-Review-Converge consensus: propose, ACK, "
        "NACK, confirm, and inspect state"
    ),
    "phase": (
        "look up your phase context (role, pipeline, assigned tasks, "
        "prior-phase artifacts) and task list"
    ),
    "progress": (
        "emit structured progress updates, error signals, or heartbeats"
    ),
    "task": "mark a contract task complete and link a commit",
}


__all__ = [
    "NAMESPACE_DESCRIPTIONS",
    "TOOL_LIST",
    "TOOL_NAMESPACES",
    "TOOL_REGISTRY",
    "ToolRegistration",
]
