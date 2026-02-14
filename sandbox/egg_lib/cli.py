"""CLI argument parsing and entry point for egg.

This module contains the main() function and argument parser setup,
plus the ``gha_exec()`` entry point for GitHub Actions.
"""

import argparse
import os
import shutil

# Import statusbar for initialization
from statusbar import init_statusbar

from .config import Config
from .context import RuntimeContext, set_context
from .docker import check_docker, check_docker_permissions, set_force_rebuild
from .network_mode import (
    PrivateMode,
    ensure_gateway_mode,
)
from .output import error, info, set_quiet_mode, success, warn
from .runtime import exec_in_new_container, run_claude
from .setup_flow import check_host_setup, setup
from .timing import _host_timer


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="Run Claude Code CLI in an isolated Docker container (egg)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  egg                                      # Run Claude Code (progress bar by default, auto-setup if needed)
  egg -v                                   # Run in verbose mode (detailed output)
  egg --time                               # Show startup timing breakdown for debugging
  egg --setup                              # Run interactive setup wizard
  egg --reset                              # Reset configuration and remove Docker image
  egg --rebuild                            # Force rebuild Docker image (even if files unchanged)
  egg --exec <command> [args...]          # Execute command in new ephemeral container
  egg --timeout 60 --exec <command>       # Execute with custom timeout (60 minutes)

Network modes:
  egg --public                             # Public mode: full internet + public repos only (default)
  egg --private                            # Private mode: network lockdown + private repos only

Docker Compose control:
  egg --compose --down                     # Stop the compose stack (gateway + orchestrator)
  egg --compose --build                    # Rebuild compose images before starting

