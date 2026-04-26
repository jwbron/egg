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
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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
    GATEWAY_PORT = 9848  # noqa: EGG002

logger = get_logger("orchestrator.gateway_client")


@dataclass
class SessionInfo:
    """Information about a gateway session."""

    session_token: str
    container_id: str
    container_ip: str | None  # Optional; k8s pod IPs are ephemeral
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


@dataclass(frozen=True)
class PushResult:
    """Outcome of a ``push_worktree_branch`` call.

    On failure (``ok`` is ``False``), ``category`` names a coarse failure
    class so callers can build actionable operator messages; ``detail``
    carries the raw git stderr or inner error text.

    Supports ``bool()`` so ``if push_result:`` callers that only care
    about success keep working — only callers that need to surface the
    reason need to inspect ``category`` / ``detail``.
    """

    ok: bool
    category: str | None = None
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    def describe(self) -> str:
        """Return a human-readable ``category: detail`` string for logs/errors."""
        if self.ok:
            return "ok"
        cat = self.category or "unknown"
        if self.detail:
            return f"{cat}: {self.detail}"
        return cat


def _classify_push_stderr(stderr: str) -> str:
    """Classify a git push stderr into a coarse failure category.

    Matches are substring-based on the lowercased stderr so the same
    classifier handles pack-protocol errors, HTTP transport errors, and
    plain push-rejected output. Unknown shapes fall back to
    ``"push_rejected"``.
    """
    s = stderr.lower()
    if "non-fast-forward" in s or "(fetch first)" in s:
        return "non_fast_forward"
    if "authentication failed" in s or "invalid credentials" in s or " 403" in s:
        return "auth_failed"
    if "permission denied" in s or ("permission to" in s and "denied" in s):
        return "permission_denied"
    if "does not exist" in s and "repository" in s:
        return "repo_missing"
    if (
        "could not resolve host" in s
        or "could not read from remote" in s
        or "connection timed out" in s
        or "connection refused" in s
        or "network is unreachable" in s
    ):
        return "network"
    if "shallow" in s:
        return "shallow_clone"
    if "already exists" in s:
        return "branch_exists"
    return "push_rejected"


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

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        use_launcher_auth: bool = False,
        bearer_token: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the gateway.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            data: Request body data
            use_launcher_auth: Use launcher secret for auth
            bearer_token: Explicit bearer token (takes precedence over launcher auth)
            timeout: Per-request timeout in seconds (overrides client default)

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
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            request = Request(url, data=body, headers=headers, method=method)
            with urlopen(request, timeout=effective_timeout) as response:
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
        container_ip: str | None = None,
        mode: str = "public",
        repos: list[str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
        phase: str | None = None,
        pipeline_id: str | None = None,
        agent_role: str | None = None,
        agent_anchor_id: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        claude_code_version: str | None = None,
        branch: str | None = None,
        worktree_container_id: str | None = None,
        jira_ticket: str | None = None,
    ) -> SessionInfo:
        """Register a session for a container.

        Requires launcher secret authentication.

        Args:
            container_id: Docker container ID or k8s Job name
            container_ip: Container IP address (optional; for audit logging only)
            mode: Repository visibility mode (private, public, or local)
            repos: List of repositories in owner/name format
            uid: Host UID for worktree ownership
            gid: Host GID for worktree ownership
            phase: Optional SDLC pipeline phase
            pipeline_id: Optional pipeline run ID for multi-agent correlation
            agent_role: Optional agent role (e.g., "coder", "tester")
            agent_anchor_id: Optional agent anchor ID for scoped anchor file writes
            issue_number: Optional GitHub issue number for checkpoint linkage
            pr_number: Optional GitHub PR number for checkpoint linkage
            claude_code_version: Optional Claude Code version string
            branch: Optional git branch for non-pushing session metadata
            worktree_container_id: Optional container_id under which per-agent
                worktrees were already created by a prior create_worktrees
                call.  When provided, the gateway reuses those worktrees
                instead of re-creating them — avoids a second
                ``git worktree add`` racing on ``.git/config.lock`` (#1857).

        Returns:
            SessionInfo with the created session

        Raises:
            GatewayError: On registration failure
        """
        request_data: dict[str, Any] = {
            "container_id": container_id,
            "mode": mode,
        }
        if container_ip is not None:
            request_data["container_ip"] = container_ip
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
        if agent_anchor_id is not None:
            request_data["agent_anchor_id"] = agent_anchor_id
        if issue_number is not None:
            request_data["issue_number"] = issue_number
        if pr_number is not None:
            request_data["pr_number"] = pr_number
        if claude_code_version is not None:
            request_data["claude_code_version"] = claude_code_version
        if branch is not None:
            request_data["branch"] = branch
        if worktree_container_id is not None:
            request_data["worktree_container_id"] = worktree_container_id
        if jira_ticket:
            # Advisory: gateway records it in the Session and echoes it in
            # every /api/v1/jira/* audit line (issue #1556).  It does NOT gate
            # any Jira call on its value — the project allowlist is the only
            # hard boundary.
            request_data["jira_ticket"] = jira_ticket
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
                f"/api/v1/sessions/{quote(session_token, safe='')}",
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
                f"/api/v1/sessions/{quote(session_token, safe='')}",
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
                f"/api/v1/sessions/{quote(session_token, safe='')}",
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
                f"/api/v1/sessions/by-container/{quote(container_id, safe='')}",
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

    def heartbeat_session_by_container(self, container_id: str) -> bool:
        """Refresh a session's idle timer by container ID.

        Requires launcher secret authentication.  Used to keep gateway
        sessions alive while an agent is heartbeating on the BRC bus but
        not making gateway requests — see #2068.

        Args:
            container_id: Container ID whose session to refresh.

        Returns:
            True if the session was refreshed; False if there is no
            matching session or the gateway request failed.  Best-effort
            — callers should not fail on a False return.
        """
        try:
            result = self._make_request(
                f"/api/v1/sessions/by-container/{quote(container_id, safe='')}/heartbeat",
                method="POST",
                use_launcher_auth=True,
            )
            return result.get("success", False)
        except GatewayError as e:
            # Log the full container_id (not a secret — already shows up
            # in k8s `get pods` output) so the failing pipeline+role is
            # identifiable from #2068's exact failure mode.  The sibling
            # ``delete_session_by_container`` truncates to 12 chars
            # (``egg-agent-is`` for realistic ids), which loses both
            # pipeline and role; reviewer NB4 on #2076 flagged that as
            # un-debuggable here even if it's pre-existing there.
            logger.warning(
                "Failed to heartbeat session by container",
                container_id=container_id,
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
        assigned_branch: str | None = None,
        timeout: int = 120,
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
            assigned_branch: Remote branch that pushes from the worktree
                should target.  When set, the gateway configures
                ``branch.<local>.merge`` so a naive ``git push`` from the
                worktree resolves to a refspec targeting this branch
                instead of the per-worktree local branch name.  See #1809.
            timeout: Request timeout in seconds. Defaults to 120s because
                concurrent pipeline starts may queue behind per-repo locks
                in the gateway.

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
        if assigned_branch is not None:
            request_data["assigned_branch"] = assigned_branch
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
                timeout=timeout,
            )

            data = result.get("data", {})
            return WorktreeResult(
                success=result.get("success", False),
                # Handle both missing key and explicit null from API
                worktrees=data.get("worktrees") or {},
                errors=data.get("errors") or [],
            )
        except GatewayError as e:
            # The gateway returns per-repo failure reasons in details["errors"]
            # when every worktree fails. Inline them into the message so
            # downstream callers that only see str(e) (e.g. kubernetes_spawner
            # wrapping in KubernetesSpawnError) still surface the specific
            # cause instead of the generic "Failed to create any worktrees".
            # See #1838.
            specific = e.details.get("errors") if e.details else None
            if specific:
                raise GatewayError(
                    f"{e.message}: {'; '.join(str(x) for x in specific)}",
                    status_code=e.status_code,
                    details=e.details,
                ) from e
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
        mode: Literal["public", "private"] = "public",
        ref: str | None = None,
        base_branch: str | None = None,
        force: bool = False,
    ) -> PushResult:
        """Push a branch to remote with launcher-auth (orchestrator-trusted).

        Called after contract initialization, phase completion, or pipeline
        failure.  Authenticates with the launcher secret rather than a
        sandbox session token: the orchestrator is on the privileged side
        of the trust boundary and its programmatic pushes bypass the
        agent-targeted pipeline-push enforcement (#2028, #2051).

        When ``ref`` is ``None`` (default), pushes the worktree's current
        ``HEAD`` — used when ``repo_path`` is a worktree checked out to
        ``branch``. On non-fast-forward rejection, performs
        ``git fetch origin`` + ``git rebase origin/{branch}`` in the
        worktree (with ``.egg-state/agent-outputs/`` auto-resolve) and
        retries the push once.

        When ``ref`` is set, pushes ``refs/heads/{ref}:refs/heads/{branch}``
        — used when ``repo_path`` is a plain repository (not a worktree
        checked out to ``branch``) whose ``.git/`` holds the commits to
        push. Reconcile is skipped: there is no working tree at
        ``repo_path`` that can be rebased onto the remote tip without
        disturbing its checkout.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to the repo directory the gateway will ``cd`` into
            branch: Remote branch name to push to
            mode: Gateway session mode (public/private)
            ref: Local ref to push (omit to push worktree HEAD)
            base_branch: Pipeline's base branch (e.g. ``"main"``).  When
                set, the reconcile rebase uses ``--onto origin/{branch}
                origin/{base_branch}`` so commits already on the base
                branch are not replayed onto the pipeline branch (#1976).
                Ignored when ``ref`` is set (reconcile is skipped there).
            force: When ``True``, send ``--force`` so the push overwrites
                a non-ancestor remote tip.  Used by the rebase-on-resume
                helper to replace a stale ``origin/<branch>`` with a
                rebased-onto-base version (#2098).  Skips reconcile on
                failure since force-push has nothing to reconcile against.

        Returns:
            ``PushResult`` whose ``ok`` flag is ``True`` on success and
            ``False`` otherwise. On failure, ``category`` and ``detail``
            describe why so callers can surface an operator-actionable
            error (e.g. ``"non_fast_forward"``, ``"auth_failed"``,
            ``"reconcile_fetch_failed"``). ``PushResult`` is truthy on
            success so existing ``if push_ok:`` callers work unchanged.
        """
        refspec = f"refs/heads/{ref}:refs/heads/{branch}" if ref else f"HEAD:refs/heads/{branch}"

        first = self._do_push(
            pipeline_id=pipeline_id,
            repo_path=repo_path,
            branch=branch,
            mode=mode,
            refspec=refspec,
            force=force,
        )
        if first.ok:
            return first

        # Reconcile is only meaningful for worktree-HEAD pushes: the rebase
        # mutates the checkout at repo_path, which we only want to do when
        # that checkout is a dedicated pipeline worktree.  Force pushes
        # also skip reconcile — the caller has already decided to overwrite.
        if ref is not None or force:
            logger.warning(
                "Push failed (no reconcile available)",
                pipeline_id=pipeline_id,
                branch=branch,
                ref=ref,
                force=force,
                category=first.category,
                detail=first.detail,
            )
            return first

        return self._reconcile_and_retry_push(
            pipeline_id=pipeline_id,
            worktree_path=repo_path,
            branch=branch,
            mode=mode,
            refspec=refspec,
            initial_failure=first,
            base_branch=base_branch,
        )

    def _do_push(
        self,
        pipeline_id: str,
        repo_path: str,
        branch: str,
        mode: Literal["public", "private"],
        refspec: str,
        force: bool = False,
    ) -> PushResult:
        """Send a single push request to the gateway with launcher auth.

        The orchestrator authenticates directly with the launcher secret —
        no register-session/push/delete ceremony.  The push endpoint
        recognises launcher auth as orchestrator-trusted and skips the
        agent-targeted enforcement (pipeline-push block, push-target,
        role/phase file restrictions).  ``mode`` is forwarded in the
        request body so the private-repo policy still applies.

        Returns ``PushResult(ok=True)`` on success.  On failure the gateway
        HTTP 500 body carries git stderr in ``details["stderr"]``; we
        classify it into a category and propagate both category and raw
        stderr so callers can build an operator-actionable error.
        """
        try:
            # Do NOT include container_id — the repo_path is already resolved
            # (orchestrator-side worktree on the shared hostPath).  Including
            # one would route through map_container_path_to_worktree() and
            # fail "worktree not found" (#1500).
            self._make_request(
                "/api/v1/git/push",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "refspec": refspec,
                    "mode": mode,
                    "force": force,
                },
                use_launcher_auth=True,
            )

            logger.info(
                "Pushed branch to remote",
                pipeline_id=pipeline_id,
                branch=branch,
                refspec=refspec,
            )
            return PushResult(ok=True)
        except GatewayError as e:
            # Gateway returns 500 + details={"stderr": ...} on push failure
            # (see gateway/gateway.py push handler). Connection/transport
            # errors surface as GatewayError without details.
            stderr = ""
            if isinstance(e.details, dict):
                stderr = (e.details.get("stderr") or "").strip()
            if stderr:
                category = _classify_push_stderr(stderr)
                detail = stderr
            else:
                category = "gateway_unreachable" if e.status_code is None else "gateway_error"
                detail = e.message or str(e)
            logger.info(
                "Push attempt failed — caller may retry via reconcile",
                pipeline_id=pipeline_id,
                branch=branch,
                refspec=refspec,
                category=category,
                error=detail,
            )
            return PushResult(ok=False, category=category, detail=detail)
        except Exception as e:
            logger.info(
                "Push attempt failed — caller may retry via reconcile",
                pipeline_id=pipeline_id,
                branch=branch,
                refspec=refspec,
                error=str(e),
            )
            return PushResult(ok=False, category="unknown", detail=str(e))

    def _reconcile_and_retry_push(
        self,
        pipeline_id: str,
        worktree_path: str,
        branch: str,
        mode: Literal["public", "private"],
        refspec: str,
        initial_failure: PushResult,
        base_branch: str | None = None,
    ) -> PushResult:
        """Fetch, rebase the worktree onto ``origin/{branch}``, and retry push.

        Runs directly against the worktree filesystem (shared hostPath) so
        the orchestrator can mutate the checkout without round-tripping
        through the gateway. Conflicts confined to
        ``.egg-state/agent-outputs/`` are resolved in favour of the remote;
        conflicts elsewhere abort the rebase and return a failure result.

        When ``base_branch`` is provided, ``origin/{base_branch}`` is also
        fetched before the rebase and used as the ``--onto`` upstream so
        commits already on main are not replayed onto the pipeline branch
        (#1976).

        Returns ``PushResult(ok=True)`` when the retry push succeeds. On
        failure, the returned ``PushResult`` carries a category that
        identifies which stage of reconcile failed
        (``reconcile_fetch_failed``, ``reconcile_rebase_failed``,
        ``reconcile_retry_failed/<inner>``) so callers can distinguish
        "original push was rejected and reconcile never ran" from
        "reconcile ran but retry push still failed" without reading the
        gateway source.
        """
        logger.warning(
            "Push rejected — attempting fetch+rebase+retry to reconcile divergence",
            pipeline_id=pipeline_id,
            branch=branch,
            initial_category=initial_failure.category,
            initial_detail=initial_failure.detail,
        )

        git_base = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            f"safe.directory={worktree_path}",
            "-C",
            str(worktree_path),
        ]

        try:
            subprocess.run(
                [*git_base, "fetch", "origin", branch],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as fetch_err:
            stderr = (fetch_err.stderr or "").strip()
            logger.error(
                "Push reconcile: fetch failed — work remains on local worktree only",
                pipeline_id=pipeline_id,
                branch=branch,
                stderr=stderr,
            )
            return PushResult(
                ok=False,
                category="reconcile_fetch_failed",
                detail=stderr or f"git fetch exited {fetch_err.returncode}",
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "Push reconcile: fetch timed out — work remains on local worktree only",
                pipeline_id=pipeline_id,
                branch=branch,
            )
            return PushResult(
                ok=False,
                category="reconcile_fetch_timeout",
                detail=f"git fetch origin {branch} timed out after 60s",
            )

        # Refresh origin/{base_branch} so the --onto upstream in the rebase
        # reflects the current main tip (#1976).  A stale origin/{base_branch}
        # would cause commits that landed on main since the worktree was
        # created to be replayed as duplicate-by-content commits.  Best-effort:
        # if fetching the base fails (network, permissions, branch absent),
        # fall through to the plain ``git rebase origin/{branch}`` form.
        if base_branch:
            try:
                base_fetch = subprocess.run(
                    [*git_base, "fetch", "origin", base_branch],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Push reconcile: base-branch fetch timed out — proceeding with stale origin/{base}",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    base_branch=base_branch,
                )
            else:
                if base_fetch.returncode != 0:
                    logger.warning(
                        "Push reconcile: base-branch fetch failed — proceeding with stale origin/{base}",
                        pipeline_id=pipeline_id,
                        branch=branch,
                        base_branch=base_branch,
                        returncode=base_fetch.returncode,
                        stderr=base_fetch.stderr.strip(),
                    )

        rebase_result = _rebase_with_agent_output_autoresolve(
            git_base=git_base,
            pipeline_id=pipeline_id,
            branch=branch,
            base_branch=base_branch,
        )
        if not rebase_result.ok:
            return rebase_result

        logger.info(
            "Push reconcile: rebase succeeded — retrying push",
            pipeline_id=pipeline_id,
            branch=branch,
        )
        retry = self._do_push(
            pipeline_id=pipeline_id,
            repo_path=worktree_path,
            branch=branch,
            mode=mode,
            refspec=refspec,
        )
        if retry.ok:
            return retry

        logger.error(
            "Push reconcile: retry push still failed — work remains on local worktree only",
            pipeline_id=pipeline_id,
            branch=branch,
            retry_category=retry.category,
            retry_detail=retry.detail,
        )
        inner = retry.category or "unknown"
        return PushResult(
            ok=False,
            category=f"reconcile_retry_failed/{inner}",
            detail=retry.detail,
        )

    def delete_remote_branch(
        self,
        pipeline_id: str,
        repo_path: str,
        branch: str,
        mode: Literal["public", "private"] = "public",
    ) -> bool:
        """Delete a remote branch using a temporary session.

        Best-effort operation used to clean up worktree branches from the
        remote when a pipeline is deleted. Registers a temp session, pushes
        a deletion refspec (`:branch`), then cleans up the session.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to a repo directory (for the push endpoint)
            branch: Remote branch name to delete

        Returns:
            True if deletion succeeded, False otherwise
        """
        temp_container_id = f"{pipeline_id}-branch-cleanup-{branch.replace('/', '-')}"
        session_token: str | None = None
        try:
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
                branch=branch,
            )
            session_token = session.session_token

            # Push with empty source = delete remote branch.
            # Do NOT include container_id — repo_path is already the
            # resolved path; the synthetic container_id has no real
            # worktree and would trigger a "worktree not found" error.
            self._make_request(
                "/api/v1/git/push",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "refspec": f":{branch}",
                },
                bearer_token=session_token,
            )

            logger.info(
                "Deleted remote branch",
                pipeline_id=pipeline_id,
                branch=branch,
            )
            return True
        except Exception as e:
            logger.warning(
                "Best-effort remote branch deletion failed",
                pipeline_id=pipeline_id,
                branch=branch,
                error=str(e),
            )
            return False
        finally:
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass

    def create_pr(
        self,
        pipeline_id: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        issue_number: int | None = None,
        agent_role: str | None = None,
        mode: Literal["public", "private"] = "public",
        draft: bool = False,
    ) -> str | None:
        """Create a pull request via the gateway using a temporary session.

        Registers a temp session with phase="pr" (so the gateway allows the
        operation), creates the PR, then cleans up the session.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo: Repository in owner/name format
            title: PR title
            body: PR body/description
            head: Head branch name
            base: Base branch name (default: None, gateway auto-detects)
            issue_number: Optional issue number for pipeline metadata
            agent_role: Optional agent role for pipeline metadata

        Returns:
            PR URL if creation succeeded, None otherwise

        Raises:
            GatewayError: On request failure. Unlike push_worktree_branch/
                delete_remote_branch/fetch_worktree_branch (which catch errors
                internally and return bool), this method lets errors propagate
                so the caller can decide whether a failed PR creation should
                abort the phase.
        """
        temp_container_id = f"{pipeline_id}-auto-pr"
        session_token: str | None = None
        try:
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
                phase="pr",
                repos=[repo],
                issue_number=issue_number,
                agent_role=agent_role,
            )
            session_token = session.session_token

            pr_data: dict[str, Any] = {
                "repo": repo,
                "title": title,
                "body": body,
                "head": head,
                "draft": draft,
            }
            if base:
                pr_data["base"] = base

            result = self._make_request(
                "/api/v1/gh/pr/create",
                method="POST",
                data=pr_data,
                bearer_token=session_token,
            )

            pr_url: str | None = None
            stdout = result.get("data", {}).get("stdout", "")
            if stdout:
                # gh pr create outputs the PR URL on stdout
                pr_url = stdout.strip()

            logger.info(
                "Auto-created PR via gateway",
                pipeline_id=pipeline_id,
                repo=repo,
                head=head,
                pr_url=pr_url,
            )
            return pr_url
        finally:
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass

    def fetch_worktree_branch(
        self,
        pipeline_id: str,
        repo_path: str,
        mode: Literal["public", "private"] = "public",
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
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
            )
            session_token = session.session_token

            # Do NOT include container_id — repo_path is already the
            # resolved worktree path; the synthetic container_id has no
            # real worktree and would trigger a "worktree not found" error.
            self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
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

    def fetch_branch(
        self,
        pipeline_id: str,
        repo_path: str,
        args: list[str] | None = None,
        mode: Literal["public", "private"] = "public",
    ) -> bool:
        """Fetch with custom args using a temporary session.

        Best-effort operation used to fetch specific refs from remote.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to the repo directory
            args: Additional args for git fetch (e.g., ["+remote:local"])

        Returns:
            True if fetch succeeded, False otherwise
        """
        temp_container_id = f"{pipeline_id}-state-fetch"
        session_token: str | None = None
        try:
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
            )
            session_token = session.session_token

            # Do NOT include container_id — repo_path is already the
            # resolved path; the synthetic container_id has no real
            # worktree and would trigger a "worktree not found" error.
            self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "args": args or [],
                },
                bearer_token=session_token,
            )

            logger.info(
                "Fetched branch from remote",
                pipeline_id=pipeline_id,
                fetch_args=args,
            )
            return True
        except Exception as e:
            logger.warning(
                "Best-effort fetch failed",
                pipeline_id=pipeline_id,
                fetch_args=args,
                error=str(e),
            )
            return False
        finally:
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass

    def ls_remote_branch(
        self,
        pipeline_id: str,
        repo_path: str,
        ref: str,
        mode: Literal["public", "private"] = "public",
    ) -> bool:
        """Check if a remote branch exists using ls-remote.

        Args:
            pipeline_id: Pipeline ID (used as container_id for the temp session)
            repo_path: Path to the repo directory
            ref: Branch ref to check (e.g., "refs/heads/egg/pipeline-state")

        Returns:
            True if the remote branch exists, False otherwise
        """
        temp_container_id = f"{pipeline_id}-state-ls-remote"
        session_token: str | None = None
        try:
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
            )
            session_token = session.session_token

            # Do NOT include container_id — repo_path is already the
            # resolved path; the synthetic container_id has no real
            # worktree and would trigger a "worktree not found" error.
            result = self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "operation": "ls-remote",
                    "args": ["--heads", ref],
                },
                bearer_token=session_token,
            )

            # ls-remote returns output in data.stdout; non-empty means branch exists
            stdout = result.get("data", {}).get("stdout", "")
            return bool(stdout.strip())
        except Exception as e:
            logger.warning(
                "ls-remote check failed",
                pipeline_id=pipeline_id,
                ref=ref,
                error=str(e),
            )
            return False
        finally:
            if session_token:
                try:
                    self.delete_session(session_token)
                except Exception:
                    pass

    def get_repo_visibility(self, repo: str) -> str | None:
        """Query repo visibility from gateway.

        Args:
            repo: Repository in owner/name format

        Returns:
            Visibility string ('public', 'private', 'internal') or None on failure
        """
        try:
            result = self._make_request(
                f"/api/v1/repos/visibility?repos={quote(repo, safe='')}",
                use_launcher_auth=True,
            )
            visibilities = result.get("data", {}).get("visibilities", {})
            return visibilities.get(repo)
        except Exception as e:
            logger.warning("Failed to query repo visibility", repo=repo, error=str(e))
            return None


