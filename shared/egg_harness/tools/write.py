"""Write tool — file writing for the egg harness.

Provides :func:`create_write_tool` which returns a tool definition and async
handler for writing files, creating parent directories as needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)


def create_write_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a Write tool definition and handler.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Write",
        description=(
            "Writes content to a file on the local filesystem. Creates "
            "parent directories if they do not exist. Overwrites the file "
            "if it already exists."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        file_path = input["file_path"]
        content: str = input["content"]

        path = Path(file_path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except PermissionError:
            return ToolResult(
                output=f"Permission denied: {file_path}",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                output=f"Error writing file: {exc}",
                is_error=True,
            )

        return ToolResult(output=f"Successfully wrote to {file_path}")

    return definition, handler


# ---------------------------------------------------------------------------
# Synchronous convenience wrapper
# ---------------------------------------------------------------------------


def write_file(file_path: str, content: str) -> ToolResult:
    """Synchronous convenience wrapper for writing files."""
    import asyncio

    _, handler = create_write_tool()
    return asyncio.run(handler({"file_path": file_path, "content": content}))
