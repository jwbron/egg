"""Start command implementation."""

import argparse
import sys

from ..config import load_config, validate_config
from ..docker import (
    GATEWAY_CONTAINER,
    GATEWAY_ISOLATED_IP,
    check_docker,
    container_running,
    create_network,
)


def run_start(args: argparse.Namespace) -> int:
    """Start the egg sandbox environment.

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

    # Load configuration
    try:
        config = load_config(args.config if hasattr(args, "config") else None)
    except FileNotFoundError as e:
        print(f"Error: Configuration file not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    # Validate configuration
    is_valid, errors = validate_config(config)
    if not is_valid:
        print("Configuration validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # Ensure directories exist
    config.ensure_directories()

    # Determine network mode
    private_mode = getattr(args, "private", False) or config.private_mode

    # Create networks
    from ..docker import EXTERNAL_SUBNET, ISOLATED_SUBNET

    print("Creating Docker networks...")
    if not create_network("egg-isolated", ISOLATED_SUBNET):
        print("Error: Failed to create egg-isolated network", file=sys.stderr)
        return 1
    if not create_network("egg-external", EXTERNAL_SUBNET):
        print("Error: Failed to create egg-external network", file=sys.stderr)
        return 1

    # Check if gateway is already running
    if container_running(GATEWAY_CONTAINER):
        print(f"Gateway container '{GATEWAY_CONTAINER}' is already running")
    else:
        print("Starting gateway container...")
        # TODO: Implement gateway container start
        # For now, show instructions
        print("  Note: Gateway container start not yet implemented")
        print("  Run manually with:")
        print(f"    docker run -d --name {GATEWAY_CONTAINER} \\")
        print("      --network egg-isolated \\")
        print(f"      --ip {GATEWAY_ISOLATED_IP} \\")
        print("      -p 9847:9847 -p 3128:3128 \\")
        print("      egg-gateway")

    # Determine mode description
    mode_desc = "PRIVATE (network lockdown)" if private_mode else "PUBLIC (full internet)"
    print(f"\nNetwork mode: {mode_desc}")

    if not config.repositories:
        print("\nWarning: No repositories configured")
        print("Add repositories to egg.yaml or use 'egg config' to configure")
        return 0

    print(f"\nRepositories ({len(config.repositories)}):")
    for repo in config.repositories:
        name = repo.name or repo.path.name
        print(f"  - {name}: {repo.path}")

    # TODO: Implement full container startup
    print("\nSandbox environment ready to start")
    print("Note: Full container startup not yet implemented")

    return 0
