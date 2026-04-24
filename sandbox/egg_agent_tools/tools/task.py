"""Task-level @tool wrappers (complete, add_commit, update_notes, mark_gap)."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import task as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "task"

_COMPLETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task ID (e.g. 'task-1' or 'task-1-2')",
        },
        "commit": {
            "type": "string",
            "description": "Optional git commit SHA to link to the task",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["task"],
}

_ADD_COMMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task ID (e.g. 'task-1' or 'task-1-2')",
        },
        "commit": {
            "type": "string",
            "description": "Git commit SHA (7-40 hex characters)",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["task", "commit"],
}

_UPDATE_NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task ID (e.g. 'task-1' or 'task-1-2')",
        },
        "notes": {
            "type": "string",
            "description": "Implementation notes to store on the task",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["task", "notes"],
}

_MARK_GAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task ID (e.g. 'task-1' or 'task-1-2')",
        },
        "description": {
            "type": "string",
            "description": "Free-text description of the uncovered gap",
        },
        "to_role": {
            "type": "string",
            "description": "Target role (defaults to 'coder')",
        },
        "from_role": {
            "type": "string",
            "description": "Sender role (defaults to EGG_AGENT_ROLE)",
        },
        "gap_id": {
            "type": "string",
            "description": "Optional gap ID (auto-generated if omitted)",
        },
        "issue": {"type": "integer"},
        "pipeline_id": {"type": "string"},
        "repo_path": {"type": "string"},
    },
    "required": ["task", "description"],
}


@tool(
    "complete",
    "Mark a contract task complete, optionally linking a commit SHA. "
    "State-machine effect: transitions the task's status to 'complete'. "
    "Prefer this over 'egg-contract complete-task'.",
    _COMPLETE_SCHEMA,
)
async def task_complete(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.task_complete, args)


@tool(
    "add_commit",
    "Link a git commit SHA to a task (state-machine effect: sets the "
    "task's `commit` field; does NOT mark the task complete — call "
    "task__complete separately). Prefer this over 'egg-contract add-commit'.",
    _ADD_COMMIT_SCHEMA,
)
async def task_add_commit(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.task_add_commit, args)


@tool(
    "update_notes",
    "Append/replace implementation notes on a task. State-machine effect: "
    "sets the task's `notes` field; does NOT mark the task complete. "
    "Prefer this over 'egg-contract update-notes'.",
    _UPDATE_NOTES_SCHEMA,
)
async def task_update_notes(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.task_update_notes, args)


@tool(
    "mark_gap",
    "Record a tester→coder coverage-gap handoff on a task. State-machine "
    "effect: appends a structured gap entry to the task's `gaps` list. "
    "No CLI counterpart — this is a net-new capability (decision-4).",
    _MARK_GAP_SCHEMA,
)
async def task_mark_gap(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.task_mark_gap, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__task__complete",
        namespace=NAMESPACE,
        handler=handlers.task_complete,
        sdk_tool=task_complete,
        cli_command=("egg-contract", "complete-task"),
    ),
    ToolRegistration(
        name="mcp__task__add_commit",
        namespace=NAMESPACE,
        handler=handlers.task_add_commit,
        sdk_tool=task_add_commit,
        cli_command=("egg-contract", "add-commit"),
    ),
    ToolRegistration(
        name="mcp__task__update_notes",
        namespace=NAMESPACE,
        handler=handlers.task_update_notes,
        sdk_tool=task_update_notes,
        cli_command=("egg-contract", "update-notes"),
    ),
    ToolRegistration(
        name="mcp__task__mark_gap",
        namespace=NAMESPACE,
        handler=handlers.task_mark_gap,
        sdk_tool=task_mark_gap,
        cli_command=None,
    ),
]
