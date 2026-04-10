"""Write tool — create or overwrite files."""

from __future__ import annotations

import os
from typing import Any

from egg_harness.tools.registry import ToolImpl


class WriteTool(ToolImpl):
    def __init__(self) -> None:
        super().__init__(
            name="Write",
            description="Write content to a file. Creates the file if it doesn't exist.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["file_path", "content"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        file_path = input_data["file_path"]
        content = input_data["content"]

        try:
            # Create parent directories if needed
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote to {file_path}"
        except PermissionError:
            return f"Error: Permission denied: {file_path}"
        except Exception as e:
            return f"Error writing to {file_path}: {e}"
