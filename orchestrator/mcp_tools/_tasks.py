"""PipelineToolHandler input / feedback / list / cancel task handlers (#3312 slice-13).

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

from mcp_tools import logger


def _handle_provide_input(self, args: dict[str, Any]) -> dict[str, Any]:
    """Resolve an escalation decision."""
    task_id = quote(args["task_id"], safe="")
    decision_id = quote(args["decision_id"], safe="")
    data = {"resolution": args["response"]}
    result = self._make_request(
        f"/api/v1/pipelines/{task_id}/decisions/{decision_id}/resolve",
        method="POST",
        data=data,
    )
    return result


def _handle_answer_feedback(self, args: dict[str, Any]) -> dict[str, Any]:
    """Answer an agent-registered contract feedback request (#3007).

    Routes to the pipeline-scoped ``/feedback/answer`` endpoint, which
    writes the answers into the contract feedback so a refiner blocked
    on pre-proposal feedback unblocks. Unlike ``provide_input``, this
    resolves against the gateway-backed contract, not the orchestrator
    decision queue.
    """
    task_id = quote(args["task_id"], safe="")
    data: dict[str, Any] = {"answers": args["answers"]}
    if args.get("feedback_id"):
        data["feedback_id"] = args["feedback_id"]
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/feedback/answer",
        method="POST",
        data=data,
    )


def _handle_list_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
    """List pipelines."""
    result = self._make_request("/api/v1/pipelines")
    pipelines = result.get("data", {}).get("pipelines", [])

    status_filter = args.get("status_filter", "active")
    repo_filter = args.get("repo")
    issue_filter = args.get("issue_number")

    filtered_pipelines = []
    for p in pipelines:
        # Apply repo filter
        if repo_filter and p.get("repo") != repo_filter:
            continue

        # Apply issue_number filter
        if issue_filter is not None and p.get("issue_number") != issue_filter:
            continue

        # Apply status filter
        p_status = p.get("status", "")
        if status_filter == "all":
            filtered_pipelines.append(p)
        elif status_filter == "active" and p_status in (
            "pending",
            "running",
            "awaiting_human",
        ):
            filtered_pipelines.append(p)
        elif status_filter == "completed" and p_status == "complete":
            filtered_pipelines.append(p)
        elif status_filter == "failed" and p_status == "failed":
            filtered_pipelines.append(p)

    limit = args.get("limit", 10)
    return {
        "tasks": filtered_pipelines[:limit],
        "total": len(filtered_pipelines),
    }


def _handle_cancel_task(self, args: dict[str, Any]) -> dict[str, Any]:
    """Cancel a pipeline.

    When cleanup=True, also deletes the pipeline state (containers,
    sessions, worktrees, state files) so the same issue can be
    resubmitted without a 409 conflict.
    """
    task_id = quote(args["task_id"], safe="")
    data: dict[str, Any] = {"status": "cancelled"}
    if args.get("reason"):
        data["reason"] = args["reason"]
    result = self._make_request(
        f"/api/v1/pipelines/{task_id}",
        method="PATCH",
        data=data,
        timeout=120,
    )

    if args.get("cleanup"):
        # Fire DELETE in a background thread so the MCP call returns
        # immediately.  The DELETE endpoint cleans up containers,
        # remote branches, Redis messages, and the state file.  The
        # PATCH handler already runs container cleanup in its own
        # background thread, so DELETE acts as a safety net.  See #1594.
        import threading

        def _background_delete() -> None:
            try:
                self._make_request(
                    f"/api/v1/pipelines/{task_id}",
                    method="DELETE",
                    timeout=120,
                )
            except Exception as e:
                logger.warning(
                    "Background cleanup DELETE failed",
                    task_id=task_id,
                    error=str(e),
                )

        threading.Thread(
            target=_background_delete,
            daemon=True,
            name=f"mcp-cleanup-{task_id}",
        ).start()

        return {
            "cancelled": True,
            "cleanup_started": True,
            "message": "Pipeline cancelled; cleanup running in background",
        }

    return result
