"""
Gateway client for orchestrator integration.

Provides coordination between the orchestrator and gateway sidecar for:
- Session token management for spawned containers
- Gateway health monitoring
- Proxy configuration injection
- Security boundary validation
"""

import os
import socket
import subprocess  # noqa: F401  -- live patch seam: ``gateway_client.subprocess.run``
import sys
import time  # noqa: F401  -- live patch seam: ``patch.object(gateway_client.time, "sleep")`` (_request's backoff)
from pathlib import Path
from typing import Any
from urllib.request import urlopen  # noqa: F401  -- live patch seam: ``gateway_client.urlopen``

# Add shared directory to path for egg_logging and config
_shared_path = Path(__file__).parent.parent.parent / "shared"
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
    GATEWAY_PORT = 9848  # noqa: EGG002


logger = get_logger("orchestrator.gateway_client")


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


class GatewayConnectionError(GatewayError):
    """Transient connection-level failure talking to the gateway.

    Raised by :meth:`GatewayClient._make_request` for ``URLError`` —
    connection refused, DNS resolution failure ([Errno -3]), host
    unreachable — i.e. cases where the request was not delivered/processed
    and is therefore safe to retry regardless of HTTP-method idempotency.
    A response-phase failure deliberately does *not* map to this subclass:
    a disconnect (http.client.RemoteDisconnected, a ``ConnectionResetError``
    — NOT a ``URLError``) falls through to ``_make_request``'s ``except
    OSError`` branch, and a partial read (http.client.IncompleteRead, an
    ``HTTPException``) falls through to the trailing ``except HTTPException``
    branch — both as a plain :class:`GatewayError`, because such a failure
    may mean the gateway already processed the request and so must not be
    blindly retried.

    Subclasses :class:`GatewayError` so callers that broadly catch
    ``GatewayError`` (e.g. the spawner's session-registration handler)
    keep working unchanged; callers that want bounded retry-with-backoff
    on a brief networking blip before hard-failing (#2869) route the call
    through :meth:`GatewayClient._retry_transient`, which retries only on
    this subclass.
    """


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
            gateway_port: Gateway port (default: GATEWAY_PORT or env)
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

    @property
    def self_ip(self) -> str:
        """Get the local IP address used to reach the gateway.

        The gateway validates that request source IP matches the session's
        registered container_ip. For temporary sessions created by the
        orchestrator itself, we must register with the IP that the gateway
        will see as request.remote_addr.

        Uses a UDP socket probe (no data sent) to determine which local
        interface routes to the gateway host. Result is cached for the
        lifetime of this client instance.
        """
        if not hasattr(self, "_self_ip_cache"):
            self._self_ip_cache = self._resolve_self_ip()
        return self._self_ip_cache

    def _resolve_self_ip(self) -> str:
        """Resolve the local IP that routes to the gateway host."""
        try:
            # Resolve gateway hostname to an IP for the UDP probe
            gateway_addr = socket.getaddrinfo(self.gateway_host, self.gateway_port, socket.AF_INET)[
                0
            ][4][0]
            # UDP connect (no data sent) reveals which local IP routes there
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((gateway_addr, self.gateway_port))
                local_ip: str = s.getsockname()[0]
            logger.debug(
                "Resolved self IP for gateway sessions",
                self_ip=local_ip,
                gateway_host=self.gateway_host,
            )
            return local_ip
        except Exception as e:
            logger.warning(
                "Failed to resolve self IP, falling back to 127.0.0.1",
                error=str(e),
            )
            return "127.0.0.1"


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


