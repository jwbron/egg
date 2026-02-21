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
        GATEWAY_CONTAINER_NAME,
        GATEWAY_PORT,
    )
except ImportError:
    # Fallback defaults
    GATEWAY_CONTAINER_NAME = "egg-gateway"
    GATEWAY_PORT = 9848

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
class WorktreeResult:
    """Result of a worktree create/delete operation."""

    success: bool
    worktrees: dict[str, str]  # repo_name -> host_path (create) or repo_name -> status (delete)
    errors: list[str]


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
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the gateway.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            data: Request body data
            use_launcher_auth: Use launcher secret for auth
            bearer_token: Explicit bearer token (takes precedence over launcher auth)

        Returns:
            Response JSON data

        Raises:
            GatewayError: On request failure
        """
        url = f"{self.base_url}{endpoint}"
        headers: dict[str, str] = {"Content-Type": "application/json"}

        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif use_launcher_auth and self.launcher_secret:
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
        pipeline_id: str | None = None,
        agent_role: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        claude_code_version: str | None = None,
        branch: str | None = None,
        complexity_tier: str | None = None,
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
            pipeline_id: Optional pipeline run ID for multi-agent correlation
            agent_role: Optional agent role (e.g., "coder", "tester")
            issue_number: Optional GitHub issue number for checkpoint linkage
            pr_number: Optional GitHub PR number for checkpoint linkage
            claude_code_version: Optional Claude Code version string
            branch: Optional git branch for non-pushing session metadata

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
        if pipeline_id is not None:
            request_data["pipeline_id"] = pipeline_id
        if agent_role is not None:
            request_data["agent_role"] = agent_role
        if issue_number is not None:
            request_data["issue_number"] = issue_number
        if pr_number is not None:
            request_data["pr_number"] = pr_number
        if claude_code_version is not None:
            request_data["claude_code_version"] = claude_code_version
        if branch is not None:
            request_data["branch"] = branch
        if complexity_tier is not None:
            request_data["complexity_tier"] = complexity_tier

        result = self._make_request(
            "/api/v1/sessions/create",
            method="POST",
            data=request_data,
            use_launcher_auth=True,
        )

        if not result.get("success"):
            raise GatewayError(result.get("message", "Session registration failed"))

        response_data = result.get("data", {})

        session_token = response_data.get("session_token")
        if not session_token:
            raise GatewayError("Gateway response missing session_token")

        logger.info(
            "Session registered with gateway",
            container_id=container_id[:12] if len(container_id) >= 12 else container_id,
            container_ip=container_ip,
            mode=mode,
        )

        return SessionInfo(
            session_token=session_token,
            container_id=container_id,
            container_ip=container_ip,
            mode=mode,
            created_at=datetime.fromisoformat(
                response_data.get("created_at", datetime.now().isoformat())
            ),
            expires_at=datetime.fromisoformat(
                response_data.get("expires_at", (datetime.now() + timedelta(hours=24)).isoformat())
            ),
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

    def update_session(
        self,
        session_token: str,
        container_id: str | None = None,
        container_ip: str | None = None,
    ) -> bool:
        """Update a session.

        Requires launcher secret authentication.

        Args:
            session_token: Token to update
            container_id: New container ID (optional)
            container_ip: New container IP (optional)

        Returns:
            True if session was updated
        """
        try:
            data: dict[str, str] = {}
            if container_id is not None:
                data["container_id"] = container_id
            if container_ip is not None:
                data["container_ip"] = container_ip

            result = self._make_request(
                f"/api/v1/sessions/{session_token}",
                method="PATCH",
                data=data,
                use_launcher_auth=True,
            )

            return result.get("success", False)
        except GatewayError as e:
            logger.warning("Failed to update session", error=str(e))
            return False

    def delete_session_by_container(self, container_id: str) -> bool:
        """Delete a session by container ID.

        Requires launcher secret authentication.

        Uses the dedicated gateway endpoint for deletion by container ID.

        Args:
            container_id: Container ID whose session to delete

        Returns:
            True if session was deleted
        """
        try:
            result = self._make_request(
                f"/api/v1/sessions/by-container/{container_id}",
                method="DELETE",
                use_launcher_auth=True,
            )
            return result.get("success", False)
        except GatewayError as e:
            logger.warning(
                "Failed to delete session by container",
                container_id=container_id[:12] if len(container_id) >= 12 else container_id,
                error=str(e),
            )
            return False

    def create_worktrees(
        self,
        container_id: str,
        repos: list[str],
        uid: int | None = None,
        gid: int | None = None,
        base_branch: str | None = None,
    ) -> WorktreeResult:
        """Create isolated worktrees for a container.

        Calls the gateway's /api/v1/worktree/create endpoint to create
        per-container worktrees. Returns host paths suitable for Docker
        volume mount sources.

        Args:
            container_id: Container identifier (e.g., 'egg-local-abc123-coder')
            repos: List of repository names (or owner/repo format)
            uid: User ID for worktree ownership
            gid: Group ID for worktree ownership
            base_branch: Branch to base worktrees on. When None, the gateway
                resolves the remote default branch per-repo (e.g., origin/main).

        Returns:
            WorktreeResult with host paths for each repo

        Raises:
            GatewayError: On request failure
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

        try:
            result = self._make_request(
                "/api/v1/worktree/create",
                method="POST",
                data=request_data,
                use_launcher_auth=True,
            )

            data = result.get("data", {})
            return WorktreeResult(
                success=result.get("success", False),
                # Handle both missing key and explicit null from API
                worktrees=data.get("worktrees") or {},
                errors=data.get("errors") or [],
            )
        except GatewayError:
            raise
        except Exception as e:
            raise GatewayError(f"Failed to create worktrees: {e}") from e

    def delete_worktrees(
        self,
        container_id: str,
        force: bool = True,
    ) -> WorktreeResult:
        """Delete worktrees for a container.

        Calls the gateway's /api/v1/worktree/delete endpoint to clean up
        worktrees when a container exits.

        Args:
            container_id: Container identifier
            force: Force removal even with uncommitted changes

        Returns:
            WorktreeResult with deletion status

        Raises:
            GatewayError: On request failure
        """
        try:
            result = self._make_request(
                "/api/v1/worktree/delete",
                method="POST",
                data={
                    "container_id": container_id,
                    "force": force,
                },
                use_launcher_auth=True,
            )

            data = result.get("data", {})
            return WorktreeResult(
                success=result.get("success", False),
                # Handle both missing key and explicit null from API
                worktrees=dict.fromkeys(data.get("deleted") or [], "deleted"),
                errors=data.get("errors") or [],
            )
        except GatewayError:
            raise
        except Exception as e:
            raise GatewayError(f"Failed to delete worktrees: {e}") from e

    def push_worktree_branch(
        self,
        pipeline_id: str,
        repo_path: str,
        branch: str,
    ) -> bool:
        """Push a worktree branch to remote using a temporary session.

        Best-effort operation used to push worktree contents to remote —
        called after contract initialization, phase completion, or pipeline
        failure. Registers a temp session, pushes, then cleans up the session.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to the worktree repo directory
            branch: Branch name to push

        Returns:
            True if push succeeded, False otherwise
        """
        temp_container_id = f"{pipeline_id}-failsafe-push"
        session_token: str | None = None
        try:
            # Register a temporary session for the push
            session = self.register_session(
                container_id=temp_container_id,
                container_ip="127.0.0.1",
                mode="local",
                pipeline_id=pipeline_id,
            )
            session_token = session.session_token

            # Push the branch
            self._make_request(
                "/api/v1/git/push",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "refspec": branch,
                    "container_id": temp_container_id,
                },
                bearer_token=session_token,
            )

            logger.info(
                "Pushed worktree branch to remote",
                pipeline_id=pipeline_id,
                branch=branch,
            )
            return True
        except Exception as e:
            logger.warning(
                "Best-effort push failed (work preserved locally in worktree)",
                pipeline_id=pipeline_id,
                branch=branch,
                error=str(e),
            )
            return False
        finally:
            # Clean up temp session
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass

    def fetch_worktree_branch(
        self,
        pipeline_id: str,
        repo_path: str,
    ) -> bool:
        """Fetch latest remote state into a worktree using a temporary session.

        Best-effort operation to sync remote changes into a worktree —
        called before phase execution to ensure the worktree has all state
        from previous phases (e.g., after orchestrator restart where the
        local branch diverged from remote).

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to the worktree repo directory

        Returns:
            True if fetch succeeded, False otherwise
        """
        temp_container_id = f"{pipeline_id}-failsafe-fetch"
        session_token: str | None = None
        try:
            session = self.register_session(
                container_id=temp_container_id,
                container_ip="127.0.0.1",
                mode="local",
                pipeline_id=pipeline_id,
            )
            session_token = session.session_token

            self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "container_id": temp_container_id,
                },
                bearer_token=session_token,
            )

            logger.info(
                "Fetched remote state into worktree",
                pipeline_id=pipeline_id,
            )
            return True
        except Exception as e:
            logger.warning(
                "Best-effort fetch failed (continuing with local state)",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            return False
        finally:
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass


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
