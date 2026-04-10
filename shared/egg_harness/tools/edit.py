"""Edit tool — exact string replacement in files for the egg harness.

Provides :func:`create_edit_tool` which returns a tool definition and async
handler for performing precise string replacements within existing files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)


def create_edit_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create an Edit tool definition and handler.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Edit",
        description=(
            "Performs exact string replacements in files. The old_string "
            "must be unique in the file unless replace_all is True."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to modify.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace all occurrences of old_string. "
                        "Defaults to false."
                    ),
                    "default": False,
                },
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        file_path = input["file_path"]
        old_string: str = input["old_string"]
        new_string: str = input["new_string"]
        replace_all: bool = input.get("replace_all", False)

        path = Path(file_path)

        # Read existing content
        if not path.exists():
            return ToolResult(
                output=f"File not found: {file_path}",
                is_error=True,
            )

        if not path.is_file():
            return ToolResult(
                output=f"Not a file: {file_path}",
                is_error=True,
            )

        try:
            content = path.read_text(encoding="utf-8")
        except PermissionError:
            return ToolResult(
                output=f"Permission denied: {file_path}",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                output=f"Error reading file: {exc}",
                is_error=True,
            )

        # Check that old_string exists
        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                output=f"old_string not found in {file_path}",
                is_error=True,
            )

        # If not replace_all, old_string must be unique
        if not replace_all and count > 1:
            return ToolResult(
                output=(
                    f"old_string is not unique in {file_path} "
                    f"({count} occurrences found). Use replace_all=true "
                    f"to replace all occurrences, or provide more context "
                    f"to make the match unique."
                ),
                is_error=True,
            )

        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        # Write back
        try:
            path.write_text(new_content, encoding="utf-8")
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

        replacements = count if replace_all else 1
        return ToolResult(
            output=f"Successfully edited {file_path} ({replacements} replacement(s))"
        )

    return definition, handler
