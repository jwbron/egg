"""Gateway sidecar management for jib.

This module handles the gateway sidecar container that provides
policy enforcement for git/gh operations.
"""

import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import (
    GATEWAY_CONTAINER_NAME,
    GATEWAY_IMAGE_NAME,
    GATEWAY_PORT,
    GATEWAY_PROXY_PORT,
    Config,
)
from .output import error, info, success

# Launcher secret file location (for session and worktree management)
LAUNCHER_SECRET_FILE = Config.USER_CONFIG_DIR / "launcher-secret"


def create_worktrees(
    container_id: str,
    repos: list[str],
    base_branch: str = "HEAD",
    uid: int | None = None,
    gid: int | None = None,
) -> tuple[bool, dict[str, str], list[str]]:
    """Request the gateway to create worktrees for a container.

    Args:
        container_id: Container identifier
        repos: List of repository names (or owner/repo format)
        base_branch: Branch to base worktrees on
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
        "base_branch": base_branch,
    }
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
    operations with the gateway sidecar. Generates a new secret if one
    doesn't exist.

    Returns:
        The launcher secret string
    """
    if LAUNCHER_SECRET_FILE.exists():
        return LAUNCHER_SECRET_FILE.read_text().strip()

    # Generate a new secret
    new_secret = secrets.token_urlsafe(32)
    LAUNCHER_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_SECRET_FILE.write_text(new_secret)
    LAUNCHER_SECRET_FILE.chmod(0o600)
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

    Args:
        endpoint: API endpoint path (e.g., "/api/v1/sessions/create")
        method: HTTP method (GET, POST, or DELETE)
        data: Optional JSON data for POST requests
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success, response_data)
    """
    url = f"http://localhost:{GATEWAY_PORT}{endpoint}"
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
    result = subprocess.run(
        ["docker", "container", "inspect", "-f", "{{.State.Running}}", GATEWAY_CONTAINER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def gateway_image_exists() -> bool:
    """Check if gateway Docker image exists."""
    return (
        subprocess.run(
            ["docker", "image", "inspect", GATEWAY_IMAGE_NAME], capture_output=True, check=False
        ).returncode
        == 0
    )


def build_gateway_image() -> bool:
    """Build the gateway sidecar Docker image.

    Builds from the repo root using the Dockerfile at
    host-services/gateway/Dockerfile.

    Returns:
        True if build succeeded, False otherwise
    """
    # Find repo root (parent of sandbox directory)
    script_dir = Path(__file__).resolve().parent.parent
    repo_root = script_dir.parent

    dockerfile_path = repo_root / "host-services" / "gateway" / "Dockerfile"
    if not dockerfile_path.exists():
        error(f"Gateway Dockerfile not found at {dockerfile_path}")
        return False

    info("Building gateway sidecar image...")
    result = subprocess.run(
        ["docker", "build", "-t", GATEWAY_IMAGE_NAME, "-f", str(dockerfile_path), str(repo_root)],
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

    # Use container name for health check since we're on the same network
    # But during startup from host, we need to use localhost or check via docker exec
    health_url = f"http://localhost:{GATEWAY_PORT}/api/v1/health"
    proxy_url = f"http://localhost:{GATEWAY_PROXY_PORT}"

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
    """Ensure the gateway sidecar is available.

    The gateway is managed by systemd (gateway.service). This function
    checks if it's running and healthy. If not, it tells the user how to start it.

    The health check verifies both:
    1. Gateway API responds on port 9847
    2. Squid proxy can reach api.anthropic.com on port 3128

    Returns:
        True if gateway is healthy, False otherwise
    """
    # First do a quick check without proxy verification (for fast feedback)
    if wait_for_gateway_health(timeout=5, check_proxy=False):
        # API is up, now verify proxy connectivity with more time
        # This is the critical check that prevents container startup failures
        if wait_for_gateway_health(timeout=15, check_proxy=True):
            return True
        # API healthy but proxy failed
        error("Gateway API is healthy but Squid proxy is not responding")
        error("")
        error("The proxy may still be initializing. Check Squid logs:")
        error("  docker logs egg-gateway 2>&1 | grep -i squid")
        error("")
        error("Try restarting the gateway service:")
        error("  systemctl --user restart gateway.service")
        return False

    # Gateway not available - check systemd service status
    service_result = subprocess.run(
        ["systemctl", "--user", "is-active", "gateway.service"],
        capture_output=True,
        text=True,
        check=False,
    )

    if service_result.returncode != 0:
        error("Gateway sidecar service is not running")
        error("")
        error("To start the gateway:")
        error("  systemctl --user start gateway.service")
        error("")
        error("To set up the gateway (if not installed):")
        error("  ./gateway/setup.sh")
        return False

    # Service is active but not healthy - check logs
    error("Gateway sidecar service is running but not healthy")
    error("")
    error("Check service logs:")
    error("  journalctl --user -u gateway.service -f")
    error("")
    error("Try restarting the service:")
    error("  systemctl --user restart gateway.service")

    return False