# =============================================================================
# Rebase helpers for push_worktree_branch reconcile path
# =============================================================================


def _rebase_with_agent_output_autoresolve(
    git_base: list[str],
    pipeline_id: str,
    branch: str,
    base_branch: str | None = None,
    max_autoresolve_iterations: int = 3,
) -> PushResult:
    """Rebase the worktree onto ``origin/{branch}`` with agent-outputs auto-resolve.

    When ``base_branch`` is provided and ``origin/{base_branch}`` exists
    locally, the rebase uses the ``--onto origin/{branch}
    origin/{base_branch}`` form so only commits that are unique to the
    local worktree (i.e. ``origin/{base_branch}..HEAD``) are replayed.
    Without the ``--onto`` form, a plain ``git rebase origin/{branch}``
    replays the full ``merge-base(HEAD, origin/{branch})..HEAD`` range;
    when ``origin/{branch}`` is based on an older snapshot of main and
    HEAD is based on a newer snapshot, that range includes the upstream
    main commits that landed in between, producing duplicate-by-content
    commits with different SHAs on the pipeline branch (#1976).

    Conflicts confined to ``.egg-state/agent-outputs/`` are resolved in
    favour of the remote (``git checkout --theirs``) and the rebase is
    continued; conflicts anywhere else cause the rebase to be aborted
    and a failure ``PushResult`` returned.

    The auto-resolve loop is bounded by ``max_autoresolve_iterations``
    to defend against pathological cases where every replayed commit
    re-introduces an agent-outputs conflict — three iterations is
    plenty for a handful of housekeeping commits.

    Returns ``PushResult(ok=True)`` when the rebase finished cleanly
    (possibly after auto-resolve). On failure, the ``category`` names
    which part of the rebase went wrong (``reconcile_rebase_timeout``,
    ``reconcile_rebase_conflict``, ``reconcile_rebase_failed``).
    """
    rebase_cmd = _build_rebase_cmd(git_base, branch, base_branch)
    try:
        rebase_result = subprocess.run(
            rebase_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Push reconcile: rebase timed out — aborting",
            pipeline_id=pipeline_id,
            branch=branch,
        )
        _abort_rebase_best_effort(git_base, pipeline_id, branch)
        return PushResult(
            ok=False,
            category="reconcile_rebase_timeout",
            detail=f"git rebase {' '.join(rebase_cmd[len(git_base) + 1 :])} timed out after 120s",
        )

    if rebase_result.returncode == 0:
        return PushResult(ok=True)

    for iteration in range(max_autoresolve_iterations):
        unmerged_paths = _list_unmerged_paths(git_base)
        if not unmerged_paths:
            logger.error(
                "Push reconcile: rebase stopped with no unmerged paths — aborting",
                pipeline_id=pipeline_id,
                branch=branch,
                stdout=rebase_result.stdout,
                stderr=rebase_result.stderr,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_failed",
                detail=(rebase_result.stderr or rebase_result.stdout or "").strip()
                or "rebase stopped with no unmerged paths",
            )

        non_ephemeral = [p for p in unmerged_paths if not p.startswith(".egg-state/agent-outputs/")]
        if non_ephemeral:
            logger.error(
                "Push reconcile: rebase failed — conflicts outside agent-outputs, aborting",
                pipeline_id=pipeline_id,
                branch=branch,
                conflicting_paths=unmerged_paths,
                stdout=rebase_result.stdout,
                stderr=rebase_result.stderr,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_conflict",
                detail=f"conflicts outside .egg-state/agent-outputs/: {', '.join(non_ephemeral)}",
            )

        logger.warning(
            "Push reconcile: auto-resolving agent-outputs conflicts (taking remote)",
            pipeline_id=pipeline_id,
            branch=branch,
            resolved_paths=unmerged_paths,
            iteration=iteration + 1,
        )
        try:
            subprocess.run(
                [
                    *git_base,
                    "checkout",
                    "--theirs",
                    "--",
                    ".egg-state/agent-outputs",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            # Some paths may have been deleted on ``--theirs``; --all handles that.
            subprocess.run(
                [*git_base, "add", "--all", "--", ".egg-state/agent-outputs"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as resolve_err:
            logger.error(
                "Push reconcile: auto-resolve failed — aborting rebase",
                pipeline_id=pipeline_id,
                branch=branch,
                error=str(resolve_err),
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_failed",
                detail=f"agent-outputs auto-resolve failed: {resolve_err}",
            )

        # If resolution cleared the index, ``--continue`` errors with
        # "No changes - did you forget to use 'git add'?". Use --skip.
        diff_result = subprocess.run(
            [*git_base, "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        continue_cmd = "--skip" if diff_result.returncode == 0 else "--continue"

        # GIT_EDITOR=true suppresses editor prompt on --continue only.
        env = {**os.environ, "GIT_EDITOR": "true"} if continue_cmd == "--continue" else None
        try:
            rebase_result = subprocess.run(
                [*git_base, "rebase", continue_cmd],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "Push reconcile: rebase --continue timed out — aborting",
                pipeline_id=pipeline_id,
                branch=branch,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_timeout",
                detail=f"git rebase {continue_cmd} timed out after 120s",
            )

        if rebase_result.returncode == 0:
            return PushResult(ok=True)

    logger.error(
        "Push reconcile: rebase auto-resolve exceeded iteration limit — aborting",
        pipeline_id=pipeline_id,
        branch=branch,
        max_iterations=max_autoresolve_iterations,
    )
    _abort_rebase_best_effort(git_base, pipeline_id, branch)
    return PushResult(
        ok=False,
        category="reconcile_rebase_failed",
        detail=(f"agent-outputs auto-resolve exceeded {max_autoresolve_iterations} iterations"),
    )


def _list_unmerged_paths(git_base: list[str]) -> list[str]:
    """Return the set of paths currently in a conflicted state in the worktree.

    Returns an empty list when the query itself fails — callers should be
    aware that ``[]`` can mean either "no conflicts" or "query failed".
    """
    result = subprocess.run(
        [*git_base, "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            "_list_unmerged_paths: git diff --diff-filter=U failed",
            returncode=result.returncode,
            stderr=result.stderr,
        )
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _build_rebase_cmd(
    git_base: list[str],
    branch: str,
    base_branch: str | None,
) -> list[str]:
    """Construct the ``git rebase`` argv for the push-reconcile path.

    When ``base_branch`` is set and ``origin/{base_branch}`` resolves in
    the worktree, return the ``--onto origin/{branch} origin/{base_branch}``
    form so only ``origin/{base_branch}..HEAD`` commits are replayed.
    Otherwise return the plain ``git rebase origin/{branch}`` form
    (pre-#1976 behavior) — used as a safe fallback when the base branch
    is unknown or unavailable locally.
    """
    if base_branch:
        base_ref = f"origin/{base_branch}"
        try:
            verify = subprocess.run(
                [*git_base, "rev-parse", "--verify", base_ref],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            verify = None
        if verify and verify.returncode == 0:
            return [*git_base, "rebase", "--onto", f"origin/{branch}", base_ref]
    return [*git_base, "rebase", f"origin/{branch}"]


def _abort_rebase_best_effort(
    git_base: list[str],
    pipeline_id: str,
    branch: str,
) -> None:
    """Run ``git rebase --abort`` and swallow any failure (worktree is junk anyway)."""
    try:
        subprocess.run(
            [*git_base, "rebase", "--abort"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        logger.warning(
            "Push reconcile: rebase --abort also failed",
            pipeline_id=pipeline_id,
            branch=branch,
        )


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
