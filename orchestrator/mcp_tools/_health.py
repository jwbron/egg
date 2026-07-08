"""PipelineToolHandler health-check / container-list / container-logs / send-message handlers (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


def _handle_check_health(self, args: dict[str, Any]) -> dict[str, Any]:
    """Check orchestrator and gateway health.

    Each per-service entry includes readiness history (``healthy_since``,
    ``last_unhealthy_at``, ``recent_transitions``) so operators can
    diagnose "was this service reachable 30 seconds ago?" without having
    to cross-reference logs. See issue #1855.
    """
    result: dict[str, Any] = {}

    # Orchestrator health
    try:
        orch = self._make_request("/api/v1/health")
        result["orchestrator"] = {
            "healthy": orch.get("status") == "healthy",
            "status": orch.get("status", "unknown"),
            "healthy_since": orch.get("healthy_since"),
            "last_unhealthy_at": orch.get("last_unhealthy_at"),
            "process_start_time": orch.get("process_start_time"),
            "recent_transitions": orch.get("recent_transitions", []),
        }
    except Exception as e:
        result["orchestrator"] = {
            "healthy": False,
            "status": "unreachable",
            "error": str(e),
            "healthy_since": None,
            "last_unhealthy_at": None,
            "process_start_time": None,
            "recent_transitions": [],
        }

    # Gateway health — use direct HTTP to avoid importing orchestrator.gateway_client
    # which may not be available when the MCP server runs outside the orchestrator venv
    try:
        import json
        from urllib.request import ProxyHandler, Request, build_opener

        gw_url = f"{self.gateway_url}/api/v1/health"
        opener = build_opener(ProxyHandler({}))
        req = Request(gw_url, method="GET")
        with opener.open(req, timeout=10) as response:
            gw = json.loads(response.read().decode())
        result["gateway"] = {
            "healthy": gw.get("status") == "healthy",
            "status": gw.get("status", "unknown"),
            "version": gw.get("version"),
            "healthy_since": gw.get("healthy_since"),
            "last_unhealthy_at": gw.get("last_unhealthy_at"),
            "process_start_time": gw.get("process_start_time"),
            "recent_transitions": gw.get("recent_transitions", []),
        }
    except Exception as e:
        result["gateway"] = {
            "healthy": False,
            "status": "unreachable",
            "error": str(e),
            "healthy_since": None,
            "last_unhealthy_at": None,
            "process_start_time": None,
            "recent_transitions": [],
        }

    result["healthy"] = result.get("orchestrator", {}).get("healthy", False) and result.get(
        "gateway", {}
    ).get("healthy", False)
    return result


def _handle_list_containers(self, args: dict[str, Any]) -> dict[str, Any]:
    """List containers for a pipeline."""
    task_id = quote(args["task_id"], safe="")
    include_stopped = args.get("include_stopped", True)
    all_param = "true" if include_stopped else "false"
    return self._make_request(f"/api/v1/pipelines/{task_id}/containers?all={all_param}")


def _handle_get_container_logs(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get container logs, with auto-selection if container_id not specified.

    One-shot agent pods are reaped within minutes of exit, so when no live
    container matches (or the live fetch 404s) this falls back to the
    post-reap captures persisted by ``remove_agent_job`` (#3547); fallback
    results carry ``"source": "persisted"``.
    """
    task_id = quote(args["task_id"], safe="")
    container_id = args.get("container_id")
    agent_role = args.get("agent_role")
    lines = args.get("lines", 100)

    selected: dict[str, Any] = {}
    if not container_id:
        # Auto-select: list containers, filter by role, pick best match
        containers_result = self._make_request(f"/api/v1/pipelines/{task_id}/containers?all=true")
        containers = containers_result.get("data", {}).get("containers", [])

        # Filter by agent_role if specified
        if agent_role:
            filtered = [c for c in containers if c.get("agent_role") == agent_role]
            if filtered:
                containers = filtered
            elif containers:
                # No live container for this role; the pod was likely
                # already reaped; go straight to the persisted captures.
                containers = []

        if not containers:
            fallback = self._persisted_agent_logs_fallback(task_id, None, agent_role)
            if fallback is not None:
                return fallback
            return {"error": "No containers found for this pipeline"}

        # Prefer running containers, then most recently started
        running = [c for c in containers if c.get("status") == "running"]
        if running:
            selected = running[0]
        else:
            containers.sort(key=lambda c: c.get("started_at", ""), reverse=True)
            selected = containers[0]

        container_id = selected.get("container_id", "")

    cid = quote(container_id, safe="")
    try:
        logs_result = self._make_request(
            f"/api/v1/pipelines/{task_id}/containers/{cid}/logs?tail={lines}"
        )
    except Exception:
        # Live pod gone between listing and fetch (or an explicit
        # container_id for an already-reaped pod): try the captures.
        # When auto-select picked the container, forward its known role so
        # role re-narrowing can recover the capture even if the exact
        # container_id (a pod UID) misses the job-name-keyed lookup; the
        # miss-on-ambiguity guard would otherwise give up in that race
        # window (#3566 review).
        fallback = self._persisted_agent_logs_fallback(
            task_id, container_id, agent_role or selected.get("agent_role")
        )
        if fallback is not None:
            return fallback
        raise

    data = logs_result.get("data", {})
    result = {
        "container_id": container_id,
        "agent_role": agent_role or selected.get("agent_role") or data.get("agent_role") or None,
        "status": selected.get("status") or None,
        "logs": data.get("logs", ""),
    }
    if data.get("source") == "persisted":
        # The route itself served a post-reap capture; surface its metadata.
        result["source"] = "persisted"
        result["captured_at"] = data.get("captured_at")
        result["exit_code"] = data.get("exit_code")
    return result


