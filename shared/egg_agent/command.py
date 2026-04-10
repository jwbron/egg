"""Build agent commands for container execution.

The orchestrator spawns containers that run the Agent SDK or egg harness
via ``python3 -m egg_agent`` or ``python3 -m egg_harness``.  This module
provides :func:`build_agent_command` which returns the command list passed
to :func:`spawn_agent_container`.

Harness selection is controlled by the ``EGG_HARNESS`` env var:
- ``egg`` — use the egg harness (``python3 -m egg_harness``)
- ``claude-sdk`` — use Claude Agent SDK (``python3 -m egg_agent``) [default]
"""

from __future__ import annotations

import os


def get_harness() -> str:
    """Get the configured harness.

    Returns ``"egg"`` or ``"claude-sdk"`` based on the ``EGG_HARNESS``
    environment variable. Defaults to ``"claude-sdk"`` during the
    transition period.
    """
    harness = os.environ.get("EGG_HARNESS", "claude-sdk").strip().lower()
    if harness in ("egg", "egg-harness", "egg_harness"):
        return "egg"
    return "claude-sdk"


def build_agent_command(
    prompt: str,
    *,
    model: str = "opus",
    max_turns: int = 200,
    system_prompt: str | None = None,
    harness: str | None = None,
) -> list[str]:
    """Build a container command list for running a Claude agent.

    Routes to either ``python3 -m egg_harness`` or ``python3 -m egg_agent``
    depending on the harness selection.

    Args:
        prompt: The prompt text (passed as the last positional argument).
        model: Model alias or ID (default: ``"opus"``).
        max_turns: Maximum conversation turns (default: 200).
        system_prompt: Optional system prompt override.
        harness: Override harness selection (``"egg"`` or ``"claude-sdk"``).
            Defaults to the ``EGG_HARNESS`` env var.

    Returns:
        Command list suitable for container execution.
    """
    effective_harness = harness or get_harness()

    if effective_harness == "egg":
        module = "egg_harness"
    else:
        module = "egg_agent"

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
