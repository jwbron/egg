"""Gateway sidecar management for egg.

This module handles the gateway sidecar container that provides
policy enforcement for git/gh operations.

Gateway Lifecycle Management:
- Gateway starts automatically when egg runs (no manual setup needed)
- Hash-based rebuild detection ensures image is rebuilt when code changes
- Cross-platform support (Linux and macOS)
"""

import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

from .context import get_context
from .output import error, info, success, warn

# Launcher secret file location (for session and worktree management)

# Label used to store build content hash on gateway Docker image
GATEWAY_BUILD_HASH_LABEL = "org.egg.gateway-build-hash"

# Container home path (fixed user in gateway container)
CONTAINER_HOME = "/home/egg"


# =============================================================================
# Hash-Based Rebuild Detection
# =============================================================================


def _hash_file(path: Path, hasher: Any) -> None:
    """Add a single file's content to the hasher."""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
    except OSError:
        pass


def _hash_directory(path: Path, hasher: Any, exclude_tests: bool = False) -> None:
    """Recursively hash all files in a directory.

    Args:
        path: Directory to hash
        hasher: Hash object to update
        exclude_tests: If True, skip test files and directories
    """
    if not path.exists():
        return
    for item in sorted(path.rglob("*")):
        if item.is_file() and not item.name.startswith("."):
            # Skip test files if requested
            if exclude_tests and ("test" in item.name.lower() or "tests" in str(item)):
                continue
            # Include relative path in hash to detect renames/moves
            hasher.update(str(item.relative_to(path)).encode())
            _hash_file(item, hasher)


def compute_gateway_build_hash() -> str:
    """Compute a SHA256 hash of all files that affect the gateway Docker image.

    This includes:
    - gateway/Dockerfile, gateway/entrypoint.sh
    - gateway/*.py (excluding tests)
    - gateway/squid.conf, gateway/squid-allow-all.conf
    - gateway/allowed_domains.txt
    - gateway/scripts/*.sh
    - shared/egg_logging/, shared/egg_config/

    Returns:
        Hex-encoded SHA256 hash string
    """
    # Find repo root (parent of sandbox directory)
    script_dir = Path(__file__).resolve().parent.parent
    repo_root = script_dir.parent
    gateway_dir = repo_root / "gateway"
    shared_dir = repo_root / "shared"

    hasher = hashlib.sha256()

    # Single files in gateway/
    single_files = [
        gateway_dir / "Dockerfile",
        gateway_dir / "entrypoint.sh",
        gateway_dir / "squid.conf",
        gateway_dir / "squid-allow-all.conf",
        gateway_dir / "allowed_domains.txt",
    ]
    for path in single_files:
        if path.exists():
            hasher.update(path.name.encode())
            _hash_file(path, hasher)

    # Python files in gateway/ (excluding tests)
    for py_file in sorted(gateway_dir.glob("*.py")):
        if "test" not in py_file.name.lower():
            hasher.update(py_file.name.encode())
            _hash_file(py_file, hasher)

    # Scripts in gateway/scripts/
    scripts_dir = gateway_dir / "scripts"
    if scripts_dir.exists():
        hasher.update(b"scripts")
        _hash_directory(scripts_dir, hasher)

    # Shared modules (egg_logging, egg_config)
    for module_name in ["egg_logging", "egg_config"]:
        module_path = shared_dir / module_name
        if module_path.exists():
            hasher.update(module_name.encode())
            _hash_directory(module_path, hasher, exclude_tests=True)

    # Shared pyproject.toml
    shared_pyproject = shared_dir / "pyproject.toml"
    if shared_pyproject.exists():
        hasher.update(b"shared/pyproject.toml")
        _hash_file(shared_pyproject, hasher)

    return hasher.hexdigest()


def get_gateway_image_hash() -> str | None:
    """Get the build hash stored in the gateway Docker image label.

    Returns:
        Hash string if image exists and has the label, None otherwise
    """
    if not gateway_image_exists():
        return None

    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{index .Config.Labels "{GATEWAY_BUILD_HASH_LABEL}"}}}}',
                get_context().gateway_image,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            hash_value = result.stdout.strip()
            # Docker returns empty string or "<no value>" if label doesn't exist
            if hash_value and hash_value != "<no value>":
                return hash_value
        return None
    except Exception:
        return None


