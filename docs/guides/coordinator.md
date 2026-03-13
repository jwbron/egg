# Coordinator Guide

The coordinator is an optional feature that enables dynamic, conversational orchestration of SDLC pipelines. Instead of following a fixed phase sequence, a coordinator agent analyzes each task and determines the optimal workflow — skipping unnecessary phases, spawning agents on demand, and adapting based on results.

## Overview

The coordinator runs as a standard agent container with the `coordinator` role and elevated orchestrator permissions. It uses the existing container infrastructure, gateway sessions, and message bus — no new services required beyond the MCP server sidecar.

### Architecture

```
Human (Claude Code session)
  ↓
MCP Server (port 9850) ←→ Orchestrator API (:9849)
  ↓
Coordinator Container (claude --print)
  ↓ (egg-orch coordinator commands)
Agent Containers (coder, tester, documenter, etc.)
```

The human interacts conversationally via a Claude Code session connected to the MCP server. The coordinator agent makes all workflow decisions autonomously, escalating to the human only when input is needed (ambiguous requirements, architecture decisions, etc.).

### Key Design Decisions

- **Coordinator is advisory, not authoritative** — the orchestrator retains execution authority. The coordinator instructs the orchestrator what to do; the orchestrator enforces guardrails.
- **State lives in the orchestrator** — all workflow decisions, agent spawn history, and guardrail counters persist in the `Pipeline.coordinator_state` field. A crashed coordinator can resume from this state.
- **No new infrastructure** — the coordinator reuses existing container spawning, gateway sessions, event bus, and HITL decision mechanisms.

## Enabling Coordinator Mode

Enable coordinator mode when creating a pipeline:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"coordinator_enabled": true}'
```

Or via the MCP server's `submit_task` tool from a Claude Code session.

### Configuration Options

These fields in `PipelineConfig` control coordinator behavior:

| Field | Default | Description |
|-------|---------|-------------|
| `coordinator_enabled` | `false` | Enable coordinator-driven dynamic orchestration |
| `coordinator_max_agents` | `10` | Maximum total agents the coordinator can spawn |
| `coordinator_max_retries_per_role` | `2` | Maximum retry attempts per agent role |
| `coordinator_max_respawns` | `2` | Maximum coordinator container respawns after crash |

## MCP Server

The MCP server runs alongside the orchestrator on port 9850, bridging Claude Code sessions to the coordinator via SSE transport.

### Setup

Configure Claude Code to connect to the MCP server:

```json
{
  "mcpServers": {
    "egg-coordinator": {
      "type": "sse",
      "url": "http://localhost:9850/mcp/v1/sse"
    }
  }
}
```

The MCP server binds to `127.0.0.1` only — no authentication required.

### Available Tools

| Tool | Description | Required Parameters | Optional Parameters |
|------|-------------|---------------------|---------------------|
| `submit_task` | Submit a task for coordinator processing | `description` | `issue_number`, `repo`, `urgency`, `workflow_hint` |
| `get_status` | Check task/pipeline status | `task_id` | |
| `provide_input` | Respond to a coordinator escalation | `task_id`, `decision_id`, `response` | |
| `list_tasks` | List coordinator-managed pipelines | (none) | `status_filter`, `limit` |
| `cancel_task` | Cancel a task | `task_id` | `reason` |

**`submit_task` parameters:**
- `description` (required) — Natural language task description
- `issue_number` (int) — GitHub issue number
- `repo` (string) — Repository in `owner/name` format
- `urgency` — `"low"`, `"normal"` (default), or `"high"`
- `workflow_hint` — Hint for workflow selection (e.g., `"bug_fix"`, `"feature"`, `"refactor"`)

**`list_tasks` parameters:**
- `status_filter` — `"active"` (default), `"completed"`, `"failed"`, or `"all"`
- `limit` (int) — Maximum results to return (default: 10)

### MCP Server Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server health check |
| `/mcp/v1/tools` | GET | List available tools |
| `/mcp/v1/tools/call` | POST | Execute a tool call |
| `/mcp/v1/sse` | GET | SSE stream for MCP protocol events |

Rate limiting is enforced at 30 requests per minute by default.

## Coordinator REST API

All coordinator endpoints are under `/api/v1/pipelines/{id}/coordinator/`. They require `coordinator_enabled: true` on the pipeline (returns 403 otherwise).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/coordinator/spawn` | Spawn an agent container |
| `GET` | `/coordinator/state` | Get coordinator state (agents, phases, decisions, guardrails) |
| `POST` | `/coordinator/phase` | Advance or skip to a target phase |
| `POST` | `/coordinator/escalate` | Create a HITL escalation |
| `DELETE` | `/coordinator/agents/{role}` | Cancel a running agent by role |

### Response Format

All coordinator endpoints return a standard envelope:

```json
// Success
{"success": true, "message": "...", "data": {...}}

// Error
{"success": false, "message": "...", "details": {...}}
```

