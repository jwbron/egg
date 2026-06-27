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
    """Get container logs, with auto-selection if container_id not specified."""
    task_id = quote(args["task_id"], safe="")
    container_id = args.get("container_id")
    agent_role = args.get("agent_role")
    lines = args.get("lines", 100)

    selected: dict[str, Any] = {}
    if not container_id:
        # Auto-select: list containers, filter by role, pick best match
        containers_result = self._make_request(f"/api/v1/pipelines/{task_id}/containers?all=true")
        containers = containers_result.get("data", {}).get("containers", [])
        if not containers:
            return {"error": "No containers found for this pipeline"}

        # Filter by agent_role if specified
        if agent_role:
            filtered = [c for c in containers if c.get("agent_role") == agent_role]
            if filtered:
                containers = filtered

        # Prefer running containers, then most recently started
        running = [c for c in containers if c.get("status") == "running"]
        if running:
            selected = running[0]
        else:
            containers.sort(key=lambda c: c.get("started_at", ""), reverse=True)
            selected = containers[0]

        container_id = selected.get("container_id", "")

    cid = quote(container_id, safe="")
    logs_result = self._make_request(
        f"/api/v1/pipelines/{task_id}/containers/{cid}/logs?tail={lines}"
    )

    return {
        "container_id": container_id,
        "agent_role": agent_role or selected.get("agent_role") or None,
        "status": selected.get("status") or None,
        "logs": logs_result.get("data", {}).get("logs", ""),
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
