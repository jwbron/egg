"""
MCP tool definitions for coordinator integration.

Provides tool schemas and handlers that proxy to orchestrator APIs,
enabling external Claude Code sessions to interact with the coordinator
via the MCP protocol.
"""

import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("orchestrator.mcp_tools")


# Tool definitions following MCP protocol schema
COORDINATOR_TOOLS = [
    {
        "name": "submit_task",
        "description": "Submit a task for the coordinator to process. Creates a coordinator-enabled pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Natural language task description",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number (optional)",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name override (optional). Auto-generated as 'egg/issue-<N>' when issue_number is provided.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository to work on, in owner/name format (e.g. 'myorg/myrepo')",
                },
            },
            "required": ["description", "repo"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "Get the current status of a coordinator-managed task. "
            "Returns coordinator state (current_phase, status, running_agents, "
            "completed_agents, pending_decisions, guardrail_counters), "
            "pipeline details (id, repo, issue_number, created_at, mode), "
            "and recent_messages (from_role, type, subject, timestamp)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID to check",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "provide_input",
        "description": "Provide human input for a coordinator escalation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "decision_id": {
                    "type": "string",
                    "description": "Decision ID to resolve",
                },
                "response": {
                    "type": "string",
                    "description": "Human's response to the escalation",
                },
            },
            "required": ["task_id", "decision_id", "response"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List active and recent coordinator-managed pipelines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["active", "completed", "failed", "all"],
                    "description": "Filter by status",
                    "default": "active",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tasks to return",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel a coordinator-managed task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID to cancel",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for cancellation",
                },
            },
            "required": ["task_id"],
        },
    },
]


