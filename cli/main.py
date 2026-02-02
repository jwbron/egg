"""Main entry point for the egg CLI."""

import argparse
import sys
from pathlib import Path

from .commands import run_config, run_exec, run_logs, run_start, run_status, run_stop


def main() -> int:
    """Main entry point for the egg CLI."""
    parser = argparse.ArgumentParser(
        prog="egg",
        description="Sandboxed LLM code execution environment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  egg start                      # Start the sandbox environment
  egg start --private            # Start in private network mode
  egg stop                       # Stop all containers
  egg status                     # Show container status
  egg exec <command>             # Execute a command in the sandbox
  egg logs -f                    # Follow container logs
  egg config validate            # Validate configuration
  egg config init                # Create a new configuration file
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start command
    start_parser = subparsers.add_parser("start", help="Start the sandbox environment")
    start_parser.add_argument(
        "--config",
        "-c",
        type=Path,
        help="Path to egg.yaml config file",
    )
    start_parser.add_argument(
        "--private",
        action="store_true",
        help="Enable private network mode (lockdown + private repos)",
    )
    start_parser.add_argument(
        "--public",
        action="store_true",
        help="Enable public network mode (full internet + public repos only)",
    )
    start_parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in non-interactive/headless mode",
    )

    # Stop command
    subparsers.add_parser("stop", help="Stop the sandbox environment")

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
        "--tail",
        "-n",
        type=int,
        help="Number of lines to show from end",
    )
    logs_parser.add_argument(
        "--container",
        choices=["sandbox", "gateway"],
        help="Container to show logs for (default: sandbox)",
    )

    # Status command
    subparsers.add_parser("status", help="Show running containers and health status")

    # Config command
    config_parser = subparsers.add_parser("config", help="Configuration management")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("validate", help="Validate configuration files")
    config_subparsers.add_parser("show", help="Show current configuration")
    init_parser = config_subparsers.add_parser("init", help="Create a new configuration file")
    init_parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file path (default: egg.yaml)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    # Command dispatch
    try:
        if args.command == "start":
            return run_start(args)
        elif args.command == "stop":
            return run_stop(args)
        elif args.command == "exec":
            return run_exec(args)
        elif args.command == "logs":
            return run_logs(args)
        elif args.command == "status":
            return run_status(args)
        elif args.command == "config":
            return run_config(args)
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
