"""Docker Compose integration for egg.

This module provides functionality for launching the egg stack (gateway +
orchestrator) using Docker Compose. The default ``egg`` CLI path uses
``ensure_compose_services()`` to start both services before launching the
sandbox container via ``docker run``.

Usage:
    egg                   # Default: compose up gateway+orchestrator, then sandbox
    egg --compose --down  # Stop the compose stack
    egg --compose --build # Rebuild images before starting
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .context import get_context
from .output import error, info, success, warn


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


def _generate_env_file(compose_file: Path) -> bool:
    """Generate .env file dynamically from the same sources as gateway docker run.

    Reads secrets, git identity, and host paths to produce a complete .env
    that docker-compose.yml can interpolate.

    Args:
        compose_file: Path to docker-compose.yml (env written next to it)

    Returns:
        True if .env was written successfully
    """
    from .config import get_local_repos
    from .gateway import _get_user_git_config, _load_secrets, get_launcher_secret

    ctx = get_context()
    config_dir = ctx.config_dir
    config_file = config_dir / "repositories.yaml"

    env_vars: dict[str, str] = {}

    # Host identity
    env_vars["HOST_UID"] = str(os.getuid())
    env_vars["HOST_GID"] = str(os.getgid())
    env_vars["HOST_HOME"] = str(Path.home())

    # Config path (host path for volume mounts)
    env_vars["EGG_CONFIG_DIR"] = str(config_dir)

    # Build repo map for orchestrator (host paths for Docker socket volume mounts)
    repos = get_local_repos(config_file)
    repo_map = {r.name: str(r) for r in repos}
    env_vars["EGG_HOST_REPO_MAP"] = json.dumps(repo_map)

    # Launcher secret (read or generate)
    env_vars["EGG_LAUNCHER_SECRET"] = get_launcher_secret()

    # Secrets from ~/.config/egg/secrets.env
    secrets_dict = _load_secrets()
    if "GITHUB_USER_TOKEN" in secrets_dict:
        env_vars["GITHUB_USER_TOKEN"] = secrets_dict["GITHUB_USER_TOKEN"]
    if "BOT_GITHUB_TOKEN" in secrets_dict:
        env_vars["BOT_GITHUB_TOKEN"] = secrets_dict["BOT_GITHUB_TOKEN"]

    # Gateway policy configuration from secrets.env
    for key in ["GATEWAY_BOT_NAME", "GATEWAY_BOT_BRANCH_PREFIX", "GATEWAY_TRUSTED_USERS"]:
        if key in secrets_dict:
            env_vars[key] = secrets_dict[key]

    # Git identity and per-repo checks from repositories.yaml
    if config_file.exists():
        git_name, git_email = _get_user_git_config(config_file)
        if git_name:
            env_vars["EGG_USER_GIT_NAME"] = git_name
        if git_email:
            env_vars["EGG_USER_GIT_EMAIL"] = git_email

        # Build per-repo checks map for the orchestrator
        try:
            import yaml

            with config_file.open() as f:
                cfg = yaml.safe_load(f) or {}
            repo_checks: dict[str, list[dict[str, str]]] = {}
            for repo_name, settings in (cfg.get("repo_settings") or {}).items():
                checks = settings.get("checks") if isinstance(settings, dict) else None
                if checks and isinstance(checks, list):
                    valid = [
                        {"name": str(c["name"]), "command": str(c["command"])}
                        for c in checks
                        if isinstance(c, dict) and "name" in c and "command" in c
                    ]
                    if valid:
                        repo_checks[repo_name] = valid
            env_vars["EGG_REPO_CHECKS"] = json.dumps(repo_checks)
        except Exception:
            env_vars["EGG_REPO_CHECKS"] = "{}"

    # Write .env file
    env_file = compose_file.parent / ".env"
    lines = [
        "# Auto-generated by egg — do not edit manually",
        "# Regenerated on each egg invocation",
        "",
    ]
    for key, value in sorted(env_vars.items()):
        # Quote and escape values for .env file safety
        if any(c in value for c in " \"'#$`\n\\"):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")

    new_content = "\n".join(lines) + "\n"
    # Only write if content changed to avoid unnecessary container restarts
    if env_file.exists():
        try:
            if env_file.read_text() == new_content:
                return True
        except OSError:
            pass
    env_file.write_text(new_content)
    # Restrict file permissions to owner only (contains secrets)
    env_file.chmod(0o600)
    return True


def _generate_override_file(compose_file: Path) -> Path | None:
    """Generate docker-compose.override.yml with per-repo volume mounts.

    Reads local_repos.paths from repositories.yaml and generates an override
    file that mounts each configured repo individually into gateway and
    orchestrator containers at /home/egg/repos/<name>.

    Args:
        compose_file: Path to docker-compose.yml (override written next to it)

    Returns:
        Path to override file, or None if no repos configured
    """
    from .config import get_local_repos

    ctx = get_context()
    config_file = ctx.config_dir / "repositories.yaml"

    repos = get_local_repos(config_file)
    if not repos:
        # Remove stale override file from previous runs
        override_file = compose_file.parent / "docker-compose.override.yml"
        if override_file.exists():
            override_file.unlink()
        return None

    # Validate no duplicate repo names
    names = [r.name for r in repos]
    if len(names) != len(set(names)):
        seen: set[str] = set()
        dupes = [n for n in names if n in seen or seen.add(n)]  # type: ignore[func-returns-value]
        error(f"Duplicate repo names in local_repos.paths: {dupes}")
        return None

    # Build per-repo volume mount lines
    _bad_path_chars = re.compile(r"[\x00-\x1f\x7f]")
    volume_lines = []
    for repo_path in repos:
        name = repo_path.name
        path_str = str(repo_path)
        # Reject paths with control characters (newlines, null bytes, etc.)
        if _bad_path_chars.search(path_str) or _bad_path_chars.search(name):
            error(f"Repo path contains invalid characters: {repo_path!r}")
            return None
        # Quote the volume spec to handle YAML-special chars (:, #, ', ")
        mount = f"{path_str}:/home/egg/repos/{name}"
        volume_lines.append(f'      - "{mount}"')

    lines = [
        "# Auto-generated by egg — do not edit manually",
        "# Per-repo volume mounts from repositories.yaml",
        "",
        "services:",
        "  gateway:",
        "    volumes:",
        *volume_lines,
        "  orchestrator:",
        "    volumes:",
        *volume_lines,
    ]

    override_file = compose_file.parent / "docker-compose.override.yml"
    new_content = "\n".join(lines) + "\n"

    # Only write if content changed
    if override_file.exists():
        try:
            if override_file.read_text() == new_content:
                return override_file
        except OSError:
            pass

    override_file.write_text(new_content)
    return override_file


def _has_docker_compose() -> bool:
    """Check if docker compose (v2 plugin) is available."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _cleanup_stale_resources() -> None:
    """Remove stale Docker containers and networks that block Compose.

    When switching from ``docker run`` to ``docker compose``, pre-existing
    containers (egg-gateway, egg-orchestrator) and networks (egg-isolated,
    egg-external) may conflict because they lack Compose labels.  This
    function detects and removes them so ``docker compose up`` succeeds.
    """
    from egg_config.constants import (
        EGG_EXTERNAL_NETWORK,
        EGG_ISOLATED_NETWORK,
        GATEWAY_CONTAINER_NAME,
        ORCHESTRATOR_CONTAINER_NAME,
    )

    # Remove stale containers first (they may hold references to the networks)
    for name in (GATEWAY_CONTAINER_NAME, ORCHESTRATOR_CONTAINER_NAME):
        try:
            result = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.docker.compose.service"}}',
                    name,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                # Container doesn't exist — nothing to clean up
                continue

            compose_label = result.stdout.strip()
            if compose_label:
                # Container was created by Compose — leave it alone
                continue

            info(f"Removing stale container {name}...")
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    # Remove stale networks that weren't created by Compose
    for network in (EGG_ISOLATED_NETWORK, EGG_EXTERNAL_NETWORK):
        try:
            result = subprocess.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    '{{index .Labels "com.docker.compose.network"}}',
                    network,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                # Network doesn't exist — nothing to clean up
                continue

            compose_label = result.stdout.strip()
            if compose_label:
                # Network was created by Compose — leave it alone
                continue

            info(f"Removing stale network {network}...")
            # Disconnect any remaining containers before removing
            inspect = subprocess.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    "--format",
                    "{{range .Containers}}{{.Name}} {{end}}",
                    network,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect.returncode == 0:
                for container in inspect.stdout.split():
                    container = container.strip()
                    if container:
                        subprocess.run(
                            ["docker", "network", "disconnect", "-f", network, container],
                            capture_output=True,
                            check=False,
                        )

            subprocess.run(
                ["docker", "network", "rm", network],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass


def compose_up(compose_file: Path, build: bool = False, override_file: Path | None = None) -> bool:
    """Start the egg stack using Docker Compose.

    Args:
        compose_file: Path to docker-compose.yml
        build: Whether to rebuild images
        override_file: Optional docker-compose.override.yml with per-repo mounts

    Returns:
        True if successful, False otherwise
    """
    info("Starting egg services via Docker Compose...")

    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
    ]

    if override_file:
        cmd.extend(["-f", str(override_file)])

    cmd.extend(
        [
            "up",
            "-d",
        ]
    )

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


