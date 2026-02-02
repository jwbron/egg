"""Exec command implementation."""

import argparse
import sys

from ..docker import (
    SANDBOX_CONTAINER,
    check_docker,
    container_running,
    exec_in_container,
)


def run_exec(args: argparse.Namespace) -> int:
    """Execute a command in the egg sandbox container.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check Docker is available
    ok, error = check_docker()
    if not ok:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    # Get command to execute
    cmd = getattr(args, "cmd", [])
    if not cmd:
        print("Error: No command specified", file=sys.stderr)
        print("Usage: egg exec <command> [args...]", file=sys.stderr)
        return 1

    # Check if sandbox container is running
    if not container_running(SANDBOX_CONTAINER):
        print(f"Error: Sandbox container '{SANDBOX_CONTAINER}' is not running", file=sys.stderr)
        print("Start the sandbox with: egg start", file=sys.stderr)
        return 1

    # Execute command in container
    result = exec_in_container(
        name=SANDBOX_CONTAINER,
        command=cmd,
        workdir="/home/sandbox/repos",
        interactive=True,
        tty=sys.stdin.isatty(),
    )

    return result.returncode
