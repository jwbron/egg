"""Edit tool — exact string replacement in files."""

from __future__ import annotations

from typing import Any

from egg_harness.tools.registry import ToolImpl


class EditTool(ToolImpl):
    def __init__(self) -> None:
        super().__init__(
            name="Edit",
            description="Perform exact string replacement in a file.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "old_string": {"type": "string", "description": "The text to replace"},
                    "new_string": {"type": "string", "description": "The replacement text"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences",
                        "default": False,
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        file_path = input_data["file_path"]
        old_string = input_data["old_string"]
        new_string = input_data["new_string"]
        replace_all = input_data.get("replace_all", False)

        if old_string == new_string:
            return "Error: old_string and new_string are identical"

        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except Exception as e:
            return f"Error reading {file_path}: {e}"

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}"

        if count > 1 and not replace_all:
            return (
                f"Error: old_string found {count} times in {file_path}. "
                f"Use replace_all=true or provide more context to make the match unique."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            replacements = count if replace_all else 1
            return f"Successfully edited {file_path} ({replacements} replacement{'s' if replacements > 1 else ''})"
        except Exception as e:
            return f"Error writing {file_path}: {e}"
