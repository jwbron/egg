#!/usr/bin/env python3
"""
CLI entry points for jib_logging tool wrappers.

These entry points provide drop-in replacements for bd, git, gh, and claude
commands that automatically add logging. They pass through all arguments
to the underlying tool while capturing invocation metadata.

Usage:
    # Direct Python invocation
    python -m jib_logging.cli bd --allow-stale list

    # Or via installed entry points (if configured in pyproject.toml)
    jib-bd --allow-stale list
    jib-git status
    jib-gh pr list
    jib-claude -p "Hello"

Environment:
    JIB_LOGGING_PASSTHROUGH: Set to "1" to skip logging entirely
    JIB_LOGGING_QUIET: Set to "1" to suppress wrapper messages
"""

import os
import sys


def _run_wrapper(wrapper_class, tool_name: str) -> int:
    """Run a wrapper with sys.argv arguments.

    Args:
        wrapper_class: The wrapper class to instantiate
        tool_name: Name of the tool for help messages

    Returns:
        Exit code from the command
    """
    # Check for passthrough mode (skip logging entirely)
    if os.environ.get("JIB_LOGGING_PASSTHROUGH") == "1":
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

    return result.exit_code


def bd_main() -> int:
    """Entry point for jib-bd command."""
    from .wrappers.bd import BdWrapper

    return _run_wrapper(BdWrapper, "bd")


def git_main() -> int:
    """Entry point for jib-git command."""
    from .wrappers.git import GitWrapper

    return _run_wrapper(GitWrapper, "git")


def gh_main() -> int:
    """Entry point for jib-gh command."""
    from .wrappers.gh import GhWrapper

    return _run_wrapper(GhWrapper, "gh")


def claude_main() -> int:
    """Entry point for jib-claude command."""
    from .wrappers.claude import ClaudeWrapper

    return _run_wrapper(ClaudeWrapper, "claude")


def main() -> int:
    """Dispatcher for 'python -m jib_logging.cli <tool> [args...]'."""
    if len(sys.argv) < 2:
        print("Usage: python -m jib_logging.cli <tool> [args...]", file=sys.stderr)
        print("Tools: bd, git, gh, claude", file=sys.stderr)
        return 1

    tool = sys.argv[1]
    # Remove the tool name from argv so wrappers see correct args
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    dispatch = {
        "bd": bd_main,
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
