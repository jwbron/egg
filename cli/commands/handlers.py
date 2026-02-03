"""Command handlers for egg CLI.

Each handler takes parsed arguments and returns an exit code.
"""

import sys
from argparse import Namespace
from pathlib import Path

from cli.commands.config import get_config_info, validate_config_files
from cli.commands.docker import (
    check_docker_installed,
    check_docker_running,
    compose_down,
    compose_up,
    exec_in_container,
    get_container_status,
    list_egg_containers,
    stream_logs,
)
from shared.egg_config.loader import find_config_file, load_config

# Default container names
GATEWAY_CONTAINER = "egg-gateway"
SANDBOX_CONTAINER = "egg-sandbox"


def _find_compose_file(config_path: str | None = None) -> Path | None:
    """Find docker-compose.yaml in config directory or current directory.

    Args:
        config_path: Path to egg.yaml config file

    Returns:
        Path to compose file, or None if not found
    """
    # If config path is provided, look in same directory
    if config_path:
        config_dir = Path(config_path).parent
        compose_file = config_dir / "docker-compose.yaml"
        if compose_file.exists():
            return compose_file
        compose_file = config_dir / "docker-compose.yml"
        if compose_file.exists():
            return compose_file

    # Look in current directory
    for name in ["docker-compose.yaml", "docker-compose.yml"]:
        compose_file = Path.cwd() / name
        if compose_file.exists():
            return compose_file

    # Look relative to found config file
    config_file = find_config_file("egg.yaml", "EGG_CONFIG")
    if config_file:
        config_dir = config_file.parent
        for name in ["docker-compose.yaml", "docker-compose.yml"]:
            compose_file = config_dir / name
            if compose_file.exists():
                return compose_file

    return None


def _check_docker_prerequisites() -> int:
    """Check Docker is installed and running.

    Returns:
        0 if OK, non-zero exit code if not
    """
    if not check_docker_installed():
        print("Error: Docker is not installed", file=sys.stderr)
        print("Install Docker: https://docs.docker.com/get-docker/", file=sys.stderr)
        return 1

    if not check_docker_running():
        print("Error: Docker daemon is not running", file=sys.stderr)
        print("Start Docker and try again", file=sys.stderr)
        return 1

    return 0


def handle_start(args: Namespace) -> int:
    """Handle the 'start' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Check Docker
    docker_check = _check_docker_prerequisites()
    if docker_check != 0:
        return docker_check

    # Validate config first
    result = validate_config_files(config_path=args.config)
    if not result.valid:
        print("Error: Configuration validation failed", file=sys.stderr)
        for error in result.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    # Find compose file
    compose_file = _find_compose_file(args.config)
    if not compose_file:
        print("Error: docker-compose.yaml not found", file=sys.stderr)
        print("Ensure docker-compose.yaml exists in your config directory", file=sys.stderr)
        return 1

    # Load config to get settings
    config_path = Path(args.config) if args.config else find_config_file("egg.yaml", "EGG_CONFIG")
    if not config_path:
        print("Error: Cannot find egg.yaml configuration", file=sys.stderr)
        return 1

    config = load_config(config_path=config_path)
    sandbox_name = config.get("egg", {}).get("name", "egg-sandbox")

    print(f"Starting egg environment: {sandbox_name}")
    if args.private:
        print("Mode: private (network restricted)")
    else:
        print("Mode: public (internet access)")

    # Start containers
    success = compose_up(str(compose_file), build=False)
    if not success:
        print("Error: Failed to start containers", file=sys.stderr)
        return 1

    print("Containers started successfully")

    # If prompt provided, run in non-interactive mode
    if args.prompt:
        print(f"Running with prompt: {args.prompt[:50]}...")
        # This will be implemented when the sandbox supports it
        print("Note: Non-interactive mode not yet fully implemented")
        return 0

    return 0


def handle_stop(args: Namespace) -> int:
    """Handle the 'stop' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Check Docker
    docker_check = _check_docker_prerequisites()
    if docker_check != 0:
        return docker_check

    # Find compose file
    compose_file = _find_compose_file()

    if compose_file:
        print("Stopping egg environment...")
        success = compose_down(str(compose_file))
        if success:
            print("Containers stopped successfully")
            return 0
        else:
            print("Error: Failed to stop containers", file=sys.stderr)
            return 1
    else:
        # Try to stop individual containers
        containers = list_egg_containers()
        if not containers:
            print("No egg containers found")
            return 0

        print(f"Stopping {len(containers)} container(s)...")
        from cli.commands.docker import stop_container

        all_stopped = True
        for container in containers:
            if container.running:
                if not stop_container(container.name):
                    print(f"Warning: Failed to stop {container.name}", file=sys.stderr)
                    all_stopped = False
                else:
                    print(f"Stopped {container.name}")

        return 0 if all_stopped else 1


