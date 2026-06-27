"""PipelineToolHandler pipeline-snapshot + contract read handlers (#3312 slice-13).

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


def _handle_get_pipeline_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get a comprehensive pipeline snapshot combining multiple data sources."""
    task_id = quote(args["task_id"], safe="")
    include_messages = args.get("include_messages", True)
    include_containers = args.get("include_containers", True)

    # Pipeline state (always included)
    pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
    pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})

    snapshot: dict[str, Any] = {"pipeline": pipeline_data}

    # Phase details
    try:
        phase_result = self._make_request(f"/api/v1/pipelines/{task_id}/phase")
        snapshot["phase"] = phase_result.get("data", {})
    except Exception:
        pass

    # Status with concurrent/consensus info
    try:
        status_result = self._make_request(f"/api/v1/pipelines/{task_id}/status")
        status_data = status_result.get("data", {})
        if "concurrent" in status_data:
            snapshot["concurrent"] = status_data["concurrent"]
        if "pending_decision" in status_data:
            snapshot["pending_decision"] = status_data["pending_decision"]
        if "slice_admit" in status_data:
            snapshot["slice_admit"] = status_data["slice_admit"]
    except Exception:
        pass

    # Containers
    if include_containers:
        try:
            containers_result = self._make_request(
                f"/api/v1/pipelines/{task_id}/containers?all=true"
            )
            snapshot["containers"] = containers_result.get("data", {}).get("containers", [])
        except Exception:
            pass

    # Messages
    if include_messages:
        try:
            messages_result = self._make_request(f"/api/v1/pipelines/{task_id}/messages?limit=20")
            snapshot["recent_messages"] = messages_result.get("data", {}).get("messages", [])
        except Exception:
            pass

    # Decisions
    decisions = pipeline_data.get("decisions", [])
    snapshot["pending_decisions"] = [d for d in decisions if d.get("status") == "pending"]

    return snapshot


def _handle_get_contract(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get SDLC contract state.

    Routes through the orchestrator's ``/api/v1/contracts/<identifier>``
    endpoint with the pipeline_id as the path identifier so qualified
    pipelines (e.g. ``issue-42-v9``) resolve to their own contract file
    instead of the unqualified ``issue-42.json`` (#2427).
    """
    issue_number = args.get("issue_number")
    pipeline_id: str | None = args.get("task_id")

    if not pipeline_id and issue_number is None:
        return {"error": "Either issue_number or task_id is required"}

    # When only issue_number was provided, find the active pipeline so we
    # use its qualified ID — the canonical issue-<N> key would always
    # resolve the unqualified contract on disk even when a qualified one
    # exists.
    if not pipeline_id:
        issue_int = int(issue_number)
        try:
            pipelines_resp = self._make_request("/api/v1/pipelines?active_only=true")
            # If multiple active pipelines exist for this issue (e.g. a retry
            # started before the previous one was cancelled), we pick the most
            # recently created one.  The API response order is not guaranteed,
            # so we scan all matching entries and keep the latest by created_at.
            best: dict[str, Any] | None = None
            for p in pipelines_resp.get("data", {}).get("pipelines", []):
                if p.get("issue_number") == issue_int:
                    if best is None or p.get("created_at", "") > best.get("created_at", ""):
                        best = p
            if best is not None:
                pipeline_id = best["id"]
        except Exception:
            pass  # best-effort; fall back to canonical issue-<N>

        if not pipeline_id:
            pipeline_id = f"issue-{issue_int}"

    encoded = quote(pipeline_id, safe="")
    url = f"/api/v1/contracts/{encoded}?pipeline_id={encoded}"
    return self._make_request(url)
