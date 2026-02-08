#!/usr/bin/env python3
"""egg-launcher entrypoint.

This module provides the main entry point for the egg-launcher container.
It orchestrates the full egg stack by creating gateway and sandbox containers
on the host Docker daemon.

The launcher:
1. Parses command-line arguments
2. Creates Docker networks if they don't exist
3. Starts the gateway container
4. Waits for gateway health
5. Starts the sandbox container
6. Forwards stdin/stdout for interactive use
7. Cleans up on exit

Usage:
    docker run -it ... ghcr.io/jwbron/egg-launcher:latest [options]

Options:
    --print PROMPT    Run in non-interactive mode with the given prompt
    --private         Enable private mode (network lockdown)
    --public          Enable public mode (default)
    --status-port     Port for status API (default: 8080)
"""

import argparse
import os
import signal
import sys

from api import run_status_api
from lifecycle import EggLifecycleManager


def main() -> int:
    """Main entry point for the launcher."""
    parser = argparse.ArgumentParser(
        description="egg-launcher: Single-container deployment for egg",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--print",
        dest="prompt",
        metavar="PROMPT",
        help="Run in non-interactive mode with the given prompt",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Enable private mode (Anthropic API only)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        default=True,
        help="Enable public mode (default)",
    )
    parser.add_argument(
        "--status-port",
        type=int,
        default=8080,
        help="Port for status API (default: 8080)",
    )
    parser.add_argument(
        "--gateway-image",
        default=os.environ.get("EGG_GATEWAY_IMAGE", "ghcr.io/jwbron/egg-gateway:latest"),
        help="Gateway image to use",
    )
    parser.add_argument(
        "--sandbox-image",
        default=os.environ.get("EGG_SANDBOX_IMAGE", "ghcr.io/jwbron/egg-sandbox:latest"),
        help="Sandbox image to use",
    )
    parser.add_argument(
        "--config-dir",
        default=os.environ.get("EGG_CONFIG_DIR", "/config"),
        help="Path to configuration directory",
    )
    parser.add_argument(
        "--repos-dir",
        default=os.environ.get("EGG_REPOS_DIR", "/repos"),
        help="Path to repositories directory",
    )

    args = parser.parse_args()

    # Determine mode
    mode = "private" if args.private else "public"

    # Create lifecycle manager
    manager = EggLifecycleManager(
        gateway_image=args.gateway_image,
        sandbox_image=args.sandbox_image,
        config_dir=args.config_dir,
        repos_dir=args.repos_dir,
        mode=mode,
    )

    # Set up signal handlers for cleanup
    def signal_handler(signum, frame):
        print(f"\nReceived signal {signum}, cleaning up...")
        manager.cleanup()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Start the stack
        print("Starting egg stack...")
        if not manager.start():
            print("Failed to start egg stack", file=sys.stderr)
            return 1

        # Start status API in background if requested
        if args.status_port:
            run_status_api(manager, port=args.status_port, background=True)

        # Run sandbox
        if args.prompt:
            # Non-interactive mode
            exit_code = manager.run_print_mode(args.prompt)
        else:
            # Interactive mode
            exit_code = manager.run_interactive()

        return exit_code

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    finally:
        # Always clean up
        manager.cleanup()


if __name__ == "__main__":
    sys.exit(main())
