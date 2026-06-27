"""Gateway session create / teardown (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import os
from datetime import datetime, timedelta

import kubernetes_spawner as _pkg
from gateway_client import SessionInfo
from kubernetes_spawner import (
    logger,
)
from models import AgentRole


def _get_or_create_session(
    self,
    pipeline_id: str,
    agent_role: AgentRole,
    slice_id: str | None = None,
    mode: str = "public",
    repos: list[str] | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    phase: str | None = None,
    issue_number: int | None = None,
    upstream: str | None = None,
    upstream_model: str | None = None,
    jira_ticket: str | None = None,
) -> SessionInfo | None:
    """Return a live session for *agent_role*, or register a new one.

    Checks the in-memory session token cache for this role+slice first.
    If a cached token exists and the gateway confirms the session is
    still live (:meth:`GatewayClient.heartbeat_session_by_container`),
    it is reused without a round-trip. Otherwise a fresh session is
    registered via the gateway.

    Returns the :class:`SessionInfo` (or a stub with the session token)
    on success, or ``None`` on registration failure.

    Signature matches the tester's test-first contract:
    ``(pipeline_id, agent_role, slice_id, mode, repos, ...) -> SessionInfo | None``.
    """
    job_name, _jn2 = self._build_k8s_job_names(pipeline_id, agent_role, slice_id=slice_id)
    cache_key = (pipeline_id, agent_role.value, slice_id, job_name)
    cached_token = self._session_token_cache.get(cache_key)

    if cached_token is not None:
        try:
            if self.gateway.heartbeat_session_by_container(job_name):
                # Reuse returns a stub WITHOUT re-registering, so the gateway
                # session keeps its original phase/branch/upstream. This is
                # safe only because reuse is confined to one role within one
                # slice+phase (the propose→ack→confirm arc): those fields are
                # stable across that arc. Reuse MUST NOT cross a phase
                # boundary — phase end tears the session down (see
                # ``cleanup_pipeline`` / ``_teardown_session``), so the next
                # phase re-registers fresh rather than inheriting stale
                # gateway policy.
                logger.info(
                    "Reusing live cached session",
                    job_name=job_name,
                    role=agent_role.value,
                )
                return SessionInfo(
                    session_token=cached_token,
                    container_id=job_name,
                    container_ip=None,
                    mode=mode,
                    created_at=datetime.now(),
                    expires_at=datetime.now() + timedelta(hours=24),
                )
        except Exception:
            logger.info(
                "Session heartbeat failed, will re-register",
                job_name=job_name,
                role=agent_role.value,
            )

    # Register a fresh session.
    try:
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))
        agent_anchor_id = f"{agent_role.value}-{job_name[:8]}"
        session_info = self.gateway.register_session(
            container_id=job_name,
            container_ip=None,
            mode=mode,
            repos=repos,
            uid=host_uid,
            gid=host_gid,
            phase=phase,
            pipeline_id=pipeline_id,
            agent_role=agent_role.value,
            agent_anchor_id=agent_anchor_id,
            issue_number=issue_number,
            claude_code_version=os.environ.get("CLAUDE_CODE_VERSION"),
            branch=branch,
            base_branch=base_branch,
            jira_ticket=jira_ticket,
            # Per-agent upstream routing — forward so a
            # session reused via this path keeps its litellm routing
            # instead of silently falling back to the Anthropic default.
            upstream=upstream,
            upstream_model=upstream_model,
            retry_transient=True,
        )
        self._session_token_cache[cache_key] = session_info.session_token
        return session_info
    except Exception as e:
        logger.warning(
            "Failed to register gateway session",
            job_name=job_name,
            role=agent_role.value,
            error=str(e),
        )
        return None


def _teardown_session(
    self,
    pipeline_id: str,
    agent_role: AgentRole,
    slice_id: str | None = None,
) -> None:
    """Tear down a role's reused gateway session.

    In orchestrator-ownership mode a single gateway session is reused
    across a role's successive one-shot event spawns (see
    :meth:`_get_or_create_session`), so the per-event Job stop/remove paths
    deliberately do NOT delete it. This method is the explicit teardown for
    that long-lived session — called at phase end (via
    :meth:`cleanup_pipeline`) or on streak exhaustion, when the role will
    spawn no further events. It deletes the gateway session keyed by the
    stable base ``container_id`` and evicts the in-memory cache entry so the
    cache stays bounded by roster size rather than growing per event.

    Best-effort: a gateway error is logged and swallowed (teardown must
    never wedge the caller). The per-event Job stop/remove paths do not
    reach this stable-base-keyed session, which is why this explicit
    teardown exists. Note that :meth:`cleanup_pipeline` applies this
    primitive across *every* cached entry for the pipeline, including
    pod-mode entries (keyed by the per-Job name) — those are normally
    cleaned by the stop/remove paths, but the extra
    ``delete_session_by_container`` here is idempotent, so the broad sweep
    is harmless rather than exclusive to event-mode sessions.
    """
    session_id, _ = self._build_k8s_job_names(pipeline_id, agent_role, slice_id=slice_id)
    try:
        self.gateway.delete_session_by_container(session_id)
        logger.info(
            "Tore down reused gateway session",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            session_container_id=session_id,
        )
    except _pkg.GatewayError as e:
        logger.warning(
            "Failed to tear down reused gateway session",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            session_container_id=session_id,
            error=str(e),
        )
    finally:
        self._session_token_cache.pop((pipeline_id, agent_role.value, slice_id, session_id), None)
