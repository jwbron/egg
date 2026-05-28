# Health Check Framework

Two-tier health check framework for proactive pipeline failure detection. Catches both infrastructure failures (containers missing, state inconsistencies) and semantic failures (agents completed but produced no artifacts).

> **Tier 2 registration status:** The framework retains full two-tier *capability* — the `HealthTier.AGENT` enum and the Tier 1 → Tier 2 escalation logic in `runner.py` are intact, so a new Tier 2 check can be registered without framework changes. However, **no Tier 2 checks are currently registered.** `AgentInspectorCheck` (the only Tier 2 check) was removed as unused in [#2850](https://github.com/jwbron/egg/pull/2850); every active check below is Tier 1. The Tier 2 rows and notes in this doc describe framework behavior that activates only once a Tier 2 check is registered.

## Architecture

```
HealthCheckRunner
├── registers checks via .register()
├── dispatches by trigger + tier
├── emits results to EventBus
│
├── Tier 1 (Programmatic) ─── fast, deterministic, always run
│   ├── ContainerLivenessCheck
│   ├── StartupStateCheck
│   ├── PhaseOutputPresenceCheck
│   ├── StateConsistencyCheck
│   ├── ConsensusStallCheck
│   └── IncompleteConsensusStallCheck
```

## Core Types (`types.py`)

### Enums

| Enum | Values | Purpose |
|------|--------|---------|
| `HealthStatus` | `HEALTHY`, `DEGRADED`, `FAILED` | Check outcome |
| `HealthTier` | `PROGRAMMATIC` (tier1), `AGENT` (tier2) | Which tier |
| `HealthTrigger` | `STARTUP`, `RUNTIME_TICK`, `WAVE_COMPLETE`, `PHASE_COMPLETE`, `ON_DEMAND` | When to run |
| `HealthAction` | `CONTINUE`, `FAIL_PIPELINE`, `ALERT` | Suggested response |

### HealthCheck Protocol

Structural protocol (no inheritance required). Any class with these attributes and method is a valid check:

```python
class MyCheck:
    name: str = "my_check"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset({HealthTrigger.ON_DEMAND})

    def run(self, context: PipelineHealthContext) -> HealthResult:
        ...
```

Implementations must never raise — catch internal errors and return a `HealthResult` with appropriate status.

### HealthResult

Frozen dataclass returned by every check:

```python
HealthResult(
    status=HealthStatus.HEALTHY,
    check_name="my_check",
    tier=HealthTier.PROGRAMMATIC,
    reasoning="All containers alive.",
    action=HealthAction.CONTINUE,
    details={"container_count": 3},  # arbitrary debug data
)
```

Call `.to_dict()` to serialize for JSON/event payloads.

## Context (`context.py`)

`PipelineHealthContext` packages everything a check needs. Constructor parameters are cheap (already-loaded objects); expensive operations use lazy properties that compute on first access and cache the result.

**Cheap accessors:** `pipeline_id`, `branch`, `current_phase`

**Lazy properties (compute once, cache):**

| Property | What it does | Used by |
|----------|-------------|---------|
| `git_log` | `git log --oneline -20` on the branch | _None — see note below_ |
| `git_diff_stat` | `git diff --stat origin/main...HEAD` (truncated to ~4000 tokens) | _None — see note below_ |
| `agent_outputs` | Reads `.egg-state/` files (max 4KB each) | StateConsistencyCheck |
| `contract` | Parses SDLC contract JSON for the pipeline's issue | _None — see note below_ |
| `live_container_ids` | Lists running Docker containers | ContainerLivenessCheck, StartupStateCheck, StateConsistencyCheck |

> **Unused properties:** `git_log`, `git_diff_stat`, and `contract` are defined on the context but consumed by no currently registered check — they were the removed `AgentInspectorCheck`'s (Tier 2) context fields, retained for a future Tier 2 check (see the Tier 2 registration status note at the top). Note that `StateConsistencyCheck` reads contract data via `agent_outputs` (scanning for a `contract` file), not via the `contract` property.

**Truncation:** `git_diff_stat` is capped at ~16,000 chars (~4000 tokens) via `_TIER2_CHAR_CAP` in `context.py` (the constant keeps its legacy `TIER2` name). Because `git_diff_stat` is currently unused (see the note above), this bound is presently inert. Agent output files — which *are* consumed, by `StateConsistencyCheck` — are capped at 4KB each.

## Runner (`runner.py`)

`HealthCheckRunner` is the central dispatcher:

1. **Register checks** via `runner.register(check)` at startup
2. **Run checks** via `runner.run(context, trigger)` — filters by trigger, applies tier escalation
3. **Emit events** to the EventBus for each result and an aggregate completion event

### Tier Escalation Logic

| Trigger | Tier 1 | Tier 2 |
|---------|--------|--------|
| `STARTUP` | Always | Never |
| `RUNTIME_TICK` | Always | Never |
| `WAVE_COMPLETE` | Always | Only if Tier 1 returned DEGRADED |
| `PHASE_COMPLETE` | Always | Always |
| `ON_DEMAND` | Always | Always |

### Helper

`worst_action(results)` returns the most severe action across a list of results: `FAIL_PIPELINE` > `ALERT` > `CONTINUE`.

## Tier 1 Checks

### ContainerLivenessCheck (`tier1/container_liveness.py`)

Verifies containers the pipeline considers RUNNING actually exist in Docker.

- **Triggers:** All
- **FAILED** (+ FAIL_PIPELINE): Expected containers missing from Docker
- **HEALTHY**: All expected containers alive, or pipeline not RUNNING

### StartupStateCheck (`tier1/startup_state.py`)

Belt-and-suspenders verification that startup reconciliation worked. Adapts existing `reconcile_stale_containers` logic.

- **Triggers:** STARTUP, ON_DEMAND
- **FAILED** (+ FAIL_PIPELINE): Stale containers or agents with missing Docker containers
- **HEALTHY**: No stale entries, or pipeline not RUNNING

### PhaseOutputPresenceCheck (`tier1/phase_output.py`)

Detects the issue-835 pattern: agents completed successfully but produced no artifacts.

- **Triggers:** WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND
- **Phase-specific logic:**
  - **implement**: Checks for commits on the remote branch (`git rev-list --count`)
  - **plan**: Checks for plan artifacts in `.egg-state/drafts/`
  - **Other phases**: Returns HEALTHY (no artifact requirements yet)
- **DEGRADED** (+ ALERT): Agents completed but expected artifacts missing
- **HEALTHY**: Artifacts present or no completed agents

### StateConsistencyCheck (`tier1/state_consistency.py`)

Cross-references orchestrator state against Docker reality and contract state.

- **Triggers:** RUNTIME_TICK, WAVE_COMPLETE, PHASE_COMPLETE, ON_DEMAND
- **Three checks:**
  1. RUNNING agents with missing Docker containers → **FAILED**
  2. Container status mismatch (container FAILED/EXITED but agent RUNNING) → **FAILED**
  3. COMPLETE agents with PENDING contract tasks → **DEGRADED**
- Uses `worst_action` to determine aggregate severity

### ConsensusStallCheck (`tier1/consensus_stall.py`)

Detects BRC consensus-complete-but-phase-stuck conditions for concurrent execution phases.

- **Triggers:** RUNTIME_TICK, ON_DEMAND
- **DEGRADED** (+ ALERT): All agents confirmed but phase still RUNNING past grace period (default 60s)
- **HEALTHY**: Pipeline not running, phase not using concurrent execution, within grace period, or consensus not yet complete
- Recovery is driven by `ContainerMonitor._handle_consensus_stall_recovery()`: tracker reconstruction first, then aggressive agent/phase completion with optimistic locking

### IncompleteConsensusStallCheck (`tier1/incomplete_consensus_stall.py`)

Detects BRC consensus-*incomplete*-and-not-progressing conditions: most agents have confirmed but one or more remain stuck (e.g. in a heartbeat loop after a re-review cycle).

- **Triggers:** RUNTIME_TICK, ON_DEMAND
- **DEGRADED** (+ ALERT): The same set of blocking (unconfirmed) agents persists for `stall_tick_threshold` consecutive ticks (default 10), after the phase grace period (default 300s) and post-proposal grace period have elapsed
- **HEALTHY**: Pipeline not running, phase not using concurrent execution, within a grace period, no blocking agents, the blocking set is still changing, or blocking agents show recent progress activity
- Purely diagnostic — recovery is escalated to the overseer (`details.recovery_action = escalate_to_overseer`)

## Integration Points

### Startup (`cli.py`)

Runner is initialized with all Tier 1 checks registered (no Tier 2 checks are currently registered — see the Tier 2 registration status note at the top). Stored in `app.config["HEALTH_CHECK_RUNNER"]` for route access. Runs STARTUP checks on all RUNNING pipelines.

### Container Monitor (`container_monitor.py`)

`set_health_check_runner()` connects the runner to the monitor. RUNTIME_TICK checks fire when container state changes are detected.

### Wave Complete (`concurrent_executor.py`)

WAVE_COMPLETE checks run after each agent wave. If `worst_action` returns `FAIL_PIPELINE`, wave execution breaks.

### Phase Advance (`routes/phases.py`)

PHASE_COMPLETE checks run before phase transitions. If `worst_action` returns `FAIL_PIPELINE`, the advance is blocked with a 409 Conflict response containing the health check results.

### On-Demand Endpoint (`routes/health.py`)

```
GET /api/v1/pipelines/{pipeline_id}/health
```

Runs all checks with ON_DEMAND trigger. Returns aggregate status + per-check results:

```json
{
    "pipeline_id": "issue-99",
    "status": "healthy",
    "results": [
        {
            "status": "healthy",
            "check_name": "container_liveness",
            "tier": "tier1",
            "reasoning": "All 3 expected containers are alive.",
            "action": "continue",
            "details": {},
            "timestamp": "2024-01-15T12:00:00Z"
        }
    ],
    "timestamp": "2024-01-15T12:00:00Z"
}
```

Status codes: 200 (checks executed), 404 (pipeline not found), 503 (runner not initialized).

## Adding a New Check

1. Create a class satisfying the `HealthCheck` protocol in `tier1/`
2. Set `name`, `tier`, `triggers`, and implement `run(context) -> HealthResult`
3. Never raise from `run()` — catch internal errors and return a HealthResult with appropriate status
4. Export from the tier's `__init__.py`
5. Register in `cli.py` startup: `runner.register(MyNewCheck())`

For Tier 2 checks specifically: use `HealthAction.ALERT` (not `FAIL_PIPELINE`) and degrade gracefully on API/external service failures.

## Related

- [Orchestrator README](../README.md)
- [Orchestrator Architecture](../../docs/architecture/orchestrator.md)
- Issue [#850](https://github.com/jwbron/egg/issues/850) — Design and motivation
