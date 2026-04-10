"""Glob tool — file pattern matching."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from egg_harness.tools.registry import ToolImpl


class GlobTool(ToolImpl):
    def __init__(self, *, default_path: str | None = None) -> None:
        super().__init__(
            name="Glob",
            description="Find files matching a glob pattern.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern to match"},
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: cwd)",
                    },
                },
                "required": ["pattern"],
            },
        )
        self._default_path = default_path

    async def execute(self, input_data: dict[str, Any]) -> str:
        pattern = input_data["pattern"]
        search_path = input_data.get("path") or self._default_path or os.getcwd()

        try:
            base = Path(search_path)
            if not base.is_dir():
                return f"Error: {search_path} is not a directory"

            matches = sorted(base.glob(pattern))
            # Sort by modification time (newest first)
            matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

            if not matches:
                return "No files matched the pattern."

            # Return relative paths
            results = []
            for m in matches[:1000]:  # Cap at 1000 results
                try:
                    results.append(str(m.relative_to(base)))
                except ValueError:
                    results.append(str(m))

            return "\n".join(results)
        except Exception as e:
            return f"Error: {e}"
