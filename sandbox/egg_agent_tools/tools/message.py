"""Event-driven message @tool wrappers (wait, wait_loop, send_heartbeat).

Exposes the three event-driven primitives #1897 added to
``egg-orch message`` as first-class MCP tools so SDK agents can consume
them on the ``tool_use`` stream instead of shelling out to Bash.

The tools live under the ``brc`` namespace because every current
consumer is a BRC coordination loop; they render as
``mcp__brc__wait_for_event`` / ``mcp__brc__wait_loop`` /
``mcp__brc__send_heartbeat``.  Sharing the namespace with
``mcp__brc__propose`` / ``ack`` / ``nack`` / ``confirm`` keeps
related verbs discoverable together.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import message as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "brc"


_WAIT_PROPS: dict[str, Any] = {
    "for_types": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Message types to block on (e.g. CONSENSUS_ACK, CONSENSUS_NACK). "
            "Required — at least one entry."
        ),
        "minItems": 1,
    },
    "role": {"type": "string", "description": "Filter for this receiver role"},
    "from_role": {"type": "string", "description": "Filter by sender role"},
    "since": {
        "type": "string",
        "description": (
            "Return messages after this ID. Thread the ``cursor`` from the "
            "previous wait_for_event / wait_loop response here to avoid "
            "missing events that arrive between successive calls."
        ),
    },
    "limit": {"type": "integer", "description": "Max messages to return"},
    "timeout": {
        "type": "integer",
        "description": "Server-side block timeout in seconds (default 60)",
        "default": 60,
    },
    "pipeline_id": {"type": "string"},
}


_WAIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _WAIT_PROPS,
    "required": ["for_types"],
}


_WAIT_LOOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_WAIT_PROPS,
        "max_iterations": {
            "type": "integer",
            "description": (
                "Safety cap on outer-loop iterations.  Non-positive / "
                "absent means loop forever (matches the CLI's default)."
            ),
        },
    },
    "required": ["for_types"],
}


_HEARTBEAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "enum": [
                "WORKING",
                "WAITING_ON_ROLE",
                "WAITING_FOR_EVENT",
                "PROPOSED",
                "IDLE",
            ],
            "description": "Agent state for this heartbeat",
        },
        "waiting_on": {
            "type": "string",
            "description": ("Peer role being waited on; required when state=WAITING_ON_ROLE."),
        },
        "since": {
            "type": "string",
            "description": "ISO-8601 / epoch timestamp naming when the state began",
        },
        "body": {"type": "string", "description": "Optional free-form body text"},
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["state"],
}


@tool(
    "wait_for_event",
    "Block until a typed message (e.g. CONSENSUS_ACK, CONSENSUS_NACK) arrives "
    "for this agent. Event-driven alternative to polling in a Bash loop. "
    "Prefer this over 'egg-orch message wait'.",
    _WAIT_SCHEMA,
)
async def brc_wait_for_event(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.message_wait, args)


@tool(
    "wait_loop",
    "Loop 'wait_for_event' until a match arrives or 'max_iterations' trips. "
    "Rides through timeouts and short transient gateway errors so callers "
    "don't have to. Prefer this over 'egg-orch message wait-loop'.",
    _WAIT_LOOP_SCHEMA,
)
async def brc_wait_loop(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.message_wait_loop, args)


@tool(
    "send_heartbeat",
    "Emit a structured HEARTBEAT (schema-validated, per-role deduped, "
    "rate-limited) to the dedicated /heartbeat endpoint. Use "
    "state=WAITING_ON_ROLE + waiting_on=<peer> while blocking on BRC. "
    "Prefer this over 'egg-orch message heartbeat'.",
    _HEARTBEAT_SCHEMA,
)
async def brc_send_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.message_heartbeat, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__brc__wait_for_event",
        namespace=NAMESPACE,
        handler=handlers.message_wait,
        sdk_tool=brc_wait_for_event,
        cli_command=("egg-orch", "message", "wait"),
    ),
    ToolRegistration(
        name="mcp__brc__wait_loop",
        namespace=NAMESPACE,
        handler=handlers.message_wait_loop,
        sdk_tool=brc_wait_loop,
        cli_command=("egg-orch", "message", "wait-loop"),
    ),
    ToolRegistration(
        name="mcp__brc__send_heartbeat",
        namespace=NAMESPACE,
        handler=handlers.message_heartbeat,
        sdk_tool=brc_send_heartbeat,
        cli_command=("egg-orch", "message", "heartbeat"),
    ),
]
