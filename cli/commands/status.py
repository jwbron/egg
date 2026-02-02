"""Status command implementation."""

import argparse
import sys

from ..docker import (
    GATEWAY_CONTAINER,
    SANDBOX_CONTAINER,
    check_docker,
    container_exists,
    container_running,
    get_container_status,
)


def run_status(args: argparse.Namespace) -> int:
    """Show status of the egg sandbox environment.

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

    print("Egg Sandbox Status")
    print("=" * 40)

    # Check gateway container
    print(f"\nGateway ({GATEWAY_CONTAINER}):")
    if container_running(GATEWAY_CONTAINER):
        status = get_container_status(GATEWAY_CONTAINER)
        print("  Status: RUNNING")
        if status:
            started = status.get("StartedAt", "unknown")
            print(f"  Started: {started}")
    elif container_exists(GATEWAY_CONTAINER):
        status = get_container_status(GATEWAY_CONTAINER)
        print("  Status: STOPPED")
        if status:
            finished = status.get("FinishedAt", "unknown")
            exit_code = status.get("ExitCode", "unknown")
            print(f"  Finished: {finished}")
            print(f"  Exit Code: {exit_code}")
    else:
        print("  Status: NOT CREATED")

    # Check sandbox container
    print(f"\nSandbox ({SANDBOX_CONTAINER}):")
    if container_running(SANDBOX_CONTAINER):
        status = get_container_status(SANDBOX_CONTAINER)
        print("  Status: RUNNING")
        if status:
            started = status.get("StartedAt", "unknown")
            print(f"  Started: {started}")
    elif container_exists(SANDBOX_CONTAINER):
        status = get_container_status(SANDBOX_CONTAINER)
        print("  Status: STOPPED")
        if status:
            finished = status.get("FinishedAt", "unknown")
            exit_code = status.get("ExitCode", "unknown")
            print(f"  Finished: {finished}")
            print(f"  Exit Code: {exit_code}")
    else:
        print("  Status: NOT CREATED")

    # Check networks
    print("\nNetworks:")
    import subprocess

    for network in ["egg-isolated", "egg-external"]:
        result = subprocess.run(
            ["docker", "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        exists = network in result.stdout
        print(f"  {network}: {'EXISTS' if exists else 'NOT CREATED'}")

    return 0
