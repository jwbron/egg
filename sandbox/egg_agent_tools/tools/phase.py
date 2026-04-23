"""Phase-context @tool wrappers."""

from __future__ import annotations

from typing import Any

try:
    from claude_agent_sdk import tool
except ImportError:  # pragma: no cover

    def tool(name, description, input_schema, annotations=None):  # type: ignore[no-redef]
        def _decorator(handler):
            class _Stub:
                def __init__(self) -> None:
                    self.name = name
                    self.description = description
                    self.input_schema = input_schema
                    self.handler = handler
                    self.annotations = annotations

            return _Stub()

        return _decorator

from egg_agent_tools.handlers import phase as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration

NAMESPACE = "phase"

_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "phase": {
            "type": "string",
            "enum": ["refine", "plan", "implement", "pr"],
        },
        "role": {"type": "string"},
        "include_artifacts": {
            "type": "boolean",
            "default": True,
            "description": "Include referenced prior-phase artifact paths.",
        },
        "issue": {"type": "integer"},
        "repo_path": {"type": "string"},
    },
}

_ASSIGNED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "description": "Agent role filter"},
        "status": {
            "type": "string",
            "description": "Optional task-status filter",
        },
        "pipeline_id": {"type": "string"},
        "issue": {"type": "integer"},
        "repo_path": {"type": "string"},
    },
}


@tool(
    "get_context",
    "Bundle the caller's phase context: pipeline id, phase, role, assigned "
    "tasks, and prior-phase artifact paths. Replaces 'cat CLAUDE.md && ls "
    ".egg-state/' archaeology.",
    _CONTEXT_SCHEMA,
)
async def phase_get_context(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.phase_get_context, args)


@tool(
    "get_assigned_tasks",
    "Return only the contract tasks assigned to the caller's role (filtered by "
    "EGG_AGENT_ROLE). Optional status filter.",
    _ASSIGNED_SCHEMA,
)
async def phase_get_assigned_tasks(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.phase_get_assigned_tasks, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__phase__get_context",
        namespace=NAMESPACE,
        handler=handlers.phase_get_context,
        sdk_tool=phase_get_context,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__phase__get_assigned_tasks",
        namespace=NAMESPACE,
        handler=handlers.phase_get_assigned_tasks,
        sdk_tool=phase_get_assigned_tasks,
        cli_command=None,
    ),
]
