# Coordinator Agent Guide

> Dynamic agent orchestration via a conversational coordinator — adaptive workflows that skip, reorder, and loop phases based on task complexity.

The coordinator is an opt-in execution mode where a Claude agent manages the full pipeline lifecycle dynamically, rather than following the fixed REFINE → PLAN → IMPLEMENT → PR sequence. It reads tasks, decides which agents to spawn, monitors their progress, and adapts the workflow in real time.

For the fixed-phase SDLC pipeline (the default), see [SDLC Pipeline Guide](sdlc-pipeline.md).

## Architecture

The coordinator runs as a standard agent container with the `COORDINATOR` role and elevated gateway permissions. It is **not** a separate service — it reuses the existing container lifecycle, gateway sessions, message bus, and checkpoint infrastructure.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Coordinator Mode                             │
│                                                                  │
│  ┌──────────────────┐     ┌──────────────┐                       │
│  │   MCP Server     │────▶│ Orchestrator │                       │
│  │   (SSE :9850)    │◀────│   :9849      │                       │
│  └────────┬─────────┘     └──────┬───────┘                       │
│           │                      │                               │
│    Claude Code             ┌─────┴──────┐                        │
│    session                 │ Coordinator│                        │
│    (human)                 │ Container  │                        │
│                            └─────┬──────┘                        │
│                                  │                               │
│              ┌───────────┬───────┴───────┬───────────┐           │
│              ▼           ▼               ▼           ▼           │
│        ┌──────────┐ ┌──────────┐  ┌──────────┐ ┌──────────┐     │
│        │  Coder   │ │  Tester  │  │ Docmter  │ │ Integr.  │     │
│        └──────────┘ └──────────┘  └──────────┘ └──────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- **Coordinator is an agent container**: Runs `claude --print` in headless mode like all other agents. No direct Anthropic API calls from infrastructure code (per EGG200 convention).
- **Orchestrator remains the execution authority**: The coordinator instructs the orchestrator via REST APIs; it does not spawn containers directly.
- **Hybrid phase model**: Existing phases (REFINE → PLAN → IMPLEMENT → PR) are advisory. The coordinator can skip, reorder, or combine phases based on task complexity.
- **State in orchestrator, not context window**: Workflow decisions are persisted as a `CoordinatorState` Pydantic model in the pipeline. A crashed coordinator re-assesses from current orchestrator state rather than replaying prior reasoning.
- **Opt-in via `coordinator_enabled`**: Existing pipelines are unaffected. The coordinator mode is activated by setting `coordinator_enabled: true` in PipelineConfig.

## Configuration

### Enabling Coordinator Mode

Set `coordinator_enabled: true` in the pipeline configuration:

```bash
# Create a coordinator-driven pipeline
egg-orch pipeline create --repo owner/name --coordinator-enabled

# Or via the MCP server from a Claude Code session
# (see MCP Server Setup below)
```

When `coordinator_enabled` is false (the default), the pipeline uses the standard fixed-phase dispatch.

### Guardrails

The coordinator operates within configurable guardrails enforced by the orchestrator:

| Guardrail | Default | Description |
|-----------|---------|-------------|
| Max agents per task | 10 | Total agents the coordinator can spawn |
| Max retries per role | 2 | How many times a failed agent role is re-spawned |
| Max wall-clock time | Configurable | Maximum pipeline duration before timeout |
| Max coordinator respawns | 2 | How many times a crashed coordinator is restarted |

These are configured via `PipelineConfig` and enforced server-side by the orchestrator — the coordinator cannot override them.

## How It Works

### Workflow Selection

The coordinator reads the task (issue, Slack message, or human description) and determines the appropriate workflow:

| Task Type | Typical Workflow |
|-----------|-----------------|
| Simple bug fix | Skip refine/plan, jump to implement with a single coder |
| Feature with clear spec | Skip refine, plan briefly, then implement |
| Complex feature | Full SDLC: refine → plan → implement with multiple agents |
| Documentation only | Spawn documenter directly |
| Test failure loop | Coder → tester → detect failure → re-spawn coder with context |

The coordinator follows the "orientation not pre-fetching" principle: it passes agents task objectives and lightweight metadata rather than pre-fetched diffs or file contents.

### Agent Spawning

The coordinator spawns agents via orchestrator APIs:

```bash
# From within the coordinator container
egg-orch coordinator spawn --role coder --context "Fix auth bug in #432"
egg-orch coordinator spawn --role tester --context "Test coder's branch for edge cases"
```

Each spawned agent gets:
- A standard sandbox container with gateway session
- Task-specific context from the coordinator
- Access to the message bus for cross-agent communication

### Monitoring and Adaptation

The coordinator monitors agent progress via the message bus and orchestrator state:

```bash
egg-orch coordinator state    # Current pipeline state, running agents, decisions
```

Based on results, the coordinator can:
- **Loop back**: If a tester finds issues, re-spawn the coder with the test failure context
- **Skip phases**: If analysis is straightforward, jump directly to implementation
- **Escalate**: Surface decisions to the human when input is needed
- **Cancel**: Stop an agent that is no longer needed

### Escalation

The coordinator escalates to humans for:
- Ambiguous requirements
- Architecture decisions not covered by ADRs
- PR readiness confirmation
- Any situation where the coordinator lacks confidence