def should_rebuild_gateway() -> tuple[bool, str]:
    """Check if the gateway Docker image needs to be rebuilt.

    Returns:
        Tuple of (should_rebuild, reason)
    """
    if not gateway_image_exists():
        return True, "image does not exist"

    current_hash = compute_gateway_build_hash()
    stored_hash = get_gateway_image_hash()

    if stored_hash is None:
        return True, "no build hash stored (legacy image)"

    if current_hash != stored_hash:
        return True, "gateway source files changed"

    return False, "build hash matches (skipping rebuild)"


# =============================================================================
# Helper Functions for Gateway Container Configuration
# =============================================================================


def _load_secrets() -> dict[str, str]:
    """Load secrets from ~/.config/egg/secrets.env.

    Returns:
        Dict of environment variable name to value
    """
    secrets_file = get_context().config_dir / "secrets.env"
    secrets_dict: dict[str, str] = {}

    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip()
                # Strip surrounding quotes (bash-style env files)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                secrets_dict[key.strip()] = value

    return secrets_dict


def _parse_git_mounts(config_file: Path, home_dir: str) -> list[str]:
    """Parse repositories.yaml and return git mount specifications.

    Matches logic from gateway/parse-git-mounts.py.

    Args:
        config_file: Path to repositories.yaml
        home_dir: Container home directory path

    Returns:
        List of mount specs in "source:destination" format
    """
    mounts: list[str] = []

    if not config_file.exists():
        return mounts

    try:
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}

        local_repos = config.get("local_repos", {})
        paths = local_repos.get("paths", [])

        for repo_path_str in paths:
            repo_path = Path(repo_path_str).expanduser()
            if not repo_path.exists():
                continue

            repo_name = repo_path.name
            git_dir = repo_path / ".git"

            if git_dir.is_file():
                # Worktree - read the actual git dir location
                content = git_dir.read_text().strip()
                if content.startswith("gitdir:"):
                    actual_git = content[7:].strip()
                    git_dir = Path(actual_git)

            if git_dir.exists():
                # Mount git directory to a known location
                container_git_path = f"{home_dir}/.git-main/{repo_name}"
                mounts.append(f"{git_dir}:{container_git_path}")

    except Exception as e:
        warn(f"Failed to parse git mounts: {e}")

    return mounts


def _get_user_git_config(config_file: Path) -> tuple[str | None, str | None]:
    """Get git identity from user_mode config in repositories.yaml.

    Args:
        config_file: Path to repositories.yaml

    Returns:
        Tuple of (git_name, git_email), either may be None
    """
    if not config_file.exists():
        return None, None

    try:
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}

        user_mode = config.get("user_mode", {})
        return user_mode.get("git_name"), user_mode.get("git_email")
    except Exception:
        return None, None


# =============================================================================


