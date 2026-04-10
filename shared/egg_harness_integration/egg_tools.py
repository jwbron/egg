"""Egg-native tools — shell out to CLIs.

Per HITL decision #3, these tools shell out to CLI binaries initially.
Native Python implementations come incrementally post-MVP.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from egg_harness.tools.registry import ToolImpl


class EggOrchTool(ToolImpl):
    """Orchestrator CLI operations."""

    def __init__(self) -> None:
        super().__init__(
            name="EggOrch",
            description="Run egg-orch orchestrator CLI commands.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The egg-orch subcommand and arguments (e.g., 'consensus status')",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        return await _run_cli("egg-orch", input_data["command"])


class EggContractTool(ToolImpl):
    """SDLC contract operations."""

    def __init__(self) -> None:
        super().__init__(
            name="EggContract",
            description="Run egg-contract SDLC contract CLI commands.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The egg-contract subcommand and arguments",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        return await _run_cli("egg-contract", input_data["command"])


class EggCheckpointTool(ToolImpl):
    """Checkpoint browsing operations."""

    def __init__(self) -> None:
        super().__init__(
            name="EggCheckpoint",
            description="Run egg-checkpoint browsing CLI commands.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The egg-checkpoint subcommand and arguments",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        return await _run_cli("egg-checkpoint", input_data["command"])


class GitOpsTool(ToolImpl):
    """Git operations routed through gateway."""

    def __init__(self) -> None:
        super().__init__(
            name="GitOps",
            description="Run git operations (routed through the gateway for policy enforcement).",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Git subcommand and arguments (e.g., 'push origin HEAD')",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        return await _run_cli("git", input_data["command"])


class GhCliTool(ToolImpl):
    """GitHub CLI operations routed through gateway."""

    def __init__(self) -> None:
        super().__init__(
            name="GhCli",
            description="Run GitHub CLI (gh) operations (routed through the gateway).",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "gh subcommand and arguments (e.g., 'pr create --title ...')",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        return await _run_cli("gh", input_data["command"])


async def _run_cli(binary: str, args_str: str) -> str:
    """Run a CLI command without shell=True.

    Splits args_str and executes directly.
    """
    import shlex

    try:
        args = shlex.split(args_str)
    except ValueError as e:
        return f"Error parsing arguments: {e}"

    cmd = [binary] + args

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        output = ""
        if stdout:
            output += stdout.decode("utf-8", errors="replace")
        if stderr:
            output += stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            output = f"Exit code {proc.returncode}\n{output}"

        return output or "(no output)"

    except TimeoutError:
        return f"Command timed out: {' '.join(cmd)}"
    except FileNotFoundError:
        return f"Command not found: {binary}"
    except Exception as e:
        return f"Error: {e}"
