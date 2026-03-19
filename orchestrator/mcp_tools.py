"""
MCP tool definitions for pipeline management.

Provides tool schemas and handlers that proxy to orchestrator APIs,
enabling external Claude Code sessions to manage SDLC pipelines
via the MCP protocol.
"""

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Ensure the repo root is on sys.path so that `from orchestrator.*` imports
# work when this module is loaded from the MCP server sidecar (which runs
# inside the orchestrator/ directory, not from the repo root).
_repo_root_path = Path(__file__).parent.parent
if (_repo_root_path / "orchestrator" / "__init__.py").exists() and str(
    _repo_root_path
) not in sys.path:
    sys.path.insert(0, str(_repo_root_path))

try:
    from egg_config import GATEWAY_PORT
except ImportError:
    GATEWAY_PORT = 9848  # noqa: EGG002

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("orchestrator.mcp_tools")


# Tool definitions following MCP protocol schema
PIPELINE_TOOLS = [
    {
        "name": "submit_task",
        "description": "Submit a task for processing. Creates an SDLC pipeline.",
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
                "config": {
                    "type": "object",
                    "description": 'Optional pipeline configuration overrides (e.g. {"start_phase": "implement", "hitl_gates": false})',
                },
            },
            "required": ["description", "repo"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "Get the current status of a pipeline task. "
            "Returns pipeline state (current_phase, status, agents, decisions), "
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
        "description": "Provide human input for a pipeline decision.",
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
        "description": "List active and recent pipelines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["active", "completed", "failed", "all"],
                    "description": "Filter by status",
                    "default": "active",
                },
                "repo": {
                    "type": "string",
                    "description": "Filter by repository (owner/name format, e.g. 'myorg/myrepo')",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "Filter by GitHub issue number",
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
        "description": "Cancel a pipeline task. Use cleanup=true to also delete pipeline state, allowing the same issue to be resubmitted.",
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
                "cleanup": {
                    "type": "boolean",
                    "description": "If true, fully delete pipeline state after cancellation (containers, sessions, worktrees, state files). Allows the same issue to be resubmitted without a 409 conflict.",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
    },
    # --- Orchestrator-backed diagnostic tools ---
    {
        "name": "check_health",
        "description": "Check health of the orchestrator and gateway services. Returns combined status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_containers",
        "description": "List containers (agents) for a pipeline, including their status, role, and timing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "include_stopped": {
                    "type": "boolean",
                    "description": "Include stopped/exited containers",
                    "default": True,
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_container_logs",
        "description": (
            "Get logs from a pipeline container. If container_id is omitted, "
            "auto-selects the best container (filtered by agent_role if given, "
            "preferring running containers)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "container_id": {
                    "type": "string",
                    "description": "Specific container ID (optional — auto-selects if omitted)",
                },
                "agent_role": {
                    "type": "string",
                    "description": "Filter by agent role (e.g. 'coder', 'tester') when auto-selecting",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return",
                    "default": 100,
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a message to an agent in a pipeline. Sent as the 'overseer' role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "to_role": {
                    "type": "string",
                    "description": "Target agent role (e.g. 'coder', 'tester', 'all')",
                },
                "body": {
                    "type": "string",
                    "description": "Message body text",
                },
                "message_type": {
                    "type": "string",
                    "description": "Message type",
                    "default": "STATUS",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional message subject",
                },
            },
            "required": ["task_id", "to_role", "body"],
        },
    },
    {
        "name": "get_consensus_status",
        "description": (
            "Get BRC consensus status for a pipeline. Shows which agents have "
            "proposed, ACKed, NACKed, or confirmed. Falls back to message-based "
            "inference when structured consensus data is unavailable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_phase",
        "description": "Get current phase details for a pipeline, including execution timing and review cycles.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_pipeline_snapshot",
        "description": (
            "Get a comprehensive pipeline snapshot combining pipeline state, "
            "phase details, containers, messages, consensus, and decisions "
            "into a single response. Replaces egg-pipeline-watch --once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "include_messages": {
                    "type": "boolean",
                    "description": "Include recent messages",
                    "default": True,
                },
                "include_containers": {
                    "type": "boolean",
                    "description": "Include container list",
                    "default": True,
                },
            },
            "required": ["task_id"],
        },
    },
    # --- Gateway-backed tools ---
    {
        "name": "list_checkpoints",
        "description": (
            "List agent checkpoints (transcripts, tool calls, token usage). "
            "Filter by issue, pipeline, agent_type, phase, or status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "integer",
                    "description": "Filter by GitHub issue number",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Filter by pipeline ID",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Filter by agent type (coder, tester, documenter, reviewer)",
                },
                "phase": {
                    "type": "string",
                    "description": "Filter by pipeline phase",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by session status",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum checkpoints to return",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "search_checkpoints",
        "description": (
            "Search checkpoint metadata for matching text. Searches agent_type, "
            "pipeline_phase, pipeline_id, branch, repo, and status fields. "
            "Note: full-text transcript search is not supported — this searches metadata only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to search for in checkpoint metadata",
                },
                "issue": {
                    "type": "integer",
                    "description": "Filter by GitHub issue number",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Filter by pipeline ID",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Filter by agent type",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum checkpoints to search",
                    "default": 10,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_contract",
        "description": (
            "Get the SDLC contract state for a pipeline. Provide either "
            "issue_number directly or task_id to look it up."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID (used to look up issue_number if not provided)",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number",
                },
            },
        },
    },
    {
        "name": "validate_config",
        "description": "Validate a pipeline configuration without creating a pipeline. Returns validation results including any errors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "description": 'Pipeline configuration to validate (e.g. {"start_phase": "implement", "hitl_gates": false})',
                },
            },
            "required": ["config"],
        },
    },
]


