"""Main entry point for the egg CLI."""

import argparse
import sys
from pathlib import Path

from .runtime import (
    RuntimeConfig,
    check_docker,
    create_network,
    exec_in_container,
    get_logs,
    print_status,
    remove_container,
    start_gateway,
    start_sandbox,
    stop_container,
)


def cmd_start(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """Start the sandbox environment."""
    if not check_docker():
        return 1

    # Create network if needed
    if not create_network(config):
        return 1

    # Start gateway
    if not start_gateway(config, private_mode=args.private):
        return 1

    # Determine repos directory
    repos_dir = Path(args.repos) if args.repos else None
    if repos_dir is None:
        # Default to current directory or ~/repos
        default_repos = Path.home() / "repos"
        if default_repos.exists():
            repos_dir = default_repos

    # Start sandbox
    if not start_sandbox(
        config,
        repos_dir=repos_dir,
        private_mode=args.private,
        prompt=args.prompt,
    ):
        return 1

    print()
    print("egg sandbox started successfully!")
    print(f"Gateway API: http://localhost:{config.gateway_port}")
    if args.private:
        print("Mode: PRIVATE (network lockdown)")
    else:
        print("Mode: PUBLIC (full internet access)")

    return 0


def cmd_stop(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """Stop the sandbox environment."""
    success = True

    if not stop_container(config.sandbox_container):
        success = False

    if not stop_container(config.gateway_container):
        success = False

    if args.remove:
        if not remove_container(config.sandbox_container):
            success = False
        if not remove_container(config.gateway_container):
            success = False

    return 0 if success else 1


def cmd_exec(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """Execute a command in the sandbox."""
    if not args.cmd:
        print("Error: No command specified", file=sys.stderr)
        return 1

    return exec_in_container(config, args.cmd, interactive=True)


def cmd_logs(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """View container logs."""
    container = args.container if hasattr(args, "container") and args.container else "sandbox"
    return get_logs(config, container, follow=args.follow)


def cmd_status(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """Show status of containers."""
    print_status(config)
    return 0


def cmd_config_validate(args: argparse.Namespace, config: RuntimeConfig) -> int:
    """Validate configuration files."""
    from gateway.config_validator import validate_all

    is_valid, messages = validate_all()

    for msg in messages:
        print(msg)

    if is_valid:
        print()
        print("Configuration is valid.")
        return 0
    else:
        print()
        print("Configuration has errors.", file=sys.stderr)
        return 1


def main() -> int:
    """Main entry point for the egg CLI."""
    parser = argparse.ArgumentParser(
        prog="egg",
        description="Sandboxed LLM code execution environment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  egg start                         Start the sandbox environment
  egg start --private               Start in private mode (network lockdown)
  egg start -p "fix the bug"        Run with prompt in non-interactive mode
  egg stop                          Stop the sandbox
  egg exec bash                     Open a shell in the sandbox
  egg exec pytest tests/            Run tests in the sandbox
  egg logs -f                       Follow sandbox logs
  egg status                        Show container status
  egg config validate               Validate configuration files
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start command
    start_parser = subparsers.add_parser("start", help="Start the sandbox environment")
    start_parser.add_argument(
        "--repos",
        type=str,
        help="Path to repositories directory to mount",
    )
    start_parser.add_argument(
        "--private",
        action="store_true",
        help="Enable private network mode (network lockdown)",
    )
    start_parser.add_argument(
        "-p",
        "--prompt",
        type=str,
        metavar="PROMPT",
        help="Run with a prompt in non-interactive mode",
    )

    # Stop command
    stop_parser = subparsers.add_parser("stop", help="Stop the sandbox environment")
    stop_parser.add_argument(
        "--remove",
        "-r",
        action="store_true",
        help="Remove containers after stopping",
    )

    # Exec command
    exec_parser = subparsers.add_parser("exec", help="Execute a command in the sandbox")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute")

    # Logs command
    logs_parser = subparsers.add_parser("logs", help="View container logs")
    logs_parser.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Follow log output",
    )
    logs_parser.add_argument(
        "--container",
        "-c",
        choices=["gateway", "sandbox"],
        default="sandbox",
        help="Container to view logs for (default: sandbox)",
    )

    # Status command
    subparsers.add_parser("status", help="Show running containers and health status")

    # Config command
    config_parser = subparsers.add_parser("config", help="Configuration management")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("validate", help="Validate configuration files")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    # Create runtime config
    config = RuntimeConfig()

    # Command dispatch
    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "exec": cmd_exec,
        "logs": cmd_logs,
        "status": cmd_status,
    }

    if args.command == "config":
        if args.config_command == "validate":
            return cmd_config_validate(args, config)
        else:
            config_parser.print_help()
            return 0

    handler = commands.get(args.command)
    if handler:
        return handler(args, config)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
