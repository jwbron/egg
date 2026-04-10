"""Read tool — read file contents."""

from __future__ import annotations

from typing import Any

from egg_harness.tools.registry import ToolImpl


class ReadTool(ToolImpl):
    def __init__(self) -> None:
        super().__init__(
            name="Read",
            description="Read a file from the filesystem. Returns content with line numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (0-indexed)",
                        "minimum": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of lines to read",
                        "exclusiveMinimum": 0,
                    },
                },
                "required": ["file_path"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        file_path = input_data["file_path"]
        offset = input_data.get("offset", 0)
        limit = input_data.get("limit", 2000)

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except PermissionError:
            return f"Error: Permission denied: {file_path}"
        except IsADirectoryError:
            return f"Error: {file_path} is a directory, not a file"
        except Exception as e:
            return f"Error reading {file_path}: {e}"

        if not lines:
            return "(empty file)"

        # Apply offset and limit
        selected = lines[offset : offset + limit]

        # Format with line numbers (1-indexed, tab-separated like cat -n)
        result_lines = []
        for i, line in enumerate(selected, start=offset + 1):
            result_lines.append(f"{i}\t{line.rstrip()}")

        return "\n".join(result_lines)
