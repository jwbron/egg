#!/usr/bin/env python3
"""
CLI entry points for egg_logging tool wrappers.

These entry points provide drop-in replacements for git, gh, and claude
commands that automatically add logging. They pass through all arguments
to the underlying tool while capturing invocation metadata.

Usage:
    # Direct Python invocation
    python -m egg_logging.cli git status

    # Or via installed entry points (if configured in pyproject.toml)
    egg-git status
    egg-gh pr list
    egg-claude -p "Hello"

Environment:
    EGG_LOGGING_PASSTHROUGH: Set to "1" to skip logging entirely
    EGG_LOGGING_QUIET: Set to "1" to suppress wrapper messages
"""

import os
import sys


def _run_wrapper(wrapper_class: type, tool_name: str) -> int:
    """Run a wrapper with sys.argv arguments.

    Args:
        wrapper_class: The wrapper class to instantiate
        tool_name: Name of the tool for help messages

    Returns:
        Exit code from the command
    """
    # Check for passthrough mode (skip logging entirely)
    if os.environ.get("EGG_LOGGING_PASSTHROUGH") == "1":
        import subprocess

        result = subprocess.run([tool_name] + sys.argv[1:], check=False)
        return result.returncode

    # Get arguments (skip the script name)
    args = sys.argv[1:]

    wrapper = wrapper_class()
    result = wrapper.run(*args)

    # Print stdout/stderr to match original tool behavior
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    return int(result.exit_code)


def git_main() -> int:
    """Entry point for egg-git command."""
    from .wrappers.git import GitWrapper

    return _run_wrapper(GitWrapper, "git")


def gh_main() -> int:
    """Entry point for egg-gh command."""
    from .wrappers.gh import GhWrapper

    return _run_wrapper(GhWrapper, "gh")


def claude_main() -> int:
    """Entry point for egg-claude command."""
    from .wrappers.claude import ClaudeWrapper

    return _run_wrapper(ClaudeWrapper, "claude")


def main() -> int:
    """Dispatcher for 'python -m egg_logging.cli <tool> [args...]'."""
    if len(sys.argv) < 2:
        print("Usage: python -m egg_logging.cli <tool> [args...]", file=sys.stderr)
        print("Tools: git, gh, claude", file=sys.stderr)
        return 1

    tool = sys.argv[1]
    # Remove the tool name from argv so wrappers see correct args
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    dispatch = {
        "git": git_main,
        "gh": gh_main,
        "claude": claude_main,
    }

    if tool not in dispatch:
        print(f"Unknown tool: {tool}", file=sys.stderr)
        print(f"Available tools: {', '.join(dispatch.keys())}", file=sys.stderr)
        return 1

    return dispatch[tool]()


if __name__ == "__main__":
    sys.exit(main())
