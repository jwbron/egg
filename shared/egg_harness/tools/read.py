"""Read tool — file reading for the egg harness.

Provides :func:`create_read_tool` which returns a tool definition and async
handler for reading files with line-number formatting.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)

# Number of bytes to sample when checking for binary content.
_BINARY_CHECK_SIZE: int = 8192

# Default maximum number of lines to return.
_DEFAULT_LIMIT: int = 2000


def create_read_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a Read tool definition and handler.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Read",
        description=(
            "Reads a file from the local filesystem. Results are returned "
            "using cat -n format, with line numbers starting at 1."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The absolute path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "The line number to start reading from (0-based). "
                        "Defaults to 0."
                    ),
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "The number of lines to read. Defaults to 2000."
                    ),
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        file_path = input["file_path"]
        offset: int = input.get("offset", 0)
        limit: int = input.get("limit", _DEFAULT_LIMIT)

        path = Path(file_path)

        # Check existence
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

        # Check for binary content
        try:
            with path.open("rb") as f:
                sample = f.read(_BINARY_CHECK_SIZE)
            if b"\x00" in sample:
                return ToolResult(
                    output=f"Cannot read binary file: {file_path}",
                    is_error=True,
                )
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

        # Read the file as text
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
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

        lines = text.splitlines(keepends=True)

        # Apply offset and limit
        selected = lines[offset : offset + limit]

        # Format with line numbers (1-based, matching offset)
        output_lines: list[str] = []
        for i, line in enumerate(selected, start=offset + 1):
            # Strip the trailing newline for consistent formatting
            output_lines.append(f"{i}\t{line.rstrip(chr(10)).rstrip(chr(13))}")

        output = "\n".join(output_lines)

        return ToolResult(output=output)

    return definition, handler
