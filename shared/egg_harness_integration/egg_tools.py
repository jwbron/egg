"""Egg-native tool registration for the harness.

Registers CLI-wrapping tools (egg-orch, egg-contract, egg-checkpoint, git, gh)
into a :class:`ToolRegistry`, giving headless agents access to the egg
platform CLIs and standard VCS operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

# Timeout for all subprocess-based tools (seconds).
_TOOL_TIMEOUT: int = 120


# ---------------------------------------------------------------------------
# Generic subprocess handler factory
# ---------------------------------------------------------------------------


def _make_cli_handler(
    executable: str,
) -> ToolHandler:
    """Create an async tool handler that shells out to *executable*.

    The handler expects ``{"command": str, "args": list[str] | None}`` in
    the tool input and runs ``[executable, command, *args]`` via
    :func:`asyncio.create_subprocess_exec`.  ``shell=True`` is **never**
    used.

    Args:
        executable: The CLI binary to invoke (e.g. ``"egg-orch"``).

    Returns:
        An async callable suitable for :meth:`ToolRegistry.register`.
    """

    async def _handler(tool_input: dict[str, Any]) -> ToolResult:
        command: str = tool_input.get("command", "")
        args: list[str] = tool_input.get("args") or []

        if not command:
            return ToolResult(
                output=f"Missing required 'command' parameter for {executable}.",
                is_error=True,
            )

        argv = [executable, command] + args

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=_TOOL_TIMEOUT,
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            output_parts: list[str] = []
            if stdout:
                output_parts.append(stdout)
            if stderr:
                output_parts.append(stderr)

            output = "\n".join(output_parts) if output_parts else "(no output)"
            is_error = proc.returncode != 0

            return ToolResult(output=output, is_error=is_error)

        except TimeoutError:
            return ToolResult(
                output=f"Command timed out after {_TOOL_TIMEOUT}s: {' '.join(argv)}",
                is_error=True,
            )
        except FileNotFoundError:
            return ToolResult(
                output=f"Executable not found: {executable}",
                is_error=True,
            )
        except Exception as exc:
            logger.exception("Unexpected error running %s", " ".join(argv))
            return ToolResult(
                output=f"Error executing {executable}: {exc}",
                is_error=True,
            )

    return _handler


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_CLI_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The sub-command to execute.",
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of arguments to pass after the command.",
        },
    },
    "required": ["command"],
}

_EGG_TOOL_SPECS: list[tuple[str, str, str]] = [
    (
        "EggOrch",
        "Orchestrator interactions via egg-orch subcommands: pipeline "
        "status, signaling, health checks, progress events, overseer "
        "alerts, anchor management, and message waits. Use this tool "
        "for every egg-orch operation; pass the subcommand as `command` "
        "and each flag and value as a separate `args` element. Never "
        "invoke egg-orch through the Bash tool: free-text fields such "
        "as --summary, --detail, --recommend, --step, --blocker, "
        "--error, and --task are corrupted (or executed) by shell "
        "metacharacters there.",
        "egg-orch",
    ),
    (
        "EggContract",
        "SDLC contract tracking via egg-contract subcommands: show, "
        "add-commit, complete-task, complete-phase, update-notes, "
        "verify-criterion, add-decision, and add-feedback. Use this "
        "tool for every egg-contract operation; pass the subcommand as "
        "`command` and each flag and value as a separate `args` "
        "element. Never invoke egg-contract through the Bash tool: "
        "free-text fields such as --question, --options, and --notes "
        "are corrupted (or executed) by shell metacharacters there.",
        "egg-contract",
    ),
    (
        "EggCheckpoint",
        "Browse agent checkpoints (transcripts, tool calls, files, "
        "token usage) via egg-checkpoint subcommands. Use this tool "
        "for every egg-checkpoint operation; pass the subcommand as "
        "`command` and each flag and value as a separate `args` "
        "element. Never invoke egg-checkpoint through the Bash tool: "
        "free-text fields such as --text are corrupted (or executed) "
        "by shell metacharacters there.",
        "egg-checkpoint",
    ),
    (
        "GitOps",
        "Execute git commands for version control operations "
        "(status, commit, push, branch, diff, log, etc.).",
        "git",
    ),
    (
        "GhCli",
        "Execute gh (GitHub CLI) commands for GitHub interactions "
        "(PR creation, issue management, API calls, etc.).",
        "gh",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_egg_tools(registry: ToolRegistry) -> None:
    """Register all five egg-native tools into *registry*.

    The tools wrap the following CLIs via subprocess execution:

    - **EggOrch** -- ``egg-orch``
    - **EggContract** -- ``egg-contract``
    - **EggCheckpoint** -- ``egg-checkpoint``
    - **GitOps** -- ``git``
    - **GhCli** -- ``gh``

    Each tool accepts ``{"command": str, "args": list[str]}`` and returns
    the combined stdout/stderr from the invoked process.

    Args:
        registry: The tool registry to populate.
    """
    for tool_name, description, executable in _EGG_TOOL_SPECS:
        definition = ToolDefinition(
            name=tool_name,
            description=description,
            input_schema=_CLI_INPUT_SCHEMA,
        )
        handler = _make_cli_handler(executable)
        registry.register(definition, handler)
