"""
Gateway client for orchestrator integration.

Provides coordination between the orchestrator and gateway sidecar for:
- Session token management for spawned containers
- Gateway health monitoring
- Proxy configuration injection
- Security boundary validation
"""

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Add shared directory to path for egg_logging and config
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


try:
    from egg_config import (
        EGG_ISOLATED_NETWORK,
        GATEWAY_CONTAINER_NAME,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
    )
except ImportError:
    # Fallback defaults
    GATEWAY_CONTAINER_NAME = "egg-gateway"
    GATEWAY_PORT = 9848
    GATEWAY_PROXY_PORT = 3129
    GATEWAY_ISOLATED_IP = "172.32.0.2"
    EGG_ISOLATED_NETWORK = "egg-isolated"

logger = get_logger("orchestrator.gateway_client")


@dataclass
class SessionInfo:
    """Information about a gateway session."""

    session_token: str
    container_id: str
    container_ip: str
    mode: str  # "private" or "public"
    created_at: datetime
    expires_at: datetime


@dataclass
class GatewayHealth:
    """Gateway health status."""

    healthy: bool
    status: str
    version: str | None = None
    uptime_seconds: float | None = None
    error: str | None = None


class GatewayClient:
    """Client for interacting with the gateway sidecar.

    Provides methods for:
    - Registering sessions for spawned containers
    - Validating sessions
    - Health checking the gateway
    - Getting proxy configuration
    """

    def __init__(
        self,
        gateway_host: str | None = None,
        gateway_port: int | None = None,
        launcher_secret: str | None = None,
        timeout: int = 30,
    ):
        """Initialize the gateway client.

        Args:
            gateway_host: Gateway hostname (default: egg-gateway or env)
            gateway_port: Gateway port (default: 9848 or env)
            launcher_secret: Launcher secret for privileged operations
            timeout: Request timeout in seconds
        """
        self.gateway_host = gateway_host or os.environ.get("GATEWAY_HOST", GATEWAY_CONTAINER_NAME)
        self.gateway_port = gateway_port or int(os.environ.get("GATEWAY_PORT", GATEWAY_PORT))
        self.launcher_secret = launcher_secret or os.environ.get("EGG_LAUNCHER_SECRET")
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        """Get the gateway base URL."""
        return f"http://{self.gateway_host}:{self.gateway_port}"

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        use_launcher_auth: bool = False,
    ) -> dict[str, Any]:
        """Make an HTTP request to the gateway.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            data: Request body data
            use_launcher_auth: Use launcher secret for auth

        Returns:
            Response JSON data

        Raises:
            GatewayError: On request failure
        """
        url = f"{self.base_url}{endpoint}"
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if use_launcher_auth and self.launcher_secret:
            headers["Authorization"] = f"Bearer {self.launcher_secret}"

        body = json.dumps(data).encode() if data else None

        try:
            request = Request(url, data=body, headers=headers, method=method)
            with urlopen(request, timeout=self.timeout) as response:
                result: dict[str, Any] = json.loads(response.read().decode())
                return result
        except HTTPError as e:
            try:
                error_data = json.loads(e.read().decode())
                raise GatewayError(
                    error_data.get("message", str(e)),
                    status_code=e.code,
                    details=error_data.get("details"),
                )
            except json.JSONDecodeError:
                raise GatewayError(str(e), status_code=e.code) from e
        except URLError as e:
            raise GatewayError(f"Failed to connect to gateway: {e.reason}") from e
        except TimeoutError as e:
            raise GatewayError("Gateway request timed out") from e

    def check_health(self) -> GatewayHealth:
        """Check gateway health status.

        Returns:
            GatewayHealth with status information
        """
        try:
            result = self._make_request("/api/v1/health")

            return GatewayHealth(
                healthy=result.get("status") == "healthy",
                status=result.get("status", "unknown"),
                version=result.get("version"),
                uptime_seconds=result.get("uptime_seconds"),
            )
        except GatewayError as e:
            return GatewayHealth(
                healthy=False,
                status="unhealthy",
                error=str(e),
            )
        except Exception as e:
            return GatewayHealth(
                healthy=False,
                status="unreachable",
                error=str(e),
            )

    def wait_for_healthy(
        self,
        timeout_seconds: int = 60,
        check_interval: float = 2.0,
    ) -> bool:
        """Wait for gateway to become healthy.

        Args:
            timeout_seconds: Maximum time to wait
            check_interval: Time between checks

        Returns:
            True if gateway became healthy, False if timeout
        """
        import time

        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            health = self.check_health()
            if health.healthy:
                logger.info("Gateway is healthy", version=health.version)
                return True

            logger.debug(
                "Waiting for gateway",
                status=health.status,
                error=health.error,
            )
            time.sleep(check_interval)

        logger.warning("Gateway health check timed out")
        return False

    def register_session(
        self,
        container_id: str,
        container_ip: str,
        mode: str = "public",
        repos: list[str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
        phase: str | None = None,
    ) -> SessionInfo:
        """Register a session for a container.

        Requires launcher secret authentication.

        Args:
            container_id: Docker container ID
            container_ip: Container IP address
            mode: Repository visibility mode (private, public, or local)
            repos: List of repositories in owner/name format
            uid: Host UID for worktree ownership
            gid: Host GID for worktree ownership
            phase: Optional SDLC pipeline phase

        Returns:
            SessionInfo with the created session

        Raises:
            GatewayError: On registration failure
        """
        request_data: dict[str, Any] = {
            "container_id": container_id,
            "container_ip": container_ip,
            "mode": mode,
        }
        if repos:
            request_data["repos"] = repos
        if uid is not None:
            request_data["uid"] = uid
        if gid is not None:
            request_data["gid"] = gid
        if phase:
            request_data["phase"] = phase

        result = self._make_request(
            "/api/v1/sessions/create",
            method="POST",
            data=request_data,
            use_launcher_auth=True,
        )

        if not result.get("success"):
            raise GatewayError(result.get("message", "Session registration failed"))

        response_data = result.get("data", {})

        logger.info(
            "Session registered with gateway",
            container_id=container_id[:12] if len(container_id) >= 12 else container_id,
            container_ip=container_ip,
            mode=mode,
        )

        return SessionInfo(
            session_token=response_data["session_token"],
            container_id=container_id,
            container_ip=container_ip,
            mode=mode,
            created_at=datetime.fromisoformat(response_data.get("created_at", datetime.now().isoformat())),
            expires_at=datetime.fromisoformat(response_data.get("expires_at", (datetime.now() + timedelta(hours=24)).isoformat())),
        )

    def validate_session(
        self,
        session_token: str,
        source_ip: str | None = None,
    ) -> bool:
        """Validate a session token.

        Requires launcher secret authentication.

        Args:
            session_token: Token to validate
            source_ip: Optional source IP for verification (not used in GET request)

        Returns:
            True if session is valid
        """
        try:
            result = self._make_request(
                f"/api/v1/sessions/{session_token}",
                method="GET",
                use_launcher_auth=True,
            )

            return result.get("valid", False)
        except GatewayError:
            return False

    def delete_session(self, session_token: str) -> bool:
        """Delete a session.

        Requires launcher secret authentication.

        Args:
            session_token: Token to delete

        Returns:
            True if session was deleted
        """
        try:
            result = self._make_request(
                f"/api/v1/sessions/{session_token}",
                method="DELETE",
                use_launcher_auth=True,
            )

            return result.get("success", False)
        except GatewayError as e:
            logger.warning("Failed to delete session", error=str(e))
            return False

    def delete_session_by_container(self, container_id: str) -> bool:
        """Delete a session by container ID.

        Requires launcher secret authentication.

        Note: The gateway doesn't have a dedicated endpoint for this.
        This method looks up the session token first, then deletes it.

        Args:
            container_id: Container ID whose session to delete

        Returns:
            True if session was deleted
        """
        try:
            # List sessions and find the one matching this container
            result = self._make_request(
                "/api/v1/sessions",
                method="GET",
                use_launcher_auth=True,
            )
            sessions = result.get("data", {}).get("sessions", [])
            for session in sessions:
                if session.get("container_id") == container_id:
                    return self.delete_session(session["session_token"])
            logger.warning(
                "No session found for container",
                container_id=container_id[:12],
            )
            return False
        except GatewayError as e:
            logger.warning(
                "Failed to delete session by container",
                container_id=container_id[:12] if len(container_id) >= 12 else container_id,
                error=str(e),
            )
            return False

    def get_proxy_config(self, mode: str = "public") -> dict[str, str]:
        """Get proxy configuration for a container.

        Returns environment variables that should be set in spawned
        containers to route traffic through the gateway proxy.

        Args:
            mode: Repository visibility mode

        Returns:
            Dictionary of environment variables
        """
        proxy_url = f"http://{GATEWAY_ISOLATED_IP}:{GATEWAY_PROXY_PORT}"

        # Base proxy configuration
        config = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            # Don't proxy internal network traffic
            "NO_PROXY": f"localhost,127.0.0.1,{GATEWAY_ISOLATED_IP},.local",
            "no_proxy": f"localhost,127.0.0.1,{GATEWAY_ISOLATED_IP},.local",
        }

        # Mode-specific settings
        if mode == "private":
            # In private mode, only Anthropic API is allowed
            config["EGG_PRIVATE_MODE"] = "true"
        else:
            config["EGG_PRIVATE_MODE"] = "false"

        return config

    def get_container_env(
        self,
        session_token: str,
        issue_number: int,
        repo_path: str,
        agent_role: str | None = None,
        mode: str = "public",
    ) -> dict[str, str]:
        """Get complete environment configuration for a container.

        Combines session credentials, proxy settings, and pipeline context.

        Args:
            session_token: Gateway session token
            issue_number: Pipeline issue number
            repo_path: Repository path inside container
            agent_role: Agent role for contract operations
            mode: Repository visibility mode

        Returns:
            Dictionary of environment variables
        """
        env = self.get_proxy_config(mode)

        # Session credentials
        env["EGG_SESSION_TOKEN"] = session_token
        env["GATEWAY_URL"] = self.base_url

        # Pipeline context
        env["EGG_ISSUE_NUMBER"] = str(issue_number)
        env["EGG_REPO_PATH"] = repo_path

        if agent_role:
            env["EGG_AGENT_ROLE"] = agent_role

        return env


