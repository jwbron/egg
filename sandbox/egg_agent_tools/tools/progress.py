"""Progress-signal @tool wrappers."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import progress as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "progress"

_PROGRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {
            "type": "string",
            "description": "Name of the current step (e.g. 'refactor handlers')",
        },
        "state": {
            "type": "string",
            "enum": ["working", "blocked", "complete"],
            "description": ("Progress state — one of working/blocked/complete"),
        },
        "detail": {"type": "string", "description": "Optional free-form detail"},
        "blocker": {
            "type": "string",
            "description": "Optional blocker identifier when state=='blocked'",
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["step", "state"],
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

_OVERSEER_ALERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anomaly": {
            "type": "string",
            "description": (
                "Anomaly type (free text). Known types: stuck-phase-transition, "
                "agent-heartbeat-stall, agent-loop, orchestrator-consensus-silent, "
                "unauthorized-overseer-action, unmediated-disagreement. NOTE: "
                "`unmediated-disagreement` is for OBSERVERS (overseer/mediator) "
                "flagging that no one is adjudicating a disagreement. If you are "
                "a producer blocked by reviewer NACKs that name an architectural "
                "scope question for the operator, use "
                "`mcp__sdlc__register_open_question` instead -- it creates a "
                "contract-tracked HITL gate; this alert is informational only."
            ),
        },
        "priority": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Alert priority",
        },
        "summary": {
            "type": "string",
            "description": "One-line summary of what was observed",
        },
        "detail": {
            "type": "string",
            "description": "Longer description / observed evidence",
        },
        "recommend": {
            "type": "string",
            "description": "Recommended action for the human operator",
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["anomaly", "priority", "summary"],
}

_QUERY_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "include_raw": {
            "type": "boolean",
            "default": False,
            "description": "Include the full raw status payload in the response",
        },
    },
}


@tool(
    "emit",
    "Emit a structured progress event to the orchestrator's progress "
    "bus (step/state/detail/blocker). Prefer this over "
    "'egg-orch progress emit'.",
    _PROGRESS_SCHEMA,
)
async def progress_emit(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_emit, args)


@tool(
    "signal_error",
    "Signal an error (recoverable or unrecoverable) to the orchestrator. Prefer "
    "this over 'egg-orch signal error'.",
    _ERROR_SCHEMA,
)
async def progress_signal_error(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_signal_error, args)


@tool(
    "heartbeat",
    "Send a heartbeat signal to the orchestrator. Prefer this over 'egg-orch signal heartbeat'.",
    _HEARTBEAT_SCHEMA,
)
async def progress_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_heartbeat, args)


@tool(
    "overseer_alert",
    "Broadcast an OVERSEER_ALERT to the human operator. Wraps the orchestrator "
    "message-send endpoint with message_type=OVERSEER_ALERT and to_role='all' "
    "hard-coded; only OVERSEER_ALERT is picked up by the sdlc-skill alert "
    "surface. Prefer this over 'egg-orch overseer alert'.",
    _OVERSEER_ALERT_SCHEMA,
)
async def progress_overseer_alert(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_overseer_alert, args)


@tool(
    "query_status",
    "Read pipeline status (state, current_phase, pending_decisions). Pure read; "
    "no mutations. Prefer this over 'egg-orch pipeline status'.",
    _QUERY_STATUS_SCHEMA,
)
async def progress_query_status(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.progress_query_status, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__progress__emit",
        namespace=NAMESPACE,
        handler=handlers.progress_emit,
        sdk_tool=progress_emit,
        cli_command=("egg-orch", "progress", "emit"),
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
    ToolRegistration(
        name="mcp__progress__overseer_alert",
        namespace=NAMESPACE,
        handler=handlers.progress_overseer_alert,
        sdk_tool=progress_overseer_alert,
        cli_command=("egg-orch", "overseer", "alert"),
    ),
    ToolRegistration(
        name="mcp__progress__query_status",
        namespace=NAMESPACE,
        handler=handlers.progress_query_status,
        sdk_tool=progress_query_status,
        cli_command=("egg-orch", "pipeline", "status"),
    ),
]
