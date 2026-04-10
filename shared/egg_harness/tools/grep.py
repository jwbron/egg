"""Grep tool — content search via ripgrep for the egg harness.

Provides :func:`create_grep_tool` which returns a tool definition and async
handler for searching file contents using ``rg`` (ripgrep).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)

# Default head limit for results.
_DEFAULT_HEAD_LIMIT: int = 250


def create_grep_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a Grep tool definition and handler.

    The handler shells out to the ``rg`` (ripgrep) binary.  ``rg`` must be
    available on ``$PATH``.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Grep",
        description=(
            "A powerful search tool built on ripgrep. Supports full regex "
            "syntax, file filtering, and multiple output modes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "The regular expression pattern to search for in "
                        "file contents."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search in. Defaults to the "
                        "current working directory."
                    ),
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "Output mode: 'content' shows matching lines, "
                        "'files_with_matches' shows file paths (default), "
                        "'count' shows match counts."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern to filter files "
                        "(e.g. '*.js', '*.{ts,tsx}')."
                    ),
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "Limit output to first N lines/entries. "
                        "Defaults to 250."
                    ),
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Number of lines to show before and after each "
                        "match. Only applies to 'content' output mode."
                    ),
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        pattern: str = input["pattern"]
        path: str | None = input.get("path")
        output_mode: str = input.get("output_mode", "files_with_matches")
        glob_filter: str | None = input.get("glob")
        head_limit: int = input.get("head_limit", _DEFAULT_HEAD_LIMIT)
        context: int | None = input.get("context")

        # Build the rg command as a list of arguments (NEVER shell=True).
        cmd: list[str] = ["rg"]

        # Output mode flags
        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif output_mode == "count":
            cmd.append("--count")
        # "content" is the default rg output mode — no flag needed.

        # Context lines (only meaningful for content mode)
        if context is not None and output_mode == "content":
            cmd.extend(["-C", str(context)])

        # Line numbers for content mode
        if output_mode == "content":
            cmd.append("-n")

        # Glob filter
        if glob_filter is not None:
            cmd.extend(["--glob", glob_filter])

        # The search pattern
        cmd.append(pattern)

        # Search path (if provided)
        if path is not None:
            cmd.append(path)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=60,
            )
        except FileNotFoundError:
            return ToolResult(
                output="ripgrep (rg) is not installed or not on PATH.",
                is_error=True,
            )
        except TimeoutError:
            return ToolResult(
                output="Grep search timed out after 60 seconds.",
                is_error=True,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # rg returns exit code 1 when no matches are found (not an error)
        if process.returncode not in (0, 1):
            return ToolResult(
                output=stderr or f"rg exited with code {process.returncode}",
                is_error=True,
            )

        if not stdout.strip():
            return ToolResult(output="No matches found.")

        # Apply head_limit
        lines = stdout.splitlines()
        if head_limit > 0 and len(lines) > head_limit:
            lines = lines[:head_limit]
            lines.append(
                f"\n[Output truncated to {head_limit} entries. "
                f"Use head_limit to see more.]"
            )

        return ToolResult(output="\n".join(lines))

    return definition, handler