def compose_down(compose_file: Path, override_file: Path | None = None) -> bool:
    """Stop and remove the egg stack.

    Args:
        compose_file: Path to docker-compose.yml
        override_file: Optional docker-compose.override.yml with per-repo mounts

    Returns:
        True if successful, False otherwise
    """
    info("Stopping egg stack via Docker Compose...")

    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
    ]

    if override_file:
        cmd.extend(["-f", str(override_file)])

    cmd.extend(
        [
            "down",
            "--remove-orphans",
        ]
    )

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


def _wait_for_health(url: str, label: str, timeout: int = 60) -> bool:
    """Wait for a service health endpoint to respond.

    Args:
        url: Health check URL
        label: Service label for log messages
        timeout: Maximum seconds to wait

    Returns:
        True if healthy, False on timeout
    """
    import urllib.error
    import urllib.request

    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)

    error(f"{label} health check timed out after {timeout}s")
    return False


def ensure_compose_services(build: bool = False) -> bool:
    """Start gateway + orchestrator via Docker Compose.

    This is the primary entry point used by the default ``egg`` CLI path.
    It:
    1. Locates docker-compose.yml
    2. Generates .env dynamically from the same config sources as
       ``start_gateway_container()`` (secrets.env, repositories.yaml, etc.)
    3. Runs ``docker compose up -d``
    4. Waits for gateway health check
    5. Waits for orchestrator health check (non-blocking on failure)

    Falls back to ``start_gateway_container()`` if docker compose is
    unavailable.

    Args:
        build: Force rebuild of compose images

    Returns:
        True if gateway is healthy (orchestrator failure is a warning only)
    """
    # Check docker compose availability
    if not _has_docker_compose():
        warn("Docker Compose not available, falling back to docker run")
        from .gateway import start_gateway_container

        return start_gateway_container()

    # Find compose file
    try:
        compose_file = get_compose_file()
    except FileNotFoundError:
        warn("docker-compose.yml not found, falling back to docker run")
        from .gateway import start_gateway_container

        return start_gateway_container()

    # Generate .env from current config
    if not _generate_env_file(compose_file):
        error("Failed to generate .env for Docker Compose")
        return False

    # Generate docker-compose.override.yml with per-repo volume mounts
    override_file = _generate_override_file(compose_file)

    # Clean up stale containers/networks from previous non-Compose runs
    _cleanup_stale_resources()

    # Start services
    if not compose_up(compose_file, build=build, override_file=override_file):
        return False

    ctx = get_context()

    # Wait for gateway health
    gateway_url = f"http://localhost:{ctx.gateway_port}/api/v1/health"
    info("Waiting for gateway health check...")
    if not _wait_for_health(gateway_url, "Gateway", timeout=60):
        error("Gateway failed to start. Check logs: docker compose logs gateway")
        return False
    success("Gateway is healthy")

    # Wait for orchestrator health (non-blocking — orchestrator is optional)
    orchestrator_url = f"http://localhost:{ctx.orchestrator_port}/api/v1/health"
    info("Waiting for orchestrator health check...")
    if _wait_for_health(orchestrator_url, "Orchestrator", timeout=30):
        success("Orchestrator is healthy")
    else:
        warn("Orchestrator not healthy — continuing without it")

    return True


