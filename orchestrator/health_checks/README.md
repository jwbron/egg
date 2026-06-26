# Health Check Framework

This package now hosts **two related mechanisms**. Know which one you are
touching:

1. **Detection plane** (`detection_plane.py`, `Finding`/`FindingClass`/`Severity`
   in `types.py`, and the `detect_*` functions in `tier1/`) — the
   orchestrator-side overseership delivered by the overseer overhaul
   ([#2270](https://github.com/jwbron/egg/issues/2270)). Cheap deterministic
   detectors run over an `EventStreamSnapshot` on the event loop and return
   `Finding | None`; ambiguous findings escalate to an on-demand overseer
   adjudicator, routine ones flow to the bounded corrective vocabulary. **No LLM
   for the normal majority.** See [Detection plane](#detection-plane-2270) below
   and [docs/architecture/overseer.md](../../docs/architecture/overseer.md).
2. **`HealthCheck` framework** (`HealthCheck` protocol + `HealthResult` in
   `types.py`, `runner.py`, `context.py`, and the `*Check` classes in `tier1/`)
   — the older lifecycle-triggered framework documented in the rest of this
   file. Runs at `STARTUP` / `WAVE_COMPLETE` / `PHASE_COMPLETE` / etc., returns
   `HealthResult`, and can block a phase advance via `FAIL_PIPELINE`.

Both live in this package and both use `tier1/`; a `detect_*` function and a
`*Check` class can sit side by side in the same module file.

---

Two-tier health check framework for proactive pipeline failure detection. Catches both infrastructure failures (containers missing, state inconsistencies) and semantic failures (agents completed but produced no artifacts).

> **Tier 2 registration status:** The framework retains full two-tier *capability* — the `HealthTier.AGENT` enum and the Tier 1 → Tier 2 escalation logic in `runner.py` are intact, so a new Tier 2 check can be registered without framework changes. However, **no Tier 2 checks are currently registered.** `AgentInspectorCheck` (the only Tier 2 check) was removed as unused in [#2850](https://github.com/jwbron/egg/pull/2850); every active check below is Tier 1. The Tier 2 rows and notes in this doc describe framework behavior that activates only once a Tier 2 check is registered.

## Detection plane (#2270)

`detection_plane.py` is the structural replacement for the old respawning
overseer watcher pod. Instead of a long-lived agent polling and an LLM
classifying every observation, the orchestrator runs a set of cheap,
deterministic **detectors** over an `EventStreamSnapshot` on its own event loop.

### Snapshot and Finding types

- **`EventStreamSnapshot`** — a frozen, point-in-time view of pipeline state a
  detector evaluates: `running_agents` (each a `RunningAgent` annotated with its
  `lifecycle_owner`), `consensus`, `phase_state`, `decision_state`,
  `container_transitions`, `gateway_error_counters`, `cost_counters`,
  `midturn_messages`, `git_state`, and a permissive `raw` passthrough. Built on
  the event loop by `snapshot_from_health_context(context)`; in tests it is
  parsed from the calibration corpus fixtures (same field names).
- **`LifecycleOwner`** (`ORCHESTRATOR` / `AGENT` / `NONE`) — who owns the agent
  lifecycle when the snapshot was taken. Under orchestrator-owned on-demand
  spawning ([#3064](https://github.com/jwbron/egg/issues/3064)) a phase can be
  RUNNING with zero live containers for a beat while the next one-shot agent is
  about to spawn — `ORCHESTRATOR`/`AGENT` means progress is queued, `NONE` means
  nothing is. This distinction is what makes the stall detector honest
  ([#3230](https://github.com/jwbron/egg/issues/3230)).
- **`Finding`** — a detector's output (`finding_class`, `severity`, `evidence`,
  `recommended_action`, `requires_adjudication`, `detector_key`). Routine
  findings carry `requires_adjudication=False` and are handled by the bounded
  corrective vocabulary with no LLM; only an ambiguous / high-stakes finding
  sets `requires_adjudication=True` and triggers the on-demand adjudicator.
- **`Severity`** (`info`/`low`/`medium`/`high`) and **`FindingClass`** are
  `StrEnum`s that compare equal to the plain strings the calibration corpus
  asserts against, so the production types plug straight into the harness.

### Detector protocol and the plane

A `Detector` is any callable carrying `detector_key` and `name` attributes:

```python
def detect_something(snapshot: EventStreamSnapshot) -> Finding | None:
    ...
detect_something.detector_key = "something"
detect_something.name = "something_detector"
```

The plane is a registry + evaluator:

- `DetectionPlane.default()` / `default_detection_plane()` builds a plane
  pre-wired with the lifecycle-owner-aware `PhaseStallDetector` (slice-4) plus
  the §5 coverage-gap survey (slice-8, registered from the `tier1/` modules).
- `plane.evaluate(snapshot)` runs every registered detector and collects the
  non-`None` findings. Execution is **exception-isolated** — a detector that
  raises degrades to "no finding" and is logged, never crashing the loop.
- `plane.detectors` exposes the registry keyed by `detector_key`.
- `escalate_findings(findings, spawn_adjudicator=…)` invokes the injected
  spawner **once per finding whose `requires_adjudication` is set** — never for
  the rest, and never at all when there are no findings. This is the cost guard.

### Runtime wiring

`routes/pipelines._run_overseer_detection_plane` builds the snapshot, evaluates
the default plane, and routes findings:
`_escalate_finding_to_adjudicator` spawns a normal on-demand OVERSEER agent for
each `requires_adjudication` finding (which **advises** only), and
`escalate_findings` is the canonical escalation gate. Routine findings are
executed by the `CorrectiveExecutor` (see
[overseer/README.md](../overseer/README.md)).

### Detector catalogue

`DetectionPlane.default()` registers these. **Adjudicate** = sets
`requires_adjudication=True`.

| Layer | `detector_key` | Adjudicate? |
|-------|----------------|:-----------:|
| core | `phase_stall` | ✅ |
| container / k8s | `container_death` | — |
| container / k8s | `container_oom_evicted` | — |
| container / k8s | `container_restart_loop` | ✅ |
| container / k8s | `overseer_self_injection` | — |
| orchestrator runtime | `runtime_thread_liveness` | ✅ |
| orchestrator runtime | `duration_drift` | — |
| orchestrator runtime | `agent_restart_propagation` | — |
| decision queue | `auto_advance_wedge` | ✅ |
| decision queue | `approved_decision_orphaned` | — |
| decision queue | `restarted_decision_replay` | — |
| decision queue | `hitl_queue_backlog` | — |
| worktree / branch | `worktree_corruption` | — |
| worktree / branch | `disk_inode_pressure` | — |
| worktree / branch | `pr_external_mutation` | — |
| worktree / branch | `pushed_pr_not_updated` | — |
| gateway | `gateway_error_spike` | — |
| gateway | `gateway_repeated_denial` | — |
| gateway | `gateway_token_expiry` | — |
| BRC / thrashing | `brc_thrash` | ✅ |
| BRC / thrashing | `incomplete_consensus_deferral` | — |
| cost / budget | `cost_anomaly` | — |
| LLM substrate | `llm_substrate_unreachable` | — |
| LLM substrate | `effective_model_drift` | — |
| LLM substrate | `anthropic_5xx` | — |
| overseer self-health | `overseer_self_health` | — |

> A detector only fires in a live run once `snapshot_from_health_context()`
> populates the field it reads; until then it stays silent. The calibration
> corpus ([overseer-calibration-corpus.md](../../docs/architecture/overseer-calibration-corpus.md))
> drives every detector with fully-populated fixtures.

### Adding a new detector

1. Add `detect_<thing>(snapshot) -> Finding | None` to the relevant `tier1/`
   module; attach `detector_key` / `name` attributes.
2. Keep it pure and total — never raise, never call an LLM. Set
   `requires_adjudication=True` only when the condition is genuinely ambiguous.
3. Register it in `detection_plane._register_coverage_gap_detectors` (or
   `DetectionPlane.default`).
4. Add known-normal and known-bad rows to the calibration corpus.

---

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

## Detection-Plane Detectors (`tier1/`)

`tier1/` also contains a second family of detectors — pure functions registered into `DetectionPlane` (see `detection_plane.py`), not into `HealthCheckRunner`. These implement the `Detector` protocol: `snapshot -> Finding | None`, where the input is an `EventStreamSnapshot` rather than a `PipelineHealthContext`. They are fast, deterministic, and LLM-free.

Slice-8 (#2270 §5) adds the coverage-gap detector survey:

| Module | Detectors |
|--------|-----------|
| `brc_thrashing.py` | BRC NACK-thrash, incomplete-consensus deferral cap |
| `container_k8s.py` | Container death, OOM eviction, restart loops, overseer self-injection |
| `cost_budget.py` | LLM cost anomaly / hourly budget breach |
| `decision_queue.py` | HITL queue backlog, auto-advance wedge, orphaned decisions, restarted-decision replay |
| `gateway_health.py` | Gateway error spike, repeated denial, token expiry |
| `llm_substrate.py` | LiteLLM unreachable, effective model drift, sustained Anthropic 5xx |
| `runtime_liveness.py` | Orchestrator thread liveness, duration drift, restart propagation |
| `worktree_branch.py` | Worktree corruption, disk/inode pressure, PR external mutation, pushed-PR-not-updated |

These are registered into `DetectionPlane` via `DetectionPlane.default()` (called from `detection_plane.py`). Each is also registered in the calibration corpus (`orchestrator/tests/overseer_calibration/`) by `detector_key`, so every detector has a strict regression assertion. See [Overseer Calibration Corpus](../../docs/architecture/overseer-calibration-corpus.md) for the corpus contract.

## Adding a New HealthCheck

1. Create a class satisfying the `HealthCheck` protocol in `tier1/`
2. Set `name`, `tier`, `triggers`, and implement `run(context) -> HealthResult`
3. Never raise from `run()` — catch internal errors and return a HealthResult with appropriate status
4. Export from the tier's `__init__.py`
5. Register in `cli.py` startup: `runner.register(MyNewCheck())`

For Tier 2 checks specifically: use `HealthAction.ALERT` (not `FAIL_PIPELINE`) and degrade gracefully on API/external service failures.

## Related

- [Overseer Architecture](../../docs/architecture/overseer.md) — the detection plane, on-demand adjudicator, and bounded corrective vocabulary (#2270)
- [Overseer package README](../overseer/README.md) — server-side adjudicator + corrective executor
- [Calibration corpus](../../docs/architecture/overseer-calibration-corpus.md) — the detector calibration contract
- [Orchestrator README](../README.md)
- [Orchestrator Architecture](../../docs/architecture/orchestrator.md)
- Issue [#850](https://github.com/jwbron/egg/issues/850) — Design and motivation
- Issue [#2270](https://github.com/jwbron/egg/issues/2270) — Overseer overhaul
