"""Grep tool — content search via ripgrep."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from egg_harness.tools.registry import ToolImpl


class GrepTool(ToolImpl):
    def __init__(self, *, default_path: str | None = None) -> None:
        super().__init__(
            name="Grep",
            description="Search file contents using regex patterns (powered by ripgrep).",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "File or directory to search"},
                    "glob": {"type": "string", "description": "Filter files by glob pattern"},
                    "type": {"type": "string", "description": "File type filter (js, py, etc.)"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "Output mode (default: files_with_matches)",
                    },
                    "-i": {"type": "boolean", "description": "Case insensitive"},
                    "-n": {"type": "boolean", "description": "Show line numbers"},
                    "-A": {"type": "number", "description": "Lines after match"},
                    "-B": {"type": "number", "description": "Lines before match"},
                    "-C": {"type": "number", "description": "Context lines"},
                    "context": {"type": "number", "description": "Context lines (alias for -C)"},
                    "head_limit": {"type": "number", "description": "Limit output lines"},
                    "multiline": {"type": "boolean", "description": "Enable multiline mode"},
                },
                "required": ["pattern"],
            },
        )
        self._default_path = default_path

    async def execute(self, input_data: dict[str, Any]) -> str:
        pattern = input_data["pattern"]
        search_path = input_data.get("path") or self._default_path or os.getcwd()
        output_mode = input_data.get("output_mode", "files_with_matches")
        head_limit = input_data.get("head_limit", 250)

        cmd = ["rg", "--no-config"]

        # Output mode
        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif output_mode == "count":
            cmd.append("--count")

        # Options
        if input_data.get("-i"):
            cmd.append("-i")
        if input_data.get("-n", True) and output_mode == "content":
            cmd.append("-n")
        if input_data.get("-A") and output_mode == "content":
            cmd.extend(["-A", str(int(input_data["-A"]))])
        if input_data.get("-B") and output_mode == "content":
            cmd.extend(["-B", str(int(input_data["-B"]))])

        context = input_data.get("-C") or input_data.get("context")
        if context and output_mode == "content":
            cmd.extend(["-C", str(int(context))])

        if input_data.get("glob"):
            cmd.extend(["--glob", input_data["glob"]])
        if input_data.get("type"):
            cmd.extend(["--type", input_data["type"]])
        if input_data.get("multiline"):
            cmd.extend(["-U", "--multiline-dotall"])

        cmd.append(pattern)
        cmd.append(search_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")

            if head_limit and head_limit > 0:
                lines = output.split("\n")
                if len(lines) > head_limit:
                    output = "\n".join(lines[:head_limit])

            if not output.strip():
                return "No matches found."

            return output

        except TimeoutError:
            return "Search timed out after 30s"
        except FileNotFoundError:
            return "Error: ripgrep (rg) not found. Install it to use Grep."
        except Exception as e:
            return f"Error: {e}"
