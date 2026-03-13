"""
Claude Code interactive mode runner.

This module provides the interactive mode entry point for Claude Code.
For programmatic mode, use llm.claude.runner directly.
"""

import os
import shutil
import sys


def build_claude_cmd() -> list[str]:
    """Build the base Claude CLI command with standard flags.

    Returns the command list starting with the resolved binary path and
    ``--dangerously-skip-permissions``.  Callers can extend the list with
    additional flags (e.g. ``--model``, ``--append-system-prompt``).

    Raises:
        FileNotFoundError: If the ``claude`` binary is not on PATH.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise FileNotFoundError(
            "'claude' not found in PATH. Rebuild the sandbox image with: egg --reset"
        )
    return [claude_bin, "--dangerously-skip-permissions"]


def run_interactive() -> None:
    """Launch Claude Code CLI in interactive mode.

    This function does not return - it replaces the current process
    with the Claude CLI.

    Example:
        from llm import run_interactive

        # Use environment defaults (API key or OAuth)
        run_interactive()
    """
    try:
        cmd = build_claude_cmd()
    except FileNotFoundError as e:
        print(f"[llm] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    cmd.extend(["--model", "opus[1m]"])

    # Set up environment for Claude
    env = os.environ.copy()
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_COST_WARNINGS", "1")
    env.setdefault("NO_PROXY", "127.0.0.1")

    print("[llm] Launching Claude Code...")
    os.execvpe(cmd[0], cmd, env)
