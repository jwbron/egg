"""
CLI tool for configuration validation and health checks.

Usage:
    egg-config validate              # Validate all configurations
    egg-config validate --service X  # Validate specific service
    egg-config health                # Run health checks
    egg-config show                  # Show current config (secrets masked)
    egg-config watch                 # Watch for changes (for debugging)
"""

import argparse
import json
import sys

from .base import ConfigStatus, ValidationResult
from .registry import get_registry


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate configurations and return exit code."""
    registry = get_registry()

    if not registry.configs:
        print("No configurations registered.")
        print("Hint: Import and register configs before running validation.")
        return 1

    if args.service:
        # Validate specific service
        config = registry.get(args.service)
        if config is None:
            print(f"Unknown service: {args.service}")
            print(f"Available services: {', '.join(registry.configs.keys())}")
            return 1

        result = config.validate()
        _print_validation_result(args.service, result, verbose=args.verbose)
        return 0 if result.is_valid else 1

    # Validate all services
    aggregate = registry.validate_all()

    for name, result in aggregate.results.items():
        _print_validation_result(name, result, verbose=args.verbose)

    if aggregate.all_valid:
        print("\nAll configurations valid.")
        return 0
    else:
        print("\nSome configurations have errors.")
        return 1


def _print_validation_result(name: str, result: "ValidationResult", *, verbose: bool = False) -> None:
    """Print a single validation result."""
    status_icons = {
        ConfigStatus.VALID: "[OK]",
        ConfigStatus.INVALID: "[FAIL]",
        ConfigStatus.DEGRADED: "[WARN]",
    }
    icon = status_icons.get(result.status, "[?]")
    print(f"{icon} {name}: {result.status.value}")

    if result.errors:
        for error in result.errors:
            print(f"      ERROR: {error}")

    if verbose and result.warnings:
        for warning in result.warnings:
            print(f"      WARNING: {warning}")


def cmd_health(args: argparse.Namespace) -> int:
    """Run health checks and return exit code."""
    registry = get_registry()

    if not registry.configs:
        print("No configurations registered.")
        return 1

    timeout = args.timeout if hasattr(args, "timeout") else 5.0
    aggregate = registry.health_check_all(timeout=timeout)

    status_icons = {
        "healthy": "[OK]",
        "degraded": "[WARN]",
        "unhealthy": "[FAIL]",
    }

    print(f"Overall: {status_icons.get(aggregate.status, '[?]')} {aggregate.status}")
    print()

    for name, result in aggregate.services.items():
        icon = "[OK]" if result.healthy else "[FAIL]"
        latency_str = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
        print(f"  {icon} {name}: {result.message}{latency_str}")

    if args.json:
        print()
        print(json.dumps(aggregate.to_dict(), indent=2))

    return 0 if aggregate.status == "healthy" else 1


def cmd_show(args: argparse.Namespace) -> int:
    """Show current configuration (secrets masked)."""
    registry = get_registry()

    if not registry.configs:
        print("No configurations registered.")
        return 1

    if args.service:
        config = registry.get(args.service)
        if config is None:
            print(f"Unknown service: {args.service}")
            return 1

        print(f"Configuration for {args.service}:")
        print(json.dumps(config.to_dict(), indent=2))
        return 0

    # Show all configs
    all_configs = registry.to_dict()
    print(json.dumps(all_configs, indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch for configuration changes (placeholder for Phase 4)."""
    print("Watch mode not yet implemented (Phase 4)")
    print("This will monitor config files and report changes in real-time.")
    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-config",
        description="Configuration validation and health check tool for egg services.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # validate command
    validate_parser = subparsers.add_parser("validate", help="Validate configurations")
    validate_parser.add_argument("--service", "-s", help="Validate specific service only")
    validate_parser.add_argument("--verbose", "-v", action="store_true", help="Show warnings")

    # health command
    health_parser = subparsers.add_parser("health", help="Run health checks")
    health_parser.add_argument(
        "--timeout", "-t", type=float, default=5.0, help="Timeout per check in seconds"
    )
    health_parser.add_argument("--json", "-j", action="store_true", help="Output full JSON result")

    # show command
    show_parser = subparsers.add_parser("show", help="Show current configuration")
    show_parser.add_argument("--service", "-s", help="Show specific service only")

    # watch command
    subparsers.add_parser("watch", help="Watch for configuration changes")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "validate": cmd_validate,
        "health": cmd_health,
        "show": cmd_show,
        "watch": cmd_watch,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