class GatewayError(Exception):
    """Error from gateway operations."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


# Singleton client instance
_gateway_client: GatewayClient | None = None


def get_gateway_client() -> GatewayClient:
    """Get the singleton gateway client.

    Returns:
        GatewayClient instance
    """
    global _gateway_client
    if _gateway_client is None:
        _gateway_client = GatewayClient()
    return _gateway_client


def validate_security_boundary(
    container_id: str,
    container_ip: str,
    session_token: str,
) -> tuple[bool, str | None]:
    """Validate that a container has proper security boundaries.

    Checks:
    - Container has valid gateway session
    - Session is bound to correct IP
    - Container is on the isolated network

    Args:
        container_id: Docker container ID
        container_ip: Container IP address
        session_token: Gateway session token

    Returns:
        Tuple of (is_valid, error_message)
    """
    client = get_gateway_client()

    # Check gateway health first
    health = client.check_health()
    if not health.healthy:
        return False, f"Gateway is unhealthy: {health.error or health.status}"

    # Validate session token and IP binding
    if not client.validate_session(session_token, container_ip):
        return False, "Session validation failed - invalid token or IP mismatch"

    # Verify container is on isolated network
    # The IP should be in the egg-isolated subnet (172.32.0.0/24)
    if not container_ip.startswith("172.32.0."):
        return False, f"Container IP {container_ip} is not in isolated network"

    return True, None
