"""Progress-signal @tool wrappers."""

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

from egg_agent_tools.handlers import progress as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration

NAMESPACE = "progress"

_PROGRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "percent": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Completion percent (0-100)",
        },
        "task": {"type": "string", "description": "Optional current task ID"},
        "message": {"type": "string", "description": "Optional free-form message"},
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["percent"],
}

_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string", "description": "Error message"},
        "recoverable": {
            "type": "boolean",
            "description": "Whether the error is recoverable",
            "default": False,
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["error"],
}

_HEARTBEAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
}


@tool(
    "mcp__progress__emit",
    "Signal a structured progress update to the orchestrator. Prefer this over "
    "'egg-orch signal progress'.",
    _PROGRESS_SCHEMA,
)
async def progress_emit(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_emit, args)


@tool(
    "mcp__progress__signal_error",
    "Signal an error (recoverable or unrecoverable) to the orchestrator. Prefer "
    "this over 'egg-orch signal error'.",
    _ERROR_SCHEMA,
)
async def progress_signal_error(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_signal_error, args)


@tool(
    "mcp__progress__heartbeat",
    "Send a heartbeat signal to the orchestrator. Prefer this over "
    "'egg-orch signal heartbeat'.",
    _HEARTBEAT_SCHEMA,
)
async def progress_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_heartbeat, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__progress__emit",
        namespace=NAMESPACE,
        handler=handlers.progress_emit,
        sdk_tool=progress_emit,
        cli_command=("egg-orch", "signal", "progress"),
    ),
    ToolRegistration(
        name="mcp__progress__signal_error",
        namespace=NAMESPACE,
        handler=handlers.progress_signal_error,
        sdk_tool=progress_signal_error,
        cli_command=("egg-orch", "signal", "error"),
    ),
    ToolRegistration(
        name="mcp__progress__heartbeat",
        namespace=NAMESPACE,
        handler=handlers.progress_heartbeat,
        sdk_tool=progress_heartbeat,
        cli_command=("egg-orch", "signal", "heartbeat"),
    ),
]
