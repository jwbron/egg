"""
Claude Code interactive mode runner.

This module provides the interactive mode entry point for Claude Code.
For programmatic mode, use llm.claude.runner directly.
"""

import os

from egg_config.constants import MODEL_DEFAULT


def run_interactive() -> None:
    """Launch Claude Code CLI in interactive mode.

    This function does not return - it replaces the current process
    with the Claude CLI.

    Example:
        from llm import run_interactive

        # Use environment defaults (API key or OAuth)
        run_interactive()
    """
    cmd = ["claude", "--dangerously-skip-permissions", "--model", MODEL_DEFAULT]

    # Set up environment for Claude
    env = os.environ.copy()
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_COST_WARNINGS", "1")
    env.setdefault("NO_PROXY", "127.0.0.1")

    print(f"[llm] Launching Claude Code with model: {MODEL_DEFAULT}...")
    os.execvpe(cmd[0], cmd, env)
