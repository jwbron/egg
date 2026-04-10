"""Build agent commands for container execution.

The orchestrator spawns containers that run the Agent SDK via
``python3 -m egg_agent``.  This module provides :func:`build_agent_command`
which returns the command list passed to :func:`spawn_agent_container`.
"""

from __future__ import annotations

import os


def build_agent_command(
    prompt: str,
    *,
    model: str = "opus",
    max_turns: int = 200,
    system_prompt: str | None = None,
) -> list[str]:
    """Build a container command list for running a Claude agent.

    Returns the ``python3 -m egg_agent`` command that the orchestrator
    passes to ``spawn_agent_container()``.  The entry point wraps the
    Agent SDK (``claude_agent_sdk.query()``), so CLI-specific flags like
    ``--print`` and ``--dangerously-skip-permissions`` are not needed.

    Args:
        prompt: The prompt text (passed as the last positional argument).
        model: Model alias or ID (default: ``"opus"``).
        max_turns: Maximum conversation turns (default: 200).
        system_prompt: Optional system prompt override.

    Returns:
        Command list suitable for container execution.
    """
    # Select the Python module based on the EGG_HARNESS env var.
    # When EGG_HARNESS=egg, route to the new egg_harness module;
    # otherwise default to the existing egg_agent module.
    harness = os.environ.get("EGG_HARNESS", "claude-sdk")
    module = "egg_harness" if harness == "egg" else "egg_agent"

    cmd: list[str] = [
        "python3",
        "-m",
        module,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    if system_prompt is not None:
        cmd.extend(["--system-prompt", system_prompt])
    cmd.append(prompt)
    return cmd
