"""Container runtime management for egg sandbox.

Handles container lifecycle operations (start, stop, exec, logs).
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class RuntimeConfig:
    """Configuration for the egg runtime."""

    # Container names
    gateway_container: str = "egg-gateway"
    sandbox_container: str = "egg-sandbox"

    # Image names
    gateway_image: str = "egg-gateway:latest"
    sandbox_image: str = "egg-sandbox:latest"

    # Network
    network_name: str = "egg-isolated"
    network_subnet: str = "172.30.0.0/24"
    gateway_ip: str = "172.30.0.2"

    # Ports
    gateway_port: int = 9847
    proxy_port: int = 3128

    # Paths (use field default_factory to evaluate at instantiation, not class definition)
    config_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "egg")
    sharing_dir: Path = field(default_factory=lambda: Path.home() / ".egg" / "sharing")
    secrets_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "egg" / "secrets")


def check_docker() -> bool:
    """Check if Docker is available and running."""
    if not shutil.which("docker"):
        print("Error: Docker is not installed", file=sys.stderr)
        print("Install Docker: https://docs.docker.com/get-docker/", file=sys.stderr)
        return False

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print("Error: Docker daemon is not running", file=sys.stderr)
            print("Start Docker and try again", file=sys.stderr)
            return False
    except subprocess.TimeoutExpired:
        print("Error: Docker daemon not responding", file=sys.stderr)
        return False

    return True


def container_running(name: str) -> bool:
    """Check if a container is running."""
    result = subprocess.run(
        ["docker", "container", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def container_exists(name: str) -> bool:
    """Check if a container exists (running or stopped)."""
    result = subprocess.run(
        ["docker", "container", "inspect", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def network_exists(name: str) -> bool:
    """Check if a Docker network exists."""
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def create_network(config: RuntimeConfig) -> bool:
    """Create the isolated Docker network."""
    if network_exists(config.network_name):
        return True

    result = subprocess.run(
        [
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            config.network_subnet,
            config.network_name,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error creating network: {result.stderr}", file=sys.stderr)
        return False

    print(f"Created network: {config.network_name}")
    return True


def start_gateway(config: RuntimeConfig, private_mode: bool = False) -> bool:
    """Start the gateway container."""
    if container_running(config.gateway_container):
        print(f"Gateway already running: {config.gateway_container}")
        return True

    # Remove existing stopped container
    if container_exists(config.gateway_container):
        subprocess.run(["docker", "rm", config.gateway_container], capture_output=True)

    # Ensure directories exist
    config.secrets_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        config.gateway_container,
        "--network",
        config.network_name,
        "--ip",
        config.gateway_ip,
        "-p",
        f"{config.gateway_port}:{config.gateway_port}",
        "-p",
        f"{config.proxy_port}:{config.proxy_port}",
        "-v",
        f"{config.secrets_dir}:/secrets:ro",
        "-e",
        f"PRIVATE_MODE={'true' if private_mode else 'false'}",
        config.gateway_image,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error starting gateway: {result.stderr}", file=sys.stderr)
        return False

    print(f"Started gateway: {config.gateway_container}")
    return True


def start_sandbox(
    config: RuntimeConfig,
    repos_dir: Path | None = None,
    private_mode: bool = False,
    prompt: str | None = None,
) -> bool:
    """Start the sandbox container.

    Args:
        config: Runtime configuration
        repos_dir: Directory containing repositories to mount
        private_mode: Enable private network mode
        prompt: Optional prompt for non-interactive mode
    """
    if container_running(config.sandbox_container):
        print(f"Sandbox already running: {config.sandbox_container}")
        return True

    # Remove existing stopped container
    if container_exists(config.sandbox_container):
        subprocess.run(["docker", "rm", config.sandbox_container], capture_output=True)

    # Ensure directories exist
    config.sharing_dir.mkdir(parents=True, exist_ok=True)

    # Get current user's UID/GID for file permissions
    uid = os.getuid()
    gid = os.getgid()

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        config.sandbox_container,
        "--network",
        config.network_name,
        "-e",
        f"RUNTIME_UID={uid}",
        "-e",
        f"RUNTIME_GID={gid}",
        "-e",
        f"GATEWAY_URL=http://{config.gateway_ip}:{config.gateway_port}",
        "-v",
        f"{config.sharing_dir}:/home/egg/sharing",
    ]

    # Mount repos directory if provided
    if repos_dir and repos_dir.exists():
        cmd.extend(["-v", f"{repos_dir}:/home/egg/repos"])

    # Set proxy environment for private mode
    if private_mode:
        cmd.extend(
            [
                "-e",
                f"HTTPS_PROXY=http://{config.gateway_ip}:{config.proxy_port}",
                "-e",
                f"HTTP_PROXY=http://{config.gateway_ip}:{config.proxy_port}",
            ]
        )

    # Add prompt if provided (non-interactive mode)
    if prompt:
        cmd.extend(["-e", f"EGG_PROMPT={prompt}"])

    cmd.append(config.sandbox_image)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error starting sandbox: {result.stderr}", file=sys.stderr)
        return False

    print(f"Started sandbox: {config.sandbox_container}")
    return True


def stop_container(name: str) -> bool:
    """Stop a container."""
    if not container_running(name):
        print(f"Container not running: {name}")
        return True

    result = subprocess.run(
        ["docker", "stop", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error stopping {name}: {result.stderr}", file=sys.stderr)
        return False

    print(f"Stopped: {name}")
    return True


def remove_container(name: str) -> bool:
    """Remove a container."""
    if not container_exists(name):
        return True

    result = subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error removing {name}: {result.stderr}", file=sys.stderr)
        return False

    print(f"Removed: {name}")
    return True


def exec_in_container(
    config: RuntimeConfig,
    command: "Sequence[str]",
    interactive: bool = False,
) -> int:
    """Execute a command in the sandbox container.

    Args:
        config: Runtime configuration
        command: Command and arguments to execute
        interactive: Whether to run interactively (with TTY)

    Returns:
        Exit code from the command
    """
    if not container_running(config.sandbox_container):
        print(f"Error: Sandbox container not running: {config.sandbox_container}", file=sys.stderr)
        return 1

    cmd = ["docker", "exec"]
    if interactive:
        cmd.extend(["-it"])
    cmd.append(config.sandbox_container)
    cmd.extend(command)

    result = subprocess.run(cmd)
    return result.returncode


def get_logs(config: RuntimeConfig, container: str, follow: bool = False) -> int:
    """Get container logs.

    Args:
        config: Runtime configuration
        container: Container name ('gateway' or 'sandbox')
        follow: Whether to follow log output

    Returns:
        Exit code
    """
    name = config.gateway_container if container == "gateway" else config.sandbox_container

    if not container_exists(name):
        print(f"Error: Container not found: {name}", file=sys.stderr)
        return 1

    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd.append(name)

    result = subprocess.run(cmd)
    return result.returncode


def get_status(config: RuntimeConfig) -> dict:
    """Get status of all containers.

    Returns:
        Dictionary with container status information
    """
    status = {}

    for name in [config.gateway_container, config.sandbox_container]:
        if container_running(name):
            status[name] = "running"
        elif container_exists(name):
            status[name] = "stopped"
        else:
            status[name] = "not created"

    status["network"] = "exists" if network_exists(config.network_name) else "not created"

    return status


def print_status(config: RuntimeConfig) -> None:
    """Print status of all containers."""
    status = get_status(config)

    print("egg sandbox status:")
    print(f"  Gateway:  {status[config.gateway_container]}")
    print(f"  Sandbox:  {status[config.sandbox_container]}")
    print(f"  Network:  {status['network']}")

    # Check health endpoint if gateway is running
    if status[config.gateway_container] == "running":
        try:
            import requests

            response = requests.get(
                f"http://localhost:{config.gateway_port}/api/v1/health",
                timeout=5,
            )
            if response.status_code == 200:
                health = response.json()
                print(f"  Health:   {health.get('status', 'unknown')}")
        except Exception:
            print("  Health:   unable to check")