def _persisted_agent_logs_fallback(
    self,
    task_id: str,
    container_id: str | None,
    agent_role: str | None,
) -> dict[str, Any] | None:
    """Serve logs from the post-reap agent-log captures, or ``None`` on miss (#3547).

    Prefers an exact ``container_id``/job-name match, then the newest capture
    for ``agent_role``, then the newest capture overall. ``task_id`` arrives
    already URL-quoted by the caller.
    """
    record: dict[str, Any] = {}
    if container_id:
        # Exact match first; but captures are keyed by Job name while an
        # auto-selected container_id may be a pod UID, so a miss here falls
        # through to the role/index lookup rather than giving up.
        try:
            cid = quote(container_id, safe="")
            record_result = self._make_request(f"/api/v1/pipelines/{task_id}/agent-logs/{cid}")
            record = record_result.get("data") or {}
        except Exception:
            record = {}
    if not record and container_id and not agent_role:
        # An explicit container_id that missed the exact lookup must not be
        # silently substituted with an unrelated capture. Only a role filter is
        # a legitimate re-narrowing (below); without one, treat it as a miss
        # rather than returning "newest capture overall" labelled as a different
        # job — that would hand the operator job-B when they asked for job-A
        # (#3566 review). Newest-overall stays reserved for the no-container_id
        # auto-select path.
        return None
    if not record:
        try:
            index_result = self._make_request(f"/api/v1/pipelines/{task_id}/agent-logs")
            records = index_result.get("data", {}).get("records", [])
            if agent_role:
                records = [r for r in records if r.get("agent_role") == agent_role]
            if not records:
                return None
            # Newest first per the route's ordering; fetch the full body.
            job = quote(records[0].get("job_name", ""), safe="")
            record_result = self._make_request(f"/api/v1/pipelines/{task_id}/agent-logs/{job}")
            record = record_result.get("data") or {}
        except Exception:
            return None
    if not record:
        return None
    return {
        "container_id": record.get("job_name") or container_id,
        "agent_role": record.get("agent_role") or agent_role,
        "status": "reaped",
        "source": "persisted",
        "captured_at": record.get("captured_at"),
        "exit_code": record.get("exit_code"),
        "slice_id": record.get("slice_id"),
        "truncated": record.get("truncated", False),
        "logs": record.get("logs", ""),
    }


def _handle_get_agent_transcript(self, args: dict[str, Any]) -> dict[str, Any]:
    """Read a role's session transcript from the session-state store (#3547).

    Transcripts are pushed on every event-pod exit (``session-state push``)
    and are the one artifact that always survives a one-shot run; this is the
    operator-facing read path. On a miss (or when ``agent_role`` is omitted)
    returns the store's index so the caller can pick a valid
    ``(agent_role, slice_id)`` pair and retry.
    """
    task_id = quote(args["task_id"], safe="")
    agent_role = args.get("agent_role")
    slice_id = args.get("slice_id")
    lines = args.get("lines", 200)

    if agent_role:
        query = f"role={quote(agent_role, safe='')}"
        if slice_id:
            query += f"&slice_id={quote(slice_id, safe='')}"
        result = self._make_request(f"/api/v1/pipelines/{task_id}/session-state?{query}")
        if result.get("found"):
            data = result.get("data", {})
            transcript = data.get("transcript") or ""
            all_lines = transcript.splitlines()
            tail = all_lines[-lines:] if isinstance(lines, int) and lines > 0 else []
            return {
                "found": True,
                "agent_role": agent_role,
                "slice_id": slice_id,
                "session_id": data.get("session_id"),
                "window_occupancy": data.get("window_occupancy"),
                "total_transcript_lines": len(all_lines),
                "lines_returned": len(tail),
                "transcript_tail": "\n".join(tail),
            }

    index = self._make_request(f"/api/v1/pipelines/{task_id}/session-state/index")
    return {
        "found": False,
        "agent_role": agent_role,
        "slice_id": slice_id,
        "available_transcripts": index.get("records", []),
        "hint": (
            "No stored transcript for that (agent_role, slice_id); retry with a "
            "pair from available_transcripts. Records expire 6h after the "
            "agent's last push."
        ),
    }


def _handle_send_message(self, args: dict[str, Any]) -> dict[str, Any]:
    """Send a message to an agent in a pipeline."""
    task_id = quote(args["task_id"], safe="")
    data: dict[str, Any] = {
        "from_role": "overseer",
        "to_role": args["to_role"],
        "message_type": args.get("message_type", "STATUS"),
        "body": args["body"],
    }
    if args.get("subject"):
        data["subject"] = args["subject"]
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/messages",
        method="POST",
        data=data,
    )
