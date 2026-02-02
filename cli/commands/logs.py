"""Logs command implementation."""

import argparse
import sys

from ..docker import (
    GATEWAY_CONTAINER,
    SANDBOX_CONTAINER,
    check_docker,
    container_exists,
    get_container_logs,
)


def run_logs(args: argparse.Namespace) -> int:
    """View container logs.

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

    follow = getattr(args, "follow", False)
    container = getattr(args, "container", None)
    tail = getattr(args, "tail", None)

    # Determine which container to show logs for
    containers = []
    if container:
        if container == "gateway":
            containers = [GATEWAY_CONTAINER]
        elif container == "sandbox":
            containers = [SANDBOX_CONTAINER]
        else:
            containers = [container]
    else:
        # Default to sandbox if running, otherwise gateway
        if container_exists(SANDBOX_CONTAINER):
            containers = [SANDBOX_CONTAINER]
        elif container_exists(GATEWAY_CONTAINER):
            containers = [GATEWAY_CONTAINER]

    if not containers:
        print("No egg containers found", file=sys.stderr)
        return 1

    for name in containers:
        if not container_exists(name):
            print(f"Container '{name}' does not exist", file=sys.stderr)
            continue

        if len(containers) > 1:
            print(f"=== {name} ===")

        get_container_logs(name, follow=follow, tail=tail)

    return 0
