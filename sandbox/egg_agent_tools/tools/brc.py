"""BRC consensus @tool wrappers."""

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

from egg_agent_tools.handlers import brc as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration

NAMESPACE = "brc"

_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Substantive proposal summary (>=50 chars recommended)",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Artifact references (paths, IDs, URLs)",
        },
        "risk_considered": {"type": "string", "description": "Summary of risks considered"},
        "commit_sha": {
            "type": "string",
            "description": "Commit SHA; defaults to HEAD",
        },
        "files_changed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files touched by the proposal",
        },
        "tests_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Test identifiers executed",
        },
        "tasks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Contract task IDs the proposal satisfies",
        },
        "attestation": {
            "type": "object",
            "description": "Optional attestation payload forwarded to the orchestrator",
        },
        "changed_artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Re-proposal delta (changed artifact references)",
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["summary"],
}

_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "producer_role": {"type": "string", "description": "Producer being ACKed"},
        "reason": {"type": "string", "description": "Reason / review summary"},
        "files_reviewed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Artifacts actually reviewed",
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["producer_role", "reason"],
}

_NACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "producer_role": {"type": "string", "description": "Producer being NACKed"},
        "reason": {
            "type": "string",
            "description": "Specific blocking reason; producer must address",
        },
        "files_reviewed": {
            "type": "array",
            "items": {"type": "string"},
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["producer_role", "reason"],
}

_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
}

_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "verbose": {
            "type": "boolean",
            "description": "Include the full orchestrator status payload",
            "default": False,
        },
    },
}

_LIST_BLOCKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
    },
}


@tool(
    "mcp__brc__propose",
    "Send a CONSENSUS_PROPOSE signal starting or re-starting the BRC cycle for "
    "this producer. Prefer this over 'egg-orch consensus propose'.",
    _PROPOSE_SCHEMA,
)
async def brc_propose(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_propose, args)


@tool(
    "mcp__brc__ack",
    "ACK a producer's proposal (reviewer side). Prefer this over "
    "'egg-orch consensus ack'.",
    _ACK_SCHEMA,
)
async def brc_ack(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_ack, args)


@tool(
    "mcp__brc__nack",
    "NACK a producer's proposal with a specific blocking reason. Prefer this "
    "over 'egg-orch consensus nack'.",
    _NACK_SCHEMA,
)
async def brc_nack(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_nack, args)


@tool(
    "mcp__brc__confirm",
    "Send CONSENSUS_CONFIRMED after all reviewers ACK. Returns status "
    "'pending_acks' if any reviewer has not yet re-ACKed. Prefer this over "
    "'egg-orch consensus confirmed'.",
    _CONFIRM_SCHEMA,
)
async def brc_confirm(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_confirm, args)


@tool(
    "mcp__brc__get_state",
    "Fetch the current BRC consensus state (agent matrix, blocking roles, "
    "phase). Structured — no text scrape needed.",
    _STATE_SCHEMA,
)
async def brc_get_state(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_get_state, args)


@tool(
    "mcp__brc__list_blocking",
    "Return the agent roles currently blocking consensus. Derived view of the "
    "BRC state.",
    _LIST_BLOCKING_SCHEMA,
)
async def brc_list_blocking(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_list_blocking, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__brc__propose",
        namespace=NAMESPACE,
        handler=handlers.brc_propose,
        sdk_tool=brc_propose,
        cli_command=("egg-orch", "consensus", "propose"),
    ),
    ToolRegistration(
        name="mcp__brc__ack",
        namespace=NAMESPACE,
        handler=handlers.brc_ack,
        sdk_tool=brc_ack,
        cli_command=("egg-orch", "consensus", "ack"),
    ),
    ToolRegistration(
        name="mcp__brc__nack",
        namespace=NAMESPACE,
        handler=handlers.brc_nack,
        sdk_tool=brc_nack,
        cli_command=("egg-orch", "consensus", "nack"),
    ),
    ToolRegistration(
        name="mcp__brc__confirm",
        namespace=NAMESPACE,
        handler=handlers.brc_confirm,
        sdk_tool=brc_confirm,
        cli_command=("egg-orch", "consensus", "confirmed"),
    ),
    ToolRegistration(
        name="mcp__brc__get_state",
        namespace=NAMESPACE,
        handler=handlers.brc_get_state,
        sdk_tool=brc_get_state,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__brc__list_blocking",
        namespace=NAMESPACE,
        handler=handlers.brc_list_blocking,
        sdk_tool=brc_list_blocking,
        cli_command=None,
    ),
]
