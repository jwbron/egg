"""Glob tool — file pattern matching for the egg harness.

Provides :func:`create_glob_tool` which returns a tool definition and async
handler for finding files by glob patterns, sorted by modification time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)


def create_glob_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a Glob tool definition and handler.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Glob",
        description=(
            "Fast file pattern matching tool that works with any codebase "
            "size. Supports glob patterns like '**/*.js' or 'src/**/*.ts'. "
            "Returns matching file paths sorted by modification time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The directory to search in. If not specified, the "
                        "current working directory will be used."
                    ),
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        pattern: str = input["pattern"]
        search_path: str | None = input.get("path")

        base = Path(search_path) if search_path else Path.cwd()

        if not base.is_dir():
            return ToolResult(
                output=f"Directory not found: {base}",
                is_error=True,
            )

        try:
            matches = list(base.glob(pattern))
        except ValueError as exc:
            return ToolResult(
                output=f"Invalid glob pattern: {exc}",
                is_error=True,
            )

        # Filter to files only and sort by modification time (newest first)
        files = [p for p in matches if p.is_file()]

        def _safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        files.sort(key=_safe_mtime, reverse=True)

        if not files:
            return ToolResult(output="No matches found.")

        output = "\n".join(str(f) for f in files)
        return ToolResult(output=output)

    return definition, handler


# ---------------------------------------------------------------------------
# Synchronous convenience wrapper
# ---------------------------------------------------------------------------


def glob_files(pattern: str, *, path: str | None = None) -> ToolResult:
    """Synchronous convenience wrapper for glob file search."""
    import asyncio

    _, handler = create_glob_tool()
    params: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        params["path"] = path
    return asyncio.run(handler(params))
