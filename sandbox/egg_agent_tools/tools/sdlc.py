"""SDLC / HITL @tool wrappers."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import sdlc as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "sdlc"

_REGISTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "Decision question"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional decision options (Other is always appended)",
        },
        "phase": {
            "type": "string",
            "enum": ["refine", "plan", "implement", "pr"],
            "description": "Pipeline phase (defaults to contract's current_phase)",
        },
        "issue": {"type": "integer", "description": "Optional issue-number override"},
        "pipeline_id": {
            "type": "string",
            "description": "Optional pipeline-id override",
        },
        "repo_path": {"type": "string", "description": "Optional repo-path override"},
    },
    "required": ["question"],
}

_FEEDBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Open-ended questions to ask the human",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["questions"],
}

_HITL_ANSWERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["refine", "plan", "implement", "pr"],
            "description": (
                "Optional phase filter. When omitted, returns HITL from all "
                "phases so later-phase callers can see earlier-phase answers."
            ),
        },
        "include_unresolved": {
            "type": "boolean",
            "description": "Include decisions that have not been resolved yet",
            "default": False,
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
}

_SHOW_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional projection: only return the named top-level contract "
                "fields (e.g. ['current_phase', 'decisions']). Unknown names "
                "raise an error — do not use this to probe for unknown fields."
            ),
        },
        "audit": {
            "type": "boolean",
            "description": "Include the audit log in the response (mirrors --audit)",
            "default": False,
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
}

_VERIFY_CRITERION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "criterion": {
            "type": "string",
            "description": "Criterion ID (e.g. 'ac-1'); REVIEWER role required.",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["criterion"],
}


@tool(
    "register_open_question",
    "Create a HITL decision point on the SDLC contract so a human can choose between "
    "options. Prefer this over running 'egg-contract add-decision'.",
    _REGISTER_SCHEMA,
)
async def register_open_question(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.register_open_question, args)


@tool(
    "request_feedback",
    "Open an open-ended feedback request so humans can answer with free-form text. "
    "Prefer this over running 'egg-contract add-feedback'.",
    _FEEDBACK_SCHEMA,
)
async def request_feedback(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.request_feedback, args)


@tool(
    "check_hitl_answers",
    "Fetch resolved HITL decisions and feedback (submitted or pending) for the current "
    "contract. With no args, returns everything the operator has already "
    "resolved across all phases; pass 'phase' to narrow to a single phase. "
    "No CLI counterpart — reads straight from the contract gateway.",
    _HITL_ANSWERS_SCHEMA,
)
async def check_hitl_answers(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.check_hitl_answers, args)


@tool(
    "show_contract",
    "Read the SDLC contract (optionally projected via `fields=[...]`). Reads the "
    "contract; does not mutate state. Prefer this over 'egg-contract show'.",
    _SHOW_CONTRACT_SCHEMA,
)
async def show_contract(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.show_contract, args)


@tool(
    "verify_criterion",
    "Mark an acceptance criterion as verified. REVIEWER role required (gateway "
    "rejects non-reviewer writers). State-machine effect: flips "
    "`acceptance_criteria.<N>.verified` to True; no-op if already verified. "
    "Prefer this over 'egg-contract verify-criterion'.",
    _VERIFY_CRITERION_SCHEMA,
)
async def verify_criterion(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.verify_criterion, args)


from egg_agent_tools.tools._registry import ToolRegistration  # noqa: E402,I001

REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__sdlc__register_open_question",
        namespace=NAMESPACE,
        handler=handlers.register_open_question,
        sdk_tool=register_open_question,
        cli_command=("egg-contract", "add-decision"),
    ),
    ToolRegistration(
        name="mcp__sdlc__request_feedback",
        namespace=NAMESPACE,
        handler=handlers.request_feedback,
        sdk_tool=request_feedback,
        cli_command=("egg-contract", "add-feedback"),
    ),
    ToolRegistration(
        name="mcp__sdlc__check_hitl_answers",
        namespace=NAMESPACE,
        handler=handlers.check_hitl_answers,
        sdk_tool=check_hitl_answers,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__sdlc__show_contract",
        namespace=NAMESPACE,
        handler=handlers.show_contract,
        sdk_tool=show_contract,
        cli_command=("egg-contract", "show"),
    ),
    ToolRegistration(
        name="mcp__sdlc__verify_criterion",
        namespace=NAMESPACE,
        handler=handlers.verify_criterion,
        sdk_tool=verify_criterion,
        cli_command=("egg-contract", "verify-criterion"),
    ),
]
