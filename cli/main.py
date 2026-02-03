"""Main entry point for the egg CLI."""

import argparse
import sys


def main() -> int:
    """Main entry point for the egg CLI."""
    parser = argparse.ArgumentParser(
        prog="egg",
        description="Sandboxed LLM code execution environment",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start command
    start_parser = subparsers.add_parser("start", help="Start the sandbox environment")
    start_parser.add_argument(
        "--config",
        type=str,
        default="./egg.yaml",
        help="Path to egg.yaml config file",
    )
    start_parser.add_argument(
        "--private",
        action="store_true",
        help="Enable private network mode",
    )
    start_parser.add_argument(
        "-p",
        "--prompt",
        type=str,
        metavar="PROMPT",
        help="Run with a prompt in non-interactive mode",
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

    # Command dispatch (to be implemented)
    print(f"Command '{args.command}' not yet implemented")
    return 1


if __name__ == "__main__":
    sys.exit(main())
