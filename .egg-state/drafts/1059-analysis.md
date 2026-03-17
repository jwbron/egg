# Analysis: Implement two-tier pipeline health monitoring — orchestrator tripwires + overseer agent

> Issue: #1059 | Phase: refine

## Problem Statement

Pipeline health monitoring is currently limited to basic container lifecycle events and periodic health checks. When agents stall, loop, go off-track, or encounter ambiguous failures, there is no automated detection or corrective response. The coordinator agent (now removed per #1164) previously had some reactive failure handling, but that capability was lost. The system needs a robust, cost-efficient monitoring architecture that handles both clear-cut failures instantly and ambiguous situations with semantic analysis.

The desired outcome is a **two-tier monitoring system**:
1. **Tier 1 (Orchestrator)**: Deterministic, in-process rule evaluation against structured agent progress events — instant response, zero LLM cost.
2. **Tier 2 (Overseer Agent)**: LLM-powered analysis for ambiguous cases, using Haiku for classification and Sonnet/Opus for decision-making, running as a separate container.

## Current Behavior

### Existing Health Check Infrastructure

The orchestrator already has a health check framework (`orchestrator/health_checks/`):

- **`HealthCheckRunner`** (`runner.py`): Dispatches checks by trigger type (STARTUP, RUNTIME_TICK, WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND). Supports Tier 1 → Tier 2 escalation.
- **Tier 1 checks** (programmatic):
  - `container_liveness.py`: Verifies live container IDs
  - `phase_output.py`: Validates output artifact presence
  - `startup_state.py`: Pipeline state consistency
  - `state_consistency.py`: State validation
- **Tier 2 checks** (agent-based):
  - `agent_inspector.py`: Deep code/output inspection via LLM
- **Health endpoints**: `/api/v1/health`, `/api/v1/ready`, `/api/v1/live`, `/api/v1/pipelines/<id>/health`

### Existing Event System

The `EventBus` (`events.py`) provides pub/sub for ~25 event types including pipeline lifecycle, phase, agent, container, messaging, consensus, and health check events. Any component can subscribe to real-time events.

### Existing Message Bus

Inter-agent messaging via REST endpoints (`/api/v1/pipelines/<id>/messages`) supports send, poll, and consume operations.

### Existing Container Monitoring

`ContainerMonitor` (`container_monitor.py`) provides periodic health checks and tracks container state changes (started, stopped, exited, failed, removed, unhealthy).

### What's Missing

1. **Structured agent progress reporting** — agents have no way to report what step they're on, whether they're blocked, or what they're working on. The orchestrator can only observe container liveness, not semantic progress.
2. **Deterministic tripwire rules** — no heartbeat timeout enforcement, no repeated error detection, no message volume throttling, no progress stall detection.
3. **Semantic analysis of ambiguous situations** — no ability to distinguish "agent doing legitimate long-running work" from "agent is stuck."
4. **Corrective action capability** — no automated nudge/redirect mechanism when agents stall or go off-track.
5. **Autonomous diagnostic issue filing** — no ability to file GitHub issues with structured diagnostics when problems can't be self-resolved.
6. **Overseer agent role** — no `OVERSEER` role in the agent role system, no container spawn logic for a monitoring-only agent.

## Constraints

### Technical Constraints

- **Agent SDK dependency (#1088)**: All LLM calls must use `shared/egg_agent/` (`run_agent_async`). This dependency is already merged.
- **Coordinator removal (#1164)**: The coordinator has been removed. The overseer fills part of that gap (reactive failure handling) but is NOT a coordinator replacement — it is read-only and cannot direct agent workflow. This dependency is already closed.
- **Agent roster (#1030)**: The OVERSEER role definition needs to be added to `shared/egg_contracts/agent_roles.py` and `gateway/agent_restrictions.py`. #1030 is still open but the pattern for adding roles is well-established.
- **No repo access for overseer**: The overseer container must not have repo volume mounts — it operates exclusively through orchestrator APIs, GitHub API, and message bus.
- **Gateway enforcement**: The gateway must enforce overseer restrictions (no git push, no PR merge, no phase advancement). The dual-layer enforcement model (Python-side + gateway-side) must be updated.
- **Existing health check framework**: The new deterministic tripwires should integrate with the existing `HealthCheckRunner` and `HealthCheck` protocol rather than creating a parallel system.
- **Hallucination risk in multi-tier agents**: Anthropic's internal findings show parent agents can fabricate child results. The Haiku→Sonnet/Opus escalation flow must enforce explicit data dependencies.

### Operational Constraints

- **Cost efficiency**: Haiku for frequent classification, Sonnet/Opus only on anomaly escalation. Most issues resolved deterministically without any LLM cost.
- **Latency**: Deterministic tripwires must fire immediately (in-process). LLM-based analysis has inherent latency (~seconds per call) which is acceptable for ambiguous cases.
- **Always-on**: One overseer per pipeline, auto-spawned at pipeline start, runs until completion. The two-tier design keeps costs low despite always-on operation.

### Scope Boundaries

- **Token budget tracking**: Explicitly deferred per issue requirements.
- **Mediator boundary**: Contradictory message loops and inter-agent disagreements go to the mediator, not the overseer. If no mediator exists, overseer escalates to HITL.
- **Restart authority**: HITL only — overseer cannot restart agents autonomously.

## Options Considered

### Option A: Extend Existing Health Check Framework

**Approach**: Add new Tier 1 health checks for deterministic tripwires (heartbeat, error repeats, message volume, progress stalls) within the existing `HealthCheckRunner`. The overseer becomes a specialized Tier 2 check provider that uses the existing escalation path.

**Pros**:
- Reuses existing health check infrastructure (runner, check protocol, triggers, results)
- Consistent with the established Tier 1 → Tier 2 pattern already in the codebase
- Unified health reporting through existing endpoints
- Lower implementation complexity — extends proven patterns

**Cons**:
- The existing framework is trigger-based (STARTUP, RUNTIME_TICK, etc.), not event-driven — may need adaptation for real-time tripwires
- The overseer needs a continuous monitoring loop, which is different from the on-demand health check model
- May strain the abstraction if the overseer's responsibilities are too different from a "health check"

### Option B: Parallel Monitoring Subsystem

**Approach**: Build a new `orchestrator/health_monitor.py` module for deterministic tripwires and a separate overseer agent container, operating independently of the existing health check framework.

**Pros**:
- Clean separation between existing health checks (pipeline state validation) and new monitoring (agent behavior analysis)
- The overseer's continuous monitoring loop is a natural fit for an independent subsystem
- Easier to reason about and test in isolation
- Clearer ownership boundary: health checks = pipeline state, health monitor = agent behavior

**Cons**:
- Duplication of some patterns (event subscription, result reporting)
- Two health-related subsystems to maintain
- May diverge over time

### Option C: Hybrid — Deterministic Tripwires In-Process, Overseer as Container Agent

**Approach**: Add deterministic tripwire processing as an event-driven component within the orchestrator process (subscribing to EventBus events and evaluating structured progress data against thresholds). The overseer runs as a separate container agent (spawned like other agents) that receives escalations from the orchestrator's tripwire processor and performs LLM-powered analysis.

**Pros**:
- Deterministic tripwires fire immediately in-process with zero latency
- Overseer container is isolated with appropriate restrictions (no repo access)
- Clean architectural boundary: orchestrator = deterministic rules, overseer = semantic analysis
- The overseer is "just another agent" from the container spawning perspective
- Aligns exactly with the issue's two-tier design
- Structured progress API serves both tiers

**Cons**:
- The overseer container needs access to orchestrator APIs, which adds network dependency
- Two deployment units to manage (in-process tripwire processor + container)
- Need to define a clear escalation protocol between the two tiers

## Recommended Approach

**Option C: Hybrid** — This is the approach specified in the issue and it is architecturally sound. The deterministic tier handles the 90%+ of cases that are clear-cut, keeping costs and latency minimal. The LLM tier handles the remaining ambiguous cases where human-like judgment is needed.

**Key architectural decisions supporting this choice:**

1. **Structured progress API**: A new `POST /api/v1/pipelines/<id>/progress` endpoint and `egg-orch progress emit` CLI command give agents a standard way to report progress. The orchestrator processes these events deterministically; the overseer consumes them via `GET /api/v1/pipelines/<id>/progress` for semantic analysis.

2. **Event-driven tripwires**: The orchestrator's tripwire processor subscribes to EventBus events (AGENT_STARTED, MESSAGE_SENT, etc.) and evaluates structured progress data against configurable thresholds in PipelineConfig. This is in-process and immediate.

3. **Container-based overseer**: The overseer runs as a standard agent container with the `OVERSEER` role, spawned via `ContainerSpawner` with no repo volume mounts. It uses `egg_agent` (`run_agent_async`) for Haiku classification and Sonnet/Opus decision-making.

4. **Escalation via message bus**: The orchestrator escalates ambiguous situations to the overseer via the existing message bus (`/api/v1/pipelines/<id>/messages`), providing structured context (anomaly type, agent role, relevant progress events).

### Implementation Scope Assessment

This is a **high-complexity** task spanning multiple subsystems:

- **New orchestrator components**: Structured progress API (routes, CLI), deterministic tripwire processor, overseer auto-spawn logic
- **New agent role**: OVERSEER in both `agent_roles.py` and `agent_restrictions.py`, with gateway enforcement
- **New container configuration**: Overseer container without repo mounts, with appropriate env vars
- **Agent prompt instructions**: CLAUDE.md section for the overseer's monitoring loop, tiered escalation, and self-monitoring
- **Agent updates**: All agent CLAUDE.md sections need `egg-orch progress emit` instructions
- **Testing**: Unit tests for tripwire logic, integration tests for escalation flow, mock tests for LLM classification

### Key Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `orchestrator/models.py` | Modify | Add OVERSEER to AgentRole, add tripwire config fields to PipelineConfig |
| `shared/egg_contracts/agent_roles.py` | Modify | Add OVERSEER AgentRoleDefinition with FileAccessPattern |
| `gateway/agent_restrictions.py` | Modify | Add OVERSEER file access patterns (no repo access) |
| `orchestrator/routes/progress.py` | Create | Structured progress ingestion and query endpoints |
| `orchestrator/cli/progress.py` | Create | `egg-orch progress emit` and `progress query` CLI commands |
| `orchestrator/health_monitor.py` | Create | Deterministic tripwire processor (event-driven) |
| `orchestrator/cli/health.py` | Modify/Create | `egg-orch health alerts` CLI command |
| `orchestrator/routes/pipelines.py` | Modify | Auto-spawn overseer alongside phase agents |
| `orchestrator/container_spawner.py` | Modify | Support overseer container config (no repo mount) |
| `sandbox/.claude/rules/mission.md` | Modify | Add `egg-orch progress emit` instructions for all agents |
| `sandbox/.claude/rules/overseer.md` | Create | Overseer agent prompt instructions |

### Dependencies

| Dependency | Status | Impact |
|------------|--------|--------|
| #1088 (Agent SDK migration) | **Merged** | No blocker — `shared/egg_agent/` is available |
| #1164 (Coordinator removal) | **Closed** | No blocker — coordinator already removed |
| #1030 (Agent team roster) | **Open** | Moderate risk — OVERSEER role definition depends on #1030's pattern for adding new roles. However, the pattern is well-established and the OVERSEER can be added independently if needed. |

## Open Questions

The following questions have been registered as contract decisions (written to `.egg-state/contracts/1059.json`). Note: the gateway contract API was unreachable from this container, so decisions were written directly to the contract file.

### decision-1: Tripwire Integration Architecture

**Should the overseer's deterministic tripwires integrate with the existing HealthCheckRunner framework, or be a separate event-driven module?**

The existing framework is trigger-based (STARTUP, RUNTIME_TICK) while tripwires need real-time event processing against structured progress data.

- [ ] **opt-1**: Extend existing HealthCheckRunner with real-time event support
- [ ] **opt-2**: New separate health_monitor.py module (recommended — cleaner separation of concerns: health checks validate pipeline state, health monitor watches agent behavior)
- [ ] Other (explain in reply)

### decision-2: Progress Data Persistence

**Should the structured progress API store progress events persistently or in-memory only?**

Persistent storage enables post-mortem analysis but increases state file size; in-memory is simpler but loses data on orchestrator restart.

- [ ] **opt-1**: Persistent (stored in pipeline state)
- [ ] **opt-2**: In-memory with configurable retention window (recommended — progress events are high-volume and temporal; post-mortem analysis can use oversight logs instead)
- [ ] **opt-3**: Hybrid — in-memory for processing, periodic snapshot to disk
- [ ] Other (explain in reply)

### decision-3: Orchestrator-to-Overseer Communication

**How should the orchestrator communicate escalations to the overseer container?**

The overseer has no repo access and relies entirely on APIs.

- [ ] **opt-1**: Existing message bus — overseer polls /messages like other agents (recommended — no new API surface, consistent with inter-agent communication patterns)
- [ ] **opt-2**: Dedicated escalation endpoint — POST /api/v1/pipelines/<id>/overseer/escalations
- [ ] **opt-3**: Overseer polls health alerts — GET /api/v1/pipelines/<id>/health/alerts
- [ ] Other (explain in reply)

### decision-4: Autonomous Issue Filing Policy

**Should the overseer auto-file GitHub issues autonomously, or require HITL approval?**

The issue spec says autonomous filing, but this creates noise risk if the overseer misfires or if Haiku classification is wrong.

- [ ] **opt-1**: Fully autonomous issue filing (as specified in issue)
- [ ] **opt-2**: Issue filing requires HITL approval (safer, less noise)
- [ ] **opt-3**: Autonomous for high-confidence classifications, HITL for low-confidence (recommended — balances speed with safety)
- [ ] Other (explain in reply)

### decision-5: OVERSEER Role vs. #1030 Dependency

**Should the OVERSEER role be added independently or wait for #1030 (agent team roster)?**

#1030 is still open but the pattern for adding roles is well-established in the codebase. Adding independently avoids blocking.

- [ ] **opt-1**: Add OVERSEER independently (recommended — the pattern is clear and #1030's scope is broader)
- [ ] **opt-2**: Wait for #1030 to land first
- [ ] Other (explain in reply)

### decision-6: Default Decision-Maker Model

**What default model should the overseer's Sonnet/Opus decision-maker tier use?**

The issue mentions "Sonnet or Opus for higher-stakes decisions" but doesn't specify the default.

- [ ] **opt-1**: Sonnet by default (recommended — cheaper, sufficient for most corrective decisions)
- [ ] **opt-2**: Opus by default (better reasoning for complex situations)
- [ ] **opt-3**: Configurable per-pipeline via PipelineConfig
- [ ] Other (explain in reply)

## Complexity Assessment

**High** — This is a cross-cutting architectural change spanning the orchestrator (new APIs, event-driven processing, auto-spawn), shared libraries (new role definition), gateway (new restrictions), container configuration (repo-less containers), agent prompts (overseer instructions + progress emission for all agents), and the Agent SDK integration. Multiple independent workstreams could be parallelized (e.g., structured progress API, tripwire processor, overseer agent role, agent prompt updates).

---

*Authored-by: egg*