def handle_status(args: Namespace) -> int:
    """Handle the 'status' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Check Docker
    docker_check = _check_docker_prerequisites()
    if docker_check != 0:
        return docker_check

    containers = list_egg_containers()

    if not containers:
        print("No egg containers found")
        print("\nRun 'egg start' to start the environment")
        return 0

    print("Egg containers:")
    print("-" * 60)
    print(f"{'Name':<25} {'Status':<20} {'Health':<10}")
    print("-" * 60)

    for container in containers:
        health = container.health or "-"
        status_icon = "●" if container.running else "○"
        print(f"{status_icon} {container.name:<23} {container.status:<20} {health:<10}")

    print("-" * 60)

    # Summary
    running = sum(1 for c in containers if c.running)
    total = len(containers)
    print(f"\n{running}/{total} containers running")

    return 0


def handle_exec(args: Namespace) -> int:
    """Handle the 'exec' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Check Docker
    docker_check = _check_docker_prerequisites()
    if docker_check != 0:
        return docker_check

    if not args.cmd:
        print("Error: No command specified", file=sys.stderr)
        print("Usage: egg exec <command> [args...]", file=sys.stderr)
        return 1

    # Check sandbox is running
    status = get_container_status(SANDBOX_CONTAINER)
    if not status or not status.running:
        print(f"Error: Sandbox container '{SANDBOX_CONTAINER}' is not running", file=sys.stderr)
        print("Run 'egg start' first", file=sys.stderr)
        return 1

    # Execute command
    return exec_in_container(
        SANDBOX_CONTAINER,
        args.cmd,
        interactive=sys.stdin.isatty(),
    )


def handle_logs(args: Namespace) -> int:
    """Handle the 'logs' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Check Docker
    docker_check = _check_docker_prerequisites()
    if docker_check != 0:
        return docker_check

    # Determine which container to show logs for
    # Default to sandbox, but show both if available
    containers = list_egg_containers()
    if not containers:
        print("No egg containers found", file=sys.stderr)
        return 1

    # For now, show sandbox logs by default
    target = None
    for container in containers:
        if SANDBOX_CONTAINER in container.name:
            target = container.name
            break

    if not target and containers:
        target = containers[0].name

    if not target:
        print("Error: No container found to show logs for", file=sys.stderr)
        return 1

    print(f"Showing logs for {target}...")
    if args.follow:
        print("(Press Ctrl+C to stop following)\n")

    return stream_logs(
        target,
        follow=args.follow,
        tail=100 if not args.follow else None,
    )


def handle_config(args: Namespace) -> int:
    """Handle the 'config' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    if args.config_command == "validate":
        return handle_config_validate(args)
    else:
        print("Config subcommands: validate")
        return 0


def handle_config_validate(args: Namespace) -> int:
    """Handle the 'config validate' command.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code
    """
    # Get config file locations
    info = get_config_info()
    print("Configuration files:")
    print(f"  egg.yaml:     {info['config_path']}")
    print(f"  secrets.yaml: {info['secrets_path']}")
    print()

    # Validate
    result = validate_config_files()

    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"  ⚠ {warning}")
        print()

    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"  ✗ {error}")
        print()
        print("Configuration is INVALID")
        return 1

    print("✓ Configuration is valid")
    return 0
