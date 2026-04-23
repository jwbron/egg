"""Task-level @tool wrappers (task_complete)."""

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

from egg_agent_tools.handlers import task as handlers
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration

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


@tool(
    "mcp__task__complete",
    "Mark a contract task complete, optionally linking a commit SHA. Prefer this "
    "over 'egg-contract complete-task'.",
    _COMPLETE_SCHEMA,
)
async def task_complete(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.task_complete, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__task__complete",
        namespace=NAMESPACE,
        handler=handlers.task_complete,
        sdk_tool=task_complete,
        cli_command=("egg-contract", "complete-task"),
    ),
]
