"""Event-driven message @tool wrappers (send_heartbeat).

Exposes the heartbeat primitive as a first-class MCP tool so SDK agents
can emit structured liveness signals on the ``tool_use`` stream instead
of shelling out to Bash.

The blocking-wait variants (``wait_for_event`` / ``wait_loop``) were
removed in #2211 — long-poll waits don't fit the MCP transport (the
in-process SDK caps tool calls at ~60 s and the streamable-HTTP MCP
caps at ~30 s), and the cap-elapsed return is a full LLM turn.  No
replacement exists at the agent tier by design (#2908, #3157): the
consensus wrapper owns every blocking wait on the bus — it blocks in
``egg-orch message wait-loop`` between BRC events and invokes the
agent one-shot per actionable event.  Agents act on the single event
in their prompt and exit; they never wait.  See §0 of
``docs/reference/agent-wait-patterns.md`` for the contract.

The remaining tool lives under the ``brc`` namespace; it renders as
``mcp__brc__send_heartbeat``.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import message as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "brc"


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
    "send_heartbeat",
    "Emit a structured HEARTBEAT (schema-validated, per-role deduped, "
    "rate-limited) to the dedicated /heartbeat endpoint on a state "
    "transition (WORKING, WAITING_ON_ROLE + waiting_on=<peer>, PROPOSED, "
    "IDLE). Prefer this over 'egg-orch message heartbeat'. Do not emit "
    "WAITING_FOR_EVENT — the consensus wrapper owns bus waits and their "
    "heartbeats; never block waiting on BRC yourself.",
    _HEARTBEAT_SCHEMA,
)
async def brc_send_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.message_heartbeat, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__brc__send_heartbeat",
        namespace=NAMESPACE,
        handler=handlers.message_heartbeat,
        sdk_tool=brc_send_heartbeat,
        cli_command=("egg-orch", "message", "heartbeat"),
    ),
]
