# Plan: Holistic pipeline failure detection with two-tier health check framework

> Issue: #850 | Phase: plan | Pipeline: issue-850

## Approach

This PR introduces a unified health check framework in the orchestrator that
replaces the current collection of ad-hoc failure detection mechanisms with a
composable, two-tier architecture. The framework is implemented in a new
`orchestrator/health/` package.

**Tier 1 (Programmatic)** checks are fast, deterministic, and run on every
lifecycle event. They cover structural invariants: container liveness, startup
reconciliation, phase output presence (the check that would have caught
issue-835), state consistency, and repeated failure patterns.

**Tier 2 (Agent Inspector)** is an LLM-based evaluator (Haiku) that runs at
phase boundaries and when Tier 1 signals degradation. It reads agent outputs,
git diffs, and execution results to determine whether agents accomplished
meaningful work — catching semantic failures that structural checks cannot.

Both tiers implement a shared `HealthCheck` interface and produce a common
`HealthResult` with structured verdicts (`HEALTHY`/`DEGRADED`/`FAILED`) and
suggested actions (`CONTINUE`/`FAIL_PIPELINE`/`ALERT`/`RETRY`). A central
`HealthCheckRunner` manages registration, trigger filtering, Tier 1-to-Tier 2
escalation, and result application (event emission, state persistence, retry
consultation via `AgentRetryManager`).

The existing startup reconciliation (`startup_reconciliation.py`) and container
monitor detection (`container_monitor.py`) are migrated to the new interface
without changing their observable behavior. Trigger points are wired into the
orchestrator lifecycle at `STARTUP`, `RUNTIME_TICK`, `WAVE_COMPLETE`,
`PHASE_COMPLETE`, and `ON_DEMAND` (via a new REST endpoint).

### Key design decisions

1. **New `orchestrator/health/` package** rather than extending existing files.
   The health check framework is a cohesive concern. Avoids bloating
   `routes/pipelines.py` (already 5500+ lines).
2. **Lazy context assembly** for `PipelineHealthContext`. Expensive fields (git
   log/diff, agent output files) are only computed when accessed via
   `@cached_property`. Runtime tick checks only need in-memory state.
3. **Checks are pure evaluation functions** — context in, result out. The
   runner handles all side effects (event emission, state mutation, retry
   consultation). This makes every check independently testable.
4. **Haiku for Tier 2** — structured evaluation, not open-ended reasoning.
   Fast, cheap, upgradeable per-check.
5. **FAIL_PIPELINE is automatic but consulted against retry logic** — the
   runner checks `AgentRetryManager` before failing. Retries if available,
   fails if exhausted.
6. **Inspector failures are non-blocking** — API timeout or malformed response
   yields `DEGRADED`/`CONTINUE`, not a pipeline failure.
7. **Results persisted in `PhaseExecution.health_checks`**, capped at 10 per
   phase to prevent state bloat.

### Backward compatibility

Startup reconciliation and container monitor behavior is preserved exactly.
Error messages, state mutations, and event emission remain identical. Original
functions are kept as deprecated wrappers. All schema changes use optional
fields with defaults.

## Phase breakdown

### Phase 1: Core types, interface, and runner

**Goal:** Define the foundation that all checks and lifecycle integration build
on. This is the `orchestrator/health/` package skeleton with the abstract
`HealthCheck` base class, all type definitions, the `PipelineHealthContext`
with lazy loading, and the `HealthCheckRunner` that manages the check
registry, trigger filtering, Tier 1-to-Tier 2 escalation, and result
application.

**Files:**
- `orchestrator/health/__init__.py` — public API exports
- `orchestrator/health/types.py` — `HealthStatus`, `Action`, `Tier`, `Trigger`
  enums and `HealthResult` dataclass
- `orchestrator/health/base.py` — abstract `HealthCheck` class
- `orchestrator/health/context.py` — `PipelineHealthContext` with lazy
  `@cached_property` fields for git_log, git_diff, agent_output_files
- `orchestrator/health/runner.py` — `HealthCheckRunner` with register(),
  run_checks(), _apply_result(). Uses per-pipeline locking and optimistic
  versioning. Emits `HEALTH_CHECK` events. Caps results at 10 per phase.
  Consults `AgentRetryManager` before `FAIL_PIPELINE`.

