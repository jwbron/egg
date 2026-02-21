# Health Check Framework

Two-tier health check framework for proactive pipeline failure detection. Catches both infrastructure failures (containers missing, state inconsistencies) and semantic failures (agents completed but produced no artifacts).

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
│   └── StateConsistencyCheck
│
└── Tier 2 (Semantic) ─── LLM-based, conditional (phase 2)
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
| `git_log` | `git log --oneline -20` on the branch | Tier 2 |
| `git_diff_stat` | `git diff --stat origin/main...HEAD` | Tier 2 |
| `agent_outputs` | Reads `.egg-state/` files (max 4KB each) | StateConsistencyCheck, Tier 2 |
| `live_container_ids` | Lists running Docker containers | ContainerLivenessCheck, StartupStateCheck, StateConsistencyCheck |

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

## Integration Points

### Startup (`cli.py`)

Runner is initialized with all Tier 1 checks registered. Stored in `app.config["HEALTH_CHECK_RUNNER"]` for route access. Runs STARTUP checks on all RUNNING pipelines.

### Container Monitor (`container_monitor.py`)

`set_health_check_runner()` connects the runner to the monitor. RUNTIME_TICK checks fire when container state changes are detected.

### Wave Complete (`multi_agent.py`)

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

1. Create a class satisfying the `HealthCheck` protocol in the appropriate tier directory
2. Set `name`, `tier`, `triggers`, and implement `run(context) -> HealthResult`
3. Export from the tier's `__init__.py`
4. Register in `cli.py` startup: `runner.register(MyNewCheck())`

## Related

- [Orchestrator README](../README.md)
- [Orchestrator Architecture](../../docs/architecture/orchestrator.md)
- Issue [#850](https://github.com/jwbron/egg/issues/850) — Design and motivation
