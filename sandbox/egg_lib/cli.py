"""CLI argument parsing and entry point for jib.

This module contains the main() function and argument parser setup.
"""

import argparse
import shutil

# Import statusbar for initialization
from statusbar import init_statusbar

from .config import Config
from .docker import check_docker, check_docker_permissions, set_force_rebuild
from .network_mode import (
    PrivateMode,
    ensure_gateway_mode,
)
from .output import error, info, set_quiet_mode, success, warn
from .runtime import exec_in_new_container, run_claude
from .setup_flow import check_host_setup, run_setup_script
from .timing import _host_timer


def main():
    parser = argparse.ArgumentParser(
        description="Run Claude Code CLI in an isolated Docker container (egg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  jib                                      # Run Claude Code (progress bar by default, auto-setup if needed)
  jib -v                                   # Run in verbose mode (detailed output)
  jib --time                               # Show startup timing breakdown for debugging
  jib --setup                              # Run full setup (delegates to setup.py)
  jib --reset                              # Reset configuration and remove Docker image
  jib --rebuild                            # Force rebuild Docker image (even if files unchanged)
  jib --exec <command> [args...]          # Execute command in new ephemeral container
  jib --timeout 60 --exec <command>       # Execute with custom timeout (60 minutes)

Network modes:
  jib --public                             # Public mode: full internet + public repos only (default)
  jib --private                            # Private mode: network lockdown + private repos only

Note: --exec spawns a new container for each execution (automatic cleanup with --rm)
      Default timeout is 30 minutes, configurable via --timeout
      If setup is incomplete, jib will prompt to run setup automatically
      Default shows progress bar; use -v for verbose output
      Use --rebuild if container seems stale (forces fresh Docker build)
        """,
    )
    parser.add_argument(
        "--setup", action="store_true", help="Run full jib setup (services, config, Docker image)"
    )
    parser.add_argument("--reset", action="store_true", help="Clear configuration and start over")
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="MINUTES",
        help="Timeout in minutes for --exec commands (default: 30)",
    )
    parser.add_argument(
        "--auth",
        choices=["api-key", "oauth-token"],
        default="oauth-token",
        help="Anthropic authentication method for --exec: 'oauth-token' (default) or 'api-key'",
    )
    parser.add_argument(
        "--exec",
        nargs=argparse.REMAINDER,
        help="Execute a command in a new ephemeral container (automatic cleanup)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose mode: show detailed output instead of progress bar (default: quiet with progress bar)",
    )
    parser.add_argument(
        "--time",
        action="store_true",
        help="Show startup timing breakdown for debugging slow startup",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild of Docker image even if files haven't changed",
    )

    # Private mode arguments (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--public",
        action="store_true",
        help="Public mode: full internet access + public repos only (default).",
    )
    mode_group.add_argument(
        "--private",
        action="store_true",
        help="Private mode: network lockdown (Anthropic API only) + private repos only.",
    )

    args = parser.parse_args()

    # Enable timing if --time flag is set
    if args.time:
        _host_timer.enabled = True

    # Initialize quiet mode globally
    # Quiet is the default; verbose (-v) overrides it
    quiet_mode = not args.verbose
    set_quiet_mode(quiet_mode)

    # Initialize force rebuild flag
    set_force_rebuild(args.rebuild)

    if quiet_mode:
        # Initialize statusbar with estimated steps for interactive mode
        # Steps: check docker image, check auth, build image, prepare container,
        #        create worktrees, configure mounts, configure github, launch
        init_statusbar(total_steps=8, enabled=True)

    # Determine mode from CLI flags (default: public)
    # No persistent state - mode is determined purely from flags each invocation
    if args.private:
        requested_mode = PrivateMode.PRIVATE
    else:
        # Default to public mode (explicit --public or no flag)
        requested_mode = PrivateMode.PUBLIC

    # Ensure gateway is running with the correct mode
    if not ensure_gateway_mode(requested_mode, quiet=quiet_mode):
        error("Failed to ensure gateway is in correct mode")
        return 1

    # Handle reset
    if args.reset:
        warn("Resetting configuration...")
        if Config.CONFIG_DIR.exists():
            shutil.rmtree(Config.CONFIG_DIR)

        # Ask about persistent directories
        if Config.SHARING_DIR.exists():
            print()
            warn("Persistent directories found:")
            if Config.SHARING_DIR.exists():
                print(f"  • {Config.SHARING_DIR} (shared artifacts, context documents)")
            print()
            response = input("Remove these as well? (yes/no): ").strip().lower()
            if response == "yes":
                if Config.SHARING_DIR.exists():
                    shutil.rmtree(Config.SHARING_DIR)
                    warn(f"Removed: {Config.SHARING_DIR}")
            else:
                info("Preserved persistent directories")

        success("Configuration reset. Run again to set up fresh.")
        return 0

    # Check prerequisites
    if not check_docker():
        return 1

    if not check_docker_permissions():
        return 1

    # Check host setup (services and directories)
    if not check_host_setup():
        return 1

    # Handle setup - delegate to setup.py
    if args.setup:
        info("Delegating to setup.py for complete jib configuration...")
        print()
        if not run_setup_script():
            return 1
        return 0

    # Mode is already determined from CLI flags above
    repo_mode = requested_mode.value

    # Handle exec - execute in a new ephemeral container
    if args.exec:
        if not exec_in_new_container(
            args.exec, timeout_minutes=args.timeout, auth_mode=args.auth, repo_mode=repo_mode
        ):
            return 1
        return 0

    # Normal run
    if not run_claude(repo_mode=repo_mode):
        return 1

    return 0