### Phase 2: Pipeline state model extension

**Goal:** Add `HealthCheckResult` model to pipeline state for persistence and
observability.

**Files:**
- `orchestrator/models.py` — add `HealthCheckResult` Pydantic model and
  `health_checks: list[HealthCheckResult]` field to `PhaseExecution`

### Phase 3: Migrate startup reconciliation

**Goal:** Wrap the existing `reconcile_stale_containers()` logic in a
`StartupReconciliationCheck` implementing `HealthCheck`. Preserve exact
existing error messages and state mutations.

**Files:**
- `orchestrator/health/checks/__init__.py` — checks sub-package init
- `orchestrator/health/checks/startup.py` — `StartupReconciliationCheck`
  (tier=PROGRAMMATIC, triggers=[STARTUP])
- `orchestrator/startup_reconciliation.py` — deprecate, keep as wrapper

### Phase 4: Migrate container monitor detection

**Goal:** Extract `_reconcile_container_state()` evaluation logic into a
`ContainerLivenessCheck`. The monitor continues polling Docker and emitting
`ContainerEvent`s; the check evaluates and the runner applies.

**Files:**
- `orchestrator/health/checks/container_liveness.py` —
  `ContainerLivenessCheck` (tier=PROGRAMMATIC, triggers=[RUNTIME_TICK])
- `orchestrator/container_monitor.py` — update reconciliation handler to route
  through `HealthCheckRunner`

### Phase 5: New Tier 1 programmatic checks

**Goal:** Add the three new programmatic checks that cover currently-undetected
failure modes.

**Sub-tasks:**
1. **Phase output presence** — the check that would have caught issue-835.
   Evaluates by phase: implement checks for commits on remote, plan checks for
   architect/planner output files, refine checks for analysis document. Returns
   `DEGRADED` on missing outputs. Triggers: `PHASE_COMPLETE`, `WAVE_COMPLETE`.
2. **State consistency** — verifies containers marked RUNNING in pipeline state
   actually exist in Docker. Returns `DEGRADED` on drift, `FAILED` on critical
   inconsistency. Triggers: `WAVE_COMPLETE`, `PHASE_COMPLETE`.
3. **Repeated failure pattern** — detects agents failing repeatedly with the
   same error. Configurable threshold (default 3). Replaces the blunt
   `max_waves=5` silent stop with a structured verdict. Triggers:
   `WAVE_COMPLETE`.

**Files:**
- `orchestrator/health/checks/phase_output.py` — `PhaseOutputPresenceCheck`
- `orchestrator/health/checks/state_consistency.py` — `StateConsistencyCheck`
- `orchestrator/health/checks/failure_patterns.py` — `RepeatedFailureCheck`

### Phase 6: Tier 2 agent inspector

**Goal:** Implement the LLM-based semantic health evaluator that catches novel
failure patterns.

**Sub-tasks:**
1. **LLM client** — thin wrapper for Anthropic API calls from the orchestrator.
   Uses Haiku by default. JSON mode for structured output. 30-second timeout.
   Graceful failure handling (returns None on API error).
2. **Agent inspector check** — assembles structured prompt with phase goal,
   agent outputs, git log/diff, and execution results. LLM evaluates whether
   agents produced meaningful work. Parses structured JSON verdict into
   `HealthResult`. On LLM failure: returns `DEGRADED`/`CONTINUE`.
   Triggers: `PHASE_COMPLETE`, `ON_DEMAND`; also `WAVE_COMPLETE` when Tier 1
   returns `DEGRADED`.

**Files:**
- `orchestrator/health/llm_client.py` — `HealthInspectorClient`
- `orchestrator/health/checks/agent_inspector.py` — `AgentInspectorCheck`
  (tier=AGENT, triggers=[PHASE_COMPLETE, ON_DEMAND])

### Phase 7: Lifecycle wiring and observability

**Goal:** Wire `HealthCheckRunner` into the orchestrator's main execution
paths at all trigger points. Add on-demand health check REST endpoint.

**Integration points:**
1. `orchestrator/api.py` — create `HealthCheckRunner` singleton, register all
   checks, expose via module-level getter
2. `orchestrator/cli.py` — replace direct `reconcile_stale_containers()` call
   with `runner.run_checks(trigger=STARTUP)`
