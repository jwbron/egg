# Pipeline Health Monitoring

Pipeline health monitoring uses a **two-tier architecture** to detect and respond to agent failures during pipeline execution. The orchestrator tier handles clear-cut failures deterministically (no LLM cost), while the overseer agent tier handles ambiguous situations requiring semantic analysis.

## Architecture Overview

```
Agent containers emit structured progress events
  │
  ▼
┌───────────────────────────────────────────────────────────────┐
│ Tier 1: Orchestrator (Deterministic)                          │
│                                                               │
│  Structured progress events → Tripwire rules → Auto-action    │
│  • Heartbeat timeout      → Auto-nudge agent                  │
│  • Container exit         → HITL escalation                   │
│  • Repeated errors (N×)   → Escalate to overseer              │
│  • Message volume spike   → Auto-throttle                     │
│  • Progress stall         → Nudge, then escalate              │
│                                                               │
│  Ambiguous cases ──────────────────────┐                      │
└────────────────────────────────────────┼──────────────────────┘
                                         ▼
┌───────────────────────────────────────────────────────────────┐
│ Tier 2: Overseer Agent (LLM-Powered)                          │
│                                                               │
│  ┌────────────────────┐    ┌────────────────────────────┐     │
│  │ Haiku Classifiers  │───►│ Sonnet/Opus Decision-Maker │     │
│  │                    │    │                            │     │
│  │ • Stall vs. work   │    │ • Compose redirect msgs    │     │
│  │ • Loop detection   │    │ • Decide escalation level  │     │
│  │ • Error triage     │    │ • File diagnostic issues   │     │
│  │ • Off-track check  │    │ • HITL escalation          │     │
│  └────────────────────┘    └────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
```

## Structured Progress API

Agents emit structured progress events to the orchestrator, replacing reliance on parsing unstructured container output.

### Emitting Progress

```bash
# Report current work step
egg-orch progress emit --step "running tests" --state working --detail "pytest suite 3/5"

# Report a blocker
egg-orch progress emit --step "applying fix" --state blocked --blocker "missing dependency"

# Report step completion
egg-orch progress emit --step "code review" --state complete
```

**Progress states:**

| State | Meaning |
|-------|---------|
| `working` | Actively working on this step |
| `blocked` | Waiting on something (specify `--blocker`) |
| `complete` | Step finished |

### Querying Progress

```bash
# All progress for the current pipeline
egg-orch progress query

# Progress for a specific agent
egg-orch progress query --agent coder

# Recent progress since a timestamp
egg-orch progress query --since "2026-03-16T10:00:00Z" --limit 50
```

**API endpoint:**

```
GET /api/v1/pipelines/{id}/progress?agent_role=<role>&since=<timestamp>&limit=<n>
```

### When to Emit Progress

All agents should emit structured progress at key milestones:

