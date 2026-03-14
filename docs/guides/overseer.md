# Overseer Agent Guide

The overseer is a continuously running health monitor that watches pipeline health across all coordinator-spawned pipelines. It detects agent stalls, repeated failures, circular behavior, and off-track work, then takes corrective action before escalating to humans.

## Overview

The overseer runs as a standard agent container with the `overseer` role and read-only access to pipeline infrastructure. Like the coordinator, it has no repository access — it monitors agents via orchestrator APIs, container logs, and the message bus. It uses Claude Haiku for lightweight semantic analysis to keep costs low.

### Architecture

```
Orchestrator
  ├── Coordinator (spawns agents, manages workflow)
  ├── Overseer (monitors all agents including coordinator)
  │     ├── Polls heartbeats, progress, messages
  │     ├── Haiku-powered log analysis
  │     ├── Sends nudge/redirect to stalled agents
  │     └── Escalates to HITL / files issues
  └── Task Agents (coder, tester, documenter, etc.)
```

The overseer complements the existing health check framework:

| Aspect | Health Check Framework | Overseer |
|--------|----------------------|----------|
| Trigger | Lifecycle events (startup, wave complete, phase complete) | Continuous polling |
| Scope | Pipeline-level (container liveness, output presence) | Agent-level (behavior, progress, patterns) |
| Action | FAIL_PIPELINE or ALERT | Nudge → Redirect → HITL → Issue → Slack |
| LLM use | Tier 2 spawns inspector container (Sonnet) | Direct Haiku calls (lighter) |
| Runs as | Orchestrator code (not a container) | Dedicated container (like coordinator) |

### Key Design Decisions

- **Always-on**: Auto-spawned on every coordinator pipeline — catches problems early
- **Adaptive stall detection**: Base threshold + Haiku classifies silence as stall vs. legitimate work
- **HITL-only restart authority**: All restarts go through the decision queue; humans approve disruption
- **Monitors coordinator**: Treats coordinator as another agent; files issue if coordinator stalls
- **Hands contradictory loops to mediator**: Inter-agent conflicts are mediator's domain

## Enabling the Overseer

The overseer is enabled by default when coordinator mode is active. Configure via `PipelineConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `overseer_enabled` | `true` | Auto-spawn overseer with coordinator |
| `overseer_poll_interval_seconds` | `30` | How often overseer checks health (seconds) |
| `overseer_stall_base_threshold_seconds` | `120` | Base threshold before Haiku stall classification (seconds) |
| `overseer_max_redirects_before_escalation` | `2` | Redirect attempts before HITL escalation |

To disable:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"coordinator_enabled": true, "overseer_enabled": false}'
```

## Spawn Policy

- Auto-spawned alongside the coordinator when `overseer_enabled=true`
- One overseer per pipeline
- Runs from pipeline start until completion or halt
- Does NOT count against `coordinator_max_agents` guardrail — it's infrastructure, not a task agent
- Crash does not fail the pipeline (advisory role)

## Access Model

The overseer follows the same no-repo-access pattern as the coordinator:

**Has access to:**
- Orchestrator APIs: `egg-orch pipeline status`, `egg-orch container list`, `egg-orch container logs`, `egg-orch message poll`, `egg-orch health`
- All agent container logs (including coordinator)
- Gateway health API (`curl http://egg-gateway:9848/api/v1/health`)
- GitHub API: `gh issue create` for diagnostic issue filing
- Message bus: read all inter-agent traffic + send redirect messages
- Slack notifications via `~/sharing/notifications/`

**Blocked from:**
- All git operations (no repo mounted)
- `gh pr merge` and `gh pr create`
- `egg-orch phase advance` / `egg-orch phase complete` (no pipeline control)
- `egg-orch coordinator spawn` (cannot spawn agents)
- Direct agent restart (HITL only)

## Monitoring Loop

The overseer runs a continuous polling loop:

1. **Check container statuses**: `egg-orch container list`
2. **Poll message bus**: `egg-orch message poll`
3. **Check orchestrator health**: `egg-orch health`
4. **Check gateway health**: `curl http://egg-gateway:9848/api/v1/health`
5. **Per-agent check**: For each running agent, verify last heartbeat/progress/message timestamp
6. **Anomaly detection**: If anomaly found, run Haiku-assisted classification before acting

The loop repeats every `overseer_poll_interval_seconds` (default: 30s).

## Adaptive Stall Detection

Rather than fixed timeouts, the overseer uses Haiku-assisted classification:

1. **Base threshold** (configurable, default 120s) — if no activity from an agent within this window, flag as potentially stalled
2. **Log analysis** — fetch recent logs via `egg-orch container logs`
3. **Haiku classification** — ask Haiku: "Is this agent stuck, or doing legitimate long-running work (e.g., running a large test suite)?"
4. **Decision** — if stuck → proceed to corrective action; if legitimate → extend threshold 2x and re-check next cycle

## Corrective Action Ladder

When the overseer detects a problem, it escalates through a graduated response:

1. **Nudge**: Send a status message asking if the agent is stuck
   ```bash
   egg-orch message send --to <role> --type STATUS \
     --subject "Health check" --body "No heartbeat in Xs, are you stuck?"
   ```

2. **Redirect**: Send a Haiku-generated suggestion for getting unstuck
   ```bash
   egg-orch message send --to <role> --type STATUS \
     --subject "Redirect" --body "<Haiku-generated suggestion>"
   ```

3. **HITL escalation**: Request human approval to restart the agent
   ```bash
   egg-orch decision create --question "Agent {role} appears stuck after {N} redirects. Approve restart?"
   ```

4. **Issue filing**: File a GitHub issue with structured diagnostics
   ```bash
   gh issue create --title "Pipeline Health Alert: {agent} stalled" \
     --label "overseer-alert" --body-file /tmp/diagnostic.md
   ```

5. **Slack notification**: Alert humans for immediate attention
   ```bash
   cat > ~/sharing/notifications/$(date +%Y%m%d-%H%M%S)-overseer-alert.md
   ```

## Failure Pattern Detection

The overseer detects several classes of problems:

| Pattern | Detection Method | Action |
|---------|-----------------|--------|
| **Agent stall** | No heartbeat/progress within threshold | Adaptive stall detection → corrective ladder |
| **Repeated errors** | Same error signal 3+ times across retries | Escalate to HITL |
| **Circular loops** | Feed last 50 messages to Haiku for pattern analysis | Escalate to HITL |
| **Contract divergence** | Compare agent activity against task descriptions | Redirect agent |
| **Coordinator stall** | Monitor coordinator like any other agent | File issue + notify humans |

## Self-Monitoring

The overseer monitors its own health:

- **Poll cycle timing**: Warn if a cycle takes >2x expected duration
- **Redirect volume**: Warn if >10 redirects per minute (may indicate false positives)
- **Self-malfunction**: If detected, file issue about itself and signal `BLOCKED`

Self-monitoring is best-effort — a truly broken overseer may not be able to report its own failure. The orchestrator's container monitor provides a safety net by detecting container exit.

## Completion Summary

At pipeline completion, the overseer produces a health summary including:
- Total anomalies detected
- Actions taken (nudges, redirects, escalations)
- Unresolved issues
- Overall pipeline health assessment

The summary is sent via the message bus to all agents.

## Autonomous Issue Filing

When the overseer files a GitHub issue, it uses a structured diagnostic template:

```markdown
## Pipeline Health Alert

**Pipeline**: {pipeline_id}
**Issue**: #{issue_number}
**Failing agent**: {role} (container: {container_id})
**Error pattern**: {description}

### Timeline
- {timestamp}: First anomaly detected
- {timestamp}: Nudge sent
- {timestamp}: Redirect sent
- {timestamp}: Escalation requested

### Log Excerpt
{recent_log_lines}

### Haiku Analysis
{haiku_classification_result}

### Suggested Next Step
{recommendation}
```

Labels: `overseer-alert` + error category (e.g., `agent-stall`, `repeated-error`, `circular-loop`)

## Environment Variables

The overseer container receives these environment variables:

| Variable | Value | Description |
|----------|-------|-------------|
| `EGG_OVERSEER_MODE` | `"true"` | Indicates this container is the overseer |
| `EGG_AGENT_ROLE` | `"overseer"` | Standard agent role identifier |
| `EGG_OVERSEER_POLL_INTERVAL` | seconds | Poll interval from config |
| `EGG_OVERSEER_STALL_THRESHOLD` | seconds | Base stall threshold from config |
| `EGG_OVERSEER_MAX_REDIRECTS` | count | Max redirects before escalation |
| `EGG_PIPELINE_ID` | pipeline ID | Standard pipeline identifier |

## Troubleshooting

**Overseer not spawning**: Verify both `coordinator_enabled: true` and `overseer_enabled: true` in the pipeline config via `egg-orch pipeline get <id>`.

**Overseer crash**: Check container logs via `egg-orch container logs <pipeline_id> <container_id>`. Overseer crash does not fail the pipeline — it's advisory. The container monitor logs the exit.

**False positive stalls**: Increase `overseer_stall_base_threshold_seconds` for pipelines with long-running agents (e.g., large test suites). The Haiku classifier should catch most false positives, but a higher base threshold reduces unnecessary analysis.

**Too many redirects**: If agents are receiving too many redirect messages, check `overseer_max_redirects_before_escalation`. The overseer limits itself to this many redirects per agent per issue before escalating to HITL.

**Overseer monitors coordinator**: This is by design. If the coordinator appears stalled, the overseer files an issue and notifies humans. It cannot restart the coordinator directly.

## Related Documentation

- [Coordinator Guide](coordinator.md) — Coordinator agent operations
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard pipeline operations
- [Concurrent Execution Guide](concurrent-execution.md) — Multi-agent coordination
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and orchestrator internals
- [Agent Roles Reference](../reference/agent-roles.md) — All agent roles and permissions
