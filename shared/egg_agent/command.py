"""Build agent commands for container execution.

The orchestrator spawns containers that run the Agent SDK via
``python3 -m egg_agent``.  This module provides :func:`build_agent_command`
which returns the command list passed to :func:`spawn_agent_container`.
"""

from __future__ import annotations


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
    cmd: list[str] = [
        "python3",
        "-m",
        "egg_agent",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    if system_prompt is not None:
        cmd.extend(["--system-prompt", system_prompt])
    cmd.append(prompt)
    return cmd