- **Starting a major work step** (e.g., "analyzing codebase", "writing tests", "reviewing proposal")
- **Completing a step** (transition to next step or mark complete)
- **Encountering a blocker** (dependency, missing data, unclear requirements)
- **Long-running operations** (emit periodically so the orchestrator knows you're alive)

Progress events supplement heartbeats — they provide richer context about what an agent is doing, not just that it's alive.

## Tier 1: Orchestrator Tripwires

The orchestrator processes structured progress events with deterministic rules. No LLM is involved. Tripwires fire instantly when thresholds are exceeded.

### Tripwire Rules

| Tripwire | Condition | Auto-Action |
|----------|-----------|-------------|
| **Heartbeat timeout** | No heartbeat or progress within threshold | Nudge the agent; after 2 unanswered nudges, escalate to overseer/HITL |
| **Container exit** | Agent container dies unexpectedly | Immediate HITL escalation |
| **Repeated errors** | Same error N times consecutively | Escalate to overseer (or HITL if no overseer) |
| **Message volume spike** | Agent sending > N messages/minute | Auto-throttle |
| **Progress stall** | No structured progress update within threshold | Nudge, then escalate to overseer |

### Viewing and Resolving Alerts

```bash
# List active deterministic alerts for the current pipeline
egg-orch health alerts

# List alerts for a specific pipeline
egg-orch health alerts --pipeline issue-123

# Resolve (remove) alerts after an issue is addressed
egg-orch health resolve --agent-id coder --alert-type heartbeat_timeout
```

**API endpoints:**

```
GET  /api/v1/pipelines/{id}/health/alerts
POST /api/v1/pipelines/{id}/health/alerts/resolve
     Body: {"agent_id": "<role>", "alert_type": "<type>"}
```

### Configuration

Tripwire thresholds are configurable in `PipelineConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `overseer_enabled` | `true` | Auto-spawn overseer on all pipelines |
| `orchestrator_heartbeat_timeout_seconds` | `120` | Auto-nudge after this many seconds without heartbeat |
| `orchestrator_error_repeat_threshold` | `3` | Escalate after N identical consecutive errors |
| `orchestrator_message_rate_limit` | `20` | Auto-throttle above this many messages per minute |
| `overseer_poll_interval_seconds` | `30` | How often the overseer checks health |
| `overseer_max_redirects_before_escalation` | `2` | Redirect attempts before HITL escalation |
| `overseer_decision_maker_model` | `"sonnet"` | LLM model for overseer decision-making tier |
| `overseer_max_respawns` | `3` | Max times to auto-respawn the overseer if it exits mid-pipeline (0 disables respawning) |
| `overseer_rerun_min_work_seconds` | `60` | Minimum work duration required after a `request_changes` phase-gate decision; completions faster than this with `content_changed=False` are flagged as re-run anomalies |
| `overseer_hitl_propagation_timeout_seconds` | `300` | Seconds to wait for a resolved phase-gate decision to appear in the SDLC contract before raising a propagation-failure alert |

## Tier 2: Overseer Agent

The overseer is a continuously running, read-only agent that handles cases the orchestrator's deterministic rules can't resolve. It runs as a separate container with no git repository access.

### Lifecycle

- **Auto-spawned** on every pipeline (when `overseer_enabled` is true)
- **Runs across all phases** — spawned at pipeline start, persists until pipeline completion
- **Auto-respawned** if the overseer exits before the pipeline reaches a terminal state (up to `overseer_max_respawns` attempts, checked every 30 seconds by the orchestrator's health monitor thread)
- **One overseer per pipeline**
- **No code access** — cannot clone, checkout, or modify code

### Internal Architecture

The overseer uses a two-sub-tier LLM architecture for cost efficiency:

#### Haiku Classifiers

Lightweight Haiku agents handle classification tasks. They run only when the orchestrator escalates an ambiguous situation.

| Task | Prompt Pattern |
|------|---------------|
| **Stall classification** | "Is this agent stuck, or doing legitimate long-running work?" |
| **Loop detection** | "Is this agent repeating the same actions in a cycle?" |
| **Error triage** | "Is this error recoverable or fatal?" |
| **Off-track detection** | "Is this agent's work aligned with the contract?" |
| **Decision consistency** | "Does this phase's output respect prior resolved HITL decisions?" |

**Consensus-aware stall classification**: The stall classifier receives BRC consensus state as authoritative context when available. The classifier is instructed that an agent with confirmed consensus is not stalled — this prevents false stall diagnoses during the window between consensus confirmation and phase transition.

Characteristics:
- Short, focused prompts — single-purpose classification
- Results are cached to avoid re-analyzing the same log lines
- Budget: ~1-2 Haiku calls per poll cycle per agent (only on anomalies)
- Falls back to heuristic checks if the API is unavailable

#### Sonnet/Opus Decision-Maker

A Sonnet or Opus agent handles corrective decision-making when Haiku monitors escalate.

Responsibilities:
- Decide corrective action: nudge, redirect, HITL escalation, or issue filing
- Compose redirect messages with actionable guidance
- Determine whether a pattern warrants an issue vs. HITL escalation
- Produce pipeline health summary at completion

Characteristics:
- Only invoked when Haiku detects an anomaly requiring a decision
- Receives structured context from the Haiku tier
- Higher cost per call, but called infrequently

All LLM calls use `shared/egg_agent/` (`run_agent_async`) — no direct API calls.

### Escalation Flow

```
Orchestrator detects anomaly via structured logs (deterministic)
  → Clear-cut (heartbeat timeout, container exit, error repeat)
    → Orchestrator handles directly (auto-nudge or HITL)
  → Ambiguous
    → Escalate to overseer

Overseer receives escalation (or detects anomaly in own polling)
  → Haiku classifies (stall / loop / error / off-track)
    → Simple action needed (e.g., nudge)
      → Haiku handles directly
    → Decision needed (redirect content, escalation level)
      → Escalate to Sonnet/Opus
        → Sonnet/Opus decides corrective action
          → Execute action (nudge / redirect / HITL / file issue / Slack)
```

**Phase-scoped alert processing**: Health alerts are filtered to only include agents in the current pipeline phase. Alerts for agents from completed phases (e.g., a coder alert during the test phase) are excluded to prevent false stall diagnoses.

### Corrective Action Ladder

The overseer follows a progressive escalation ladder:

| Step | Action | When |
|------|--------|------|
| 1 | **Auto-nudge** | Orchestrator detects heartbeat/progress timeout; nudge sent via message bus (`NUDGE` message type) |
| 1b | **Escalate to overseer/HITL** | After 2 unanswered heartbeat nudges, or a progress stall that persists after an initial nudge (orchestrator-level escalation) |
| 2 | **Redirect message** | Overseer sends actionable guidance to the agent |
| 3 | **HITL escalation** | Agent still stuck after max redirects |
| 4 | **File GitHub issue** | Structured diagnostic report for persistent problems |
| 5 | **Slack notification** | Human escalation for urgent issues |

**Escalation safety net**: If the decision-maker selects `nudge` or `redirect` but the accompanying message indicates human intervention is required (e.g., contains phrases combining human/manual/operator with intervention/review/needed), the action is automatically upgraded to `hitl`. This prevents under-escalation caused by LLM phrasing that signals urgency without selecting the appropriate action level.

### Post-Consensus Stall Detection

If all agents have confirmed BRC consensus but the pipeline phase has not transitioned within ~90 seconds (3× the poll interval), the overseer escalates with a HITL decision, Slack notification, and message bus broadcast (`OVERSEER_ALERT`). This detects potential orchestrator transition failures after a successful concurrent phase. The escalation fires only once per consensus cycle to avoid duplicate alerts.

### Additional Overseer Health Checks

Each poll cycle the overseer evaluates six targeted health checks (the fourth triggers only on phase transitions; the fifth triggers only at pipeline completion). Only the fourth (cross-phase consistency) uses an LLM classifier; the rest are deterministic (no LLM cost):

> **Note:** All checks also broadcast an `OVERSEER_ALERT` message to the `all` target on the message bus, allowing the `/sdlc` monitoring session and other listeners to surface findings via `egg-orch message recent`.

| Check | Detects | Action |
|-------|---------|--------|
| **Re-run anomaly** | Agent completes in < `overseer_rerun_min_work_seconds` after a `request_changes` phase-gate decision with `content_changed=False` — a likely no-op re-run | HITL escalation + Slack notification + message bus broadcast (deduplicated per decision ID) |
| **Status inconsistency** | Pipeline shows `failed` while all agents show `complete` — a possible transient state | HITL escalation + Slack notification + message bus broadcast (after one poll-cycle grace period) |
| **HITL propagation failure** | A resolved phase-gate decision is not reflected in the SDLC contract after `overseer_hitl_propagation_timeout_seconds` | HITL escalation + Slack notification + message bus broadcast |
| **Cross-phase consistency** | On a phase transition, the new phase's contract output may not honour prior resolved HITL decisions (uses the Haiku `decision_consistency` classifier; requires confidence > 0.7 to escalate) | HITL escalation + Slack notification + message bus broadcast (deduplicated per phase-transition pair) |
| **PR phase no PR** | Pipeline reaches `complete` with `current_phase=pr` but no `pr_url` in phase artifacts — defense-in-depth for edge cases where primary PR creation failure handling was bypassed, so stranded branch work is not silently lost | HITL decision + Slack notification + message bus broadcast |
| **Orchestrator unreachability** | Both pipeline status and phase queries return empty for 3 consecutive poll cycles — likely orchestrator container crash or network partition | Slack notification + oversight event + message bus broadcast (re-alerts every 3 cycles until recovered; oversight event also logged on recovery) |

### Autonomous Issue Filing

When the overseer files a GitHub issue (decided by the Sonnet/Opus tier), it uses a structured diagnostic template:

```markdown
## Pipeline Health Alert

**Pipeline:** {pipeline_id}
**Issue:** #{issue_number}
**Filed by:** Overseer (automated)

## Failing Agent
- **Role:** {agent_role}
- **Container:** {container_id}
- **Last known state:** {readiness_state}

## Error Pattern
**Category:** {stall | repeated_error | circular_loop | off_track}
**Description:** {human-readable description}

## Timeline
{chronological events leading to this alert}

## Corrective Actions Attempted
{list of auto-nudges, redirect messages, HITL requests}

## Haiku Analysis
{classification of the agent's state}

## Sonnet/Opus Decision
{reasoning for the corrective action}

## Suggested Next Step
{what a human should do}
```

Issues are auto-labeled with `overseer-alert` and the error category (e.g., `stall`, `repeated-error`).

### Overseer Access & Restrictions

**Has access to:**
- Orchestrator APIs: pipeline status, container logs, progress queries, health alerts, message bus
- Structured agent progress data via `egg-orch progress query`
- Agent container logs via `egg-orch container logs`
- Gateway and orchestrator health endpoints
- GitHub API: `gh issue create` for diagnostic filing
- `egg-orch message send` to redirect individual agents or broadcast `OVERSEER_ALERT` notifications to all

**Blocked from:**
- All git operations (no repo mounted)
- All source, test, doc, and config files
- `gh pr merge` and `gh pr create`
- `egg-orch phase advance` / `egg-orch phase complete`
- Direct agent restart (must go through HITL)

When the overseer creates a HITL decision (format: `"Agent <role> issue: <message>"`) and the human resolves it with **"Restart agent"**, the orchestrator automatically stops the stalled container and emits a `CONTAINER_STOPPED` event so the pipeline loop can respawn a replacement.

### Self-Monitoring

The overseer monitors itself:
- **Poll cycle timing** — warns if a cycle takes >2x expected duration
- **Message volume** — alerts if sending >10 redirects per minute
- **LLM call costs** — reduces poll frequency if exceeding budget
- **Self-reporting** — files an issue about itself and signals `BLOCKED` if malfunctioning

## Overseer vs. Mediator Boundary

| Signal | Owner | Reasoning |
|--------|-------|-----------|
| Agent stalls (no heartbeat) | **Orchestrator** | Clear-cut tripwire |
| Repeated identical errors | **Orchestrator** → **Overseer** | Orchestrator detects; overseer classifies if ambiguous |
| Ambiguous stall (working or stuck?) | **Overseer** | Requires semantic log analysis |
| Two agents disagree on approach | **Mediator** | Inter-agent conflict |
| Agent output diverges from contract | **Overseer** | Off-track detection |
| Contradictory message loop | **Mediator** | Inter-agent conflict; if no mediator, overseer escalates to HITL |

## Relationship to Existing Health Checks

Pipeline health monitoring extends the existing [health check framework](../../orchestrator/health_checks/README.md):

| Component | Role | Runs |
|-----------|------|------|
| **Tier 1 health checks** (existing) | Structural invariant checks (container liveness, state consistency, consensus stall detection) | At lifecycle triggers (STARTUP, RUNTIME_TICK, etc.) |
| **Tier 2 health checks** (existing) | LLM-powered semantic analysis of agent progress | At WAVE_COMPLETE (if Tier 1 degraded), PHASE_COMPLETE, ON_DEMAND |
| **Orchestrator tripwires** (new) | Deterministic real-time monitoring of structured progress events | Continuously, event-driven |
| **Overseer agent** (new) | LLM-powered analysis of ambiguous failures, corrective action | Continuously, poll-based + escalation-driven |

The orchestrator tripwires process structured agent logs in real-time (event-driven), while the existing health check framework runs at discrete lifecycle points. The overseer agent provides deeper semantic analysis than Tier 2 health checks, with the ability to take corrective action (redirects, issue filing) rather than just reporting status.

## Related Documentation

- [Concurrent Execution Guide](concurrent-execution.md) — BRC consensus protocol and agent coordination
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes, health check framework
- [Agent Roles Reference](../reference/agent-roles.md) — All agent roles including overseer
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Phase execution and agent orchestration
