"""Stop command implementation."""

import argparse
import sys

from ..docker import (
    GATEWAY_CONTAINER,
    SANDBOX_CONTAINER,
    check_docker,
    container_running,
    remove_container,
    stop_container,
)


def run_stop(args: argparse.Namespace) -> int:
    """Stop the egg sandbox environment.

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

    stopped_any = False

    # Stop sandbox container
    if container_running(SANDBOX_CONTAINER):
        print(f"Stopping sandbox container '{SANDBOX_CONTAINER}'...")
        if stop_container(SANDBOX_CONTAINER):
            print(f"  Stopped '{SANDBOX_CONTAINER}'")
            stopped_any = True
            # Remove the container after stopping
            if remove_container(SANDBOX_CONTAINER):
                print(f"  Removed '{SANDBOX_CONTAINER}'")
        else:
            print(f"  Warning: Failed to stop '{SANDBOX_CONTAINER}'", file=sys.stderr)

    # Stop gateway container
    if container_running(GATEWAY_CONTAINER):
        print(f"Stopping gateway container '{GATEWAY_CONTAINER}'...")
        if stop_container(GATEWAY_CONTAINER):
            print(f"  Stopped '{GATEWAY_CONTAINER}'")
            stopped_any = True
            # Remove the container after stopping
            if remove_container(GATEWAY_CONTAINER):
                print(f"  Removed '{GATEWAY_CONTAINER}'")
        else:
            print(f"  Warning: Failed to stop '{GATEWAY_CONTAINER}'", file=sys.stderr)

    if not stopped_any:
        print("No egg containers are running")

    return 0
