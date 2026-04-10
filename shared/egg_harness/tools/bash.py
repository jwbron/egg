"""Bash tool — execute shell commands."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from egg_harness.tools.registry import ToolImpl

# Default 2 minute timeout, max 10 minutes
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000


class BashTool(ToolImpl):
    def __init__(self, *, cwd: str | None = None) -> None:
        super().__init__(
            name="Bash",
            description="Execute a bash command and return its output.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to execute"},
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in milliseconds (max 600000)",
                    },
                },
                "required": ["command"],
            },
        )
        self._cwd = cwd

    async def execute(self, input_data: dict[str, Any]) -> str:
        command = input_data["command"]
        timeout_ms = min(
            input_data.get("timeout", DEFAULT_TIMEOUT_MS),
            MAX_TIMEOUT_MS,
        )
        timeout_s = timeout_ms / 1000

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                preexec_fn=os.setpgrp,  # New process group for clean kill
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                # Kill entire process group
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.sleep(2)
                    if proc.returncode is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                return f"Command timed out after {timeout_s}s"

            output = ""
            if stdout:
                output += stdout.decode("utf-8", errors="replace")
            if stderr:
                output += stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                output = f"Exit code {proc.returncode}\n{output}"

            return output or "(no output)"

        except Exception as e:
            return f"Error: {e}"
