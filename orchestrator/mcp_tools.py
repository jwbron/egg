"""
MCP tool definitions for pipeline management.

Provides tool schemas and handlers that proxy to orchestrator APIs,
enabling external Claude Code sessions to manage SDLC pipelines
via the MCP protocol.
"""

import os
import re
import sys
from datetime import UTC, datetime
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


def _is_timeout_error(exc: BaseException) -> bool:
    """Check if an OSError/URLError is a timeout.

    On Python 3.14, ``socket.timeout`` is an alias for ``TimeoutError`` so
    the explicit check is redundant — but it's kept to keep the call sites
    obvious and to remain robust against urllib wrapping the real timeout
    in ``URLError.reason``.
    """
    import socket

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if hasattr(exc, "reason") and isinstance(exc.reason, (TimeoutError, socket.timeout)):
        return True
    return False


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
                "base_branch": {
                    "type": "string",
                    "description": "Base branch for PR creation (optional). Defaults to the repo's default branch if not specified.",
                },
                "config": {
                    "type": "object",
                    "description": 'Optional pipeline configuration overrides (e.g. {"start_phase": "implement", "hitl_gates": false})',
                },
                "analysis": {
                    "type": "string",
                    "description": "Pre-generated analysis markdown (optional, used with start_phase: implement to seed the contract)",
                },
                "plan": {
                    "type": "string",
                    "description": "Pre-generated plan markdown with yaml-tasks appendix (optional, used with start_phase: implement to populate the contract with tasks)",
                },
                "jira_ticket": {
                    "type": "string",
                    "description": "JIRA ticket ID (e.g. KORE-1234). Used as the pipeline ID and branch name.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "fresh", "reassess"],
                    "description": (
                        "Epic-mode override (issue #1557). Default 'auto' — the "
                        "orchestrator fetches the ticket and treats it as an "
                        "epic when issuetype is 'Epic', then picks "
                        "'reassess' if the epic already has children else "
                        "'fresh'. 'fresh' forces the all-net-new path even "
                        "if children exist (logs a warning). 'reassess' "
                        "forces the classify-existing-children path; "
                        "rejected with HTTP 400 when the ticket isn't an "
                        "epic. Only meaningful with jira_ticket; ignored "
                        "for GitHub-issue submissions."
                    ),
                },
                "qualifier": {
                    "type": "string",
                    "description": "Optional qualifier suffix for the pipeline/branch (e.g. 'backend'). Enables multiple pipelines per ticket/issue.",
                },
                "source_branch": {
                    "type": "string",
                    "description": "Source branch to read prior-run artifacts from (plan, analysis). "
                    "When set, the orchestrator reads drafts from this branch via git show "
                    "instead of requiring inline content. Inline plan/analysis values take precedence.",
                },
                "source_artifact_prefix": {
                    "type": "string",
                    "description": "Explicit prefix for draft filenames on the source branch "
                    "(e.g. 'issue-1570-v3'). Overrides the default pipeline_id-based "
                    "prefix when reading artifacts. Only used with source_branch.",
                },
            },
            "required": ["description", "repo"],
        },
    },
    {
        "name": "run_agent_task",
        "description": (
            "Run a single SDLC phase (refine / plan / implement) against a "
            "repo with a user-chosen subset of that phase's roles (#1762). "
            "Creates a pipeline in CUSTOM mode: the pipeline terminates "
            "after the selected phase reaches CONSENSUS_REACHED — no "
            "auto-advance. Degenerate rosters (one producer, zero "
            "reviewers) short-circuit BRC via "
            "ApprovalMatrix.is_fully_acked(). "
            "Reviewer-only rosters are rejected with HTTP 400 "
            "(`details.reason=='reviewer_only_roster'`). Cross-phase roles "
            "(overseer / autofixer / conflict_resolver / inspector) are "
            "rejected with HTTP 400 "
            "(`details.reason=='cross_phase_role'`). When pr_number is "
            "supplied, CUSTOM subsumes BABYSIT's PR-diff-aware semantics "
            "(per-role staging branches, head-move guard). When no branch "
            "is supplied AND no pr_number is supplied, the pipeline auto-"
            "generates `egg/custom-<pipeline_id>` so callers can always "
            "retrieve artifacts via `git show`. HITL gates follow "
            "`config.hitl_gates` (default True, parity with submit_task)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["refine", "plan", "implement"],
                    "description": "Pipeline phase to run (required).",
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Subset of the phase's roles to spawn. Must contain at "
                        "least one producer role. When omitted or null, the full "
                        "default roster for the phase is used."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": "Repository to run against, in owner/name format.",
                },
                "description": {
                    "type": "string",
                    "description": "Free-form natural-language task description.",
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Override branch. When omitted AND no pr_number is supplied, "
                        "auto-generates `egg/custom-<pipeline_id>`."
                    ),
                },
                "base_branch": {
                    "type": "string",
                    "description": "Base branch for comparisons (optional).",
                },
                "pr_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "When supplied, the CUSTOM pipeline subsumes BABYSIT — "
                        "PR preflight runs, branch/base are auto-populated from "
                        "the PR, and per-role staging branches "
                        "(`egg/babysit-pr/<pr>/<sha>/<role>`) are used."
                    ),
                },
                "issue_number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional issue number to associate with the pipeline. "
                        "Contract-file keying uses pipeline_id (not issue_number) "
                        "for CUSTOM pipelines to avoid colliding with a "
                        "concurrent ISSUE-mode pipeline on the same issue."
                    ),
                },
                "analysis": {
                    "type": "string",
                    "description": "Pre-populated analysis draft (optional, seeds refine output).",
                },
                "plan": {
                    "type": "string",
                    "description": "Pre-populated plan draft (optional, seeds plan output).",
                },
                "qualifier": {
                    "type": "string",
                    "description": (
                        "Optional qualifier suffix (`[a-z0-9]+(-[a-z0-9]+)*`). "
                        "Disambiguates the pipeline_id when multiple CUSTOM runs "
                        "target the same issue / PR."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        'Optional pipeline configuration overrides (e.g. {"hitl_gates": false}).'
                    ),
                },
            },
            "required": ["phase", "repo", "description"],
        },
    },
    {
        "name": "babysit_pr",
        "description": (
            "Run a one-off implement-phase BRC (Broadcast-Review-Converge) "
            "cycle against a PR's diff. Creates a pipeline in BABYSIT mode: "
            "no SDLC contract is created, reviewer_contract is excluded from "
            "the roster, each cycle is isolated on per-role staging branches, "
            "and the PR head is guarded against concurrent updates. The PR "
            "must be open, non-fork, and have a non-empty diff — merged, "
            "closed, fork, or empty-diff PRs are refused up-front. Pipeline "
            "ID defaults to 'pr-<pr_number>'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": (
                        "Required. GitHub PR number to babysit (must be open, non-fork, "
                        "non-empty). Intentionally omitted from the schema's `required` "
                        "list so the handler can return a structured "
                        '`{"error": "pr_number must be a positive integer"}` envelope '
                        "when it is missing or non-positive, rather than Pydantic "
                        'raising a generic "Field required" — see #2665.'
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": "Repository to run against, in owner/name format (e.g. 'myorg/myrepo').",
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Override PR head branch (optional). Auto-populated from the "
                        "PR's head_ref when omitted."
                    ),
                },
                "base_branch": {
                    "type": "string",
                    "description": (
                        "Override PR base branch (optional). Auto-populated from the "
                        "PR's base_ref when omitted."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": 'Optional pipeline configuration overrides (e.g. {"hitl_gates": false}).',
                },
            },
            "required": ["repo"],
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
                "wait": {
                    "type": "number",
                    "description": (
                        "Seconds to wait before fetching status. Use this for "
                        "polling delays instead of external sleep commands. "
                        "Capped at 25 seconds to stay under Claude Code's "
                        "streamable-HTTP MCP tool-call timeout."
                    ),
                    "default": 0,
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
        "description": "Cancel a pipeline task. With cleanup=false (default) the pipeline state is preserved and can be resumed later via restart_phase or restart_agent. Use cleanup=true to also delete pipeline state, allowing the same issue to be resubmitted.",
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
        "description": (
            "Check health of the orchestrator and gateway services. Returns combined status "
            "plus per-service readiness history: `healthy_since` (ISO timestamp of the last "
            "transition to healthy, or process start if never unhealthy this run), "
            "`last_unhealthy_at` (most recent unhealthy observation, null if none), and a "
            "bounded `recent_transitions` list. Use this to diagnose readiness flapping or "
            "recently-started services when race-style failures happen."
        ),
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
                "repo": {
                    "type": "string",
                    "description": "Checkpoint repository in owner/repo format, e.g. owner/repo-checkpoints",
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
                "repo": {
                    "type": "string",
                    "description": "Checkpoint repository in owner/repo format, e.g. owner/repo-checkpoints",
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
    {
        "name": "restart_agent",
        "description": (
            "Restart a single agent in a pipeline. Stops the existing container, "
            "resets its consensus state, and respawns it with the same configuration. "
            "The agent's worktree is preserved so committed work is retained. "
            "Works on pipelines in running, awaiting-human, failed, or cancelled "
            "state (cancelled pipelines come from cancel_task with cleanup=false)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "agent_role": {
                    "type": "string",
                    "description": "Role of the agent to restart (e.g. 'coder', 'tester')",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for restarting the agent",
                },
            },
            "required": ["task_id", "agent_role"],
        },
    },
    {
        "name": "restart_phase",
        "description": (
            "Restart all agents in a pipeline phase. Stops all phase containers, "
            "resets consensus and review cycle state, and respawns all agents. "
            "Prior phase artifacts are preserved. Works on pipelines in running, "
            "awaiting-human, failed, or cancelled state (cancelled pipelines come "
            "from cancel_task with cleanup=false)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "phase": {
                    "type": "string",
                    "description": "Phase to restart (e.g. 'implement', 'plan')",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for restarting the phase",
                },
            },
            "required": ["task_id", "phase"],
        },
    },
    # --- Salvage tools (#2429) ---
    {
        "name": "list_agent_local_commits",
        "description": (
            "List unpushed commits sitting in this pipeline's per-agent worktrees. "
            "Use to triage whether wedged agents have local work that would be "
            "lost on cleanup — for example after a gateway branch-allowlist "
            "rejection (#2428) or a restart-reconciliation false-failure (#2411). "
            "Read-only: inspects each worktree's git log against "
            "origin/<assigned_branch> (with origin/<base_branch> fallback) and "
            "reports commits that are not yet on the remote. No fetch, no push. "
            "Pair with `salvage_agent_commits` to push the work to a recovery ref."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "agent_role": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict to a single agent role (e.g. 'coder'). "
                        "Omit to enumerate every per-agent worktree for the pipeline."
                    ),
                },
                "slice_id": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict to a single slice scope "
                        "(e.g. 'slice-2'). Omit to include all scopes."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "salvage_agent_commits",
        "description": (
            "Push unpushed agent commits to recovery refs under "
            "egg/recovered/<pipeline>/<scope>/<short_sha>. Authenticates with the "
            "orchestrator's launcher secret, which bypasses the agent-targeted "
            "branch-allowlist check that rejected the original push — so this "
            "works to recover work even when the agent's own pushes were the "
            "thing that wedged. The recovery ref name embeds the HEAD SHA so "
            "re-salvages produce immutable refs instead of force-overwriting "
            "earlier ones. After salvage, fetch and cherry-pick onto the "
            "intended branch. Always returns per-worktree results — failure of "
            "one worktree never blocks others."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "agent_role": {
                    "type": "string",
                    "description": (
                        "Optional. Salvage only this agent role's worktree "
                        "(e.g. 'coder'). Omit to salvage every per-agent "
                        "worktree for the pipeline."
                    ),
                },
                "slice_id": {
                    "type": "string",
                    "description": ("Optional. Salvage only this slice scope (e.g. 'slice-2')."),
                },
            },
            "required": ["task_id"],
        },
    },
    # --- Phase management tools ---
    {
        "name": "advance_phase",
        "description": (
            "Transition a pipeline from its current phase to target_phase. "
            "Mutates: marks the current phase_execution COMPLETE with a "
            "completed_at timestamp, sets pipeline.current_phase = target_phase, "
            "marks the target phase_execution RUNNING with started_at/"
            "work_started_at timestamps, sets pipeline.status = RUNNING, bumps "
            "pipeline.run_epoch, and launches a fresh _run_pipeline driver "
            "thread that will spawn agents for the new phase. When advancing "
            "out of plan, automatically runs populate_contract to write the "
            "SDLC contract from the plan draft (#1941); failures warn and "
            "continue so the advance hammer is not blocked. Preconditions "
            "(force=false): target_phase must be a valid transition from the "
            "current phase (else 400); the current phase_execution.status must "
            "be COMPLETE or PENDING (else 400 — not 409); PHASE_COMPLETE "
            "health checks must not return FAIL_PIPELINE (else 409, with "
            "health_results in details). When force=true, skips transition "
            "validation, the phase-status check, and health-check gating, "
            "and first stops any running containers for the pipeline so their "
            "SIGTERM does not cascade into the new phase. Response data "
            "includes previous_phase and current_phase.\n\n"
            "Error responses include a machine-readable `reason` code (#1939). "
            "Note: reason codes are only visible to direct HTTP callers; the "
            "MCP handler layer does not yet surface them.\n"
            "- `missing_target_phase` (400) — request body omitted target_phase\n"
            "- `invalid_phase` (400) — target_phase is not a known phase value\n"
            "- `invalid_phase_transition` (400) — target is not a valid next "
            "phase for the current phase (fix: change target or pass force=true)\n"
            "- `previous_phase_not_complete` (400) — current phase is still "
            "running or failed (fix: complete_phase first, or pass force=true)\n"
            "- `health_checks_failed` (409) — Tier 1/2 health checks returned "
            "FAIL_PIPELINE (fix: resolve underlying health issue, or pass "
            "force=true; `details.health_results` lists the failed checks)\n"
            "- `version_conflict` (409) — concurrent modification; retry\n"
            "- `invalid_pipeline_id` (400), `pipeline_not_found` (404)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "target_phase": {
                    "type": "string",
                    "description": "Target phase to advance to (e.g. 'plan', 'implement', 'pr')",
                },
                "force": {
                    "type": "boolean",
                    "description": "Skip transition validation, phase-status check, and health-check gating. Also stops running pipeline containers before advancing so their SIGTERM does not cascade.",
                    "default": False,
                },
            },
            "required": ["task_id", "target_phase"],
        },
    },
    {
        "name": "start_pipeline",
        "description": (
            "Recover a non-RUNNING pipeline by calling "
            "``POST /api/v1/pipelines/{id}/start``.  Targets pipeline-level "
            "state — distinct from ``start_phase``, which only flips the "
            "current phase.  Intended for the FAILED + RUNNING-phase combo "
            "that startup reconciliation can produce (#2411): the route "
            "resets the failed phase to PENDING (clears ``containers``, "
            "``agents``, ``artifacts``), bumps ``run_epoch``, sets "
            "``pipeline.status = RUNNING``, and re-launches the "
            "``_run_pipeline`` thread.  Also handles AWAITING_HUMAN "
            "recovery when all decisions are resolved, and starts PENDING "
            "pipelines (no early-return for PENDING in the route).\n\n"
            "Live-pod safety guard (#2420): before the reset clears the "
            "phase's ``containers`` / ``agents`` / ``artifacts``, the "
            "route label-queries k8s for pods carrying "
            "``egg.pipeline.id=<id>``.  If any are alive, the route "
            "returns 409 with ``reason=live_pods_present`` rather than "
            "orphan them.  Pass ``force=true`` (with an optional "
            "``force_reason`` audit note) to override — typically after "
            "you've already cleaned the pods up via "
            "``cancel_task(cleanup=true)`` and want to re-run the phase "
            "from scratch.  The guard fires on both reset paths: the "
            "FAILED-recovery branch and the AWAITING_HUMAN "
            "request_changes/change_approach branch.\n\n"
            "Error responses include a machine-readable ``reason`` code "
            "(#1939). Note: reason codes are only visible to direct HTTP "
            "callers; the MCP handler layer does not yet surface them.\n"
            "- 409 — pipeline already RUNNING / COMPLETE / CANCELLED, or "
            "AWAITING_HUMAN with pending decisions\n"
            "- ``live_pods_present`` (409) — pods labeled to the pipeline "
            "are still alive; cancel them first or pass ``force=true``\n"
            "- ``live_pod_check_failed`` (409) — the label query failed; "
            "pass ``force=true`` to override after manual verification\n"
            "- ``invalid_force_reason`` (400) — force_reason must be a "
            "string\n"
            "- ``invalid_pipeline_id`` (400), ``pipeline_not_found`` (404)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Skip the live-pod orphan guard and reset the "
                        "phase even if pods labeled to the pipeline are "
                        "still alive. The override is recorded in the "
                        "orchestrator log for audit."
                    ),
                    "default": False,
                },
                "force_reason": {
                    "type": "string",
                    "description": "Audit note explaining why force=true was used",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "start_phase",
        "description": (
            "Flip the current phase's execution status to RUNNING. Mutates: "
            "sets phase_execution.status = RUNNING on pipeline.current_phase, "
            "stamps started_at and work_started_at, and sets pipeline.status = "
            "RUNNING. Does NOT spawn agents — agent spawning is driven by the "
            "_run_pipeline loop when it observes a RUNNING phase, which is "
            "already active for pipelines created through the normal submit "
            "path. Does NOT transition to the next phase — only affects "
            "pipeline.current_phase. Intended for operator recovery when a "
            "phase needs to be re-marked RUNNING (e.g. after a crash); not "
            "the way to move a completed phase forward — use advance_phase "
            "for that.\n\n"
            "Error responses include a machine-readable `reason` code (#1939). "
            "Note: reason codes are only visible to direct HTTP callers; the "
            "MCP handler layer does not yet surface them.\n"
            "- `phase_already_running` (400) — phase is already in RUNNING "
            "status (no action needed)\n"
            "- `version_conflict` (409) — concurrent modification; retry\n"
            "- `invalid_pipeline_id` (400), `pipeline_not_found` (404)"
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
        "name": "complete_phase",
        "description": (
            "Mark the current phase's execution as COMPLETE. Mutates: sets "
            "phase_execution.status = COMPLETE and stamps completed_at on "
            "pipeline.current_phase, optionally stores artifacts on that "
            "phase_execution, persists BRC history for the phase, and clears "
            "ephemeral inter-agent messaging and consensus state. Does NOT "
            "advance the pipeline — pipeline.current_phase still points at "
            "the just-completed phase afterwards; callers must invoke "
            "advance_phase to move forward. The next_phase field in the "
            "response data names the canonical next transition, not the new "
            "current_phase — current_phase is also echoed so callers can "
            "confirm it has not moved. Returns 409 when the phase still has "
            "unresolved HITL decisions; pass force=true to abandon them "
            "(abandoned ids are recorded in the phase's artifacts for "
            "audit).\n\n"
            "Error responses include a machine-readable `reason` code (#1939). "
            "Note: reason codes are only visible to direct HTTP callers; the "
            "MCP handler layer does not yet surface them.\n"
            "- `unresolved_hitl_decisions` (409) — current phase has pending "
            "HITL decisions (fix: resolve them, or pass force=true; "
            "`details.unresolved_decision_ids` lists the blocking ids)\n"
            "- `invalid_artifacts` (400) — artifacts must be a JSON object "
            "with string values\n"
            "- `invalid_force_reason` (400) — force_reason must be a string\n"
            "- `version_conflict` (409) — concurrent modification; retry\n"
            "- `invalid_pipeline_id` (400), `pipeline_not_found` (404)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "artifacts": {
                    "type": "object",
                    "description": "Optional phase artifacts to store (e.g. commit SHAs, PR URLs)",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Skip the unresolved-decision guard and force the "
                        "phase to complete. Abandoned decision ids are "
                        "written to the phase's artifacts."
                    ),
                    "default": False,
                },
                "force_reason": {
                    "type": "string",
                    "description": "Audit note explaining why force=true was used",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "populate_contract",
        "description": (
            "Populate a pipeline's SDLC contract from its plan draft. Reads the "
            "plan document, extracts task structure, and writes tasks and acceptance "
            "criteria to the contract. On the `POPULATED` outcome the route also "
            "commits the contract and pushes the work branch to origin so fresh "
            "agent spawns (restart_phase, restart_agent, post-cancel restart) pull "
            "the populated state on respawn (#2629).\n\n"
            "Success response data includes `pushed_to_origin` (bool): True only "
            "when `push_worktree_branch` reported success (a no-op fast-forward "
            "push counts; a no-op commit alone does not). False when the push "
            "failed, the commit/push step raised, or the push was not attempted "
            "(`pipeline.branch` unset, or worktree resolves to the orchestrator's "
            "repo path). When False the operator must commit and push themselves "
            "before respawning agents — otherwise agents will pull the empty "
            "contract from origin and the implement-start guard will wedge the "
            "pipeline.\n\n"
            "Error responses include a machine-readable `reason` code (#1939, #2627). "
            "Note: reason codes are only visible to direct HTTP callers; the "
            "MCP handler layer does not yet surface them.\n"
            "- `invalid_pipeline_id` (400), `pipeline_not_found` (404)\n"
            "- `draft_missing` (404), `no_draft_path` (404) — plan draft "
            "missing or worktree has no draft path configured\n"
            "- `parse_failed` (422), `empty_result` (422) — draft parse "
            "produced an error or zero tasks\n"
            "- `contract_load_failed` (500), `egg_contracts_unavailable` "
            "(500), `unexpected_exception` (500) — structured failures "
            "from inside the populate call\n"
            "- `populate_contract_failed` (500) — endpoint-level fallback "
            "for exceptions raised outside the structured populate call\n"
            'Also emits a 422 with `{error: "forest_violation", errors: '
            "[...]}` (#2137) when contract population violates task-forest "
            "invariants; this response uses `error` rather than `reason`."
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
        "name": "get_deployment_context",
        "description": (
            "Return runtime/cluster introspection for the egg deployment. "
            "Fields include runtime (kubernetes/docker), kubeconfig context, "
            "cluster_info (server_version, node count), detected CNI, "
            "network_policy_enforcement, k3s detection, and deployed image tags "
            "for orchestrator/gateway/agents. Used by deployment-diagnose and by "
            "operators to verify a fresh rollout landed on the expected image tags."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "validate_deployment_manifests",
        "description": (
            "Run static validation rules against the rendered kustomize overlay. "
            "Rules cover secret reference presence, hostPath volumes, image tags, "
            "Service selector/Deployment label match, and env-var name collisions. "
            "Returns a list of warnings so operators can catch misconfigured "
            "overlays without applying them. Only available on the Kubernetes "
            "runtime."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "overlay_path": {
                    "type": "string",
                    "description": (
                        "Optional path to a kustomize overlay (relative to the "
                        "repo root or absolute). Defaults to the active overlay."
                    ),
                },
            },
        },
    },
    {
        "name": "prune_stale_worktrees",
        "description": (
            "Remove worktree registrations and orphan directories under "
            "~/.egg-worktrees for containers that no longer exist. Proxies to "
            "the gateway where the worktree mutex lives. Defaults to dry_run=true "
            "so operators can review the plan before mutating filesystem state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "When true, report what would be removed without mutating state.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "validate_network_isolation",
        "description": (
            "Spawn a throwaway probe Job in the egg-agents namespace to verify "
            "Calico NetworkPolicy enforcement. Returns a structured "
            "{gateway_reachable, internet_blocked, agent_pods_unreachable, "
            "orchestrator_api_reachable} result. The Job self-deletes on exit "
            "(ttlSecondsAfterFinished=0). Only available on the Kubernetes "
            "runtime and on CNIs that enforce NetworkPolicies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pipeline_id": {
                    "type": "string",
                    "description": "Pipeline id for audit labels on the probe pod.",
                },
                "role": {
                    "type": "string",
                    "description": "Agent role label for the probe pod (default: coder).",
                    "default": "coder",
                },
            },
            "required": ["pipeline_id"],
        },
    },
    {
        "name": "get_service_logs",
        "description": (
            "Return logs from the gateway or orchestrator Deployment's backing "
            "pod(s). Complements `get_container_logs` (which covers agent-sandbox "
            "containers) so operators can cross-reference gateway-side spawn "
            "failures — 'Connection refused', 'Remote end closed connection', "
            "'push_worktree_branch returned False' — without shelling into the "
            "cluster. Only available on the Kubernetes runtime."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Which service's logs to return.",
                    "enum": ["gateway", "orchestrator"],
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 100).",
                    "default": 100,
                },
                "since_seconds": {
                    "type": "integer",
                    "description": (
                        "Only return logs newer than this many seconds — useful for "
                        "scoping to 'logs around when my pipeline failed at HH:MM'."
                    ),
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "rebuild_and_rollout",
        "description": (
            "Kick off `make redeploy` (build image → k3s ctr images import → "
            "kubectl rollout restart). Returns a progress-stream id immediately "
            "because the underlying work exceeds the MCP tool-call budget. "
            "Callers poll /api/v1/deployment/rebuild-and-rollout/streams/{id} "
            "for progress or pass wait=true to block until the terminal record. "
            "Rejects concurrent invocations with `rollout_already_in_progress`. "
            "Only available on the Kubernetes runtime."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "boolean",
                    "description": (
                        "When true, the handler long-polls the progress stream "
                        "and returns the terminal record (exit_code, "
                        "rolled_out_images). When false (default), returns the "
                        "stream id for caller-driven polling."
                    ),
                    "default": False,
                },
            },
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
            "run_agent_task": self._handle_run_agent_task,
            "babysit_pr": self._handle_babysit_pr,
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
            "restart_agent": self._handle_restart_agent,
            "restart_phase": self._handle_restart_phase,
            "list_agent_local_commits": self._handle_list_agent_local_commits,
            "salvage_agent_commits": self._handle_salvage_agent_commits,
            "advance_phase": self._handle_advance_phase,
            "start_pipeline": self._handle_start_pipeline,
            "start_phase": self._handle_start_phase,
            "complete_phase": self._handle_complete_phase,
            "populate_contract": self._handle_populate_contract,
            "get_deployment_context": self._handle_get_deployment_context,
            "validate_deployment_manifests": self._handle_validate_deployment_manifests,
            "prune_stale_worktrees": self._handle_prune_stale_worktrees,
            "validate_network_isolation": self._handle_validate_network_isolation,
            "rebuild_and_rollout": self._handle_rebuild_and_rollout,
            "get_service_logs": self._handle_get_service_logs,
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
        """Make HTTP request to orchestrator.

        Always attaches ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` and
        ``X-Egg-Source: mcp`` when the secret is configured. The in-process
        MCP server runs inside the orchestrator Deployment, so it reads the
        same env var as the lifecycle-secret decorator.
        """
        import json
        from urllib.request import ProxyHandler, Request, build_opener

        url = f"{self.orchestrator_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        lifecycle_secret = os.environ.get("EGG_LIFECYCLE_SECRET")
        if lifecycle_secret:
            headers["Authorization"] = f"Bearer {lifecycle_secret}"
            headers["X-Egg-Source"] = "mcp"
        # Always send a JSON body for non-GET requests: Content-Type:
        # application/json with an empty body makes Flask's get_json() raise
        # BadRequest(400). See #1787.
        if method == "GET":
            body = json.dumps(data).encode() if data else None
        else:
            body = json.dumps(data if data is not None else {}).encode()

        opener = build_opener(ProxyHandler({}))
        req = Request(url, data=body, headers=headers, method=method)

        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode())

    def _handle_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create an SDLC pipeline."""
        import json
        from urllib.error import HTTPError

        data: dict[str, Any] = {}
        qualifier = args.get("qualifier")

        # Validate qualifier: lowercase alphanumeric with hyphens only
        if qualifier and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", qualifier):
            return {
                "error": f"Invalid qualifier '{qualifier}': must be lowercase alphanumeric segments separated by hyphens (e.g., 'backend', 'v2-hotfix')"
            }

        # Validate JIRA ticket format if provided
        if args.get("jira_ticket"):
            ticket_raw = args["jira_ticket"].strip()
            if not re.match(r"^[A-Za-z][A-Za-z0-9]+-[0-9]+$", ticket_raw):
                return {
                    "error": f"Invalid JIRA ticket format '{ticket_raw}': expected e.g. KORE-1234"
                }

        # Issue #1557: validate the new ``mode`` arg up front. Only
        # 'auto' / 'fresh' / 'reassess' are accepted; missing falls
        # back to 'auto'. Forwarded to the orchestrator API which
        # resolves the actual is_epic / pipeline_mode pair against the
        # ticket fetch.
        mode_arg = args.get("mode")
        if mode_arg is not None:
            if mode_arg not in ("auto", "fresh", "reassess"):
                return {
                    "error": (
                        f"Invalid mode '{mode_arg}': must be one of "
                        "'auto', 'fresh', 'reassess' (issue #1557)"
                    )
                }
            if not args.get("jira_ticket"):
                return {"error": ("mode is only meaningful with jira_ticket (issue #1557)")}

        if args.get("issue_number"):
            base_id = f"issue-{args['issue_number']}"
            if qualifier:
                base_id = f"{base_id}-{qualifier}"
            data["issue_number"] = args["issue_number"]
            data["pipeline_id"] = base_id
            data["branch"] = args.get("branch") or f"egg/{base_id}"
        elif args.get("jira_ticket"):
            ticket = args["jira_ticket"].upper()
            base_id = ticket
            if qualifier:
                base_id = f"{base_id}-{qualifier}"
            data["pipeline_id"] = base_id
            data["branch"] = args.get("branch") or f"egg/{base_id}"
            data["prompt"] = args["description"]
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
        if args.get("base_branch"):
            data["base_branch"] = args["base_branch"]
        if args.get("analysis"):
            data["analysis"] = args["analysis"]
        if args.get("plan"):
            data["plan"] = args["plan"]
        if args.get("source_branch"):
            data["source_branch"] = args["source_branch"]
        if args.get("source_artifact_prefix"):
            data["source_artifact_prefix"] = args["source_artifact_prefix"]
        # Issue #1557: forward jira_ticket + epic-mode override so the
        # orchestrator side can run epic detection and persist
        # ``is_epic`` / ``pipeline_mode`` on the Pipeline. The wire
        # field is named ``epic_mode`` to avoid colliding with the
        # existing ``mode`` field (PipelineMode: 'issue' / 'babysit'
        # / 'custom').
        if args.get("jira_ticket"):
            data["jira_ticket"] = args["jira_ticket"].upper()
        if mode_arg is not None:
            data["epic_mode"] = mode_arg

        try:
            # The create_pipeline route calls ls_remote_branch via the gateway,
            # which itself bounds at 30s.  We cap our request at 25s so the
            # MCP client (~30s streamable-HTTP deadline, see GET_STATUS_MAX_WAIT
            # in mcp_server.py) always sees a definite response or our own
            # timeout error within its budget, instead of the client giving
            # up first and the caller having to retry into a 409.
            result = self._make_request("/api/v1/pipelines", method="POST", data=data, timeout=25)
        except HTTPError as e:
            # Read the response body once upfront to avoid stream-exhaustion
            # issues if multiple branches need to inspect it.
            try:
                raw_body = e.read()
                resp_body = json.loads(raw_body.decode())
            except Exception:
                resp_body = {}

            if e.code == 409:
                # Parse enriched 409 body with existing pipeline details
                error_info: dict[str, Any] = {"error": "Pipeline already exists"}
                error_info["error"] = resp_body.get("message", error_info["error"])
                details = resp_body.get("details", {})
                if details:
                    reason = details.get("reason")
                    if reason:
                        error_info["reason"] = reason
                    error_info["existing_pipeline_id"] = details.get("existing_pipeline_id", "")
                    error_info["existing_status"] = details.get("existing_status", "")
                    error_info["existing_phase"] = details.get("existing_phase", "")
                return error_info
            # For all other HTTP errors, include the API response body
            # so the actual error message is visible to callers (#1396).
            error_info = {"error": f"Pipeline creation failed (HTTP {e.code})"}
            error_info["error"] = resp_body.get("message", error_info["error"])
            return error_info

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

    def _handle_run_agent_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create a CUSTOM-mode pipeline that runs one phase with a
        user-chosen subset of roles (#1762 run_agent_task primitive).

        Forwards the caller's args to ``POST /api/v1/pipelines`` with
        ``mode="custom"``. See the ``run_agent_task`` tool definition in
        :data:`PIPELINE_TOOLS` for the full schema.

        Pipeline-ID generation:
          * ``issue_number`` + ``qualifier``: ``issue-<N>-<qualifier>``
          * ``issue_number`` only: ``issue-<N>-custom``
          * ``pr_number`` + ``qualifier``: ``pr-<N>-<qualifier>``
          * ``pr_number`` only: ``pr-<N>`` (BABYSIT-compatible)
          * otherwise a synthetic ``custom-<hex>``
        """
        import json
        from urllib.error import HTTPError

        phase = args.get("phase")
        if phase not in {"refine", "plan", "implement"}:
            return {
                "error": (f"phase must be one of 'refine', 'plan', 'implement' (got {phase!r})")
            }

        repo = args.get("repo")
        if not repo or not isinstance(repo, str):
            return {"error": "repo is required (owner/name format)"}
        if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
            return {"error": "repo must be in owner/name format"}

        description = args.get("description")
        if not description or not isinstance(description, str):
            return {"error": "description is required"}

        roles = args.get("roles")
        if roles is not None and not isinstance(roles, list):
            return {"error": "roles must be a list of strings or null"}

        qualifier = args.get("qualifier")
        if qualifier and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", qualifier):
            return {
                "error": (
                    f"Invalid qualifier '{qualifier}': must be lowercase "
                    "alphanumeric segments separated by hyphens "
                    "(e.g., 'backend', 'v2-hotfix')"
                )
            }

        issue_number = args.get("issue_number")
        if issue_number is not None and (not isinstance(issue_number, int) or issue_number < 1):
            return {"error": "issue_number must be a positive integer"}
        pr_number = args.get("pr_number")
        if pr_number is not None and (not isinstance(pr_number, int) or pr_number < 1):
            return {"error": "pr_number must be a positive integer"}

        # Pipeline-ID derivation (qualifier-aware, BABYSIT-compatible).
        if issue_number is not None:
            if qualifier:
                pipeline_id = f"issue-{issue_number}-{qualifier}"
            else:
                pipeline_id = f"issue-{issue_number}-custom"
        elif pr_number is not None:
            if qualifier:
                pipeline_id = f"pr-{pr_number}-{qualifier}"
            else:
                pipeline_id = f"pr-{pr_number}"
        else:
            import uuid

            pipeline_id = f"custom-{uuid.uuid4().hex[:8]}"

        data: dict[str, Any] = {
            "mode": "custom",
            "phase": phase,
            "repo": repo,
            "prompt": description,
            "pipeline_id": pipeline_id,
        }
        if roles is not None:
            data["roles"] = roles
        if args.get("branch"):
            data["branch"] = args["branch"]
        if args.get("base_branch"):
            data["base_branch"] = args["base_branch"]
        if pr_number is not None:
            data["pr_number"] = pr_number
        if issue_number is not None:
            data["issue_number"] = issue_number
        if args.get("analysis"):
            data["analysis"] = args["analysis"]
        if args.get("plan"):
            data["plan"] = args["plan"]
        if args.get("config"):
            config = args["config"]
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError as e:
                    return {"error": f"Invalid config JSON: {e}"}
            data["config"] = config

        try:
            # Same 25s rationale as _handle_submit_task: stay inside the
            # MCP client's ~30s streamable-HTTP deadline so the caller
            # always sees a definite response or our own timeout error.
            result = self._make_request("/api/v1/pipelines", method="POST", data=data, timeout=25)
        except HTTPError as e:
            try:
                raw_body = e.read()
                resp_body = json.loads(raw_body.decode())
            except Exception:
                resp_body = {}

            if e.code == 409:
                error_info: dict[str, Any] = {
                    "error": resp_body.get("message", "Pipeline already exists"),
                }
                details = resp_body.get("details", {})
                if details:
                    reason = details.get("reason")
                    if reason:
                        error_info["reason"] = reason
                    if "existing_pipeline_id" in details:
                        error_info["existing_pipeline_id"] = details.get("existing_pipeline_id", "")
                    if "existing_status" in details:
                        error_info["existing_status"] = details.get("existing_status", "")
                    if "existing_phase" in details:
                        error_info["existing_phase"] = details.get("existing_phase", "")
                return error_info

            # 400 + other errors: surface the structured message + reason
            # so callers know which validation gate tripped.
            error_info = {
                "error": resp_body.get(
                    "message", f"run_agent_task creation failed (HTTP {e.code})"
                ),
            }
            details = resp_body.get("details", {})
            if details and details.get("reason"):
                error_info["reason"] = details["reason"]
            return error_info

        created_pipeline_id = result.get("data", {}).get("pipeline", {}).get("id", "")

        if created_pipeline_id:
            try:
                self._make_request(
                    f"/api/v1/pipelines/{quote(created_pipeline_id, safe='')}/start",
                    method="POST",
                )
            except Exception:
                logger.error(
                    "Failed to start run_agent_task pipeline",
                    pipeline_id=created_pipeline_id,
                )
                return {
                    "task_id": created_pipeline_id,
                    "status": "created_not_started",
                    "message": (
                        "run_agent_task pipeline created but failed to start. Use task_id to retry."
                    ),
                }

        return {
            "task_id": created_pipeline_id,
            "status": "started",
            "message": (
                f"run_agent_task cycle started: phase={phase}, roles="
                f"{roles if roles else 'default'}"
            ),
        }

    def _handle_babysit_pr(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create a BABYSIT-mode pipeline that runs a one-off implement-phase
        BRC cycle against a PR's diff.

        The orchestrator route validates the PR (open, non-fork, non-empty
        diff) and auto-populates branch/base_branch from ``gh pr view`` when
        omitted. The pipeline ID defaults to ``pr-<pr_number>``.

        .. note::

           As of #1762 this is a user-facing façade over the CUSTOM code
           path: the orchestrator route builds a CUSTOM-mode pipeline
           internally with ``phase="implement"``, the default implement
           roster, and ``has_contract=False``. Runtime behaviour (per-role
           staging branches, head-move guard) is identical to pre-#1762.
        """
        import json
        from urllib.error import HTTPError

        pr_number = args.get("pr_number")
        if not isinstance(pr_number, int) or pr_number < 1:
            return {"error": "pr_number must be a positive integer"}
        repo = args.get("repo")
        if not repo or not isinstance(repo, str):
            return {"error": "repo is required (owner/name format)"}
        if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
            return {"error": "repo must be in owner/name format"}

        data: dict[str, Any] = {
            "repo": repo,
            "pr_number": pr_number,
            "mode": "babysit",
            "pipeline_id": f"pr-{pr_number}",
        }
        if args.get("branch"):
            data["branch"] = args["branch"]
        if args.get("base_branch"):
            data["base_branch"] = args["base_branch"]
        if args.get("config"):
            config = args["config"]
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError as e:
                    return {"error": f"Invalid config JSON: {e}"}
            data["config"] = config

        try:
            # Same 25s rationale as _handle_submit_task.
            result = self._make_request("/api/v1/pipelines", method="POST", data=data, timeout=25)
        except HTTPError as e:
            try:
                raw_body = e.read()
                resp_body = json.loads(raw_body.decode())
            except Exception:
                resp_body = {}

            if e.code == 409:
                error_info: dict[str, Any] = {
                    "error": resp_body.get("message", "Pipeline already exists"),
                }
                details = resp_body.get("details", {})
                if details:
                    reason = details.get("reason")
                    if reason:
                        error_info["reason"] = reason
                    if "existing_pipeline_id" in details:
                        error_info["existing_pipeline_id"] = details.get("existing_pipeline_id", "")
                    if "existing_status" in details:
                        error_info["existing_status"] = details.get("existing_status", "")
                    if "existing_phase" in details:
                        error_info["existing_phase"] = details.get("existing_phase", "")
                return error_info

            # 400 (fork / validation) and other non-409 errors: bubble the
            # structured message up so the caller sees why the PR was refused.
            error_info = {
                "error": resp_body.get("message", f"babysit-pr creation failed (HTTP {e.code})"),
            }
            details = resp_body.get("details", {})
            if details and details.get("reason"):
                error_info["reason"] = details["reason"]
            return error_info

        pipeline_id = result.get("data", {}).get("pipeline", {}).get("id", "")

        if pipeline_id:
            try:
                self._make_request(
                    f"/api/v1/pipelines/{quote(pipeline_id, safe='')}/start",
                    method="POST",
                )
            except Exception:
                logger.error("Failed to start babysit-pr pipeline", pipeline_id=pipeline_id)
                return {
                    "task_id": pipeline_id,
                    "status": "created_not_started",
                    "message": (
                        "Babysit-pr pipeline created but failed to start. Use task_id to retry."
                    ),
                }

        return {
            "task_id": pipeline_id,
            "status": "started",
            "message": f"Babysit-pr cycle started for PR #{pr_number}",
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

        The optional ``wait`` parameter is handled in the async tool wrapper
        (``mcp_server._make_tool_fn``) before this sync handler runs, so no
        worker thread is held during the delay.
        """
        return self._build_status_snapshot(args["task_id"])

    def _build_status_snapshot(self, raw_task_id: str) -> dict[str, Any]:
        """Build the full enriched status snapshot for a pipeline.

        Args:
            raw_task_id: Pipeline/task ID (unquoted).

        Returns:
            The enriched status dict: ``pipeline``, ``current_phase``,
            ``status``, ``running_agents``, ``completed_agents``,
            ``phase_started_at`` / ``phase_elapsed_seconds``,
            ``pending_decisions`` (with draft content enrichment),
            ``recent_messages``. When the pipeline is wedged between
            phases (#2166), also includes ``wedged_no_successor`` with
            ``phase`` / ``completed_at`` / ``since_seconds``.
        """
        task_id = quote(raw_task_id, safe="")

        # Primary: pipeline state
        pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
        pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})

        # Extract PR info from the PR phase artifacts (#1625). The PR phase
        # writes the URL to ``phases["pr"].artifacts["pr_url"]`` after
        # auto-creating the PR, so monitoring clients can pick it up here
        # without a separate ``gh pr list`` call.
        phases = pipeline_data.get("phases", {})
        pr_url: str | None = None
        pr_number: int | None = None
        pr_artifacts = (phases.get("pr") or {}).get("artifacts") or {}
        raw_pr_url = pr_artifacts.get("pr_url")
        if raw_pr_url:
            pr_url = raw_pr_url
            match = re.search(r"/pull/(\d+)", raw_pr_url)
            if match:
                pr_number = int(match.group(1))

        # Build status from pipeline data
        pipeline_info: dict[str, Any] = {
            "id": pipeline_data.get("id", ""),
            "repo": pipeline_data.get("repo", ""),
            "issue_number": pipeline_data.get("issue_number"),
            "created_at": pipeline_data.get("created_at", ""),
        }
        if pr_url:
            pipeline_info["pr_url"] = pr_url
            if pr_number is not None:
                pipeline_info["pr_number"] = pr_number

        status: dict[str, Any] = {
            "current_phase": pipeline_data.get("current_phase", ""),
            "status": pipeline_data.get("status", ""),
            "pipeline": pipeline_info,
        }

        # Extract agent info from phases
        current_phase_key = pipeline_data.get("current_phase", "")
        phase_data = phases.get(current_phase_key, {})
        agents = phase_data.get("agents", [])
        status["running_agents"] = [a for a in agents if a.get("status") == "running"]
        status["completed_agents"] = [a for a in agents if a.get("status") == "complete"]

        # Server-computed timing (#1702)
        now = datetime.now(UTC)

        phase_started_at = phase_data.get("started_at")
        if phase_started_at:
            try:
                started_dt = datetime.fromisoformat(phase_started_at)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=UTC)
                status["phase_started_at"] = started_dt.isoformat()
                status["phase_elapsed_seconds"] = max(0, int((now - started_dt).total_seconds()))
            except ValueError, TypeError:
                pass

        for agent in status["running_agents"]:
            agent_started_at = agent.get("started_at")
            if agent_started_at:
                try:
                    agent_dt = datetime.fromisoformat(agent_started_at)
                    if agent_dt.tzinfo is None:
                        agent_dt = agent_dt.replace(tzinfo=UTC)
                    agent["elapsed_seconds"] = max(0, int((now - agent_dt).total_seconds()))
                except ValueError, TypeError:
                    pass

        # Extract decisions
        decisions = pipeline_data.get("decisions", [])
        status["pending_decisions"] = [d for d in decisions if d.get("status") == "pending"]

        # Watchdog: flag a pipeline that is nominally RUNNING but stalled
        # between phases — the current phase reports COMPLETE, no HITL gate
        # is pending, yet no successor has been scheduled within the
        # threshold (#2166). Lets operators fail loudly within a minute
        # instead of polling for 10+ min hoping to spot the absence of
        # progress.
        if (
            pipeline_data.get("status") == "running"
            and not status["pending_decisions"]
            and current_phase_key
            and phase_data.get("status") == "complete"
        ):
            phase_completed_at = phase_data.get("completed_at")
            if phase_completed_at:
                try:
                    completed_dt = datetime.fromisoformat(phase_completed_at)
                    if completed_dt.tzinfo is None:
                        completed_dt = completed_dt.replace(tzinfo=UTC)
                    since_seconds = int((now - completed_dt).total_seconds())
                    if since_seconds > 60:
                        status["wedged_no_successor"] = {
                            "phase": current_phase_key,
                            "completed_at": completed_dt.isoformat(),
                            "since_seconds": since_seconds,
                        }
                except ValueError, TypeError:
                    pass

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

        # Enrichment: attach draft content to pending decisions (optional)
        self._enrich_pending_decisions(status, raw_task_id, pipeline_data)

        return status

    def _enrich_pending_decisions(
        self,
        status: dict[str, Any],
        pipeline_id: str,
        pipeline_data: dict[str, Any],
    ) -> None:
        """Attach draft content and agent summaries to pending decisions.

        For all decision types (phase_gate, choice, feedback), reads the
        phase's draft document from the pipeline worktree and attaches it
        as ``draft_content`` so the caller can present context to the user.
        Agent summaries and reviewer feedback are attached only to
        phase_gate decisions.

        Mutates ``status["pending_decisions"]`` in place.
        """
        pending = status.get("pending_decisions", [])
        if not pending:
            return

        # Build completed agents summary (phase_gate only)
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
                from orchestrator.routes import resolve_worktree_path, resolve_worktree_repo_path
                from orchestrator.routes.pipelines import _read_phase_draft

                env_path = os.environ.get("EGG_REPO_PATH", "/home/egg/repos")
                base_path = Path(env_path)
                repo_name = repo.split("/")[-1]
                repo_path = resolve_worktree_repo_path(base_path, repo_name)
                worktree_path = resolve_worktree_path(pipeline_id, repo_path)
            except Exception:
                logger.debug(
                    "Failed to resolve worktree for decision enrichment",
                    pipeline_id=pipeline_id,
                )

        # Attach draft_content to all pending decisions from draft-producing phases
        for decision in pending:
            decision_phase = decision.get("phase") or current_phase
            draft_content = None
            if worktree_path is not None and _read_phase_draft is not None:
                try:
                    draft_content = _read_phase_draft(
                        worktree_path,
                        decision_phase,
                        issue_number=issue_number,
                        pipeline_id=pipeline_id,
                        max_chars=16_000,
                        branch=pipeline_data.get("branch"),
                    )
                except Exception:
                    logger.debug(
                        "Failed to read draft for decision enrichment",
                        pipeline_id=pipeline_id,
                    )

            if draft_content is not None:
                decision["draft_content"] = draft_content

            # Phase-gate-specific enrichments
            if decision.get("decision_type") == "phase_gate":
                if agents_summary:
                    decision["completed_agents_summary"] = agents_summary

                reviewer_feedback = self._read_reviewer_feedback(
                    worktree_path,
                    decision_phase,
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

    # --- Gateway request infrastructure ---

    def _get_gateway_client(self, **kwargs: Any) -> "GatewayClient":  # noqa: F821, UP037
        """Create a GatewayClient from the configured gateway URL.

        Extra kwargs are forwarded to the GatewayClient constructor
        (e.g. launcher_secret).
        """
        from urllib.parse import urlparse

        try:
            from orchestrator.gateway_client import GatewayClient
        except ImportError:
            from gateway_client import GatewayClient

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
            pipeline_id="mcp-server",
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
            # Same GET/non-GET split as _make_request — see #1787.
            if method == "GET":
                body = json.dumps(data).encode() if data else None
            else:
                body = json.dumps(data if data is not None else {}).encode()
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
        if args.get("repo"):
            params.append(f"source_repo={quote(str(args['repo']), safe='')}")

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
        if args.get("repo"):
            params.append(f"source_repo={quote(str(args['repo']), safe='')}")

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

    def _handle_restart_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        """Restart a single agent in a pipeline."""
        task_id = quote(args["task_id"], safe="")
        agent_role = quote(args["agent_role"], safe="")
        data: dict[str, Any] = {}
        if args.get("reason"):
            data["reason"] = args["reason"]
        try:
            result = self._make_request(
                f"/api/v1/pipelines/{task_id}/agents/{agent_role}/restart",
                method="POST",
                data=data,
                timeout=60,
            )
            return {
                "restarted": True,
                "agent_role": args["agent_role"],
                "container_id": result.get("data", {}).get("container_id", ""),
                "message": f"Agent {args['agent_role']} restarted successfully",
            }
        except (TimeoutError, OSError) as e:
            if isinstance(e, OSError) and not _is_timeout_error(e):
                return {"error": f"Failed to restart agent: {e}"}
            # Server-side restart is likely still in progress (#1594).
            return {
                "restarted": "pending",
                "agent_role": args["agent_role"],
                "message": (
                    f"Restart of agent {args['agent_role']} accepted but timed out "
                    "waiting for confirmation. The restart is likely still in "
                    "progress. Use get_status to check."
                ),
            }
        except Exception as e:
            return {"error": f"Failed to restart agent: {e}"}

    def _handle_restart_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Restart all agents in a pipeline phase."""
        task_id = quote(args["task_id"], safe="")
        phase = quote(args["phase"], safe="")
        data: dict[str, Any] = {}
        if args.get("reason"):
            data["reason"] = args["reason"]
        try:
            result = self._make_request(
                f"/api/v1/pipelines/{task_id}/phases/{phase}/restart",
                method="POST",
                data=data,
                timeout=120,
            )
            return {
                "restarted": True,
                "phase": args["phase"],
                # API response uses future-tense "agents_to_restart" (not yet spawned);
                # MCP interface uses past-tense "agents_restarted" for caller convenience.
                "agents_restarted": result.get("data", {}).get("agents_to_restart", []),
                "message": f"Phase {args['phase']} restarted successfully",
            }
        except (TimeoutError, OSError) as e:
            if isinstance(e, OSError) and not _is_timeout_error(e):
                return {"error": f"Failed to restart phase: {e}"}
            # Server-side restart is likely still in progress (#1594).
            return {
                "restarted": "pending",
                "phase": args["phase"],
                "message": (
                    f"Restart of phase {args['phase']} accepted but timed out "
                    "waiting for confirmation. The restart is likely still in "
                    "progress. Use get_status to check."
                ),
            }
        except Exception as e:
            return {"error": f"Failed to restart phase: {e}"}

    def _handle_list_agent_local_commits(self, args: dict[str, Any]) -> dict[str, Any]:
        """List unpushed commits in this pipeline's per-agent worktrees (#2429)."""
        task_id = quote(args["task_id"], safe="")
        params: list[str] = []
        if args.get("agent_role"):
            params.append(f"agent_role={quote(args['agent_role'], safe='')}")
        if args.get("slice_id"):
            params.append(f"slice_id={quote(args['slice_id'], safe='')}")
        suffix = f"?{'&'.join(params)}" if params else ""
        try:
            result = self._make_request(
                f"/api/v1/pipelines/{task_id}/local-commits{suffix}",
            )
        except Exception as e:
            return {"error": f"Failed to list local commits: {e}"}

        data = result.get("data", {}) if isinstance(result, dict) else {}
        worktrees = data.get("worktrees", [])
        n_commits = sum(len(wt.get("commits") or []) for wt in worktrees)
        return {
            "pipeline_id": data.get("pipeline_id", args["task_id"]),
            "n_worktrees": len(worktrees),
            "n_commits": n_commits,
            "worktrees": worktrees,
        }

    def _handle_salvage_agent_commits(self, args: dict[str, Any]) -> dict[str, Any]:
        """Push unpushed agent commits to recovery refs (#2429)."""
        task_id = quote(args["task_id"], safe="")
        params: list[str] = []
        if args.get("agent_role"):
            params.append(f"agent_role={quote(args['agent_role'], safe='')}")
        if args.get("slice_id"):
            params.append(f"slice_id={quote(args['slice_id'], safe='')}")
        suffix = f"?{'&'.join(params)}" if params else ""
        try:
            result = self._make_request(
                f"/api/v1/pipelines/{task_id}/salvage{suffix}",
                method="POST",
                data={},
                # Push goes through the gateway with launcher auth and may
                # block on git for a few seconds per worktree.
                timeout=120,
            )
        except Exception as e:
            return {"error": f"Failed to salvage commits: {e}"}

        data = result.get("data", {}) if isinstance(result, dict) else {}
        results = data.get("results", [])
        salvaged = [r for r in results if r.get("ok") and r.get("recovery_ref")]
        failed = [r for r in results if not r.get("ok")]
        return {
            "pipeline_id": data.get("pipeline_id", args["task_id"]),
            "n_worktrees": len(results),
            "n_salvaged": len(salvaged),
            "n_failed": len(failed),
            "recovery_refs": [r["recovery_ref"] for r in salvaged],
            "results": results,
        }

    def _handle_advance_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Advance pipeline to a target phase.

        When force=true, stops all running containers before advancing
        to prevent SIGTERM cascading into the new phase (#1570).
        """
        from models import PipelinePhase

        task_id = quote(args["task_id"], safe="")
        target_phase = args["target_phase"]
        force = args.get("force", False)

        # Validate target_phase up front so an invalid value fails fast
        # without first tearing down containers. See #1755.
        try:
            PipelinePhase(target_phase)
        except ValueError:
            valid = [p.value for p in PipelinePhase]
            return {"error": (f"Invalid target_phase: {target_phase!r}. Valid phases: {valid}")}

        # When force=true, stop running containers before the transition
        # to avoid SIGTERM cascading into the new phase.
        stopped_containers: list[str] = []
        failed_containers: list[str] = []
        if force:
            try:
                containers_result = self._make_request(
                    f"/api/v1/pipelines/{task_id}/containers?all=false"
                )
                containers = containers_result.get("data", {}).get("containers", [])
                for container in containers:
                    cid = container.get("container_id", "")
                    if cid and container.get("status") == "running":
                        try:
                            self._make_request(
                                f"/api/v1/pipelines/{task_id}/containers/{quote(cid, safe='')}/stop",
                                method="POST",
                                timeout=30,
                            )
                            stopped_containers.append(cid)
                        except Exception:
                            logger.warning(
                                "Failed to stop container before force-advance",
                                pipeline_id=args["task_id"],
                                container_id=cid,
                            )
                            failed_containers.append(cid)
            except Exception:
                logger.warning(
                    "Failed to list containers before force-advance",
                    pipeline_id=args["task_id"],
                )

        data: dict[str, Any] = {"target_phase": target_phase, "force": force}
        try:
            result = self._make_request(
                f"/api/v1/pipelines/{task_id}/phase",
                method="POST",
                data=data,
            )
        except Exception as e:
            error_result: dict[str, Any] = {"error": f"Phase advance failed: {e}"}
            if stopped_containers:
                error_result["stopped_containers"] = stopped_containers
            if failed_containers:
                error_result["failed_containers"] = failed_containers
            return error_result
        if stopped_containers:
            result["stopped_containers"] = stopped_containers
        if failed_containers:
            result["failed_containers"] = failed_containers
        return result

    def _handle_start_pipeline(self, args: dict[str, Any]) -> dict[str, Any]:
        """Recover a non-RUNNING pipeline (#2411).

        Targets the pipeline-level recovery route ``POST
        /api/v1/pipelines/{id}/start``.  See the ``start_pipeline`` tool
        definition in :data:`PIPELINE_TOOLS` for the full contract,
        including the FAILED + RUNNING-phase combo from startup
        reconciliation that this verb exists to recover from, and the
        live-pod safety guard added in #2420 (pass ``force=true`` to
        override).
        """
        task_id = quote(args["task_id"], safe="")
        data: dict[str, Any] = {}
        if args.get("force"):
            data["force"] = True
        if args.get("force_reason"):
            data["force_reason"] = args["force_reason"]
        return self._make_request(
            f"/api/v1/pipelines/{task_id}/start",
            method="POST",
            data=data if data else None,
        )

    def _handle_start_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start execution of the current phase."""
        task_id = quote(args["task_id"], safe="")
        return self._make_request(
            f"/api/v1/pipelines/{task_id}/phase/start",
            method="POST",
        )

    def _handle_complete_phase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Mark the current phase as complete."""
        task_id = quote(args["task_id"], safe="")
        data: dict[str, Any] = {}
        if args.get("artifacts"):
            data["artifacts"] = args["artifacts"]
        if args.get("force"):
            data["force"] = True
        if args.get("force_reason"):
            data["force_reason"] = args["force_reason"]
        return self._make_request(
            f"/api/v1/pipelines/{task_id}/phase/complete",
            method="POST",
            data=data if data else None,
        )

    def _handle_populate_contract(self, args: dict[str, Any]) -> dict[str, Any]:
        """Populate pipeline contract from plan draft."""
        task_id = quote(args["task_id"], safe="")
        return self._make_request(
            f"/api/v1/pipelines/{task_id}/phase/populate-contract",
            method="POST",
        )

    # ------------------------------------------------------------------
    # Deployment introspection / action tools (#1759)
    # ------------------------------------------------------------------

    def _handle_get_deployment_context(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return runtime/cluster introspection.

        Always returns a data dict.  On Docker, the response carries a
        degraded placeholder payload with ``runtime: "docker"`` and
        ``detection_source`` indicating provenance.  The k8s-gated routes
        (not this one) use the ``not_available_on_runtime`` / ``runtime_detection_failed``
        error pattern.
        """
        try:
            result = self._make_request("/api/v1/deployment/context", method="GET")
        except Exception as exc:
            return {"error": f"get_deployment_context failed: {exc}"}
        return result.get("data", result)

    def _handle_validate_deployment_manifests(self, args: dict[str, Any]) -> dict[str, Any]:
        """Static validation of the committed kustomize overlay."""
        data: dict[str, Any] = {}
        if args.get("overlay_path"):
            data["overlay_path"] = args["overlay_path"]
        try:
            result = self._make_request(
                "/api/v1/deployment/validate-manifests",
                method="POST",
                data=data,
                timeout=90,
            )
        except Exception as exc:
            return {"error": f"validate_deployment_manifests failed: {exc}"}
        return result.get("data", result)

    def _handle_prune_stale_worktrees(self, args: dict[str, Any]) -> dict[str, Any]:
        """Proxy to /api/v1/deployment/prune-worktrees (gateway-backed).

        The schema accepts only ``dry_run`` — a ``repo`` scope argument
        was removed after the review in #1759 because the gateway helper
        (:py:func:`gateway.worktrees_prune`) always sweeps every repo
        under ``REPOS_BASE_DIR``; a silent-drop would mislead callers.
        """
        body: dict[str, Any] = {"dry_run": bool(args.get("dry_run", True))}
        try:
            result = self._make_request(
                "/api/v1/deployment/prune-worktrees",
                method="POST",
                data=body,
                timeout=120,
            )
        except Exception as exc:
            return {"error": f"prune_stale_worktrees failed: {exc}"}
        return result.get("data", result)

    def _handle_validate_network_isolation(self, args: dict[str, Any]) -> dict[str, Any]:
        """Spawn the throwaway probe Job and return its JSON payload."""
        pipeline_id = args.get("pipeline_id")
        if not pipeline_id:
            return {"error": "pipeline_id is required"}
        body: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "role": args.get("role") or "coder",
        }
        try:
            result = self._make_request(
                "/api/v1/deployment/validate-network-isolation",
                method="POST",
                data=body,
                timeout=90,
            )
        except Exception as exc:
            return {"error": f"validate_network_isolation failed: {exc}"}
        return result.get("data", result)

    def _handle_get_service_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fetch logs for the gateway or orchestrator Deployment."""
        import json
        from urllib.error import HTTPError

        service = args.get("service")
        if not service:
            return {"error": "service is required"}

        params: list[str] = [f"service={quote(str(service), safe='')}"]
        lines = args.get("lines")
        if lines is not None:
            params.append(f"lines={int(lines)}")
        since_seconds = args.get("since_seconds")
        if since_seconds is not None:
            params.append(f"since_seconds={int(since_seconds)}")

        endpoint = "/api/v1/deployment/logs?" + "&".join(params)
        try:
            result = self._make_request(endpoint, method="GET", timeout=30)
        except HTTPError as exc:
            # Surface the orchestrator's structured error body — urllib's
            # default HTTPError.__str__ is just "HTTP Error N: <reason>",
            # which hides the message our route actually set. Before this
            # #1870 fix the caller saw only "HTTP Error 500: INTERNAL
            # SERVER ERROR" with no hint that the real cause was an RBAC
            # denial reading Deployments in egg-system.
            detail = ""
            try:
                raw = exc.read()
                resp_body = json.loads(raw.decode()) if raw else {}
                detail = resp_body.get("message") or ""
            except Exception:
                detail = ""
            if detail:
                return {"error": f"get_service_logs failed (HTTP {exc.code}): {detail}"}
            return {"error": f"get_service_logs failed: {exc}"}
        except Exception as exc:
            return {"error": f"get_service_logs failed: {exc}"}
        return result.get("data", result)

    def _handle_rebuild_and_rollout(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start a ``make redeploy`` and optionally wait for the terminal record."""
        import time
        from urllib.error import HTTPError

        wait = bool(args.get("wait", False))
        try:
            result = self._make_request(
                "/api/v1/deployment/rebuild-and-rollout",
                method="POST",
                data={},
                timeout=30,
            )
        except HTTPError as exc:
            try:
                import json as _json

                body = _json.loads(exc.read().decode())
            except Exception:
                body = {}
            # 409 ← rollout_already_in_progress. Surface as a structured
            # payload rather than an error so callers can branch on it.
            if exc.code == 409:
                data = body.get("data") or {}
                return {
                    "error": "rollout_already_in_progress",
                    "progress_stream_id": data.get("progress_stream_id"),
                    "message": body.get("message", "rollout_already_in_progress"),
                }
            return {"error": f"rebuild_and_rollout failed (HTTP {exc.code})"}
        except Exception as exc:
            return {"error": f"rebuild_and_rollout failed: {exc}"}

        data = result.get("data") or {}
        # not_available_on_runtime / runtime_detection_failed short-circuit
        if data.get("error") in ("not_available_on_runtime", "runtime_detection_failed"):
            return data
        stream_id = data.get("progress_stream_id")
        if not stream_id:
            return data
        if not wait:
            return data

        # wait=true: long-poll until the stream reports done.
        deadline = time.time() + 15 * 60  # 15-minute hard cap
        since = 0
        terminal: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        while time.time() < deadline:
            try:
                poll = self._make_request(
                    f"/api/v1/deployment/rebuild-and-rollout/streams/{quote(stream_id, safe='')}?since={since}",
                    method="GET",
                    timeout=30,
                )
            except Exception as exc:
                return {
                    "error": f"stream poll failed: {exc}",
                    "progress_stream_id": stream_id,
                }
            batch = (poll.get("data") or {}).get("events") or []
            since = (poll.get("data") or {}).get("next_since", since + len(batch))
            events.extend(batch)
            for event in batch:
                if event.get("phase") == "done":
                    terminal = event
                    break
            if terminal or (poll.get("data") or {}).get("done"):
                break
            time.sleep(2.0)

        payload = {
            "progress_stream_id": stream_id,
            "events": events,
        }
        if terminal:
            payload["terminal"] = terminal
            payload["exit_code"] = terminal.get("exit_code")
            payload["rolled_out_images"] = terminal.get("rolled_out_images") or {}
        else:
            payload["error"] = "wait_timeout"
        return payload
