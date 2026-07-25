"""Gateway session register / validate / delete / heartbeat (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import gateway_client as _pkg
from gateway_client import GatewayError
from gateway_client._models import SessionInfo


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
    base_branch: str | None = None,
    worktree_container_id: str | None = None,
    jira_ticket: str | None = None,
    synthetic: bool = False,
    upstream: str | None = None,
    upstream_model: str | None = None,
    retry_transient: bool = False,
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
        base_branch: Optional pipeline base branch (PR base). The gateway
            stores it on the session and uses it as the preferred diff base
            for the new-branch restricted-path push check, so a branch
            forked from a non-trunk base is not blamed for files inherited
            unchanged from that base (#3024). Omitted callers keep today's
            main/master fallback.
        worktree_container_id: Optional container_id under which per-agent
            worktrees were already created by a prior create_worktrees
            call.  When provided, the gateway reuses those worktrees
            instead of re-creating them — avoids a second
            ``git worktree add`` racing on ``.git/config.lock`` (#1857).
        upstream: Optional per-session upstream selector — ``"anthropic"``
            (default behavior when omitted) or ``"litellm"`` to route the
            session's ``/v1/messages`` traffic through the LiteLLM proxy
            in egg-system (issue #2769). Omitted callers produce a
            request body byte-identical to today. The gateway is
            authoritative: it validates this against its
            ``UpstreamRegistry`` and rejects an unknown value with
            HTTP 400, so a slice-2 resolution bug fails fast at
            session-create rather than producing a bogus-upstream
            session.
        upstream_model: Optional upstream-side model name used by the
            slice-2 body-rewrite path on the gateway. Only meaningful
            when ``upstream="litellm"``; the gateway leaves the request
            body's ``model`` field untouched when this is omitted.

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
    if base_branch is not None:
        # Only include when set — omitting keeps the wire shape identical
        # for callers that don't carry a base_branch (#3024).
        request_data["base_branch"] = base_branch
    if worktree_container_id is not None:
        request_data["worktree_container_id"] = worktree_container_id
    if jira_ticket:
        # Advisory: gateway records it in the Session and echoes it in
        # every /api/v1/jira/* audit line (issue #1556).  It does NOT gate
        # any Jira call on its value — the project allowlist is the only
        # hard boundary.
        request_data["jira_ticket"] = jira_ticket
    if synthetic:
        request_data["synthetic"] = True
    if upstream is not None:
        # Only include when caller opts in — omitting the field keeps
        # the wire shape byte-identical for pre-#2769 callers.
        request_data["upstream"] = upstream
    if upstream_model is not None:
        request_data["upstream_model"] = upstream_model

    def _do_request() -> dict[str, Any]:
        return self._make_request(
            "/api/v1/sessions/create",
            method="POST",
            data=request_data,
            use_launcher_auth=True,
        )

    # #2869 — spawn-time session registration tolerates a brief
    # DNS/connection blip when the caller opts in (the spawner and
    # slice integration-branch creation), instead of hard-failing the
    # whole pipeline on the first transient error.
    if retry_transient:
        result = self._retry_transient(_do_request, operation="register gateway session")
    else:
        result = _do_request()

    if not result.get("success"):
        raise GatewayError(result.get("message", "Session registration failed"))

    response_data = result.get("data", {})

    session_token = response_data.get("session_token")
    if not session_token:
        raise GatewayError("Gateway response missing session_token")

    _pkg.logger.info(
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
        _pkg.logger.warning("Failed to delete session", error=str(e))
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
        _pkg.logger.warning("Failed to update session", error=str(e))
        return False


def update_session_phase(self, session_token: str, phase: str) -> bool:
    """Update the SDLC pipeline phase recorded on a session (#3528).

    Requires launcher secret authentication. Wraps the gateway's
    ``PATCH /api/v1/sessions/<token>/phase`` route, which existed with no
    orchestrator-side callers while the gateway's commit gate keyed off the
    session's registration-time phase. Called on the session-reuse path and
    at phase advance so a session that survives a phase transition stops
    carrying the stale phase that deadlocked consensus in #3528.

    Args:
        session_token: Token of the session to update
        phase: The pipeline's current phase value (e.g. ``"plan"``)

    Returns:
        True if the phase was updated
    """
    try:
        result = self._make_request(
            f"/api/v1/sessions/{quote(session_token, safe='')}/phase",
            method="PATCH",
            data={"phase": phase},
            use_launcher_auth=True,
        )
        return result.get("success", False)
    except GatewayError as e:
        _pkg.logger.warning("Failed to update session phase", phase=phase, error=str(e))
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
        _pkg.logger.warning(
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
        _pkg.logger.warning(
            "Failed to heartbeat session by container",
            container_id=container_id,
            error=str(e),
        )
        return False
