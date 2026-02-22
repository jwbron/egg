# Analysis: Holistic pipeline failure detection: programmatic checks + agent inspector

> Issue: #850 | Phase: refine

## Problem Statement

Pipeline failure detection in the orchestrator is currently a collection of ad-hoc mechanisms, each built reactively for a specific failure mode. There is no shared abstraction, no unified trigger model, and no ability to detect **semantic failures** — cases where infrastructure looks healthy but agents didn't accomplish meaningful work.

The issue-835 post-mortem is the motivating example: reviewer containers exited cleanly (exit code 0), all structural checks passed, but the reviewers had nothing to review because the coders' work never reached the remote. The current detection mechanisms cannot catch this class of failure.

The desired outcome is a two-tier health check framework:
1. **Tier 1 (Programmatic)**: Fast, deterministic checks for structural invariants — run on every lifecycle event.
2. **Tier 2 (Agent Inspector)**: LLM-based semantic evaluation — run when Tier 1 passes but something looks wrong, or at phase boundaries.

Both tiers should implement a shared `HealthCheck` interface and produce a common `HealthResult` with structured verdicts (`HEALTHY` / `DEGRADED` / `FAILED`) and suggested actions.

## Current Behavior

Failure detection is spread across three independent mechanisms with no shared interface:

### 1. Startup Reconciliation (`orchestrator/startup_reconciliation.py`)

Runs once at orchestrator startup (called from `cli.py:cmd_serve`). Compares persisted `RUNNING` pipeline state against live Docker containers. If a container ID is missing from Docker, the agent and pipeline are marked `FAILED`. This handles orchestrator crashes but has no ongoing role.

- **Trigger**: Orchestrator startup only
- **Scope**: Missing containers (crash recovery)
- **Action**: Mark pipeline `FAILED` with restart instructions

### 2. Container Monitor (`orchestrator/container_monitor.py`)

Background thread polling Docker every 10 seconds. Detects container state transitions and emits `ContainerEvent`s. A pipeline reconciliation handler (`create_pipeline_reconciliation_handler`) listens for `FAILED` events (non-zero exit) and marks the corresponding pipeline as `FAILED`.

- **Trigger**: Runtime, every 10 seconds
- **Scope**: Container exit with non-zero code
- **Action**: Mark pipeline `FAILED` via state store

### 3. Implicit Signal-Based Detection (`orchestrator/routes/signals.py`)

Agents self-report completion or errors via `POST /pipelines/{id}/signal`. The `handle_error_signal` handler records the failure in the contract via the dispatcher. The `handle_complete_signal` handler records success and checks if the wave/phase is complete.

- **Trigger**: Agent-initiated signal
- **Scope**: Agent self-reported errors
- **Action**: Record in contract, mark agent `FAILED`

### 4. Blunt Iteration Caps (`orchestrator/multi_agent.py:execute_all_waves`)

`max_waves=5` prevents infinite wave loops. When reached, execution stops silently with a log warning. No structured verdict or suggested action is produced.

- **Trigger**: Wave count exceeds cap
- **Scope**: Runaway iteration prevention
- **Action**: Stop execution (no pipeline status update)

### What's Missing

None of these mechanisms can detect:
- **Phase output absence**: Implement phase completes but no commits exist on the remote branch
- **Semantic emptiness**: Reviewer exits cleanly but reviewed nothing (issue-835 scenario)
- **State inconsistencies**: Orchestrator's view of agent status diverges from Docker reality during runtime (only caught at startup)
- **Repeated failure patterns**: Same agent failing with the same error across retries
- **Degraded progress**: Agent sends heartbeats but makes no meaningful progress

### Existing Infrastructure to Build On

The codebase has several patterns and abstractions that support this work:

- **EventBus** (`orchestrator/events.py`): Pub/sub with typed `EventType` enum, wildcard subscriptions, async delivery, and event history. Already has `HEALTH_CHECK` and `ERROR` event types. Supports surfacing health results via SSE.
- **SSE streaming** (`orchestrator/sse.py`): Real-time pipeline events to external consumers. Health check results can flow through this channel.
- **Resilience patterns** (`shared/egg_contracts/resilience.py`): `RetryWithBackoff`, `CircuitBreaker`, `TimeoutCheckpoint` — reusable for health check execution.
- **Agent recovery** (`shared/egg_contracts/agent_recovery.py`): `AgentRetryManager`, `AgentCircuitBreaker`, `ConflictDetector` — provides failure classification and retry logic.
- **Pipeline state model** (`orchestrator/models.py`): Rich `Pipeline`, `PhaseExecution`, `AgentExecution`, `ContainerInfo` models with status enums — sufficient context for programmatic checks.
- **Per-pipeline locking** (`orchestrator/state_store.py`): `get_pipeline_state_lock()` with optimistic versioning — ensures health check state updates don't race with signal handlers.
- **Orchestrator metrics** (`orchestrator/metrics.py`): Counter, Gauge, Histogram classes — can track health check execution counts, latencies, and outcomes.

## Constraints

- **Performance**: Tier 1 checks run on every lifecycle event and must be fast (sub-second). They cannot involve LLM calls, network I/O beyond Docker, or expensive git operations.
- **Cost**: Tier 2 (agent inspector) involves LLM API calls. Must be triggered judiciously — only at phase boundaries, on explicit demand, or when Tier 1 flags `DEGRADED`. Cannot run on every 10-second tick.
- **Concurrency**: Health checks must not deadlock with signal handlers or state writers. The existing per-pipeline lock (`get_pipeline_state_lock`) and optimistic versioning pattern must be respected.
- **Backward compatibility**: Existing startup reconciliation and container monitor behavior must be preserved. Migration to the new interface should not change observable behavior for consumers.
- **State persistence**: Health check results should be persisted in pipeline state (for debugging/observability) but must not bloat the state JSON. A rolling window of recent results is appropriate.
- **Network mode**: Tier 2 inspector needs Anthropic API access. In private mode, this is available. In public mode, it goes through the proxy. The inspector must work in both modes.
- **Testing**: Each health check must be independently testable. The `HealthCheck` interface should support dependency injection of context (no singleton access to Docker, state store, etc. inside check logic).

## Options Considered

### Option A: Unified HealthCheck Interface (as proposed in issue)

**Approach**: Define a shared `HealthCheck` abstract class with `tier`, `triggers`, and `check(context) -> HealthResult`. Migrate existing mechanisms (startup reconciliation, container monitor) to implement this interface. Add new Tier 1 checks (phase output presence, state consistency) and Tier 2 agent inspector. Wire trigger points into the lifecycle.

**Pros**:
- Clean abstraction — all failure detection through one interface
- Extensible — new checks are just new `HealthCheck` implementations
- Unified result model — `HealthResult` with `status`, `reason`, `action` standardizes all failure detection output
- Existing mechanisms can be migrated incrementally
- Event system integration is straightforward (emit `HEALTH_CHECK` events with `HealthResult` data)

**Cons**:
- Migration of startup reconciliation and container monitor requires careful refactoring — they currently write directly to pipeline state, which would need to go through a mediator
- The `PipelineHealthContext` assembly may be expensive for some trigger points (e.g., gathering git log/diff for every wave completion)
- Introduces a new abstraction layer that every future failure detection change must go through

### Option B: Event-Driven Health Evaluator (No New Interface)

**Approach**: Instead of a new `HealthCheck` interface, extend the existing `EventBus` to include health evaluation as event handlers. Add new event types (`WAVE_HEALTH_CHECK`, `PHASE_HEALTH_CHECK`). Subscribe health evaluation functions directly as event handlers. Keep startup reconciliation and container monitor as-is, and add new handlers alongside them.

**Pros**:
- No new abstraction — reuses existing `EventBus` pub/sub
- Minimal migration — existing mechanisms stay untouched
- Lower initial effort for Tier 1 checks (just add event handlers)

**Cons**:
- No unified result model — each handler decides its own output format
- Tier classification (programmatic vs. agent) becomes implicit rather than explicit
- Trigger management is scattered across handler registrations instead of declared on the check
- Harder to reason about which checks run at which lifecycle points
- Testing requires mocking the event bus rather than calling a check directly
- Doesn't solve the core problem of fragmented, ad-hoc detection — just adds more handlers