class PipelineToolHandler:
    """Handles MCP tool calls by proxying to orchestrator APIs."""

    def __init__(
        self,
        orchestrator_url: str = "http://localhost:9849",
        gateway_url: str | None = None,
    ):
        self.orchestrator_url = orchestrator_url
        self.gateway_url = gateway_url or os.environ.get(
            "GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}"
        )
        self._gateway_session_token: str | None = None

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
            "check_health": self._handle_check_health,
            "list_containers": self._handle_list_containers,
            "get_container_logs": self._handle_get_container_logs,
            "send_message": self._handle_send_message,
            "get_consensus_status": self._handle_get_consensus_status,
            "get_phase": self._handle_get_phase,
            "get_pipeline_snapshot": self._handle_get_pipeline_snapshot,
            "list_checkpoints": self._handle_list_checkpoints,
            "search_checkpoints": self._handle_search_checkpoints,
            "get_contract": self._handle_get_contract,
            "validate_config": self._handle_validate_config,
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
        """Create an SDLC pipeline."""
        import json
        from urllib.error import HTTPError

        data: dict[str, Any] = {}
        if args.get("issue_number"):
            data["issue_number"] = args["issue_number"]
            data["branch"] = args.get("branch") or f"egg/issue-{args['issue_number']}"
        else:
            data["prompt"] = args["description"]
        if args.get("repo"):
            data["repo"] = args["repo"]
        if args.get("config"):
            config = args["config"]
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError as e:
                    return {"error": f"Invalid config JSON: {e}"}
            data["config"] = config

        try:
            result = self._make_request("/api/v1/pipelines", method="POST", data=data)
        except HTTPError as e:
            if e.code == 409:
                # Parse enriched 409 body with existing pipeline details
                error_info: dict[str, Any] = {"error": "Pipeline already exists"}
                try:
                    body = json.loads(e.read().decode())
                    error_info["error"] = body.get("message", error_info["error"])
                    details = body.get("details", {})
                    if details:
                        error_info["existing_pipeline_id"] = details.get("existing_pipeline_id", "")
                        error_info["existing_status"] = details.get("existing_status", "")
                        error_info["existing_phase"] = details.get("existing_phase", "")
                except Exception:
                    pass
                return error_info
            raise

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

    def _handle_validate_config(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate a pipeline configuration without creating a pipeline."""
        import json

        from models import PipelineConfig
        from pydantic import ValidationError

        config = args.get("config", {})
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as e:
                return {
                    "valid": False,
                    "errors": [{"field": "config", "message": f"Invalid JSON: {e}"}],
                }

        try:
            validated = PipelineConfig.model_validate(config)
            return {
                "valid": True,
                "config": validated.model_dump(mode="json"),
            }
        except ValidationError as e:
            return {
                "valid": False,
                "errors": [
                    {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
                    for err in e.errors()
                ],
            }

    def _handle_get_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get enriched pipeline status.

        Fetches pipeline state, agent executions, decisions, and recent messages.
        For phase_gate decisions, includes draft document content so the
        caller can present it to the user without needing filesystem access.
        Falls back gracefully if messages fail.
        """
        task_id = quote(args["task_id"], safe="")

        # Primary: pipeline state
        pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
        pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})

        # Build status from pipeline data
        status: dict[str, Any] = {
            "current_phase": pipeline_data.get("current_phase", ""),
            "status": pipeline_data.get("status", ""),
            "pipeline": {
                "id": pipeline_data.get("id", ""),
                "repo": pipeline_data.get("repo", ""),
                "issue_number": pipeline_data.get("issue_number"),
                "created_at": pipeline_data.get("created_at", ""),
            },
        }

        # Extract agent info from phases
        phases = pipeline_data.get("phases", {})
        current_phase_key = pipeline_data.get("current_phase", "")
        phase_data = phases.get(current_phase_key, {})
        agents = phase_data.get("agents", [])
        status["running_agents"] = [a for a in agents if a.get("status") == "running"]
        status["completed_agents"] = [a for a in agents if a.get("status") == "complete"]

        # Extract decisions
        decisions = pipeline_data.get("decisions", [])
        status["pending_decisions"] = [d for d in decisions if d.get("status") == "pending"]

        # Enrichment: recent messages (optional)
        try:
            messages_result = self._make_request(f"/api/v1/pipelines/{task_id}/messages?limit=10")
            raw_messages = messages_result.get("data", {}).get("messages", [])
            status["recent_messages"] = [
                {
                    "from_role": m.get("from_role", ""),
                    "type": m.get("message_type", ""),
                    "subject": m.get("subject", ""),
                    "timestamp": m.get("timestamp", ""),
                }
                for m in raw_messages
            ]
        except Exception:
            logger.debug("Failed to fetch messages", task_id=task_id)

        # Enrichment: attach draft content to phase_gate decisions (optional)
        raw_task_id = args["task_id"]
        self._enrich_phase_gate_decisions(status, raw_task_id, pipeline_data)

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

        # Resolve worktree path once (invariant across decisions)
        worktree_path = None
        _read_phase_draft = None
        if repo:
            try:
                from orchestrator.routes import resolve_worktree_path
                from orchestrator.routes.pipelines import _read_phase_draft

                env_path = os.environ.get("EGG_REPO_PATH", "/home/egg/repos")
                base_path = Path(env_path)
                repo_name = repo.split("/")[-1]
                repo_path = (
                    base_path / repo_name if not (base_path / ".git").exists() else base_path
                )
                worktree_path = resolve_worktree_path(pipeline_id, repo_path)
            except Exception:
                logger.debug(
                    "Failed to resolve worktree for phase_gate enrichment",
                    pipeline_id=pipeline_id,
                )

        # Attach enrichments per-decision
        for decision in phase_gates:
            draft_content = None
            if worktree_path is not None and _read_phase_draft is not None:
                try:
                    decision_phase = decision.get("phase") or current_phase
                    draft_content = _read_phase_draft(
                        worktree_path,
                        decision_phase,
                        issue_number=issue_number,
                        pipeline_id=pipeline_id,
                        max_chars=16_000,
                    )
                except Exception:
                    logger.debug(
                        "Failed to read draft for phase_gate enrichment",
                        pipeline_id=pipeline_id,
                    )

            if draft_content is not None:
                decision["draft_content"] = draft_content
            if agents_summary:
                decision["completed_agents_summary"] = agents_summary

            # Attach reviewer feedback from .egg-state/reviews/
            reviewer_feedback = self._read_reviewer_feedback(
                worktree_path,
                decision.get("phase") or current_phase,
                issue_number,
                pipeline_id,
            )
            if reviewer_feedback:
                decision["reviewer_feedback"] = reviewer_feedback

    def _read_reviewer_feedback(
        self,
        worktree_path: Path | None,
        phase: str,
        issue_number: int | None,
        pipeline_id: str,
        max_chars: int = 16_000,
    ) -> list[dict[str, str]]:
        """Read reviewer feedback from .egg-state/reviews/ for a given phase.

        Returns a list of dicts with reviewer, verdict, summary, analysis, suggestions,
        and feedback fields. Caps total content at max_chars.
        """
        if worktree_path is None:
            return []

        reviews_dir = worktree_path / ".egg-state" / "reviews"
        if not reviews_dir.is_dir():
            return []

        try:
            from orchestrator.routes.pipelines import _pipeline_identifier
        except ImportError:
            return []

        identifier = _pipeline_identifier(issue_number, pipeline_id)
        prefix = f"{identifier}-{phase}-"

        feedback: list[dict[str, str]] = []
        total_chars = 0

        try:
            review_files = sorted(reviews_dir.glob(f"{prefix}*-review.json"))
        except Exception:
            return []

        import json

        for i, review_file in enumerate(review_files):
            try:
                data = json.loads(review_file.read_text(encoding="utf-8"))
                # Extract reviewer type from filename:
                # e.g. "42-refine-refiner-review.json" -> "refiner"
                stem = review_file.stem  # "42-refine-refiner-review"
                stem = stem.removesuffix("-review")
                reviewer_type = stem.removeprefix(f"{identifier}-{phase}-")

                entry = {
                    "reviewer": reviewer_type,
                    "verdict": data.get("verdict", "unknown"),
                    "summary": data.get("summary", ""),
                    "analysis": data.get("analysis", ""),
                    "suggestions": data.get("suggestions", ""),
                    "feedback": data.get("feedback", ""),
                }

                entry_chars = sum(len(v) for v in entry.values())
                if total_chars + entry_chars > max_chars:
                    remaining = len(review_files) - i
                    feedback.append(
                        {
                            "reviewer": f"({remaining} more reviewer(s) omitted)",
                            "verdict": "truncated",
                            "summary": "Content limit reached. Review files directly.",
                            "analysis": "",
                            "suggestions": "",
                            "feedback": "",
                        }
                    )
                    break
                total_chars += entry_chars
                feedback.append(entry)
            except Exception:
                logger.debug(
                    "Failed to read review file",
                    path=str(review_file),
                )
                continue

        return feedback

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
            # Delete pipeline state so the issue can be resubmitted.
            # The DELETE endpoint cleans up containers, remote branches,
            # Redis messages, and the state file.
            cleaned_up: list[str] = []
            try:
                self._make_request(
                    f"/api/v1/pipelines/{task_id}",
                    method="DELETE",
                    timeout=120,
                )
                cleaned_up = ["pipeline_state", "containers", "worktrees", "messages"]
            except Exception as e:
                logger.warning(
                    "Cleanup after cancel failed",
                    task_id=task_id,
                    error=str(e),
                )
            return {
                "cancelled": True,
                "cleaned_up": cleaned_up,
                "message": "Pipeline cancelled and cleaned up"
                if cleaned_up
                else "Pipeline cancelled but cleanup failed",
            }

        return result

    # --- Gateway request infrastructure ---

    def _get_gateway_client(self, **kwargs: Any) -> "GatewayClient":  # noqa: F821
        """Create a GatewayClient from the configured gateway URL.

        Extra kwargs are forwarded to the GatewayClient constructor
        (e.g. launcher_secret).
        """
        from urllib.parse import urlparse

        from orchestrator.gateway_client import GatewayClient

        parsed = urlparse(self.gateway_url)
        host = parsed.hostname or "egg-gateway"
        port = parsed.port or GATEWAY_PORT
        return GatewayClient(gateway_host=host, gateway_port=port, **kwargs)

    def _ensure_gateway_session(self) -> str:
        """Ensure we have a valid gateway session token, creating one if needed."""
        if self._gateway_session_token:
            return self._gateway_session_token

        launcher_secret = os.environ.get("EGG_LAUNCHER_SECRET")
        if not launcher_secret:
            raise RuntimeError("EGG_LAUNCHER_SECRET required for gateway session registration")

        client = self._get_gateway_client(launcher_secret=launcher_secret)
        session = client.register_session(
            container_id="mcp-server",
            container_ip=client.self_ip,
            mode="public",
        )
        self._gateway_session_token = session.session_token
        return session.session_token

    def _make_gateway_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Make HTTP request to the gateway with session auth.

        Automatically registers a session if needed and retries once on 401.
        """
        import json
        from urllib.error import HTTPError
        from urllib.request import ProxyHandler, Request, build_opener

        def _do_request(token: str) -> dict[str, Any]:
            url = f"{self.gateway_url}{endpoint}"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            }
            body = json.dumps(data).encode() if data else None
            opener = build_opener(ProxyHandler({}))
            req = Request(url, data=body, headers=headers, method=method)
            with opener.open(req, timeout=timeout) as response:
                return json.loads(response.read().decode())

        token = self._ensure_gateway_session()
        try:
            return _do_request(token)
        except HTTPError as e:
            if e.code == 401:
                # Session expired — clear cache and retry once
                self._gateway_session_token = None
                token = self._ensure_gateway_session()
                return _do_request(token)
            raise

    # --- Orchestrator-backed tools ---

    def _handle_check_health(self, args: dict[str, Any]) -> dict[str, Any]:
        """Check orchestrator and gateway health."""
        result: dict[str, Any] = {}

        # Orchestrator health
        try:
            orch = self._make_request("/api/v1/health")
            result["orchestrator"] = {
                "healthy": orch.get("status") == "healthy",
                "status": orch.get("status", "unknown"),
            }
        except Exception as e:
            result["orchestrator"] = {"healthy": False, "status": "unreachable", "error": str(e)}

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
            }
        except Exception as e:
            result["gateway"] = {"healthy": False, "status": "unreachable", "error": str(e)}

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
            containers_result = self._make_request(
                f"/api/v1/pipelines/{task_id}/containers?all=true"
            )
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

    def _handle_get_consensus_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get consensus status for a pipeline's current phase."""
        task_id = quote(args["task_id"], safe="")

        result: dict[str, Any] = {}

        # Get pipeline base info
        pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
        pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})
        result["pipeline_id"] = pipeline_data.get("id", "")
        result["current_phase"] = pipeline_data.get("current_phase", "")
        result["status"] = pipeline_data.get("status", "")

        # Try to get structured consensus from status endpoint
        try:
            status_result = self._make_request(f"/api/v1/pipelines/{task_id}/status")
            concurrent = status_result.get("data", {}).get("concurrent", {})
        except Exception:
            concurrent = {}

        consensus = concurrent.get("consensus", {})

        if consensus and consensus.get("agents"):
            result["consensus"] = {
                "is_complete": consensus.get("is_complete", False),
                "blocking_agents": consensus.get("blocking_agents", []),
                "has_unresolved_nacks": consensus.get("has_unresolved_nacks", False),
                "unresolved_nacks": consensus.get("unresolved_nacks", []),
                "agents": consensus.get("agents", {}),
            }
        else:
            # Fall back to message-based inference
            try:
                messages_result = self._make_request(
                    f"/api/v1/pipelines/{task_id}/messages?limit=50"
                )
                messages = messages_result.get("data", {}).get("messages", [])
                result["consensus"] = self._infer_consensus_from_messages(messages)
                result["consensus"]["note"] = (
                    "Inferred from messages — structured consensus data not available"
                )
            except Exception:
                result["consensus"] = {"error": "Could not retrieve consensus data"}

        return result

    def _infer_consensus_from_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Infer consensus state from message history.

        Note: uses last-write-wins semantics, so messages must be in
        chronological order (as returned by the orchestrator messages endpoint).
        """
        roles: dict[str, str] = {}  # role -> last consensus message type
        nacks: dict[str, dict[str, str]] = {}  # key -> {reviewer, producer, reason}

        for msg in messages:
            msg_type = msg.get("message_type", "")
            from_role = msg.get("from_role", "")

            if msg_type == "CONSENSUS_CONFIRMED":
                roles[from_role] = "confirmed"
            elif msg_type == "CONSENSUS_PROPOSE":
                roles[from_role] = "proposed"
                # Clear NACKs targeting this producer
                nacks = {k: v for k, v in nacks.items() if not k.endswith(f"->{from_role}")}
            elif msg_type == "CONSENSUS_ACK":
                if from_role not in roles or roles[from_role] != "confirmed":
                    roles[from_role] = "acked"
            elif msg_type == "CONSENSUS_NACK":
                to_role = msg.get("to_role", "unknown")
                nacks[f"{from_role}->{to_role}"] = {
                    "reviewer": from_role,
                    "producer": to_role,
                    "reason": msg.get("body", "") or msg.get("subject", ""),
                }

        confirmed = [r for r, s in roles.items() if s == "confirmed"]
        blocking = [r for r, s in roles.items() if s != "confirmed"]

        return {
            "is_complete": len(blocking) == 0 and len(confirmed) > 0,
            "confirmed_agents": confirmed,
            "blocking_agents": blocking,
            "has_unresolved_nacks": len(nacks) > 0,
            "unresolved_nacks": list(nacks.values()),
        }

    def _handle_get_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get current phase details for a pipeline."""
        task_id = quote(args["task_id"], safe="")
        return self._make_request(f"/api/v1/pipelines/{task_id}/phase")

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
                messages_result = self._make_request(
                    f"/api/v1/pipelines/{task_id}/messages?limit=20"
                )
                snapshot["recent_messages"] = messages_result.get("data", {}).get("messages", [])
            except Exception:
                pass

        # Decisions
        decisions = pipeline_data.get("decisions", [])
        snapshot["pending_decisions"] = [d for d in decisions if d.get("status") == "pending"]

        return snapshot

    # --- Gateway-backed tools ---

    def _handle_list_checkpoints(self, args: dict[str, Any]) -> dict[str, Any]:
        """List checkpoints with optional filters."""
        params = []
        for key in ("issue", "pipeline", "agent_type", "phase", "status"):
            if args.get(key) is not None:
                params.append(f"{key}={quote(str(args[key]), safe='')}")
        limit = args.get("limit", 20)
        params.append(f"limit={limit}")

        query = "&".join(params)
        return self._make_gateway_request(f"/api/v1/checkpoints?{query}")

    def _handle_search_checkpoints(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search checkpoints by text in metadata/summaries."""
        params = []
        for key in ("issue", "pipeline", "agent_type"):
            if args.get(key) is not None:
                params.append(f"{key}={quote(str(args[key]), safe='')}")
        limit = args.get("limit", 10)
        params.append(f"limit={limit}")

        query = "&".join(params)
        result = self._make_gateway_request(f"/api/v1/checkpoints?{query}")

        # Client-side text filter on checkpoint metadata
        search_text = args["text"].lower()
        checkpoints = result.get("data", {}).get("checkpoints", [])
        filtered = []
        for cp in checkpoints:
            searchable = " ".join(
                str(cp.get(f, ""))
                for f in (
                    "session_id",
                    "agent_type",
                    "pipeline_phase",
                    "pipeline_id",
                    "branch",
                    "repo",
                    "session_status",
                )
            ).lower()
            if search_text in searchable:
                filtered.append(cp)

        return {
            "checkpoints": filtered,
            "total": len(filtered),
            "note": "Searched checkpoint metadata only — full-text transcript search not supported via this tool",
        }

    def _handle_get_contract(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get SDLC contract state."""
        issue_number = args.get("issue_number")

        if not issue_number and args.get("task_id"):
            # Look up issue_number from pipeline data
            task_id = quote(args["task_id"], safe="")
            pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
            issue_number = pipeline_result.get("data", {}).get("pipeline", {}).get("issue_number")

        if not issue_number:
            return {"error": "Either issue_number or task_id (with linked issue) is required"}

        return self._make_gateway_request(f"/api/v1/contract/{int(issue_number)}")