Escalations are surfaced via the MCP server (interactive mode) or notifications (async mode).

## MCP Server Setup

The MCP server is an SSE-based sidecar that bridges external Claude Code sessions to the coordinator. It runs alongside the orchestrator on port 9850.

### Connecting from Claude Code

Add the MCP server to your Claude Code configuration:

```json
{
  "mcpServers": {
    "egg-coordinator": {
      "url": "http://localhost:9850/sse"
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `submit_task` | Submit a task (natural language description, optional issue number, repo, urgency) |
| `get_status` | Query task state: coordinator status, running agents, phase, pending decisions |
| `provide_input` | Supply human input for a coordinator escalation |
| `list_tasks` | List active and recent coordinator-managed pipelines |
| `cancel_task` | Cancel a running task and all its agents |

### Example Interaction

```
Human: "Fix the auth bug in issue #432"
  → submit_task("Fix the auth bug in #432", issue=432, repo="owner/repo")

Coordinator analyzes issue, spawns coder → coder completes →
  coordinator spawns tester → tester finds edge case →
  coordinator re-spawns coder with edge case context →
  coder fixes → tester passes → coordinator creates PR
```

The human can redirect at any point:

```
Human: "Actually, include a documenter too"
  → provide_input(task_id, "Include documenter for API changes")

Coordinator adjusts plan, spawns documenter alongside remaining work
```

### Authentication

The MCP server validates requests against gateway session tokens. Rate limiting is configured at 30 requests/minute by default (configurable). The server listens only on the egg-isolated network (172.32.0.0/24).

## Coordinator vs Fixed-Phase Pipeline

| Aspect | Fixed-Phase Pipeline | Coordinator Mode |
|--------|---------------------|------------------|
| Phase order | Fixed: REFINE → PLAN → IMPLEMENT → PR | Adaptive: skip, reorder, combine |
| Agent selection | Pre-configured per phase | Coordinator decides per task |
| Human interaction | CLI (`egg-orch`, `egg-sdlc`) | Conversational (MCP + Claude Code) |
| Failure handling | Phase fails → pipeline fails | Coordinator can retry, loop back, or escalate |
| Activation | Default | `coordinator_enabled: true` |
| Best for | Predictable, auditable pipelines | Varied task complexity, interactive workflows |

Both modes coexist. The fixed-phase pipeline remains the default and is fully supported. The coordinator is an alternative entry point into the same orchestrator infrastructure.

## Crash Recovery

The coordinator is designed to survive crashes:

1. **Running agents continue**: Agent containers auto-commit on exit (existing behavior). A coordinator crash does not affect running agents.
2. **Orchestrator detects exit**: The `CoordinatorExecutor` monitors coordinator container health via heartbeats and timeout detection.
3. **Respawn from state**: A new coordinator session is spawned (up to `max_coordinator_respawns`) and re-assesses from the current orchestrator state — it does not replay prior reasoning.
4. **Human notification**: If respawn limit is reached, the human is notified via the existing notification system.

## Observability

Coordinator decisions are emitted as events on the message bus and visible in the SSE stream:

| Event Type | Description |
|------------|-------------|
| `COORDINATOR_DECISION` | Workflow decision (e.g., "skipping refine for simple bug fix") |
| `COORDINATOR_SPAWN` | Agent spawn request |
| `COORDINATOR_ESCALATION` | Question surfaced to human |
| `COORDINATOR_LOOPBACK` | Re-running an agent based on results |

These events appear in the pipeline SSE stream and DAG visualizer. The coordinator's full session is also captured by the checkpoint system for post-hoc review.

## Gateway Permissions

The coordinator operates under a `coordinator` pseudo-phase with specific permissions:

**Allowed:**
- `git push` to egg-owned branches
- `egg-contract` operations (create decisions, feedback)
- `egg-orch coordinator *` operations (spawn, phase, escalate, cancel)

**Blocked:**
- `gh pr merge` (human must merge)
- `gh pr create` (coordinator instructs agents, does not create PRs directly)
- `git reset --hard`, `git push --force`
- Writing source code or contracts (coordinator writes only to `.egg-state/agent-outputs/`)

## Troubleshooting

**Coordinator not spawning:**
- Verify `coordinator_enabled: true` in pipeline config
- Check orchestrator health: `egg-orch health`
- Review orchestrator logs for coordinator executor errors

**MCP server unreachable:**
- Verify the server is running: `curl http://localhost:9850/health`
- Check that the MCP server started alongside the orchestrator
- Ensure your Claude Code MCP config points to the correct URL

**Agent spawn rejected:**
- Check guardrail limits: `egg-orch coordinator state`
- Max agents or max retries per role may be exceeded
- Verify the coordinator has the COORDINATOR role (non-coordinator roles are rejected)

**Coordinator crash loop:**
- Check the coordinator's checkpoint for error context: `egg-checkpoint list --agent-type coordinator`
- Review guardrail counters — max respawns (default 2) may be exhausted
- The orchestrator marks the pipeline as failed after respawn limit

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — the fixed-phase pipeline (default mode)
- [Agent-Mode Design](agent-mode-design.md) — design principles for agent workflows
- [Orchestrator Architecture](../architecture/orchestrator.md) — orchestrator internals
- [Checkpoint Access](checkpoint-access.md) — querying coordinator checkpoints