### Option C: Phased Approach — Tier 1 Only First

**Approach**: Implement Option A's `HealthCheck` interface and Tier 1 programmatic checks only. Defer Tier 2 (agent inspector) to a follow-up issue. Migrate existing mechanisms, add phase output presence and state consistency checks. Skip the LLM evaluation layer.

**Pros**:
- Reduced scope — delivers the unified interface and most valuable checks without LLM complexity
- Phase output presence check alone would have caught issue-835 (`DEGRADED` on missing remote commits)
- Establishes the interface for Tier 2 to slot into later
- No API cost concerns for initial rollout

**Cons**:
- Defers the semantic health evaluation that catches novel failure patterns
- Some failures (reviewer reviewed nothing, coder changes don't address issue) remain undetectable
- Requires a follow-up issue for Tier 2, adding coordination overhead

## Recommended Approach

**Option A (Unified HealthCheck Interface)** is the right approach. The issue description already provides a well-considered design and the codebase has the infrastructure to support it.

The key reasons:

1. **The interface pays for itself immediately.** The `HealthCheck` / `HealthResult` / `PipelineHealthContext` trio provides a testable, composable abstraction that replaces ad-hoc state mutations with structured verdicts. This matters for debugging — instead of scanning logs for reconciliation warnings, operators get typed results with reasons and suggested actions.

2. **Migration is mechanical.** Both `startup_reconciliation.py` and `container_monitor.py` already follow the pattern of "gather context → evaluate condition → mutate state." Wrapping them in `HealthCheck.check()` with a `HealthResult` return value is straightforward. The state mutation can move to a central `HealthCheckRunner` that applies the result.

3. **Tier 2 is the differentiator but Tier 1 delivers immediate value.** The phase output presence check (Tier 1) would have caught issue-835 by detecting that no commits existed on the remote after the implement phase. The agent inspector (Tier 2) catches the broader class of semantic failures. Both should ship in the same change to establish the full framework.

4. **The event system provides the observability layer for free.** Health check results emit via `EventType.HEALTH_CHECK` → flow through `EventBus` → render in SSE streams → visible in `egg-pipeline-watch`. No new observability infrastructure needed.

The implementation should define the `HealthCheck` interface and result types in a new `orchestrator/health/` package, migrate existing mechanisms, add the new Tier 1 checks and Tier 2 inspector, and wire triggers into the lifecycle. Context assembly for `PipelineHealthContext` should be lazy — only gather git log/diff when a check that needs it is about to run.

## Open Questions

1. **Tier 2 model selection**: The agent inspector needs to call an LLM. Should it use the same Claude model configured for agent sandboxes, or a smaller/cheaper model (e.g., Haiku) to keep costs low? The inspector's task is structured evaluation, not open-ended coding, so a smaller model may suffice.

2. **Action enforcement**: When a health check returns `action: FAIL_PIPELINE`, should the framework automatically mark the pipeline as `FAILED`, or should it emit an event and let the existing phase/pipeline management code decide? Automatic enforcement is simpler but removes a human override point. Event-based leaves the action advisory but risks being ignored.

3. **Health check result persistence**: Should `HealthResult` entries be stored in the pipeline state JSON (alongside phases and agents), or in a separate file in the worktree (e.g., `.egg-state/health/{pipeline-id}.json`)? Pipeline state JSON keeps everything in one place but risks bloat. A separate file is cleaner but adds another artifact to manage.

4. **Tier 2 trigger threshold**: The proposal says Tier 2 runs "when Tier 1 passes but something still looks wrong." What defines "looks wrong"? Options include: (a) any Tier 1 check returns `DEGRADED`, (b) phase duration exceeds a configurable threshold, (c) always at `PHASE_COMPLETE`, or (d) configurable per-pipeline. This affects both cost and detection coverage.

5. **Retry integration**: When a health check detects a failure, should it interact with the existing `AgentRetryManager` and `AgentCircuitBreaker` in `shared/egg_contracts/agent_recovery.py` to determine if retry is appropriate before marking the pipeline `FAILED`? Or should health checks only report status and leave retry logic to the caller?

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: high
parallel_phases: false
```