class CoordinatorToolHandler:
    """Handles MCP tool calls by proxying to orchestrator APIs."""

    def __init__(self, orchestrator_url: str = "http://localhost:9849"):
        self.orchestrator_url = orchestrator_url

    def handle_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to the appropriate handler.

        Args:
            tool_name: Name of the MCP tool
            arguments: Tool arguments

        Returns:
            Tool result dictionary
        """
        handlers = {
            "submit_task": self._handle_submit_task,
            "get_status": self._handle_get_status,
            "provide_input": self._handle_provide_input,
            "list_tasks": self._handle_list_tasks,
            "cancel_task": self._handle_cancel_task,
        }

        handler = handlers.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            return handler(arguments)
        except Exception as e:
            logger.error("Tool call failed", tool=tool_name, error=str(e))
            return {"error": str(e)}

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Make HTTP request to orchestrator."""
        import json
        from urllib.request import ProxyHandler, Request, build_opener

        url = f"{self.orchestrator_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        body = json.dumps(data).encode() if data else None

        opener = build_opener(ProxyHandler({}))
        req = Request(url, data=body, headers=headers, method=method)

        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode())

    def _handle_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create a coordinator-enabled pipeline."""
        data: dict[str, Any] = {
            "config": {"coordinator_enabled": True},
        }
        if args.get("issue_number"):
            data["issue_number"] = args["issue_number"]
            data["branch"] = args.get("branch") or f"egg/issue-{args['issue_number']}"
        else:
            data["prompt"] = args["description"]
        if args.get("repo"):
            data["repo"] = args["repo"]

        result = self._make_request("/api/v1/pipelines", method="POST", data=data)

        pipeline_id = result.get("data", {}).get("pipeline", {}).get("id", "")

        if pipeline_id:
            try:
                self._make_request(
                    f"/api/v1/pipelines/{quote(pipeline_id, safe='')}/start", method="POST"
                )
            except Exception:
                logger.error("Failed to start pipeline", pipeline_id=pipeline_id)
                return {
                    "task_id": pipeline_id,
                    "status": "created_not_started",
                    "message": "Pipeline created but failed to start. Use task_id to retry.",
                }

        return {
            "task_id": pipeline_id,
            "status": "started",
            "message": f"Task submitted: {args['description'][:100]}",
        }

    def _handle_get_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get enriched coordinator state for a pipeline.

        Fetches coordinator state, pipeline details, and recent messages.
        For phase_gate decisions, includes draft document content so the
        caller can present it to the user without needing filesystem access.
        Falls back gracefully if pipeline details or messages fail.
        """
        task_id = quote(args["task_id"], safe="")

        # Primary: coordinator state (required)
        result = self._make_request(f"/api/v1/pipelines/{task_id}/coordinator/state")
        status = result.get("data", {})

        # Enrichment: pipeline details (optional)
        pipeline_data: dict[str, Any] = {}
        try:
            pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
            pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})
            status["pipeline"] = {
                "id": pipeline_data.get("id", ""),
                "repo": pipeline_data.get("repo", ""),
                "issue_number": pipeline_data.get("issue_number"),
                "created_at": pipeline_data.get("created_at", ""),
                "mode": pipeline_data.get("mode", ""),
            }
        except Exception:
            logger.debug("Failed to fetch pipeline details", task_id=task_id)

        # Enrichment: recent messages (optional)
        try:
            messages_result = self._make_request(f"/api/v1/pipelines/{task_id}/messages?limit=10")
            raw_messages = messages_result.get("data", {}).get("messages", [])
            status["recent_messages"] = [
                {
                    "from_role": m.get("from_role", ""),
                    "type": m.get("type", ""),
                    "subject": m.get("subject", ""),
                    "timestamp": m.get("timestamp", ""),
                }
                for m in raw_messages
            ]
        except Exception:
            logger.debug("Failed to fetch messages", task_id=task_id)

        # Enrichment: attach draft content to phase_gate decisions (optional)
        self._enrich_phase_gate_decisions(status, task_id, pipeline_data)

        return status

    def _enrich_phase_gate_decisions(
        self,
        status: dict[str, Any],
        pipeline_id: str,
        pipeline_data: dict[str, Any],
    ) -> None:
        """Attach draft content and agent summaries to phase_gate decisions.

        Reads the draft document from the pipeline worktree so the caller
        can present it to the user without needing direct filesystem access.
        Mutates ``status["pending_decisions"]`` in place.
        """
        import os

        pending = status.get("pending_decisions", [])
        phase_gates = [d for d in pending if d.get("decision_type") == "phase_gate"]
        if not phase_gates:
            return

        # Build completed agents summary
        completed_agents = status.get("completed_agents", [])
        agents_summary = [
            {
                "role": a.get("role", ""),
                "status": a.get("status", ""),
            }
            for a in completed_agents
        ]

        # Resolve repo path to read drafts from the worktree
        repo = pipeline_data.get("repo", "")
        issue_number = pipeline_data.get("issue_number")
        current_phase = status.get("current_phase", "")

        draft_content = None
        try:
            if repo:
                from orchestrator.routes import resolve_worktree_path
                from orchestrator.routes.pipelines import _get_draft_path

                env_path = os.environ.get("EGG_REPO_PATH", "/home/egg/repos")
                base_path = Path(env_path)
                repo_name = repo.split("/")[-1]
                repo_path = (
                    base_path / repo_name if not (base_path / ".git").exists() else base_path
                )
                worktree_path = resolve_worktree_path(pipeline_id, repo_path)

                # phase_gate fires after a phase completes, so the draft
                # belongs to the phase recorded in the decision (or the
                # current phase as fallback).
                for decision in phase_gates:
                    decision_phase = decision.get("phase") or current_phase
                    draft_rel = _get_draft_path(
                        decision_phase,
                        issue_number=issue_number,
                        pipeline_id=pipeline_id,
                    )
                    if draft_rel:
                        draft_path = worktree_path / draft_rel
                        if draft_path.exists():
                            content = draft_path.read_text(encoding="utf-8")
                            max_chars = 16_000
                            if len(content) > max_chars:
                                content = content[:max_chars] + (
                                    f"\n\n... (truncated, {len(content)} chars total)"
                                )
                            draft_content = content
        except Exception:
            logger.debug(
                "Failed to read draft for phase_gate enrichment",
                pipeline_id=pipeline_id,
            )

        # Attach enrichments to each phase_gate decision
        for decision in phase_gates:
            if draft_content is not None:
                decision["draft_content"] = draft_content
            if agents_summary:
                decision["completed_agents_summary"] = agents_summary

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

    def _handle_list_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        """List coordinator-managed pipelines."""
        result = self._make_request("/api/v1/pipelines")
        pipelines = result.get("data", {}).get("pipelines", [])

        # Filter to coordinator-enabled pipelines
        status_filter = args.get("status_filter", "active")
        coordinator_pipelines = []
        for p in pipelines:
            config = p.get("config", {})
            if config.get("coordinator_enabled"):
                p_status = p.get("status", "")
                if status_filter == "all":
                    coordinator_pipelines.append(p)
                elif status_filter == "active" and p_status in (
                    "pending",
                    "running",
                    "awaiting_human",
                ):
                    coordinator_pipelines.append(p)
                elif status_filter == "completed" and p_status == "complete":
                    coordinator_pipelines.append(p)
                elif status_filter == "failed" and p_status == "failed":
                    coordinator_pipelines.append(p)

        limit = args.get("limit", 10)
        return {
            "tasks": coordinator_pipelines[:limit],
            "total": len(coordinator_pipelines),
        }

    def _handle_cancel_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cancel a coordinator pipeline."""
        task_id = quote(args["task_id"], safe="")
        data = {"status": "cancelled"}
        if args.get("reason"):
            data["reason"] = args["reason"]
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}",
            method="PATCH",
            data=data,
            timeout=120,
        )
        return result