Note: --exec spawns a new container for each execution (automatic cleanup with --rm)
      Default timeout is 30 minutes, configurable via --timeout
      If setup is incomplete, egg will prompt to run setup automatically
      Default shows progress bar; use -v for verbose output
      Use --rebuild if container seems stale (forces fresh Docker build)
        """,
    )
    parser.add_argument(
        "--setup", action="store_true", help="Run full egg setup (services, config, Docker image)"
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

    # Docker Compose control arguments
    parser.add_argument(
        "--compose",
        action="store_true",
        help="Explicit compose control (use with --down or --build). "
        "Default egg path already uses compose.",
    )
    parser.add_argument(
        "--down",
        action="store_true",
        help="Stop the Docker Compose stack (use with --compose)",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Rebuild compose images before starting (use with --compose)",
    )

    # SDLC pipeline with token-gated approvals
    parser.add_argument(
        "--sdlc",
        type=int,
        metavar="ISSUE",
        help="Start SDLC pipeline with token-gated approvals for the given issue number",
    )
    import argparse as _argparse

    parser.add_argument(
        "--multi-agent",
        dest="multi_agent",
        action=_argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable multi-agent execution (--multi-agent / --no-multi-agent)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="Maximum parallel agents per wave (default: 10)",
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

    # Handle explicit compose control (--compose --down or --compose --build)
    if args.compose and (args.down or args.build):
        from .compose import run_compose_mode

        return run_compose_mode(down=args.down, build=args.build)

    # Enable timing if --time flag is set
    if args.time:
        _host_timer.enabled = True

    # Initialize quiet mode globally
    # Quiet is the default; verbose (-v) overrides it
    # Setup and reset are always verbose (interactive)
    quiet_mode = not args.verbose and not args.setup and not args.reset
    set_quiet_mode(quiet_mode)

    # Initialize force rebuild flag
    set_force_rebuild(args.rebuild)

    if quiet_mode:
        # Initialize statusbar with estimated steps for interactive mode
        # Steps: check image, build image, start gateway, prepare container,
        #        configure mounts, launch Claude
        init_statusbar(total_steps=6, enabled=True)

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
        if Config.USER_CONFIG_DIR.exists():
            print()
            warn(f"User configuration exists: {Config.USER_CONFIG_DIR}")
            response = input("Remove user configuration as well? (yes/no): ").strip().lower()
            if response == "yes":
                shutil.rmtree(Config.USER_CONFIG_DIR)
                warn(f"Removed: {Config.USER_CONFIG_DIR}")
            else:
                info("Preserved user configuration")

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

    # Handle setup - run interactive setup
    if args.setup:
        if not setup():
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
    if not run_claude(repo_mode=repo_mode, sdlc_issue=args.sdlc):
        return 1

    return 0


def gha_exec() -> int:
    """Entry point for GitHub Actions — called by ``action/entrypoint.sh``.

    Reads configuration from ``EGG_*`` environment variables (set by the
    action shell script) and orchestrates the full GHA flow:

    1. Build ``RuntimeContext`` from environment
    2. Create networks (dynamic subnet allocation)
    3. Start gateway container (pre-built image)
    4. Detect mode from ``GITHUB_EVENT_REPOSITORY_VISIBILITY``
    5. Build claude command from ``INPUT_PROMPT``, ``INPUT_MODEL``, etc.
    6. Execute in ephemeral container via ``exec_in_new_container()``
    7. Cleanup (ephemeral flag triggers gateway + network teardown)

    Returns:
        Exit code (0 = success)
    """
    from .docker import ensure_gateway_networks
    from .gateway import start_gateway_container as start_gw

    # 1. Build context from environment
    ctx = RuntimeContext.from_environment()
    set_context(ctx)

    # Verbose output for CI logs
    set_quiet_mode(False)

    info("GHA exec: starting orchestration")
    info(f"  gateway_image={ctx.gateway_image}")
    info(f"  sandbox_image={ctx.sandbox_image}")
    info(f"  isolated_network={ctx.isolated_network}")
    info(f"  external_network={ctx.external_network}")
    info(f"  ephemeral={ctx.ephemeral}")

    # 2. Create networks (dynamic subnets when "auto")
    if not ensure_gateway_networks():
        error("Failed to create gateway networks")
        return 1

    # 3. Start gateway container
    if not start_gw():
        error("Failed to start gateway container")
        return 1

    # 4. Detect mode
    mode_input = os.environ.get("INPUT_MODE", "auto")
    if mode_input == "auto":
        repo_vis = os.environ.get("GITHUB_EVENT_REPOSITORY_VISIBILITY", "public")
        mode = "private" if repo_vis in ("private", "internal") else "public"
        info(f"Auto-detected mode: {mode} (visibility={repo_vis})")
    else:
        mode = mode_input
        info(f"Configured mode: {mode}")

    # 5. Build claude command
    prompt = os.environ.get("INPUT_PROMPT", "")
    model = os.environ.get("INPUT_MODEL", "opus")
    timeout = int(os.environ.get("INPUT_TIMEOUT", "30"))

    # --max-turns 200: Ensure agent has enough turns to complete work and post
    # comments. Default (100) was observed to be insufficient for tasks requiring
    # codebase exploration + implementation + testing + comment posting.
    command = [
        "claude",
        "--dangerously-skip-permissions",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--max-turns",
        "200",
        prompt,
    ]

    # 6. Execute
    # Build extra env for container (e.g., EGG_BOT_NAME for review markers)
    extra_env: dict[str, str] = {}
    bot_name = os.environ.get("EGG_BOT_NAME")
    if bot_name:
        extra_env["EGG_BOT_NAME"] = bot_name

    # Pass issue number so egg-contract CLI can find the contract
    issue_number = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_number:
        extra_env["EGG_ISSUE_NUMBER"] = issue_number

    # Pass commit SHA so the gh wrapper can pin the review marker to the
    # commit that was actually checked out, avoiding races with new pushes
    commit_sha = os.environ.get("EGG_COMMIT_SHA")
    if commit_sha:
        extra_env["EGG_COMMIT_SHA"] = commit_sha

    # Pass agent role for gateway authorization (e.g., reviewer role)
    agent_role = os.environ.get("EGG_AGENT_ROLE")
    if agent_role:
        extra_env["EGG_AGENT_ROLE"] = agent_role

    success_flag = exec_in_new_container(
        command=command,
        timeout_minutes=timeout,
        auth_mode="oauth-token",
        repo_mode=mode,
        extra_env=extra_env if extra_env else None,
    )

    return 0 if success_flag else 1