3. `orchestrator/container_monitor.py` — update
   `create_pipeline_reconciliation_handler()` to route through runner
4. `orchestrator/multi_agent.py` — ensure `on_wave_complete` callback invokes
   `runner.run_checks(trigger=WAVE_COMPLETE)`
5. `orchestrator/routes/pipelines.py` — invoke
   `runner.run_checks(trigger=PHASE_COMPLETE)` after phase completion; act on
   results (FAIL_PIPELINE, RETRY, ALERT, CONTINUE)
6. `orchestrator/routes/health.py` — add `POST
   /api/v1/pipelines/<id>/health-check` endpoint for on-demand evaluation

**Files:**
- `orchestrator/api.py`
- `orchestrator/cli.py`
- `orchestrator/container_monitor.py`
- `orchestrator/multi_agent.py`
- `orchestrator/routes/pipelines.py`
- `orchestrator/routes/health.py`

### Phase 8: Tests

**Goal:** Comprehensive test coverage for the entire framework. Existing tests
continue passing.

**Test categories:**
1. **Type and interface tests** — HealthResult creation, enum values, context
   lazy loading verification
2. **Individual check tests** — each HealthCheck implementation tested with
   mock PipelineHealthContext: startup reconciliation (mock Docker),
   container liveness (mock pipeline state), phase output presence (mock
   git/filesystem), state consistency (mock Docker vs state), repeated
   failure (mock execution history), agent inspector (mock LLM client)
3. **Runner tests** — trigger filtering, Tier 1-to-Tier 2 escalation, result
   application (event emission, state persistence, cap at 10), retry
   consultation, concurrent execution safety
4. **Integration tests** — lifecycle wiring: startup trigger runs startup
   check, container failure triggers runtime check, wave complete triggers
   wave checks, phase complete triggers all checks including Tier 2,
   on-demand REST endpoint returns results
5. **Backward compatibility tests** — migrated startup reconciliation and
   container monitor produce identical state changes as originals

**Files:**
- `orchestrator/tests/test_health_types.py`
- `orchestrator/tests/test_health_context.py`
- `orchestrator/tests/test_health_checks.py`
- `orchestrator/tests/test_health_runner.py`
- `orchestrator/tests/test_health_integration.py`

## Test strategy

1. **Unit tests** for every new component with mock dependencies. Each
   `HealthCheck` implementation receives a mock `PipelineHealthContext` and
   returns a `HealthResult` — no real Docker, git, or LLM calls.
2. **Runner tests** verify trigger filtering, escalation, and result
   application with mock checks that return controlled results.
3. **Integration tests** verify the full lifecycle wiring by mocking external
   dependencies (Docker client, LLM API, git subprocess) and exercising the
   real runner with real checks.
4. **Backward compatibility tests** run the original startup reconciliation and
   container monitor functions alongside their migrated HealthCheck versions on
   the same input, asserting identical pipeline state mutations.
5. **Existing test suites** must continue passing unchanged — no modifications
   to existing test files.

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Wiring into pipelines.py (5500+ lines) introduces bugs | Medium | High | Minimal integration: single `runner.run_checks()` calls at well-defined lifecycle events. No structural changes to `_run_pipeline()`. |
| Concurrency: multiple threads running checks on same pipeline | Medium | Medium | Per-pipeline locking (`get_pipeline_state_lock`) and optimistic versioning. Short-circuit if pipeline already FAILED. |
| Tier 1 check performance on runtime ticks | Low | Medium | Lazy context — runtime tick checks only use in-memory state. Git operations only on PHASE_COMPLETE/WAVE_COMPLETE. 5s timeout per Tier 1 check. |
| LLM API unreliability for Tier 2 | Low | Medium | Inspector failure → DEGRADED/CONTINUE (non-blocking). 30s timeout. JSON mode for structured output. |
| Migration breaks existing behavior | Low | Low | Preserve exact error messages. Comparison tests against original functions. Keep originals as deprecated wrappers. |
| Tier 2 LLM cost accumulation | Low | Low | Haiku at ~$0.001/call. Typical pipeline: 3-4 PHASE_COMPLETE calls. Can disable per-pipeline if needed. |

