"""Docker Compose integration for egg.

This module provides functionality for launching the egg stack using Docker Compose
instead of manual container management. It provides a simplified deployment path
with consistent networking and configuration.

Usage:
    egg --compose         # Start gateway via compose, then launch sandbox
    egg --compose --down  # Stop the compose stack
"""

import os
import re
import subprocess
import time
from pathlib import Path

from .config import GATEWAY_PORT
from .output import error, info, success


def get_compose_file() -> Path:
    """Get path to the docker-compose.yml file.

    Searches for docker-compose.yml in:
    1. EGG_COMPOSE_FILE environment variable
    2. Current working directory
    3. Parent directories up to root
    4. Package installation directory

    Returns:
        Path to docker-compose.yml

    Raises:
        FileNotFoundError: If compose file cannot be found
    """
    # Check environment variable
    env_path = os.environ.get("EGG_COMPOSE_FILE")
    if env_path:
        compose_file = Path(env_path)
        if compose_file.exists():
            return compose_file
        raise FileNotFoundError(f"EGG_COMPOSE_FILE points to non-existent file: {env_path}")

    # Search current directory and parents
    current = Path.cwd()
    while current != current.parent:
        candidate = current / "docker-compose.yml"
        if candidate.exists():
            return candidate
        current = current.parent

    # Check package directory (for pip-installed egg)
    package_dir = Path(__file__).parent.parent.parent
    candidate = package_dir / "docker-compose.yml"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        "docker-compose.yml not found. Either:\n"
        "  1. Run from the egg repository directory\n"
        "  2. Set EGG_COMPOSE_FILE environment variable\n"
        "  3. Run 'egg-deploy init' to set up configuration"
    )


def get_env_file(compose_file: Path) -> Path | None:
    """Get path to the .env file for compose.

    Args:
        compose_file: Path to docker-compose.yml

    Returns:
        Path to .env file if it exists, None otherwise
    """
    env_file = compose_file.parent / ".env"
    if env_file.exists():
        return env_file
    return None


def ensure_env_configured(compose_file: Path) -> bool:
    """Ensure .env file exists and has required configuration.

    Args:
        compose_file: Path to docker-compose.yml

    Returns:
        True if configuration is valid, False otherwise
    """
    env_file = compose_file.parent / ".env"

    if not env_file.exists():
        error(f".env file not found at {env_file}")
        info("Create it by running: egg-deploy init")
        return False

    # Check for required variables
    required_vars = ["EGG_LAUNCHER_SECRET"]
    missing = []

    with open(env_file) as f:
        content = f.read()
        for var in required_vars:
            # Check if variable is set to a non-empty value
            # Pattern: VAR=value (where value is non-empty and not a comment)
            pattern = rf"^{var}=([^#\n]+)"
            match = re.search(pattern, content, re.MULTILINE)
            if not match or not match.group(1).strip():
                missing.append(var)

    if missing:
        error(f"Missing required configuration in .env: {', '.join(missing)}")
        info("Edit .env and set these variables, or run: egg-deploy init")
        return False

    return True


def compose_up(compose_file: Path, build: bool = False) -> bool:
    """Start the egg stack using Docker Compose.

    Args:
        compose_file: Path to docker-compose.yml
        build: Whether to rebuild images

    Returns:
        True if successful, False otherwise
    """
    info("Starting egg gateway via Docker Compose...")

    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "up",
        "-d",
    ]

    if build:
        cmd.append("--build")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            error(f"Docker Compose failed: {result.stderr}")
            return False
        return True
    except FileNotFoundError:
        error("Docker Compose not found. Please install Docker Compose.")
        return False
    except Exception as e:
        error(f"Failed to run Docker Compose: {e}")
        return False


def compose_down(compose_file: Path) -> bool:
    """Stop and remove the egg stack.

    Args:
        compose_file: Path to docker-compose.yml

    Returns:
        True if successful, False otherwise
    """
    info("Stopping egg stack via Docker Compose...")

    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "down",
        "--remove-orphans",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            error(f"Docker Compose down failed: {result.stderr}")
            return False
        success("Egg stack stopped")
        return True
    except Exception as e:
        error(f"Failed to stop stack: {e}")
        return False


def wait_for_gateway(compose_file: Path, timeout: int = 60) -> bool:
    """Wait for gateway to be healthy.

    Args:
        compose_file: Path to docker-compose.yml (for loading .env)
        timeout: Maximum seconds to wait

    Returns:
        True if gateway is healthy, False on timeout
    """
    info("Waiting for gateway to be healthy...")

    # Load port from .env or use default from constants
    port = str(GATEWAY_PORT)
    env_file = get_env_file(compose_file)
    if env_file:
        with open(env_file) as f:
            for line in f:
                if line.startswith("GATEWAY_API_PORT="):
                    port = line.split("=", 1)[1].strip()
                    break

    health_url = f"http://localhost:{port}/api/v1/health"
    elapsed = 0

    while elapsed < timeout:
        try:
            result = subprocess.run(
                ["curl", "-sf", "--max-time", "5", health_url],
                capture_output=True,
                timeout=10,  # subprocess timeout slightly higher than curl's
                check=False,
            )
            if result.returncode == 0:
                success("Gateway is healthy")
                return True
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        time.sleep(2)
        elapsed += 2

        if elapsed % 10 == 0:
            info(f"Still waiting... ({elapsed}/{timeout}s)")

    error(f"Gateway health check timed out after {timeout}s")
    return False


def get_compose_gateway_ip(compose_file: Path) -> str:
    """Get the gateway container IP from compose stack.

    Args:
        compose_file: Path to docker-compose.yml

    Returns:
        Gateway IP address (default 172.32.0.2)
    """
    # The compose file uses fixed IPs - read project name to construct container name
    project_name = "egg"
    env_file = get_env_file(compose_file)
    if env_file:
        with open(env_file) as f:
            for line in f:
                if line.startswith("COMPOSE_PROJECT_NAME="):
                    project_name = line.split("=", 1)[1].strip()
                    break

    container_name = f"{project_name}-gateway"

    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                '{{with index .NetworkSettings.Networks "'
                + project_name
                + '-isolated"}}{{.IPAddress}}{{end}}',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        ip = result.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass

    # Fall back to default
    return "172.32.0.2"


def run_compose_mode(down: bool = False, build: bool = False) -> int:
    """Run egg in compose mode.

    This is the main entry point for --compose mode. It:
    1. Finds the docker-compose.yml file
    2. Validates configuration
    3. Starts the gateway via compose
    4. Waits for health check
    5. Returns control to normal sandbox execution

    Args:
        down: If True, stop the stack and exit
        build: If True, rebuild images before starting

    Returns:
        Exit code (0 for success)
    """
    try:
        compose_file = get_compose_file()
    except FileNotFoundError as e:
        error(str(e))
        return 1

    info(f"Using compose file: {compose_file}")

    # Handle --compose --down
    if down:
        if compose_down(compose_file):
            return 0
        return 1

    # Validate configuration
    if not ensure_env_configured(compose_file):
        return 1

    # Start the stack
    if not compose_up(compose_file, build=build):
        return 1

    # Wait for gateway health
    if not wait_for_gateway(compose_file):
        error("Gateway failed to start. Check logs with: docker compose logs gateway")
        return 1

    info("Gateway ready. Sandbox sessions can now be started.")
    info("")
    info("To start a sandbox:")
    info("  egg --public   # or --private")
    info("")
    info("To stop the stack:")
    info("  egg --compose --down")

    return 0
