"""Docker container management for egg CLI.

Provides utilities for managing gateway and sandbox containers.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass
class ContainerStatus:
    """Status of a container."""

    name: str
    running: bool
    status: str
    health: str | None = None
    id: str | None = None


class DockerError(Exception):
    """Error from Docker operations."""

    pass


def check_docker_installed() -> bool:
    """Check if Docker is installed and accessible."""
    return shutil.which("docker") is not None


def check_docker_running() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_container_status(container_name: str) -> ContainerStatus | None:
    """Get the status of a specific container.

    Args:
        container_name: Name of the container to check

    Returns:
        ContainerStatus if container exists, None otherwise
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}}\t{{.State.Status}}\t{{.State.Health.Status}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        parts = result.stdout.strip().split("\t")
        container_id = parts[0][:12] if len(parts) > 0 else None
        status = parts[1] if len(parts) > 1 else "unknown"
        health = parts[2] if len(parts) > 2 and parts[2] else None

        return ContainerStatus(
            name=container_name,
            running=status == "running",
            status=status,
            health=health,
            id=container_id,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def list_egg_containers(prefix: str = "egg-") -> list[ContainerStatus]:
    """List all egg-related containers.

    Args:
        prefix: Container name prefix to filter by

    Returns:
        List of container statuses
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name={prefix}",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                name = parts[0]
                status_text = parts[1]
                container_id = parts[2]
                running = status_text.startswith("Up")

                # Get health status separately
                status = get_container_status(name)
                health = status.health if status else None

                containers.append(
                    ContainerStatus(
                        name=name,
                        running=running,
                        status=status_text,
                        health=health,
                        id=container_id,
                    )
                )
        return containers
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def start_container(
    container_name: str,
    *,
    compose_file: str | None = None,
) -> bool:
    """Start a container.

    Args:
        container_name: Name of the container to start
        compose_file: Optional docker-compose file path

    Returns:
        True if successful
    """
    try:
        if compose_file:
            cmd = ["docker", "compose", "-f", compose_file, "up", "-d", container_name]
        else:
            cmd = ["docker", "start", container_name]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def stop_container(container_name: str, *, timeout: int = 10) -> bool:
    """Stop a container.

    Args:
        container_name: Name of the container to stop
        timeout: Seconds to wait before killing

    Returns:
        True if successful
    """
    try:
        result = subprocess.run(
            ["docker", "stop", "-t", str(timeout), container_name],
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def exec_in_container(
    container_name: str,
    command: list[str],
    *,
    interactive: bool = False,
    workdir: str | None = None,
) -> int:
    """Execute a command in a running container.

    Args:
        container_name: Name of the container
        command: Command and arguments to execute
        interactive: Whether to run interactively
        workdir: Working directory inside the container

    Returns:
        Exit code from the command
    """
    cmd = ["docker", "exec"]
    if interactive:
        cmd.extend(["-it"])
    if workdir:
        cmd.extend(["-w", workdir])
    cmd.append(container_name)
    cmd.extend(command)

    try:
        result = subprocess.run(cmd)
        return result.returncode
    except FileNotFoundError:
        return 127  # Command not found


def stream_logs(
    container_name: str,
    *,
    follow: bool = False,
    tail: int | None = None,
    output: TextIO | None = None,
) -> int:
    """Stream container logs.

    Args:
        container_name: Name of the container
        follow: Whether to follow log output
        tail: Number of lines to show (None = all)
        output: Output stream (defaults to stdout)

    Returns:
        Exit code from docker logs
    """
    if output is None:
        output = sys.stdout

    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    cmd.append(container_name)

    try:
        result = subprocess.run(cmd, stdout=output, stderr=subprocess.STDOUT)
        return result.returncode
    except FileNotFoundError:
        return 127


def compose_up(
    compose_file: str,
    *,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
) -> bool:
    """Start services with docker-compose.

    Args:
        compose_file: Path to docker-compose file
        services: Specific services to start (None = all)
        detach: Run in background
        build: Build images before starting

    Returns:
        True if successful
    """
    cmd = ["docker", "compose", "-f", compose_file, "up"]
    if detach:
        cmd.append("-d")
    if build:
        cmd.append("--build")
    if services:
        cmd.extend(services)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def compose_down(
    compose_file: str,
    *,
    remove_volumes: bool = False,
) -> bool:
    """Stop and remove services with docker-compose.

    Args:
        compose_file: Path to docker-compose file
        remove_volumes: Also remove volumes

    Returns:
        True if successful
    """
    cmd = ["docker", "compose", "-f", compose_file, "down"]
    if remove_volumes:
        cmd.append("-v")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
