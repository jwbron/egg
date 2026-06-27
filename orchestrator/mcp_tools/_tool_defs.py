"""MCP tool schema definitions (#3312 slice-13).

The ``PIPELINE_TOOLS`` list extracted verbatim from the pre-split
``orchestrator/mcp_tools.py``. Pure data (MCP-protocol JSON schemas);
re-exported through the package barrel as the stable public API.
"""

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
                    "description": "JIRA ticket ID (e.g. PROJ-1234). Used as the pipeline ID and branch name.",
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
        "description": (
            "Provide human input for a pipeline decision. Resolves "
            "orchestrator-queued decisions (phase gates, overseer "
            "escalations); when the id is not in the queue it falls back to "
            "contract-resident HITL decisions (`cq-N`, registered by agents "
            "via `register_open_question` or impasse escalation) and writes "
            "the resolution onto the contract so the blocked agent unblocks "
            "on its next poll (#3071). For contract `feedback-N` requests "
            "use `answer_feedback` instead."
        ),
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
            "inference when structured consensus data is unavailable. In a "
            "slice-DAG implement phase each slice runs its own consensus — "
            "pass slice_id to scope the result to one slice; without it, only "
            "pipeline-level consensus is reported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "slice_id": {
                    "type": "string",
                    "description": (
                        "Optional slice to scope consensus to (e.g. "
                        "'slice-7') in a slice-DAG implement phase."
                    ),
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
        "name": "update_pipeline_config",
        "description": (
            "Update the safely-mutable subset of a live pipeline's config — "
            "currently per-role model overrides (agent_models) only. "
            "Per-role merge semantics: roles absent from the request keep "
            "their current override, a string value sets the role's model, "
            "an explicit null clears it (falling back to the repo default / "
            "built-in model). Takes effect at the next agent spawn; "
            "currently running agents keep the model they started with, so "
            "pair with restart_phase / restart_agent to apply the change to "
            "a running phase (the dominant use: the current model is "
            "failing or rate-limited, swap and restart — #3174). Confirm "
            "the swap via the resolved_model field on get_status agents / "
            "list_containers entries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "agent_models": {
                    "type": "object",
                    "description": (
                        "Role -> model map to merge into the pipeline's "
                        "agent_models override, e.g. "
                        '{"coder": "deepseek-v4-pro", "tester": null}. '
                        "Keys must be SDLC phase producer/reviewer roles "
                        "(MODEL_OVERRIDE_ROLES). Values: a Claude alias "
                        "(opus / sonnet / haiku / fable, optionally with "
                        "[1m]) routes to Anthropic; any other string routes "
                        "through the in-cluster LiteLLM proxy; null clears "
                        "the role's override."
                    ),
                },
            },
            "required": ["task_id", "agent_models"],
        },
    },
    {
        "name": "restart_agent",
        "description": (
            "Restart a single agent in a pipeline. Stops the existing container, "
            "resets its consensus state, and respawns it with the same configuration. "
            "The agent's worktree is preserved so committed work is retained. "
            "Works on pipelines in running, awaiting-human, failed, or cancelled "
            "state (cancelled pipelines come from cancel_task with cleanup=false). "
            "For a per-slice agent in a multi-slice implement phase, omit slice_id "
            "to let the orchestrator derive it from the phase's agent records; if "
            "the restart is rejected as ambiguous, re-issue with an explicit slice_id."
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
                "slice_id": {
                    "type": "string",
                    "description": (
                        "Slice scope (e.g. 'slice-2') for a per-slice agent in a "
                        "multi-slice implement phase. Omit it and the orchestrator "
                        "derives the slice from the phase's agent records when "
                        "unambiguous; supply it explicitly when an omitted-slice "
                        "restart is rejected with reason 'slice_id_required'."
                    ),
                },
            },
            "required": ["task_id", "agent_role"],
        },
    },
    {
        "name": "restart_phase",
        "description": (
            "Restart all agents in a pipeline phase. Stops all phase containers, "
            "resets consensus and review cycle state, deletes every per-agent "
            "worktree (best-effort salvage of unpushed commits to "
            "egg/recovered/* first; worktrees with a corrupted .git marker "
            "may be skipped without salvage), and respawns all agents. "
            "Per-role branch tips are NOT preserved: fresh worktrees re-fork "
            "from the shared work branch tip (origin/<assigned_branch>), so "
            "only artifacts that were pushed there survive into the "
            "respawned agents' trees (#3080). Contrast restart_agent, which "
            "keeps the agent's worktree intact. Works on pipelines in "
            "running, awaiting-human, failed, or cancelled state (cancelled "
            "pipelines come from cancel_task with cleanup=false)."
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
                    "description": "Target phase to advance to (e.g. 'plan', 'implement'). The legacy 'pr' phase was removed in #2777; IMPLEMENT is now terminal.",
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
            "NetworkPolicy enforcement (Cilium today; CNI-agnostic). Returns a structured "
            "{gateway_reachable, internet_blocked, agent_pods_unreachable, "
            "orchestrator_api_reachable} result. The route deletes the Job "
            "in a try/finally; ttlSecondsAfterFinished=30 is the backstop. "
            "Only available on the Kubernetes runtime and on CNIs that "
            "enforce NetworkPolicies."
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
            "cluster. Only available on the Kubernetes runtime. Use the "
            "`pipeline_id`, `level`, and `pattern` filters (applied server-side "
            "before truncation) to scope a noisy multi-pipeline tail to the "
            "lines you want — e.g. WARNING+ for one pipeline in the last 5 min "
            "— instead of fetching a raw tail that's mostly health-check noise."
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
                    "description": (
                        "Number of log lines to return (default 100). With a "
                        "filter active this caps the matching lines returned, "
                        "not the raw tail scanned."
                    ),
                    "default": 100,
                },
                "since_seconds": {
                    "type": "integer",
                    "description": (
                        "Only return logs newer than this many seconds — useful for "
                        "scoping to 'logs around when my pipeline failed at HH:MM'."
                    ),
                },
                "pipeline_id": {
                    "type": "string",
                    "description": (
                        "Keep only lines emitted for this pipeline/task id. "
                        "Matched against the log's `context.task_id`, with "
                        "`extra.pipeline_id` and `extra.task_id` as fallbacks "
                        "— production call sites use `pipeline_id=...`, which "
                        "the JsonFormatter lands in `extra` rather than the "
                        "context-allowlisted `task_id` slot."
                    ),
                },
                "level": {
                    "type": "string",
                    "description": (
                        "Minimum severity to return; drops lower-severity and unstructured lines."
                    ),
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Python regular expression; keep only lines it matches "
                        "(`re.search`). A plain substring is a valid pattern."
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
    {
        "name": "answer_feedback",
        "description": (
            "Answer an open-ended feedback request an agent registered on the "
            "SDLC contract. Pre-proposal feedback (e.g. a refiner asking for a "
            "goal on an empty contract) lives on the contract as `feedback-N` "
            "and is NOT an orchestrator decision, so `provide_input` returns "
            "404 for it. Use this tool instead — "
            "`get_contract(task_id).feedback` shows the pending questions. "
            "Writing the answers marks the feedback submitted and unblocks the "
            "waiting agent on its next contract poll. Operator-only "
            "(lifecycle-secret guarded)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Pipeline/task ID",
                },
                "answers": {
                    "type": "object",
                    "description": (
                        "Map of feedback question id to answer text, e.g. "
                        '{"Q1": "Add retry logic to the API client"}. Question '
                        "ids come from `get_contract(task_id).feedback.questions`."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "feedback_id": {
                    "type": "string",
                    "description": (
                        "Optional feedback id (e.g. `feedback-1`) guarding "
                        "against answering a stale request; when supplied it "
                        "must match the contract's pending feedback."
                    ),
                },
            },
            "required": ["task_id", "answers"],
        },
    },
]
