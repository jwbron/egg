# Coordinator Guide

The coordinator is an optional feature that enables dynamic, conversational orchestration of SDLC pipelines. Instead of following a fixed phase sequence, a coordinator agent analyzes each task and determines the optimal workflow.

## Overview

The coordinator runs as a standard agent container with elevated permissions. It uses the existing container infrastructure, gateway sessions, and message bus — no new services required.

### Architecture

```
User/MCP Client
    ↓
MCP Server (port 9850) ←→ Orchestrator API
    ↓
Coordinator Container (claude --print)
    ↓ (egg-orch coordinator commands)
Agent Containers (coder, tester, documenter, etc.)
```

## Configuration

Enable coordinator mode on a pipeline by setting `coordinator_enabled: true` in the pipeline config:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"coordinator_enabled": true}'
```

### Config Options

| Field | Default | Description |
|-------|---------|-------------|
| `coordinator_enabled` | `false` | Enable coordinator mode |
| `coordinator_max_agents` | `10` | Max total agents the coordinator can spawn |
| `coordinator_max_retries_per_role` | `2` | Max retries per agent role |
| `coordinator_max_respawns` | `2` | Max coordinator respawns after crash |

## MCP Server Setup

The MCP server runs alongside the orchestrator on port 9850. Configure Claude Code to connect:

```json
{
  "mcpServers": {
    "egg-coordinator": {
      "url": "http://localhost:9850/mcp/v1/sse"
    }
  }
}
```

### Available MCP Tools

- **submit_task** — Submit a task for coordinator processing
- **get_status** — Check task status and coordinator decisions
- **provide_input** — Respond to coordinator escalations
- **list_tasks** — List coordinator-managed pipelines
- **cancel_task** — Cancel a task

## How It Works

1. A pipeline is created with `coordinator_enabled: true`
2. The orchestrator spawns a coordinator container instead of fixed-phase dispatch
3. The coordinator reads the task, analyzes requirements, and decides workflow
4. It spawns agents (coder, tester, etc.) via `egg-orch coordinator spawn`
5. It monitors progress and makes phase transition decisions
6. On escalation, it creates HITL decisions for human input
7. On completion, it marks the pipeline done

## Guardrails

The coordinator operates within configurable guardrails:

- **Max agents**: Prevents runaway spawning (default: 10)
- **Max retries**: Limits retry attempts per role (default: 2)
- **Max respawns**: Limits coordinator crash recovery (default: 2)
- **Wall-clock time**: Configurable maximum pipeline duration

## Crash Recovery

If the coordinator crashes:
1. Running agents continue and auto-commit their work
2. The orchestrator detects coordinator exit
3. If respawns remain, a new coordinator is spawned
4. The new coordinator re-assesses from orchestrator state
5. If max respawns reached, pipeline fails with notification

## Troubleshooting

**Coordinator not spawning**: Check `coordinator_enabled` in pipeline config.

**Agent spawn rejected**: Check guardrail limits via `egg-orch coordinator state`.

**Escalation not resolved**: Check pending decisions via `egg-orch decision list`.

**MCP connection failed**: Verify MCP server is running on port 9850 and accessible.
