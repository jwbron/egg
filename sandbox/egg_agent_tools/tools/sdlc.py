"""SDLC / HITL @tool wrappers (register_open_question, request_feedback, check_hitl_answers)."""

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
    "Fetch resolved HITL decisions and submitted feedback for the current "
    "contract. With no args, returns everything the operator has already "
    "resolved across all phases; pass 'phase' to narrow to a single phase. "
    "No CLI counterpart — reads straight from the contract gateway.",
    _HITL_ANSWERS_SCHEMA,
)
async def check_hitl_answers(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.check_hitl_answers, args)


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
]