def stop_compose_services() -> bool:
    """Stop the compose stack (gateway + orchestrator).

    Returns:
        True if stopped successfully
    """
    try:
        compose_file = get_compose_file()
    except FileNotFoundError:
        warn("docker-compose.yml not found, nothing to stop")
        return True

    # Use existing override file if present (contains per-repo mounts)
    override_file = compose_file.parent / "docker-compose.override.yml"
    if not override_file.exists():
        override_file = None

    return compose_down(compose_file, override_file=override_file)


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
    """Run egg in explicit compose control mode.

    Used by ``egg --compose --down`` and ``egg --compose --build``.

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
        override_file = compose_file.parent / "docker-compose.override.yml"
        if not override_file.exists():
            override_file = None
        if compose_down(compose_file, override_file=override_file):
            return 0
        return 1

    # Clean up stale containers/networks from previous non-Compose runs
    _cleanup_stale_resources()

    # Generate .env and override file, then start
    if not _generate_env_file(compose_file):
        return 1

    override_file = _generate_override_file(compose_file)

    if not compose_up(compose_file, build=build, override_file=override_file):
        return 1

    ctx = get_context()

    # Wait for gateway health
    gateway_url = f"http://localhost:{ctx.gateway_port}/api/v1/health"
    info("Waiting for gateway health check...")
    if not _wait_for_health(gateway_url, "Gateway", timeout=60):
        error("Gateway failed to start. Check logs: docker compose logs gateway")
        return 1

    success("Gateway is healthy")

    # Wait for orchestrator health
    orchestrator_url = f"http://localhost:{ctx.orchestrator_port}/api/v1/health"
    info("Waiting for orchestrator health check...")
    if _wait_for_health(orchestrator_url, "Orchestrator", timeout=30):
        success("Orchestrator is healthy")
    else:
        warn("Orchestrator not healthy — continuing without it")

    return 0
