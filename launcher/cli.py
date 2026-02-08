"""CLI argument parsing for egg-launcher.

This module handles command-line argument parsing for the launcher,
supporting both interactive and --print modes.
"""

import argparse
import os
import sys


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser for egg-launcher.

    Returns:
        Configured ArgumentParser
    """
    parser = argparse.ArgumentParser(
        prog="egg-launcher",
        description="Single-container deployment for egg",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  docker run -it -v ~/.config/egg:/config:ro -v ~/repos:/repos \\
    -v /var/run/docker.sock:/var/run/docker.sock \\
    ghcr.io/jwbron/egg-launcher:latest

  # Print mode (non-interactive)
  docker run -v ~/.config/egg:/config:ro -v ~/repos:/repos \\
    -v /var/run/docker.sock:/var/run/docker.sock \\
    ghcr.io/jwbron/egg-launcher:latest --print "Fix the tests"

  # Private mode (network lockdown)
  docker run -it -v ~/.config/egg:/config:ro -v ~/repos:/repos \\
    -v /var/run/docker.sock:/var/run/docker.sock \\
    ghcr.io/jwbron/egg-launcher:latest --private

Environment Variables:
  EGG_CONFIG_DIR        Path to config directory (default: /config)
  EGG_REPOS_DIR         Path to repos directory (default: /repos)
  EGG_GATEWAY_IMAGE     Gateway image (default: ghcr.io/jwbron/egg-gateway:latest)
  EGG_SANDBOX_IMAGE     Sandbox image (default: ghcr.io/jwbron/egg-sandbox:latest)
  EGG_LAUNCHER_SECRET   Launcher authentication secret
  GITHUB_USER_TOKEN     GitHub personal access token
        """,
    )

    # Mode arguments
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--public",
        action="store_true",
        default=True,
        help="Public mode: full internet access (default)",
    )
    mode_group.add_argument(
        "--private",
        action="store_true",
        help="Private mode: Anthropic API only, network lockdown",
    )

    # Print mode
    parser.add_argument(
        "--print",
        dest="prompt",
        metavar="PROMPT",
        help="Run in non-interactive mode with the given prompt",
    )
    parser.add_argument(
        "--prompt-file",
        metavar="FILE",
        help="Read prompt from file (alternative to --print)",
    )

    # Docker images
    parser.add_argument(
        "--gateway-image",
        default=os.environ.get("EGG_GATEWAY_IMAGE", "ghcr.io/jwbron/egg-gateway:latest"),
        help="Gateway Docker image",
    )
    parser.add_argument(
        "--sandbox-image",
        default=os.environ.get("EGG_SANDBOX_IMAGE", "ghcr.io/jwbron/egg-sandbox:latest"),
        help="Sandbox Docker image",
    )

    # Directories
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

    # Timeout
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="MINUTES",
        help="Timeout in minutes for --print mode (default: 30)",
    )

    # Model selection
    parser.add_argument(
        "--model",
        default="opus",
        help="Claude model to use (default: opus)",
    )

    # Status API
    parser.add_argument(
        "--status-port",
        type=int,
        default=8080,
        help="Port for status API (default: 8080, 0 to disable)",
    )

    # Cleanup behavior
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Don't cleanup containers on exit (for debugging)",
    )

    # Verbose output
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    return parser


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        args: Arguments to parse (default: sys.argv[1:])

    Returns:
        Parsed arguments namespace
    """
    parser = create_parser()
    parsed = parser.parse_args(args)

    # Resolve prompt from file if specified
    if parsed.prompt_file:
        if not os.path.exists(parsed.prompt_file):
            parser.error(f"Prompt file not found: {parsed.prompt_file}")
        with open(parsed.prompt_file) as f:
            parsed.prompt = f.read()

    # Determine mode
    if parsed.private:
        parsed.mode = "private"
    else:
        parsed.mode = "public"

    return parsed


def validate_args(args: argparse.Namespace) -> list[str]:
    """Validate parsed arguments.

    Args:
        args: Parsed arguments namespace

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    # Check that Docker socket is available
    if not os.path.exists("/var/run/docker.sock"):
        errors.append(
            "Docker socket not found. Mount it with: -v /var/run/docker.sock:/var/run/docker.sock"
        )

    # Check config directory
    if not os.path.exists(args.config_dir):
        errors.append(
            f"Config directory not found: {args.config_dir}. Mount it with: -v ~/.config/egg:/config:ro"
        )

    # Check repos directory for interactive mode
    if not args.prompt and not os.path.exists(args.repos_dir):
        errors.append(
            f"Repos directory not found: {args.repos_dir}. Mount it with: -v ~/repos:/repos"
        )

    # Check for required config files
    repos_yaml = os.path.join(args.config_dir, "repositories.yaml")
    if not os.path.exists(repos_yaml):
        errors.append(f"repositories.yaml not found in {args.config_dir}")

    # Check for launcher secret
    secret_file = os.path.join(args.config_dir, "launcher-secret")
    if not os.path.exists(secret_file) and not os.environ.get("EGG_LAUNCHER_SECRET"):
        errors.append(
            "Launcher secret not found. Create it with: openssl rand -hex 32 > ~/.config/egg/launcher-secret"
        )

    return errors


def main() -> int:
    """Main entry point for CLI.

    Returns:
        Exit code
    """
    args = parse_args()

    # Validate arguments
    errors = validate_args(args)
    if errors:
        print("Configuration errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    # Import here to avoid circular imports
    from lifecycle import EggLifecycleManager

    # Create lifecycle manager
    manager = EggLifecycleManager(
        gateway_image=args.gateway_image,
        sandbox_image=args.sandbox_image,
        config_dir=args.config_dir,
        repos_dir=args.repos_dir,
        mode=args.mode,
    )

    try:
        # Start the stack
        if args.verbose:
            print("Starting egg stack...")
        if not manager.start():
            print("Failed to start egg stack", file=sys.stderr)
            return 1

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
        # Cleanup unless disabled
        if not args.no_cleanup:
            manager.cleanup()


if __name__ == "__main__":
    sys.exit(main())
