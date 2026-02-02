"""Docker operations for the egg CLI.

This module handles Docker image building, container management,
and related utilities.
"""

import subprocess
from typing import Any

# Label used to store build content hash on Docker image
BUILD_HASH_LABEL = "org.egg.build-hash"

# Default Docker network names
EGG_ISOLATED_NETWORK = "egg-isolated"
EGG_EXTERNAL_NETWORK = "egg-external"

# Container names
GATEWAY_CONTAINER = "egg-gateway"
SANDBOX_CONTAINER = "egg-sandbox"

# Legacy alias
GATEWAY_CONTAINER_NAME = GATEWAY_CONTAINER

# Port settings
GATEWAY_PORT = 9847
GATEWAY_PROXY_PORT = 3128

# Network subnets
ISOLATED_SUBNET = "172.30.0.0/16"
EXTERNAL_SUBNET = "172.31.0.0/16"
# Legacy aliases
EGG_ISOLATED_SUBNET = ISOLATED_SUBNET
EGG_EXTERNAL_SUBNET = EXTERNAL_SUBNET

# Default gateway IPs
GATEWAY_ISOLATED_IP = "172.30.0.2"
GATEWAY_EXTERNAL_IP = "172.31.0.2"


def check_docker_installed() -> bool:
    """Check if Docker is installed."""
    result = subprocess.run(
        ["which", "docker"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def check_docker_permissions() -> bool:
    """Check if user has permission to run Docker commands."""
    result = subprocess.run(
        ["docker", "ps"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0:
        return True

    if "permission denied" in result.stderr.lower():
        return False

    return False


def check_docker() -> tuple[bool, str]:
    """Check if Docker is installed and accessible.

    Returns:
        Tuple of (success, error_message)
    """
    if not check_docker_installed():
        return False, "Docker is not installed"

    if not check_docker_permissions():
        return False, (
            "Docker permission denied. Either:\n"
            "  1. Add yourself to docker group: sudo usermod -aG docker $USER\n"
            "  2. Log out and back in for group membership to take effect"
        )

    return True, ""


def container_exists(name: str) -> bool:
    """Check if a container exists (running or stopped)."""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in result.stdout


def container_running(name: str) -> bool:
    """Check if a container is currently running."""
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in result.stdout


def image_exists(name: str) -> bool:
    """Check if a Docker image exists."""
    result = subprocess.run(
        ["docker", "images", name, "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def get_image_label(image: str, label: str) -> str | None:
    """Get a label value from a Docker image."""
    result = subprocess.run(
        ["docker", "inspect", image, "--format", f"{{{{index .Config.Labels \"{label}\"}}}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        value = result.stdout.strip()
        return value if value and value != "<no value>" else None
    return None


def create_network(name: str, subnet: str) -> bool:
    """Create a Docker network if it doesn't exist.

    Args:
        name: Network name
        subnet: Network subnet (e.g., "172.30.0.0/16")

    Returns:
        True if network exists or was created successfully
    """
    # Check if network exists
    result = subprocess.run(
        ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if name in result.stdout:
        return True

    # Create network
    result = subprocess.run(
        ["docker", "network", "create", "--subnet", subnet, name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def stop_container(name: str, timeout: int = 10) -> bool:
    """Stop a running container.

    Args:
        name: Container name
        timeout: Seconds to wait before killing

    Returns:
        True if container was stopped or wasn't running
    """
    if not container_running(name):
        return True

    result = subprocess.run(
        ["docker", "stop", "-t", str(timeout), name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def remove_container(name: str, force: bool = False) -> bool:
    """Remove a container.

    Args:
        name: Container name
        force: Force removal even if running

    Returns:
        True if container was removed or didn't exist
    """
    if not container_exists(name):
        return True

    cmd = ["docker", "rm"]
    if force:
        cmd.append("-f")
    cmd.append(name)

    result = subprocess.run(cmd, capture_output=True, check=False)
    return result.returncode == 0


def get_container_logs(name: str, follow: bool = False, tail: int | None = None) -> None:
    """Stream container logs to stdout.

    Args:
        name: Container name
        follow: Follow log output
        tail: Number of lines from end to show
    """
    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    cmd.append(name)

    subprocess.run(cmd, check=False)


def get_container_status(name: str) -> dict[str, Any]:
    """Get container status information.

    Args:
        name: Container name

    Returns:
        Dict with status info, or empty dict if container doesn't exist
    """
    result = subprocess.run(
        [
            "docker",
            "inspect",
            name,
            "--format",
            "{{json .State}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    import json

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def exec_in_container(
    name: str,
    command: list[str],
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    interactive: bool = False,
    tty: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command in a running container.

    Args:
        name: Container name
        command: Command to execute
        workdir: Working directory inside container
        env: Environment variables to set
        interactive: Keep STDIN open
        tty: Allocate a pseudo-TTY

    Returns:
        CompletedProcess result
    """
    cmd = ["docker", "exec"]
    if interactive:
        cmd.append("-i")
    if tty:
        cmd.append("-t")
    if workdir:
        cmd.extend(["-w", workdir])
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.append(name)
    cmd.extend(command)

    return subprocess.run(cmd, check=False)