Common error codes:
- **400** — Invalid request (missing fields, invalid role/phase)
- **403** — Coordinator mode not enabled on the pipeline
- **404** — Pipeline or agent not found
- **429** — Guardrail limit exceeded (max agents or max retries per role)
- **500** — Internal error (container spawn failure, etc.)

### Spawn Agent

```json
POST /api/v1/pipelines/{id}/coordinator/spawn
{
  "role": "coder",                                     // required
  "task_context": "Fix the auth bug in issue #432",    // optional
  "extra_env": {"KEY": "VALUE"}                        // optional
}
```

The `role` must be a valid `AgentRole`: `coder`, `tester`, `documenter`, `integrator`, `refiner`, `architect`, `task_planner`, `risk_analyst`, `reviewer`, `checker`, `inspector`, or `coordinator`.

Returns 429 if guardrail limits are exceeded. The response includes the `spawn_record` with the assigned `retry_number` (0 for the first spawn of a given role, incremented for each subsequent spawn of the same role).

### Get Coordinator State

```
GET /api/v1/pipelines/{id}/coordinator/state
```

Returns the full coordinator state including categorized agent lists:

```json
{
  "success": true,
  "data": {
    "current_phase": "implement",
    "status": "running",
    "running_agents": [...],
    "completed_agents": [...],
    "pending_decisions": [...],
    "coordinator_state": {...},
    "guardrail_counters": {...}
  }
}
```

Agents are categorized by status: `running_agents` contains agents with status `"running"`, while `completed_agents` contains agents with status `"complete"`, `"failed"`, or `"cancelled"`.

### Phase Control

```json
POST /api/v1/pipelines/{id}/coordinator/phase
{
  "target_phase": "implement",    // optional — skip to specific phase
  "reason": "Simple bug fix"      // required
}
```

Omit `target_phase` to advance to the next phase in sequence. When `target_phase` is provided, the action is recorded as `"skip"`; otherwise it is recorded as `"advance"`. Valid phases: `refine`, `plan`, `implement`, `pr`.

### Escalate to Human

```json
POST /api/v1/pipelines/{id}/coordinator/escalate
{
  "question": "Which approach should we use?",    // required
  "escalation_type": "choice",                     // required — "choice" or "feedback"
  "options": ["Option A", "Option B"]              // required for "choice" type
}
```

Creates a standard HITL decision in the orchestrator's decision queue. The escalation is also recorded in `CoordinatorState.escalations` for coordinator state tracking.

### Cancel Agent

```
DELETE /api/v1/pipelines/{id}/coordinator/agents/{role}
```

Cancels the most recent running agent for the given role. The container is force-removed and the spawn record status is set to `"cancelled"`.

## CLI Commands

The `egg-orch coordinator` subcommand provides CLI access to all coordinator operations:

| Command | Description |
|---------|-------------|
| `egg-orch coordinator spawn <pid> --role <role> [--context "..."]` | Spawn an agent |
| `egg-orch coordinator state <pid>` | Get coordinator state |
| `egg-orch coordinator phase <pid> --reason "..." [--target <phase>]` | Advance/skip phase |
| `egg-orch coordinator escalate <pid> --question "..." --type choice --options "A" "B"` | Create HITL escalation |
| `egg-orch coordinator cancel <pid> --role <role>` | Cancel a running agent |

## Workflow Patterns

The coordinator analyzes each task and selects an appropriate workflow:

### Bug Fix (simple, clear reproduction)

Skip refine and plan phases, go straight to implementation:

```bash
egg-orch coordinator phase $PID --reason "Simple bug fix, skipping to implement" --target implement
egg-orch coordinator spawn $PID --role coder --context "Fix the auth bug described in issue #432"
# Wait for coder completion...
egg-orch coordinator spawn $PID --role tester --context "Test the auth fix"
```

### Feature (new functionality)

Run the full workflow with all phases:

```
refine → plan → implement (coder + tester + documenter) → integrate → complete
```

### Refactor (code restructuring)

Skip refine, go through plan:

```
plan → implement (coder + tester) → integrate → complete
```

### Investigation (unclear issue)

Start with a refiner to understand the problem:

```
refine → assess output → decide next steps
```

## Guardrails

The orchestrator enforces configurable limits to prevent runaway behavior:

- **Max agents** (`coordinator_max_agents`): Total agents the coordinator can spawn across the entire pipeline. Spawn requests exceeding this return HTTP 429.
- **Max retries per role** (`coordinator_max_retries_per_role`): Limits retry attempts for each agent role independently.
- **Max coordinator respawns** (`coordinator_max_respawns`): Limits how many times the coordinator itself can be restarted after crashes.

Guardrail counters are tracked in `CoordinatorState.guardrail_counters` and persist across coordinator respawns.

## Crash Recovery

If the coordinator container crashes:

1. Running agents continue operating and auto-commit their work on exit.
2. The orchestrator detects the coordinator exit and checks the respawn budget.
3. If respawns remain, a new coordinator container is spawned automatically.
4. The new coordinator reads `egg-orch coordinator state` to re-assess the current situation — what agents have run, their results, the current phase, and any pending decisions.
5. If max respawns are exhausted, the pipeline fails with a notification.