```yaml
# yaml-tasks
pr:
  title: "Add two-tier health check framework for pipeline failure detection"
  description: |
    Introduces a unified health check framework in the orchestrator with two tiers:
    Tier 1 (programmatic checks for structural invariants) and Tier 2 (LLM-based
    agent inspector for semantic health). Migrates existing startup reconciliation
    and container monitor to the new interface. Adds new checks for phase output
    presence, state consistency, and repeated failure patterns. Wires trigger points
    into the orchestrator lifecycle and surfaces results via the EventBus/SSE system.
phases:
  - id: 1
    name: Core types, interface, and runner
    goal: Define the HealthCheck interface, type system, lazy PipelineHealthContext, and HealthCheckRunner in orchestrator/health/
    tasks:
      - id: TASK-1-1
        description: Create orchestrator/health/types.py with HealthStatus (HEALTHY/DEGRADED/FAILED), Action (CONTINUE/FAIL_PIPELINE/ALERT/RETRY), Tier (PROGRAMMATIC/AGENT), Trigger (STARTUP/RUNTIME_TICK/WAVE_COMPLETE/PHASE_COMPLETE/ON_DEMAND) enums and HealthResult dataclass
        acceptance: All enums instantiable with expected values; HealthResult holds check_name, tier, status, reason, action, timestamp, and metadata dict
        files:
          - orchestrator/health/__init__.py
          - orchestrator/health/types.py
      - id: TASK-1-2
        description: Create orchestrator/health/base.py with abstract HealthCheck class defining tier, triggers, name properties and check(context) -> HealthResult method
        acceptance: HealthCheck is an abstract base class; subclasses must implement tier, triggers, name, and check()
        files:
          - orchestrator/health/base.py
      - id: TASK-1-3
        description: Create orchestrator/health/context.py with PipelineHealthContext using @cached_property for lazy git_log, git_diff, and agent_output_files; eager fields for pipeline, phase_execution, containers, agents
        acceptance: Eager fields available on construction; lazy fields only computed when accessed; context works with mock data for testing
        files:
          - orchestrator/health/context.py
      - id: TASK-1-4
        description: Create orchestrator/health/runner.py with HealthCheckRunner implementing register(), run_checks(trigger, context) -> list[HealthResult], _apply_result(). Uses per-pipeline locking, emits HEALTH_CHECK events, caps results at 10 per phase, consults AgentRetryManager before FAIL_PIPELINE
        acceptance: Runner filters checks by trigger; runs Tier 1 first, escalates to Tier 2 on DEGRADED or at PHASE_COMPLETE; emits events; persists results; retries before failing
        files:
          - orchestrator/health/runner.py
  - id: 2
    name: Pipeline state model extension
    goal: Add HealthCheckResult model to PhaseExecution for result persistence
    tasks:
      - id: TASK-2-1
        description: Add HealthCheckResult Pydantic model (check_name, tier, status, reason, action, timestamp, metadata) to orchestrator/models.py
        acceptance: Model serializes/deserializes correctly; all fields have appropriate types
        files:
          - orchestrator/models.py
      - id: TASK-2-2
        description: Add health_checks field (list[HealthCheckResult], default empty) to PhaseExecution model
        acceptance: Existing pipelines without health_checks deserialize with empty list; new results can be appended
        files:
          - orchestrator/models.py
  - id: 3
    name: Migrate startup reconciliation
    goal: Wrap reconcile_stale_containers() in StartupReconciliationCheck implementing HealthCheck with identical observable behavior
    dependencies:
      - phase-1
    tasks:
      - id: TASK-3-1
        description: Create orchestrator/health/checks/ sub-package with __init__.py
        acceptance: Package importable
        files:
          - orchestrator/health/checks/__init__.py
      - id: TASK-3-2
        description: Create StartupReconciliationCheck in orchestrator/health/checks/startup.py (tier=PROGRAMMATIC, triggers=[STARTUP]). Extract evaluation logic from reconcile_stale_containers(); return HealthResult with FAILED status and affected pipeline/container IDs in reason when stale containers found
        acceptance: Check returns FAILED with descriptive reason for stale containers; returns HEALTHY when all containers live; does not mutate state directly
        files:
          - orchestrator/health/checks/startup.py
      - id: TASK-3-3
        description: Deprecate reconcile_stale_containers() in startup_reconciliation.py; keep as thin wrapper for backward compatibility
        acceptance: Original function still callable; delegates to StartupReconciliationCheck internally
        files:
          - orchestrator/startup_reconciliation.py
  - id: 4
    name: Migrate container monitor detection
    goal: Extract container failure evaluation into ContainerLivenessCheck with identical observable behavior
    dependencies:
      - phase-1
    tasks:
      - id: TASK-4-1
        description: Create ContainerLivenessCheck in orchestrator/health/checks/container_liveness.py (tier=PROGRAMMATIC, triggers=[RUNTIME_TICK]). Extract evaluation logic from _reconcile_container_state(); return HealthResult
        acceptance: Check returns FAILED when RUNNING pipeline has a stale/failed container; returns HEALTHY otherwise; preserves error message format
        files:
          - orchestrator/health/checks/container_liveness.py
      - id: TASK-4-2
        description: Update create_pipeline_reconciliation_handler() in container_monitor.py to route through HealthCheckRunner instead of calling _reconcile_container_state() directly
        acceptance: Container monitor FAILED events trigger HealthCheckRunner.run_checks(RUNTIME_TICK); state mutations happen via runner._apply_result(); identical observable behavior
        files:
          - orchestrator/container_monitor.py
  - id: 5
    name: New Tier 1 programmatic checks
    goal: Add phase output presence, state consistency, and repeated failure pattern checks
    dependencies:
      - phase-1
    tasks:
      - id: TASK-5-1
        description: Create PhaseOutputPresenceCheck in orchestrator/health/checks/phase_output.py (tier=PROGRAMMATIC, triggers=[PHASE_COMPLETE, WAVE_COMPLETE]). Check by phase type — implement checks git log for commits on remote, plan checks for output files, refine checks for analysis draft. Return DEGRADED on missing outputs
        acceptance: Returns DEGRADED when implement phase has no remote commits; returns DEGRADED when plan phase has no output files; returns HEALTHY when expected artifacts exist
        files:
          - orchestrator/health/checks/phase_output.py
      - id: TASK-5-2
        description: Create StateConsistencyCheck in orchestrator/health/checks/state_consistency.py (tier=PROGRAMMATIC, triggers=[WAVE_COMPLETE, PHASE_COMPLETE]). Verify containers marked RUNNING in pipeline state exist in Docker. Return DEGRADED on partial drift, FAILED on critical inconsistency
        acceptance: Returns DEGRADED when some containers gone; returns FAILED when all containers gone but pipeline RUNNING; returns HEALTHY when state matches Docker
        files:
          - orchestrator/health/checks/state_consistency.py
      - id: TASK-5-3
        description: Create RepeatedFailureCheck in orchestrator/health/checks/failure_patterns.py (tier=PROGRAMMATIC, triggers=[WAVE_COMPLETE]). Detect same role failing N times (configurable, default 3) with same/similar error. Return FAILED with action FAIL_PIPELINE
        acceptance: Returns FAILED when agent fails 3+ times with same error pattern; returns HEALTHY when failures are diverse or under threshold; reason includes error pattern description
        files:
          - orchestrator/health/checks/failure_patterns.py
  - id: 6
    name: Tier 2 agent inspector
    goal: Implement LLM-based semantic health evaluator using Haiku for detecting novel failure patterns
    dependencies:
      - phase-1
    tasks:
      - id: TASK-6-1
        description: Create HealthInspectorClient in orchestrator/health/llm_client.py wrapping Anthropic API. Uses Haiku model. JSON mode for structured output. 30-second timeout. Returns None on API failure
        acceptance: Client returns structured JSON response on success; returns None on timeout/API error; configurable model
        files:
          - orchestrator/health/llm_client.py
      - id: TASK-6-2
        description: Create AgentInspectorCheck in orchestrator/health/checks/agent_inspector.py (tier=AGENT, triggers=[PHASE_COMPLETE, ON_DEMAND]). Assemble prompt with phase goal, agent outputs, git log/diff, execution results. Parse LLM JSON verdict into HealthResult. On LLM failure return DEGRADED/CONTINUE
        acceptance: Returns structured HealthResult from LLM verdict; handles malformed LLM responses gracefully; returns DEGRADED/CONTINUE on any LLM failure; prompt includes all relevant context
        files:
          - orchestrator/health/checks/agent_inspector.py
  - id: 7
    name: Lifecycle wiring and observability
    goal: Wire HealthCheckRunner into orchestrator lifecycle at all trigger points and add on-demand REST endpoint
    dependencies:
      - phase-2
      - phase-3
      - phase-4
      - phase-5
      - phase-6
    tasks:
      - id: TASK-7-1
        description: Create HealthCheckRunner singleton in orchestrator/api.py, register all checks, expose via module-level getter
        acceptance: Runner singleton is initialized at app creation with all 6 checks registered; accessible from routes and CLI
        files:
          - orchestrator/api.py
      - id: TASK-7-2
        description: Replace direct reconcile_stale_containers() call in cli.py with runner.run_checks(trigger=STARTUP)
        acceptance: Startup reconciliation runs through the health check framework; same pipelines marked FAILED on stale containers
        files:
          - orchestrator/cli.py
      - id: TASK-7-3
        description: Update create_pipeline_reconciliation_handler() in container_monitor.py to use runner (finalize the routing change from Phase 4)
        acceptance: Container FAILED events trigger health check framework; state mutations via runner
        files:
          - orchestrator/container_monitor.py
      - id: TASK-7-4
        description: Wire on_wave_complete callback in multi_agent.py to invoke runner.run_checks(trigger=WAVE_COMPLETE) with wave results in context
        acceptance: Wave completion triggers Tier 1 checks; DEGRADED escalates to Tier 2; results persisted
        files:
          - orchestrator/multi_agent.py
      - id: TASK-7-5
        description: Invoke runner.run_checks(trigger=PHASE_COMPLETE) after phase completion in routes/pipelines.py. Act on results — FAIL_PIPELINE marks pipeline failed, RETRY triggers agent retry, ALERT emits event only, CONTINUE proceeds
        acceptance: Phase completion triggers all checks including Tier 2; FAIL_PIPELINE result marks pipeline FAILED; RETRY triggers retry logic
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-7-6
        description: Add POST /api/v1/pipelines/<id>/health-check endpoint to routes/health.py for on-demand evaluation returning aggregated health results
        acceptance: Endpoint returns JSON array of HealthCheckResult for the pipeline; triggers all checks including Tier 2; returns 404 for unknown pipeline
        files:
          - orchestrator/routes/health.py
  - id: 8
    name: Tests
    goal: Comprehensive test coverage for the health check framework; existing tests unchanged
    dependencies:
      - phase-7
    tasks:
      - id: TASK-8-1
        description: Write unit tests for types, enums, HealthResult creation, and PipelineHealthContext lazy loading in test_health_types.py and test_health_context.py
        acceptance: All enum values tested; HealthResult creation and serialization tested; lazy fields verified not computed until accessed
        files:
          - orchestrator/tests/test_health_types.py
          - orchestrator/tests/test_health_context.py
      - id: TASK-8-2
        description: Write unit tests for each HealthCheck implementation in test_health_checks.py with mock PipelineHealthContext — startup reconciliation, container liveness, phase output presence, state consistency, repeated failure, agent inspector
        acceptance: Each check tested for HEALTHY, DEGRADED, and FAILED paths; edge cases covered; mock dependencies only
        files:
          - orchestrator/tests/test_health_checks.py
      - id: TASK-8-3
        description: Write unit tests for HealthCheckRunner in test_health_runner.py — trigger filtering, Tier 1-to-Tier 2 escalation, result application, event emission, result cap at 10, retry consultation, concurrent safety
        acceptance: Runner correctly filters by trigger; escalates on DEGRADED; emits events; respects result cap; consults retry manager
        files:
          - orchestrator/tests/test_health_runner.py
      - id: TASK-8-4
        description: Write integration tests for lifecycle wiring in test_health_integration.py — startup trigger, container failure trigger, wave complete trigger, phase complete trigger, on-demand REST endpoint
        acceptance: Full lifecycle verified end-to-end with mocked Docker and LLM API; each trigger invokes correct checks
        files:
          - orchestrator/tests/test_health_integration.py
      - id: TASK-8-5
        description: Write backward compatibility tests verifying migrated startup reconciliation and container monitor produce identical state changes as originals
        acceptance: Same pipeline state mutations for identical input; error messages preserved
        files:
          - orchestrator/tests/test_health_integration.py
```
