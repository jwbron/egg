"""Bash tool — shell command execution for the egg harness.

Provides :func:`create_bash_tool` which returns a tool definition and async
handler for executing shell commands inside the agent sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)


def create_bash_tool(
    cwd: str | None = None,
    timeout: int = 120,
) -> tuple[ToolDefinition, ToolHandler]:
    """Create a Bash tool definition and handler.

    Args:
        cwd: Working directory for commands.  ``None`` uses the current
            directory at the time of execution.
        timeout: Default command timeout in seconds.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="Bash",
        description=(
            "Executes a given bash command and returns its output. "
            "The command is run in a non-interactive bash shell."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Optional timeout in seconds. Defaults to the tool's "
                        "configured timeout."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "A short human-readable description of what this "
                        "command does."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        command: str = input["command"]
        cmd_timeout: int = input.get("timeout", timeout)

        effective_cwd = cwd or os.getcwd()

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
                start_new_session=True,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=cmd_timeout,
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            output_parts: list[str] = []
            if stdout:
                output_parts.append(stdout)
            if stderr:
                output_parts.append(stderr)
            output = "\n".join(output_parts) if output_parts else ""

            return ToolResult(
                output=output,
                is_error=process.returncode != 0,
            )

        except TimeoutError:
            # Kill the entire process group.
            if process is not None and process.pid is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        pass

            return ToolResult(
                output=f"Command timed out after {cmd_timeout} seconds.",
                is_error=True,
            )

        except Exception as exc:
            return ToolResult(
                output=f"Failed to execute command: {exc}",
                is_error=True,
            )

    return definition, handler


# ---------------------------------------------------------------------------
# Synchronous convenience wrapper
# ---------------------------------------------------------------------------

@dataclass
class BashResult:
    """Result from a synchronous bash command execution."""

    output: str
    exit_code: int


def execute_bash(command: str, *, cwd: str | None = None, timeout: int = 120) -> BashResult:
    """Synchronous convenience wrapper for bash command execution."""
    import subprocess

    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
            output = stdout.decode("utf-8", errors="replace")
            return BashResult(output=output, exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            return BashResult(output="Command timed out", exit_code=-1)
    except Exception as e:
        return BashResult(output=str(e), exit_code=-1)