# -------------------------------------------------------------------------
# Sub-package wiring (#3312): method-modules-on-class decomposition.
# The barrel keeps the GatewayClient class identity + __init__ + the
# base_url/self_ip/_resolve_self_ip helpers + the GatewayError /
# GatewayConnectionError exceptions + the singleton factory; submodules
# hold the extracted method bodies (each taking ``self`` explicitly) and
# the relocated module-level helpers. Imports sit at the bottom so the
# submodules can value-import the barrel exceptions defined above and reach
# the patched module globals (``logger``, ``urlopen``) via
# ``import gateway_client as _pkg``.
# -------------------------------------------------------------------------
from . import (  # noqa: E402
    _branches,
    _integration,
    _merge,
    _pr,
    _push,
    _rebase,
    _request,
    _session,
    _worktree,
)
from ._models import (  # noqa: E402
    GatewayHealth,
    PushResult,
    SessionInfo,
    WorktreeResult,
)
from ._pr_format import _truncate_title  # noqa: E402  -- re-exported (tests import it)
from ._push import (  # noqa: E402  -- re-exported (tests import these helpers directly)
    _classify_push_stderr,
    _rebase_with_agent_output_autoresolve,
)
from ._request import (  # noqa: E402  -- transient-retry tuning constants; tests read _TRANSIENT_MAX_ATTEMPTS via the barrel
    _TRANSIENT_BASE_DELAY,
    _TRANSIENT_JITTER,
    _TRANSIENT_MAX_ATTEMPTS,
    _TRANSIENT_MAX_DELAY,
)

# Bind extracted method bodies back onto the class.
GatewayClient._make_request = _request._make_request
GatewayClient._retry_transient = _request._retry_transient
GatewayClient.check_health = _request.check_health
GatewayClient.wait_for_healthy = _request.wait_for_healthy
GatewayClient.register_session = _session.register_session
GatewayClient.validate_session = _session.validate_session
GatewayClient.delete_session = _session.delete_session
GatewayClient.update_session = _session.update_session
GatewayClient.delete_session_by_container = _session.delete_session_by_container
GatewayClient.heartbeat_session_by_container = _session.heartbeat_session_by_container
GatewayClient.create_worktrees = _worktree.create_worktrees
GatewayClient.delete_worktrees = _worktree.delete_worktrees
GatewayClient.push_worktree_branch = _push.push_worktree_branch
GatewayClient._do_push = _push._do_push
GatewayClient._reconcile_and_retry_push = _push._reconcile_and_retry_push
GatewayClient.delete_remote_branch = _push.delete_remote_branch
GatewayClient.create_pr = _pr.create_pr
GatewayClient.create_slice_pr = _pr.create_slice_pr
GatewayClient.update_pr_body = _pr.update_pr_body
GatewayClient.list_open_prs = _pr.list_open_prs
GatewayClient.lookup_open_pr = _pr.lookup_open_pr
GatewayClient.get_pr_merge_state = _pr.get_pr_merge_state
GatewayClient.mark_pr_ready = _pr.mark_pr_ready
GatewayClient.get_repo_visibility = _pr.get_repo_visibility
GatewayClient.rebase_onto = _rebase.rebase_onto
GatewayClient.merge_base = _merge.merge_base
GatewayClient._sha_is_ancestor = _merge._sha_is_ancestor
GatewayClient.is_slice_branch_merged_into_parent = _merge.is_slice_branch_merged_into_parent
GatewayClient.find_unreachable_evidence_commits = _merge.find_unreachable_evidence_commits
GatewayClient.create_slice_integration_branch = _integration.create_slice_integration_branch
GatewayClient.list_remote_branches = _branches.list_remote_branches
GatewayClient.list_remote_branches_with_shas = _branches.list_remote_branches_with_shas
GatewayClient.fetch_worktree_branch = _branches.fetch_worktree_branch
GatewayClient.fetch_branch = _branches.fetch_branch
GatewayClient._ls_remote_branch_impl = _branches._ls_remote_branch_impl
GatewayClient.ls_remote_branch = _branches.ls_remote_branch
GatewayClient.ls_remote_branch_strict = _branches.ls_remote_branch_strict
GatewayClient.get_remote_branch_sha = _branches.get_remote_branch_sha

__all__ = [
    "GatewayClient",
    "GatewayError",
    "GatewayConnectionError",
    "GatewayHealth",
    "PushResult",
    "SessionInfo",
    "WorktreeResult",
    "get_gateway_client",
    "validate_security_boundary",
    "_truncate_title",
    "_classify_push_stderr",
    "_rebase_with_agent_output_autoresolve",
    "_TRANSIENT_MAX_ATTEMPTS",
    "_TRANSIENT_BASE_DELAY",
    "_TRANSIENT_MAX_DELAY",
    "_TRANSIENT_JITTER",
]