## Coordinator State Model

The `CoordinatorState` (stored in `Pipeline.coordinator_state`) tracks:

| Field | Type | Description |
|-------|------|-------------|
| `workflow_type` | string | Detected workflow type (e.g., "bug_fix", "feature") |
| `agents_spawned` | list[AgentSpawnRecord] | History of all spawned agents with status and timing |
| `phase_decisions` | list[PhaseDecision] | Phase transition decisions with rationale |
| `escalations` | list[Escalation] | HITL escalation history with resolutions |
| `guardrail_counters` | GuardrailCounters | Running counts for limit enforcement |

### AgentSpawnRecord

| Field | Type | Description |
|-------|------|-------------|
| `role` | AgentRole | Agent role that was spawned |
| `spawned_at` | datetime | When the agent was spawned |
| `completed_at` | datetime? | When the agent completed (null while running) |
| `status` | string | `"running"`, `"complete"`, `"failed"`, or `"cancelled"` |
| `container_id` | string? | Docker container ID |
| `task_context` | string | Task description given to the agent |
| `retry_number` | int | Retry attempt number (0 for first spawn of this role) |

### PhaseDecision

| Field | Type | Description |
|-------|------|-------------|
| `phase` | string | Target phase |
| `action` | string | `"advance"` (next in sequence), `"skip"` (jump to target), or `"loopback"` (return to earlier phase) |
| `reason` | string | Rationale for the decision |
| `decided_at` | datetime | When the decision was made |

### GuardrailCounters

| Field | Type | Description |
|-------|------|-------------|
| `total_agents_spawned` | int | Total successful agent spawns across all roles |
| `retries_by_role` | dict[str, int] | Spawn count per role (incremented on each spawn, not just retries) |
| `coordinator_respawns` | int | Number of times the coordinator has been respawned after crashes |
| `started_at` | datetime | When the coordinator was first started |

### Escalation

| Field | Type | Description |
|-------|------|-------------|
| `question` | string | Question posed to the human |
| `escalation_type` | string | `"choice"` or `"feedback"` |
| `created_at` | datetime | When the escalation was created |
| `resolved_at` | datetime? | When resolved (null while pending) |
| `resolution` | string? | Human's response text |

## Event Types

The coordinator emits events via the orchestrator's event bus for monitoring and observability:

| Event | Description | Data Fields |
|-------|-------------|-------------|
| `coordinator.spawn` | Agent spawned by the coordinator | `role`, `container_id`, `task_context`, `retry_number` |
| `coordinator.decision` | Phase transition decision made | `previous_phase`, `current_phase`, `action`, `reason` |
| `coordinator.escalation` | HITL escalation created | `question`, `escalation_type`, `options`, `decision_id` |
| `coordinator.loopback` | Coordinator respawned after crash | `reason`, `respawn_count` |

Subscribe to these via the SSE streams (`/pipelines/{id}/stream` or `/pipelines/stream`).

## Gateway Permissions

The coordinator agent role has restricted file access enforced by the gateway:

- **Allowed writes**: `.egg-state/agent-outputs/` only
- **Blocked writes**: All source code, docs, tests, contracts, drafts, and reviews

This ensures the coordinator operates purely as an orchestration layer — it cannot modify code or pipeline artifacts directly.

## Environment Variables

The coordinator container receives these additional environment variables:

| Variable | Value | Description |
|----------|-------|-------------|
| `EGG_COORDINATOR_MODE` | `"true"` | Indicates this container is the coordinator |
| `EGG_COORDINATOR_TOOLS` | `"true"` | Enables coordinator CLI tools |
| `EGG_ISSUE_NUMBER` | issue number | Set when the pipeline is tied to a GitHub issue |
| `EGG_PIPELINE_ID` | pipeline ID | Standard pipeline identifier |

The coordinator runs with `phase="coordinator"` — a special phase value distinct from the standard SDLC phases (`refine`, `plan`, `implement`, `pr`).

## Troubleshooting

**Coordinator not spawning**: Verify `coordinator_enabled: true` in the pipeline config via `egg-orch pipeline get <id>`.

**Agent spawn rejected (HTTP 429)**: Check guardrail limits via `egg-orch coordinator state <id>`. The `guardrail_counters` section shows current counts vs. configured limits.

**Escalation not resolved**: Check pending decisions via `egg-orch decision list <id>`. Escalations create standard HITL decisions that need human resolution.

**MCP connection failed**: Verify the MCP server is running on port 9850 (`curl http://localhost:9850/health`). The server starts as a background thread alongside the orchestrator.

**Coordinator crash loop**: Check the `coordinator_respawns` counter in coordinator state. If it equals `coordinator_max_respawns`, the pipeline has failed. Review container logs for the root cause: `egg-orch container logs <pipeline_id> <container_id>`.

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard fixed-phase pipeline operations
- [Agent-Mode Design](agent-mode-design.md) — When to let agents operate freely vs. constrained
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and orchestrator internals
- [Orchestrator README](../../orchestrator/README.md) — API reference and component details