def create_worktrees(
    container_id: str,
    repos: list[str],
    base_branch: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> tuple[bool, dict[str, str], list[str]]:
    """Request the gateway to create worktrees for a container.

    Args:
        container_id: Container identifier
        repos: List of repository names (or owner/repo format)
        base_branch: Branch to base worktrees on. When None, the gateway
            resolves the remote default branch per-repo (e.g., origin/main).
        uid: User ID to set worktree ownership to (for container user)
        gid: Group ID to set worktree ownership to (for container user)

    Returns:
        Tuple of (success, worktrees_dict, errors_list)
        - worktrees_dict maps repo_name to worktree_path
        - errors_list contains any error messages
    """
    request_data: dict[str, Any] = {
        "container_id": container_id,
        "repos": repos,
    }
    if base_branch is not None:
        request_data["base_branch"] = base_branch
    if uid is not None:
        request_data["uid"] = uid
    if gid is not None:
        request_data["gid"] = gid

    success_flag, response = launcher_api_call(
        "/api/v1/worktree/create",
        method="POST",
        data=request_data,
    )

    if not success_flag:
        return False, {}, [response.get("error", "Unknown error")]

    data = response.get("data", {})
    return True, data.get("worktrees", {}), data.get("errors", [])


def delete_worktrees(container_id: str, force: bool = False) -> tuple[bool, list[str], list[str]]:
    """Request the gateway to delete worktrees for a container.

    Args:
        container_id: Container identifier
        force: Force removal even with uncommitted changes

    Returns:
        Tuple of (success, deleted_repos, errors_list)
    """
    success_flag, response = launcher_api_call(
        "/api/v1/worktree/delete",
        method="POST",
        data={
            "container_id": container_id,
            "force": force,
        },
    )

    if not success_flag:
        return False, [], [response.get("error", "Unknown error")]

    data = response.get("data", {})
    return True, data.get("deleted", []), data.get("errors", [])


# =============================================================================
# Session Management for Per-Container Repository Mode
# =============================================================================


def get_launcher_secret() -> str:
    """Get the launcher authentication secret.

    Returns the shared secret used to authenticate session management
    operations with the gateway sidecar.  If ``ctx.launcher_secret``
    is set (GHA), returns it directly without file I/O.  Otherwise
    reads from (or generates) the on-disk secret file.

    Returns:
        The launcher secret string
    """
    ctx = get_context()
    if ctx.launcher_secret:
        return ctx.launcher_secret

    secret_file = ctx.config_dir / "launcher-secret"
    if secret_file.exists():
        return secret_file.read_text().strip()

    # Generate a new secret
    new_secret = secrets.token_urlsafe(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(new_secret)
    secret_file.chmod(0o600)
    return new_secret


def launcher_api_call(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[bool, dict[str, Any]]:
    """Make an authenticated API call to the gateway using launcher secret.

    This uses the launcher_secret which has elevated privileges for
    session management operations.

    In local mode the gateway ports are published to localhost.
    In GHA (``ctx.publish_ports is False``) the gateway is reached
    directly via its container IP on the isolated network.

    Args:
        endpoint: API endpoint path (e.g., "/api/v1/sessions/create")
        method: HTTP method (GET, POST, or DELETE)
        data: Optional JSON data for POST requests
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success, response_data)
    """
    ctx = get_context()
    if ctx.publish_ports:
        host = "localhost"
    else:
        host = ctx.gateway_isolated_ip
    url = f"http://{host}:{ctx.gateway_port}{endpoint}"
    secret = get_launcher_secret()

    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }

    try:
        if method == "POST" and data:
            body = json.dumps(data).encode("utf-8")
            req = Request(url, data=body, headers=headers, method=method)
        elif method == "DELETE":
            req = Request(url, headers=headers, method=method)
        else:
            req = Request(url, headers=headers, method=method)

        with urlopen(req, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
            return response_data.get("success", False), response_data

    except URLError as e:
        return False, {"error": f"Gateway connection failed: {e}"}
    except json.JSONDecodeError as e:
        return False, {"error": f"Invalid JSON response: {e}"}
    except Exception as e:
        return False, {"error": f"Gateway API error: {e}"}


def create_session(
    container_id: str,
    container_ip: str,
    mode: str,
    repos: list[str],
    uid: int | None = None,
    gid: int | None = None,
    phase: str | None = None,
    issue_number: int | None = None,
    pr_number: int | None = None,
    pipeline_id: str | None = None,
    agent_role: str | None = None,
) -> tuple[bool, str | None, dict[str, str], list[str], list[str]]:
    """Create a session with atomic visibility query, filtering, and worktree creation.

    This is the primary endpoint for per-container mode. It atomically:
    1. Queries repository visibility for all requested repos
    2. Filters repos based on mode (private keeps private/internal, public keeps public)
    3. Creates worktrees for filtered repos
    4. Registers session with the filtered repo list

    Args:
        container_id: Docker container ID
        container_ip: Container's IP address on the Docker network
        mode: Repository visibility mode ("private" or "public")
        repos: List of repository names (or owner/repo format)
        uid: User ID to set worktree ownership to
        gid: Group ID to set worktree ownership to
        phase: SDLC pipeline phase (e.g., "refine", "plan", "implement", "pr")
        issue_number: GitHub issue number for checkpoint metadata
        pr_number: GitHub PR number for checkpoint metadata
        pipeline_id: Pipeline run ID for multi-agent correlation
        agent_role: Agent role (e.g., "coder", "tester") for checkpoint metadata

    Returns:
        Tuple of (success, session_token, worktrees_dict, filtered_repos, errors_list)
        - session_token: The session token for the container to use
        - worktrees_dict: Maps repo_name to worktree_path
        - filtered_repos: List of repos that passed visibility filtering
        - errors_list: Any error messages
    """
    request_data: dict[str, Any] = {
        "container_id": container_id,
        "container_ip": container_ip,
        "mode": mode,
        "repos": repos,
    }
    if uid is not None:
        request_data["uid"] = uid
    if gid is not None:
        request_data["gid"] = gid
    if phase is not None:
        request_data["phase"] = phase
    if issue_number is not None:
        request_data["issue_number"] = issue_number
    if pr_number is not None:
        request_data["pr_number"] = pr_number
    if pipeline_id is not None:
        request_data["pipeline_id"] = pipeline_id
    if agent_role is not None:
        request_data["agent_role"] = agent_role

    success_flag, response = launcher_api_call(
        "/api/v1/sessions/create",
        method="POST",
        data=request_data,
        timeout=60,  # Session creation can take longer (visibility checks, worktrees)
    )

    if not success_flag:
        return False, None, {}, [], [response.get("error", "Unknown error")]

    data = response.get("data", {})
    return (
        True,
        data.get("session_token"),
        data.get("worktrees", {}),
        data.get("filtered_repos", []),
        data.get("errors", []),
    )


def delete_session(session_token: str) -> tuple[bool, str | None]:
    """Delete a session and clean up associated worktrees.

    Only the launcher (with launcher_secret) can delete sessions.

    Args:
        session_token: The session token to delete

    Returns:
        Tuple of (success, error_message)
    """
    success_flag, response = launcher_api_call(
        f"/api/v1/sessions/{session_token}",
        method="DELETE",
    )

    if not success_flag:
        return False, response.get("error", "Unknown error")

    return True, None


def delete_session_by_container(container_id: str) -> tuple[bool, str | None]:
    """Delete a session by container ID and clean up worktrees.

    This is a convenience function that looks up the session by container ID.
    Used when the launcher doesn't have the session token (e.g., cleanup of
    crashed containers).

    Note: This requires listing sessions and finding the right one.
    If the session token is known, use delete_session() instead.

    Args:
        container_id: Docker container ID

    Returns:
        Tuple of (success, error_message)
    """
    # List sessions to find the one for this container
    success_flag, response = launcher_api_call("/api/v1/sessions", method="GET")

    if not success_flag:
        return False, response.get("error", "Unknown error")

    sessions = response.get("data", {}).get("sessions", [])
    for session in sessions:
        if session.get("container_id") == container_id:
            # Found it - but we don't have the token from list endpoint
            # We need to delete worktrees manually
            break

    # Fall back to just deleting worktrees directly
    wt_success, _deleted, wt_errors = delete_worktrees(container_id, force=True)
    if not wt_success and wt_errors:
        return False, wt_errors[0]

    return True, None


def get_repo_visibilities(repos: list[str]) -> tuple[bool, dict[str, str | None], str | None]:
    """Query visibility for multiple repositories.

    This is used for informational purposes. For atomic session+worktree
    creation, use create_session() instead.

    Args:
        repos: List of owner/repo strings

    Returns:
        Tuple of (success, visibilities_dict, error_message)
        - visibilities_dict maps repo to visibility ("public", "private", "internal", or None)
    """
    repos_param = ",".join(repos)
    success_flag, response = launcher_api_call(
        f"/api/v1/repos/visibility?repos={repos_param}",
        method="GET",
    )

    if not success_flag:
        return False, {}, response.get("error", "Unknown error")

    data = response.get("data", {})
    return True, data.get("visibilities", {}), None


def is_gateway_running() -> bool:
    """Check if the gateway container is running.

    Returns:
        True if gateway container is running, False otherwise
    """
    ctx = get_context()
    result = subprocess.run(
        ["docker", "container", "inspect", "-f", "{{.State.Running}}", ctx.gateway_container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def gateway_image_exists() -> bool:
    """Check if gateway Docker image exists."""
    ctx = get_context()
    return (
        subprocess.run(
            ["docker", "image", "inspect", ctx.gateway_image], capture_output=True, check=False
        ).returncode
        == 0
    )


def build_gateway_image(force: bool = False) -> bool:
    """Build the gateway sidecar Docker image.

    Builds from the repo root using the Dockerfile at gateway/Dockerfile.
    Includes a build hash label for change detection.

    Args:
        force: If True, rebuild even if hash matches (default False)

    Returns:
        True if build succeeded or skipped (up-to-date), False on failure
    """
    # Check if rebuild is needed (unless forced)
    if not force:
        needs_rebuild, reason = should_rebuild_gateway()
        if not needs_rebuild:
            info(f"Gateway image up-to-date: {reason}")
            return True
        info(f"Building gateway image: {reason}")
    else:
        info("Force rebuilding gateway image...")

    # Find repo root (parent of sandbox directory)
    script_dir = Path(__file__).resolve().parent.parent
    repo_root = script_dir.parent

    dockerfile_path = repo_root / "gateway" / "Dockerfile"
    if not dockerfile_path.exists():
        error(f"Gateway Dockerfile not found at {dockerfile_path}")
        return False

    # Compute build hash for label
    build_hash = compute_gateway_build_hash()

    info("Building gateway sidecar image...")
    result = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            get_context().gateway_image,
            "--label",
            f"{GATEWAY_BUILD_HASH_LABEL}={build_hash}",
            "-f",
            str(dockerfile_path),
            str(repo_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0:
        success("Gateway image built successfully")
        return True

    error(f"Gateway image build failed: {result.stderr}")
    return False


def wait_for_gateway_health(timeout: int = 30, check_proxy: bool = True) -> bool:
    """Wait for the gateway to become healthy.

    Polls the health endpoint until it responds or timeout is reached.
    Optionally verifies proxy connectivity to ensure Squid can reach external domains.

    Args:
        timeout: Maximum seconds to wait for health
        check_proxy: Also verify Squid proxy can reach api.anthropic.com

    Returns:
        True if gateway is healthy (and proxy works if check_proxy=True), False on timeout
    """
    import urllib.error
    import urllib.request

    ctx = get_context()
    # In local mode, gateway ports are published to localhost.
    # In GHA, reach the gateway via its container IP.
    if ctx.publish_ports:
        host = "localhost"
    else:
        host = ctx.gateway_isolated_ip
    health_url = f"http://{host}:{ctx.gateway_port}/api/v1/health"
    proxy_url = f"http://{host}:{ctx.gateway_proxy_port}"

    start_time = time.time()
    api_healthy = False
    proxy_healthy = False

    while time.time() - start_time < timeout:
        # Check 1: Gateway API health endpoint
        if not api_healthy:
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:
                    if response.status == 200:
                        api_healthy = True
            except (urllib.error.URLError, OSError):
                pass  # Gateway API not ready yet

        # Check 2: Squid proxy connectivity (only after API is healthy)
        if api_healthy and check_proxy and not proxy_healthy:
            try:
                # Test proxy connectivity to api.anthropic.com
                # Use CONNECT method via proxy to verify Squid can reach external domains
                import ssl

                # Create SSL context that doesn't verify certificates
                # This is safe because we're only testing proxy connectivity,
                # not transmitting sensitive data
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                https_handler = urllib.request.HTTPSHandler(context=ssl_context)
                opener = urllib.request.build_opener(proxy_handler, https_handler)
                # Anthropic API returns 401 without auth, which proves proxy works
                req = urllib.request.Request(
                    "https://api.anthropic.com/",
                    headers={"User-Agent": "egg-gateway-check"},
                )
                with opener.open(req, timeout=10) as response:
                    # Any response (even 401) means proxy is working
                    proxy_healthy = True
            except urllib.error.HTTPError as e:
                # 401/403 means we reached Anthropic - proxy is working
                if e.code in (401, 403):
                    proxy_healthy = True
            except (urllib.error.URLError, OSError):
                pass  # Proxy not ready yet

        # Success conditions
        if api_healthy and (not check_proxy or proxy_healthy):
            return True

        time.sleep(0.5)

    return False


def start_gateway_container() -> bool:
    """Ensure the gateway sidecar is running and up-to-date.

    This function manages the complete gateway lifecycle:
    1. Checks if gateway is running and up-to-date (hash check)
    2. Creates networks if needed
    3. Builds image if needed (with hash label)
    4. Starts container with proper mounts and environment
    5. Connects to both networks (dual-homed)
    6. Waits for health check

    When ``ctx.skip_build`` is True (GHA), the image build step is
    skipped and the pre-pulled image is used directly.

    Returns:
        True if gateway is healthy, False otherwise
    """
    from .docker import ensure_gateway_networks

    ctx = get_context()

    # Quick health check - if gateway is running and healthy, verify hash
    if is_gateway_running():
        if wait_for_gateway_health(timeout=5, check_proxy=False):
            # Gateway running - check if rebuild needed
            needs_rebuild, reason = should_rebuild_gateway()
            if not needs_rebuild:
                # Already running and up-to-date - verify proxy and return
                if wait_for_gateway_health(timeout=15, check_proxy=True):
                    return True
                # Proxy not working - need to restart
                warn("Gateway restart needed: API healthy but proxy not responding")
            else:
                info(f"Gateway rebuild needed: {reason}")
        else:
            # Gateway container is running but API is not healthy
            warn("Gateway restart needed: gateway running but API not healthy")

    # Ensure networks exist
    if not ensure_gateway_networks():
        error("Failed to create gateway networks")
        return False

    # Build/update gateway image (handles hash check internally)
    # Skip when images are pre-pulled (GHA)
    if not ctx.skip_build:
        if not build_gateway_image():
            error("Failed to build gateway image")
            return False

    # Stop existing gateway container if running
    subprocess.run(
        ["docker", "rm", "-f", ctx.gateway_container_name],
        capture_output=True,
        check=False,
    )

    # Prepare mounts and environment
    mounts, env_args = _prepare_gateway_config()

    # Start gateway container on isolated network first
    info("Starting gateway container...")
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        ctx.gateway_container_name,
        "--network",
        ctx.isolated_network,
        "--ip",
        ctx.gateway_isolated_ip,
        "--security-opt",
        "label=disable",
    ]

    # Publish ports to localhost only in local mode
    if ctx.publish_ports:
        cmd.extend(
            [
                "-p",
                f"{ctx.gateway_port}:{ctx.gateway_port}",
                "-p",
                f"{ctx.gateway_proxy_port}:{ctx.gateway_proxy_port}",
            ]
        )

    cmd.extend(env_args)
    cmd.extend(mounts)
    cmd.append(ctx.gateway_image)

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error(f"Failed to start gateway container: {result.stderr}")
        return False

    # Connect to external network (dual-homed)
    info("Connecting gateway to external network...")
    result = subprocess.run(
        [
            "docker",
            "network",
            "connect",
            "--ip",
            ctx.gateway_external_ip,
            ctx.external_network,
            ctx.gateway_container_name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error(f"Failed to connect gateway to external network: {result.stderr}")
        # Clean up
        subprocess.run(
            ["docker", "rm", "-f", ctx.gateway_container_name], capture_output=True, check=False
        )
        return False

    # Wait for gateway to be healthy
    info("Waiting for gateway health check...")
    if not wait_for_gateway_health(timeout=30, check_proxy=True):
        error("Gateway failed to become healthy")
        error("")
        error(f"Check logs: docker logs {ctx.gateway_container_name}")
        return False

    success("Gateway started successfully")
    return True


def cleanup_gateway() -> None:
    """Stop and remove the gateway container and ephemeral networks.

    Called during cleanup of ephemeral (GHA) runs.
    """
    from .docker import teardown_networks

    ctx = get_context()
    subprocess.run(
        ["docker", "stop", "-t", "5", ctx.gateway_container_name],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["docker", "rm", "-f", ctx.gateway_container_name],
        capture_output=True,
        check=False,
    )
    teardown_networks()


def _prepare_gateway_config() -> tuple[list[str], list[str]]:
    """Prepare mount arguments and environment variables for gateway container.

    Returns:
        Tuple of (mount_args, env_args) lists for docker run
    """
    ctx = get_context()
    home_dir = str(Path.home())
    config_file = ctx.config_dir / "repositories.yaml"
    config_dir = ctx.config_dir
    repos_dir = Path.home() / "repos"
    worktrees_dir = Path.home() / ".egg-worktrees"
    state_dir = Path.home() / ".egg-state"
    git_main_dir = Path.home() / ".git-main"
    local_objects_dir = Path.home() / ".egg-local-objects"
    shared_certs_dir = Path.home() / ".egg-shared-certs"

    mounts = []
    env_args = []

    # Config file mount
    if config_file.exists():
        mounts.extend(["-v", f"{config_file}:/config/repositories.yaml:ro"])

    # Config directory (contains secrets.env, github-app.pem, launcher-secret)
    if config_dir.exists():
        mounts.extend(["-v", f"{config_dir}:{CONTAINER_HOME}/.config/egg:ro"])
        mounts.extend(["-v", f"{config_dir}:/secrets:ro"])

    # Repos directory
    if repos_dir.exists():
        mounts.extend(["-v", f"{repos_dir}:{CONTAINER_HOME}/repos"])
    elif config_file.exists():
        # GHA: repos are at GITHUB_WORKSPACE, not ~/repos/.
        # Mount each local_repos path so the gateway has working tree access.
        try:
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            for path_str in cfg.get("local_repos", {}).get("paths", []):
                p = Path(path_str).expanduser()
                if p.exists():
                    mounts.extend(["-v", f"{p}:{CONTAINER_HOME}/repos/{p.name}"])
        except Exception:
            pass

    # Worktrees directory
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    mounts.extend(["-v", f"{worktrees_dir}:{CONTAINER_HOME}/.egg-worktrees"])

    # State directory (session persistence across gateway restarts)
    state_dir.mkdir(parents=True, exist_ok=True)
    mounts.extend(["-v", f"{state_dir}:{CONTAINER_HOME}/.egg-state"])

    # Git main directory
    if git_main_dir.exists():
        mounts.extend(["-v", f"{git_main_dir}:{CONTAINER_HOME}/.git-main"])

    # Local objects directory
    if local_objects_dir.exists():
        mounts.extend(["-v", f"{local_objects_dir}:{CONTAINER_HOME}/.egg-local-objects:ro"])

    # Shared certs directory
    shared_certs_dir.mkdir(parents=True, exist_ok=True)
    shared_certs_dir.chmod(0o755)
    mounts.extend(["-v", f"{shared_certs_dir}:/shared/certs"])

    # Dynamic git mounts from local_repos in repositories.yaml
    if config_file.exists():
        for mount_spec in _parse_git_mounts(config_file, CONTAINER_HOME):
            mounts.extend(["-v", mount_spec])

    # Environment variables
    env_args.extend(["-e", "EGG_REPO_CONFIG=/config/repositories.yaml"])
    env_args.extend(["-e", f"HOME={CONTAINER_HOME}"])
    env_args.extend(["-e", f"HOST_HOME={home_dir}"])
    env_args.extend(["-e", f"HOST_UID={os.getuid()}"])
    env_args.extend(["-e", f"HOST_GID={os.getgid()}"])

    # Load secrets and pass relevant ones
    secrets_dict = _load_secrets()
    if "GITHUB_USER_TOKEN" in secrets_dict:
        env_args.extend(["-e", f"GITHUB_USER_TOKEN={secrets_dict['GITHUB_USER_TOKEN']}"])

    # Pass gateway configuration from secrets.env
    # These are required for policy enforcement (bot identity, branch prefixes, trusted users)
    for key in ["GATEWAY_BOT_NAME", "GATEWAY_BOT_BRANCH_PREFIX", "GATEWAY_TRUSTED_USERS"]:
        if key in secrets_dict:
            env_args.extend(["-e", f"{key}={secrets_dict[key]}"])

    # Git identity from config
    if config_file.exists():
        git_name, git_email = _get_user_git_config(config_file)
        if git_name:
            env_args.extend(["-e", f"EGG_USER_GIT_NAME={git_name}"])
        if git_email:
            env_args.extend(["-e", f"EGG_USER_GIT_EMAIL={git_email}"])

    return mounts, env_args
