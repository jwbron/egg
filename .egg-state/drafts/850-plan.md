# Plan: Holistic pipeline failure detection with two-tier health check framework

> Issue: #850 | Phase: plan | Pipeline: issue-850 | Revision: 2
>
> Revision note: Addresses all plan reviewer feedback — two-strike FAIL_PIPELINE
> confirmation (critical), exception isolation at integration points (high),
> HEALTH_CHECK_MODE kill-switch (medium), plus minor observations on docs,
> circuit breaker thread safety, 2-PR justification, and test coverage.

## Approach

This PR introduces a unified health check framework in the orchestrator that
replaces the current collection of ad-hoc failure detection mechanisms with a
composable, two-tier architecture. The framework lives in a new
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
   `routes/pipelines.py` (5543 lines).
2. **Lazy context assembly** for `PipelineHealthContext`. Expensive fields (git
   log/diff, agent output files) are only computed when accessed via
   `@cached_property`. Runtime tick checks only need in-memory state.
3. **Checks are pure evaluation functions** — context in, result out. The
   runner handles all side effects (event emission, state mutation, retry
   consultation). This makes every check independently testable.
4. **Haiku for Tier 2** — structured evaluation, not open-ended reasoning.
   Fast, cheap, upgradeable per-check.
5. **Two-strike FAIL_PIPELINE confirmation** (reviewer feedback, critical) —
   the first `FAIL_PIPELINE` result from a check is recorded but not enforced
   (downgraded to `ALERT`). Only if the same check returns `FAIL_PIPELINE` on
   the next trigger is the action applied. Checks where false positives are
   impossible (container verified absent from Docker) can set
   `allow_immediate_fail=True` to bypass two-strike.
6. **Exception isolation at every layer** (reviewer feedback, high) — the
   runner wraps each individual check in `try/except` and returns
   `DEGRADED`/`CONTINUE` for that check on exception. Every integration site
   wraps `run_checks()` in `try/except` with logging. Health check failures
   must NEVER propagate to `_run_pipeline()`, the container monitor, or the
   HTTP server.
7. **HEALTH_CHECK_MODE kill-switch** (reviewer feedback, medium) — environment
   variable with values `enforce`/`observe`/`disabled`. Start in `observe` for
   safe rollout (emit events, persist results, but never mutate pipeline
   state). Graduate to `enforce` once confidence is established.
8. **Results persisted in `PhaseExecution.health_checks`**, capped at 10 per
   phase to prevent state bloat.
9. **Per-pipeline `AgentCircuitBreaker` instances** — avoids thread-safety
   issues (RISK-8). Each instance accessed only under per-pipeline lock during
   `_apply_result()`.
10. **Single PR delivery** — shipping both tiers together validates the
    interface handles both cleanly. The `HEALTH_CHECK_MODE=observe` kill-switch
    provides the same production safety as a staged 2-PR rollout. Tier 2 adds
    exactly 2 files with well-isolated scope. If review capacity is a concern,
    the PR can be reviewed in two logical passes: core+Tier1 first, then
    Tier2+wiring.

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
with lazy loading, and the `HealthCheckRunner` with two-strike confirmation,
HEALTH_CHECK_MODE support, and per-check exception isolation.

**Files:**
- `orchestrator/health/__init__.py` — public API exports
- `orchestrator/health/types.py` — `HealthStatus`, `Action`, `Tier`, `Trigger`,
  `HealthCheckMode` enums and `HealthResult` dataclass
- `orchestrator/health/base.py` — abstract `HealthCheck` class with
  `allow_immediate_fail` property
- `orchestrator/health/context.py` — `PipelineHealthContext` with lazy
  `@cached_property` fields for git_log, git_diff, agent_output_files,
  local_branch_head
- `orchestrator/health/runner.py` — `HealthCheckRunner` with register(),
  run_checks(), _apply_result(). Two-strike confirmation via _pending_failures
  dict. HEALTH_CHECK_MODE support. Per-check exception isolation. Per-pipeline
  locking and optimistic versioning. Emits `HEALTH_CHECK` events. Caps results
  at 10 per phase. Consults per-pipeline `AgentCircuitBreaker` instances before
  `FAIL_PIPELINE`. Singleton pattern: `_runner` + `get_health_check_runner()`.

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
  (tier=PROGRAMMATIC, triggers=[STARTUP], allow_immediate_fail=True)
- `orchestrator/startup_reconciliation.py` — deprecate, keep as wrapper

### Phase 4: Migrate container monitor detection

**Goal:** Extract `_reconcile_container_state()` evaluation logic into a
`ContainerLivenessCheck`. The monitor continues polling Docker and emitting
`ContainerEvent`s; the check evaluates and the runner applies.

**Files:**
- `orchestrator/health/checks/container_liveness.py` —
  `ContainerLivenessCheck` (tier=PROGRAMMATIC, triggers=[RUNTIME_TICK],
  allow_immediate_fail=True)
- `orchestrator/container_monitor.py` — update reconciliation handler to route
  through `HealthCheckRunner`

### Phase 5: New Tier 1 programmatic checks

**Goal:** Add the three new programmatic checks that cover currently-undetected
failure modes.

**Sub-tasks:**
1. **Phase output presence** — the check that would have caught issue-835.
   Checks LOCAL branch head first before remote (reviewer feedback). If local
   has commits but remote doesn't, returns `DEGRADED` (push may be in
   progress). If neither has commits, returns `DEGRADED`. Evaluates by phase:
   implement checks for commits, plan checks for output files, refine checks
   for analysis document. `allow_immediate_fail=False` (uses two-strike).
   Triggers: `PHASE_COMPLETE`, `WAVE_COMPLETE`.
2. **State consistency** — verifies containers marked RUNNING in pipeline state
   actually exist in Docker. Returns `DEGRADED` on drift, `FAILED` on critical
   inconsistency. `allow_immediate_fail=False` (uses two-strike — transient
   Docker API errors possible). Triggers: `WAVE_COMPLETE`, `PHASE_COMPLETE`.
3. **Repeated failure pattern** — detects agents failing repeatedly with the
   same error. Configurable threshold (default 3). Replaces the blunt
   `max_waves=5` silent stop with a structured verdict. `allow_immediate_fail=
   False`. Triggers: `WAVE_COMPLETE`.

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
   Graceful failure handling (returns None on API error). No connection reuse.
   If `ANTHROPIC_API_KEY` is missing, log warning and disable Tier 2.
2. **Agent inspector check** — assembles structured prompt with phase goal,
   agent outputs, git log/diff, and execution results. LLM evaluates whether
   agents produced meaningful work. Prompt instructs LLM to err on the side of
   `DEGRADED` over `FAILED`. Parses structured JSON verdict into
   `HealthResult`. On LLM failure: returns `DEGRADED`/`CONTINUE`.
   `allow_immediate_fail=False` (uses two-strike). Triggers: `PHASE_COMPLETE`,
   `ON_DEMAND`; also `WAVE_COMPLETE` when Tier 1 returns `DEGRADED`.

**Files:**
- `orchestrator/health/llm_client.py` — `HealthInspectorClient`
- `orchestrator/health/checks/agent_inspector.py` — `AgentInspectorCheck`
  (tier=AGENT, triggers=[PHASE_COMPLETE, ON_DEMAND])

### Phase 7: Lifecycle wiring and observability

**Goal:** Wire `HealthCheckRunner` into the orchestrator's main execution
paths at all trigger points. Add on-demand health check REST endpoint.
**Every integration site wraps `run_checks()` in `try/except`** — health check
failures must NEVER propagate to the pipeline execution thread, container
monitor, or HTTP server.

**Integration points:**
1. `orchestrator/health/runner.py` — add `init_health_check_runner()` to
   create singleton, register all 6 checks, return runner
2. `orchestrator/cli.py` — call `init_health_check_runner()` at startup.
   Replace direct `reconcile_stale_containers()` call with
   `runner.run_checks(trigger=STARTUP)`. Wrap in `try/except`; on exception,
   fall back to original `reconcile_stale_containers()`.
3. `orchestrator/container_monitor.py` — update handler to route through
   runner. Wrap `run_checks(trigger=RUNTIME_TICK)` in `try/except`; on
   exception, fall back to original `_reconcile_container_state()`.
4. `orchestrator/multi_agent.py` — pass `on_wave_complete` callback to
   `execute_all_waves()` (currently called without it at `pipelines.py:3260`).
   Callback wraps `run_checks(trigger=WAVE_COMPLETE)` in `try/except`;
   on exception, log and continue.
5. `orchestrator/routes/pipelines.py` — invoke
   `runner.run_checks(trigger=PHASE_COMPLETE)` after phase completion. Wrap in
   `try/except`; on exception, log and continue. Act on results
   (`FAIL_PIPELINE`, `RETRY`, `ALERT`, `CONTINUE`).
6. `orchestrator/routes/health.py` — add `POST
   /api/v1/pipelines/<id>/health-check` endpoint for on-demand evaluation.
   Document the endpoint with docstring describing request/response format.

**Files:**
- `orchestrator/health/runner.py`
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
   lazy loading verification, local_branch_head field, HealthCheckMode enum
2. **Individual check tests** — each HealthCheck implementation tested with
   mock PipelineHealthContext: startup reconciliation (mock Docker, verify
   allow_immediate_fail=True), container liveness (mock pipeline state, verify
   allow_immediate_fail=True), phase output presence (mock git/filesystem,
   verify local-before-remote logic, verify allow_immediate_fail=False), state
   consistency (mock Docker vs state), repeated failure (mock execution
   history), agent inspector (mock LLM client, verify graceful degradation)
3. **Runner tests** — trigger filtering, Tier 1-to-Tier 2 escalation,
   two-strike confirmation (first FAIL_PIPELINE downgraded to ALERT, second
   enforced, allow_immediate_fail=True bypasses, non-FAIL clears pending),
   HEALTH_CHECK_MODE behavior (enforce applies, observe emits only, disabled
   returns empty), exception isolation (check raising exception produces
   DEGRADED/CONTINUE), result application (event emission, state persistence,
   cap at 10), per-pipeline circuit breaker instances, concurrent execution
   safety
4. **Integration tests** — lifecycle wiring: startup trigger runs startup
   check, container failure triggers runtime check, wave complete triggers
   wave checks, phase complete triggers all checks including Tier 2,
   on-demand REST endpoint returns results. HEALTH_CHECK_MODE integration
   tests (observe emits but doesn't fail, disabled skips all).
5. **Exception isolation tests** — health check exception does NOT crash
   `_run_pipeline()`, container monitor handler, or `on_wave_complete`
   callback; health check timeout does NOT block phase transition
6. **Backward compatibility tests** — migrated startup reconciliation and
   container monitor produce identical state changes as originals
7. **Performance test** — 100 RUNTIME_TICK triggers with
   ContainerLivenessCheck complete in under 1 second with no git operations
   triggered (lazy context properties not accessed)

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
2. **Runner tests** verify trigger filtering, two-strike confirmation,
   HEALTH_CHECK_MODE enforcement, exception isolation, escalation, and result
   application with mock checks that return controlled results.
3. **Integration tests** verify the full lifecycle wiring by mocking external
   dependencies (Docker client, LLM API, git subprocess) and exercising the
   real runner with real checks.
4. **Exception isolation tests** verify that health check exceptions and
   timeouts never propagate to `_run_pipeline()`, the container monitor
   handler, or `on_wave_complete` callbacks. Each integration site is tested.
5. **Backward compatibility tests** run the original startup reconciliation and
   container monitor functions alongside their migrated HealthCheck versions on
   the same input, asserting identical pipeline state mutations.
6. **Performance test** runs 100 RUNTIME_TICK triggers and verifies sub-second
   execution with no git operations triggered.
7. **Existing test suites** must continue passing unchanged — no modifications
   to existing test files.

## Risks and mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **False positive pipeline termination** (RISK-4) | High/Critical | Three-layer protection: (1) two-strike confirmation — first FAIL_PIPELINE downgraded to ALERT, second enforced; allow_immediate_fail=True bypasses for verified-true checks. (2) PhaseOutputPresenceCheck checks local branch head first — DEGRADED on local-only commits. (3) HEALTH_CHECK_MODE=observe for initial rollout. |
| **Exception propagation from health checks** (RISK-1/REC-3) | High | Two-layer exception isolation: runner catches per-check exceptions → DEGRADED/CONTINUE. Integration sites catch runner exceptions → log and fall back. |
| **Wiring into pipelines.py** (RISK-1) | High | Minimal integration: single run_checks() calls at 2 well-defined lifecycle events. All wrapped in try/except. No structural changes to _run_pipeline(). |
| **Concurrent state mutations** (RISK-2) | Medium | Per-pipeline locking and optimistic versioning. Checks are pure functions — no locks during execution. Per-pipeline circuit breaker instances under lock (TD-11). |
| **New LLM dependency in orchestrator** (RISK-3) | Medium | Isolated in HealthInspectorClient. 30s timeout. Missing API key auto-disables Tier 2. Inspector failures → DEGRADED/CONTINUE. |
| **AgentCircuitBreaker thread safety** (RISK-8) | Medium | Per-pipeline instances stored in runner._circuit_breakers. Accessed only under per-pipeline lock. |
| **Backward compatibility of migrations** (RISK-6) | Low | Exact error messages preserved. Behavioral equivalence tests. Original functions kept as deprecated wrappers. |
| **Tier 1 performance on runtime ticks** (RISK-7) | Low | Lazy context — runtime tick checks only use in-memory state. Performance test: 100 ticks < 1 second. |
| **Tier 2 LLM cost** (RISK-9) | Low | Haiku ~$0.001/call. ~3-4 per pipeline. Disable via HEALTH_CHECK_MODE or by not setting ANTHROPIC_API_KEY. |

## Reviewer feedback addressed

| # | Feedback | Severity | Resolution |
|---|----------|----------|------------|
| 1 | FAIL_PIPELINE needs two-strike confirmation (REC-2) | Critical | TASK-1-4: runner implements _pending_failures dict. First FAIL_PIPELINE → ALERT. Repeated same-check → enforce. allow_immediate_fail=True bypasses for verified-true checks. |
| 2 | PhaseOutputPresenceCheck should check LOCAL branch head first | Critical | TASK-5-1: checks context.local_branch_head before remote. Local-only commits → DEGRADED (push may be in progress). |
| 3 | Exception isolation at integration points (REC-3) | High | TASK-7-4, TASK-7-5: explicit try/except at every integration site. Runner also catches per-check exceptions. Acceptance criteria require "exceptions never propagate to pipeline execution thread." |
| 4 | HEALTH_CHECK_MODE kill-switch (REC-4) | Medium | TASK-1-1 adds HealthCheckMode enum. TASK-1-4 reads env var and enforces mode. TASK-8-4 tests all three modes. |
| 5 | Documentation for POST endpoint | Minor | TASK-7-6 includes "document with docstring describing request/response format." |
| 6 | RISK-8 AgentCircuitBreaker not thread-safe | Minor | TASK-1-4: per-pipeline instances in runner._circuit_breakers, accessed only under per-pipeline lock. Added to risk table. |
| 7 | 2-PR split acknowledged and justified | Minor | Plan acknowledges REC-1 as valid. Single PR justified: HEALTH_CHECK_MODE=observe provides same safety; Tier 2 is 2 files; interface designed for both tiers. |
| 8 | Tests: exception isolation and RUNTIME_TICK performance | Minor | TASK-8-5 (exception isolation tests at each integration site), TASK-8-6 (100 RUNTIME_TICK < 1s performance test). |

```yaml
# yaml-tasks
pr:
  title: "Add two-tier health check framework for pipeline failure detection"
  description: |
    Introduces a unified health check framework in the orchestrator with two tiers:
    Tier 1 (programmatic checks for structural invariants) and Tier 2 (LLM-based
    agent inspector for semantic health). Migrates existing startup reconciliation
    and container monitor to the new interface. Adds new checks for phase output
    presence, state consistency, and repeated failure patterns. Includes two-strike
    FAIL_PIPELINE confirmation to prevent false positives, HEALTH_CHECK_MODE
    kill-switch (enforce/observe/disabled) for safe rollout, and two-layer exception
    isolation at every integration point. Wires trigger points into the orchestrator
    lifecycle and surfaces results via the EventBus/SSE system.
phases:
  - id: 1
    name: Core types, interface, and runner
    goal: Define the HealthCheck interface, type system, lazy PipelineHealthContext, HealthCheckRunner with two-strike confirmation, HEALTH_CHECK_MODE support, and per-check exception isolation in orchestrator/health/
    tasks:
      - id: TASK-1-1
        description: Create orchestrator/health/types.py with HealthStatus (HEALTHY/DEGRADED/FAILED), Action (CONTINUE/FAIL_PIPELINE/ALERT/RETRY), Tier (PROGRAMMATIC/AGENT), Trigger (STARTUP/RUNTIME_TICK/WAVE_COMPLETE/PHASE_COMPLETE/ON_DEMAND), HealthCheckMode (ENFORCE/OBSERVE/DISABLED) enums and HealthResult dataclass
        acceptance: All enums instantiable with expected values; HealthResult holds check_name, tier, status, reason, action, timestamp, and metadata dict; HealthCheckMode enum has three values matching env var options
        files:
          - orchestrator/health/__init__.py
          - orchestrator/health/types.py
      - id: TASK-1-2
        description: Create orchestrator/health/base.py with abstract HealthCheck class defining tier, triggers, name, allow_immediate_fail properties and check(context) -> HealthResult method. allow_immediate_fail defaults to False; when True, FAIL_PIPELINE bypasses two-strike confirmation
        acceptance: HealthCheck is an abstract base class; subclasses must implement tier, triggers, name, and check(); allow_immediate_fail defaults to False; can be overridden to True
        files:
          - orchestrator/health/base.py
      - id: TASK-1-3
        description: Create orchestrator/health/context.py with PipelineHealthContext using @cached_property for lazy git_log, git_diff, agent_output_files, and local_branch_head; eager fields for pipeline, phase_execution, containers, agents, docker_client, wave_results (optional)
        acceptance: Eager fields available on construction; lazy fields only computed when accessed; local_branch_head returns git rev-parse for local branch; context is per-invocation (never cached across triggers); works with mock data for testing
        files:
          - orchestrator/health/context.py
      - id: TASK-1-4
        description: "Create orchestrator/health/runner.py with HealthCheckRunner implementing: register(), run_checks(trigger, context) -> list[HealthResult], _apply_result(). Two-strike confirmation via _pending_failures dict keyed by (pipeline_id, check_name) — first FAIL_PIPELINE downgraded to ALERT, repeated same-check FAIL_PIPELINE enforced, allow_immediate_fail=True bypasses, non-FAIL result clears pending. HEALTH_CHECK_MODE support — enforce applies actions, observe emits events only, disabled returns empty. Per-check exception isolation — each check wrapped in try/except, exceptions produce synthetic DEGRADED/CONTINUE. Per-pipeline AgentCircuitBreaker instances in _circuit_breakers dict accessed only under per-pipeline lock. Per-pipeline locking and optimistic versioning for state mutations. Emits HEALTH_CHECK events. Caps results at 10 per phase. Singleton pattern: _runner + get_health_check_runner(). Configurable per-check timeout (5s Tier 1, 30s Tier 2)"
        acceptance: "Runner filters checks by trigger; runs Tier 1 first, escalates to Tier 2 on DEGRADED or at PHASE_COMPLETE/ON_DEMAND; two-strike confirmation works correctly (first FAIL_PIPELINE -> ALERT, second -> enforce, allow_immediate_fail bypasses, non-FAIL clears); HEALTH_CHECK_MODE respected (enforce/observe/disabled); individual check exceptions caught and produce DEGRADED/CONTINUE; per-pipeline circuit breaker instances are thread-safe under pipeline lock; events emitted; results persisted and capped at 10"
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
        description: Add health_checks field (list[HealthCheckResult], default_factory=list) to PhaseExecution model
        acceptance: Existing pipelines without health_checks deserialize with empty list; new results can be appended; backward compatible
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
        description: Create StartupReconciliationCheck in orchestrator/health/checks/startup.py (tier=PROGRAMMATIC, triggers=[STARTUP], allow_immediate_fail=True — container verified absent from Docker is not a false positive). Extract evaluation logic from reconcile_stale_containers(); return HealthResult with FAILED status and affected pipeline/container IDs in reason when stale containers found
        acceptance: Check returns FAILED with descriptive reason for stale containers; returns HEALTHY when all containers live; allow_immediate_fail=True; does not mutate state directly; preserves exact error message format from original
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
        description: Create ContainerLivenessCheck in orchestrator/health/checks/container_liveness.py (tier=PROGRAMMATIC, triggers=[RUNTIME_TICK], allow_immediate_fail=True — container verified absent from Docker is not a false positive). Extract evaluation logic from _reconcile_container_state(); return HealthResult
        acceptance: Check returns FAILED when RUNNING pipeline has a stale/failed container; returns HEALTHY otherwise; allow_immediate_fail=True; preserves error message format; does not mutate state
        files:
          - orchestrator/health/checks/container_liveness.py
      - id: TASK-4-2
        description: Update create_pipeline_reconciliation_handler() in container_monitor.py to route through HealthCheckRunner instead of calling _reconcile_container_state() directly. Keep _reconcile_container_state() as deprecated wrapper
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
        description: "Create PhaseOutputPresenceCheck in orchestrator/health/checks/phase_output.py (tier=PROGRAMMATIC, triggers=[PHASE_COMPLETE, WAVE_COMPLETE], allow_immediate_fail=False). Check LOCAL branch head first before remote: implement phase checks context.local_branch_head for new commits since phase start; if local has commits but remote doesn't, return DEGRADED with reason 'Commits exist locally but not on remote — push may be in progress'; if neither has commits, return DEGRADED; plan checks for output files; refine checks for analysis document. Return HEALTHY when expected artifacts exist"
        acceptance: "Returns DEGRADED when implement phase has no remote commits; returns DEGRADED (not FAILED) when local has commits but remote doesn't; returns DEGRADED when plan phase has no output files; returns HEALTHY when expected artifacts exist; allow_immediate_fail=False (uses two-strike); checks local branch head before remote"
        files:
          - orchestrator/health/checks/phase_output.py
      - id: TASK-5-2
        description: Create StateConsistencyCheck in orchestrator/health/checks/state_consistency.py (tier=PROGRAMMATIC, triggers=[WAVE_COMPLETE, PHASE_COMPLETE], allow_immediate_fail=False). Verify containers marked RUNNING in pipeline state exist in Docker. Return DEGRADED on partial drift, FAILED on critical inconsistency (all containers gone but pipeline RUNNING)
        acceptance: Returns DEGRADED when some containers gone; returns FAILED when all containers gone but pipeline RUNNING; returns HEALTHY when state matches Docker; allow_immediate_fail=False (uses two-strike)
        files:
          - orchestrator/health/checks/state_consistency.py
      - id: TASK-5-3
        description: Create RepeatedFailureCheck in orchestrator/health/checks/failure_patterns.py (tier=PROGRAMMATIC, triggers=[WAVE_COMPLETE], allow_immediate_fail=False). Detect same role failing N times (configurable, default 3) with same/similar error. Return FAILED with action FAIL_PIPELINE. Replaces blunt max_waves=5 silent stop with structured verdict
        acceptance: Returns FAILED when agent fails 3+ times with same error pattern; returns HEALTHY when failures are diverse or under threshold; reason includes error pattern description; allow_immediate_fail=False
        files:
          - orchestrator/health/checks/failure_patterns.py
  - id: 6
    name: Tier 2 agent inspector
    goal: Implement LLM-based semantic health evaluator using Haiku for detecting novel failure patterns
    dependencies:
      - phase-1
    tasks:
      - id: TASK-6-1
        description: Create HealthInspectorClient in orchestrator/health/llm_client.py wrapping Anthropic API. Uses Haiku model by default. JSON mode for structured output. 30-second timeout. Returns None on API failure. No connection reuse across invocations. If ANTHROPIC_API_KEY missing, log warning and disable Tier 2 automatically
        acceptance: Client returns structured JSON response on success; returns None on timeout/API error; configurable model; missing API key disables gracefully
        files:
          - orchestrator/health/llm_client.py
      - id: TASK-6-2
        description: "Create AgentInspectorCheck in orchestrator/health/checks/agent_inspector.py (tier=AGENT, triggers=[PHASE_COMPLETE, ON_DEMAND], allow_immediate_fail=False). Assemble prompt with phase goal, agent outputs, git log/diff, execution results. Prompt instructs LLM to err on side of DEGRADED over FAILED — FAILED requires clear evidence. Parse LLM JSON verdict into HealthResult. On LLM failure return DEGRADED/CONTINUE"
        acceptance: Returns structured HealthResult from LLM verdict; handles malformed LLM responses gracefully; returns DEGRADED/CONTINUE on any LLM failure; prompt includes all relevant context; allow_immediate_fail=False (uses two-strike)
        files:
          - orchestrator/health/checks/agent_inspector.py
  - id: 7
    name: Lifecycle wiring and observability
    goal: Wire HealthCheckRunner into orchestrator lifecycle at all trigger points with exception isolation at every integration site; add on-demand REST endpoint with documentation
    dependencies:
      - phase-2
      - phase-3
      - phase-4
      - phase-5
      - phase-6
    tasks:
      - id: TASK-7-1
        description: Add init_health_check_runner() to orchestrator/health/runner.py that creates the singleton, registers all 6 checks (StartupReconciliation, ContainerLiveness, PhaseOutputPresence, StateConsistency, RepeatedFailure, AgentInspector), and returns the runner
        acceptance: Runner singleton is initialized with all 6 checks registered; accessible from routes and CLI via get_health_check_runner()
        files:
          - orchestrator/health/runner.py
      - id: TASK-7-2
        description: In cli.py, call init_health_check_runner() at startup. Replace direct reconcile_stale_containers() call with runner.run_checks(trigger=STARTUP) wrapped in try/except. On exception, log error and fall back to original reconcile_stale_containers()
        acceptance: Startup reconciliation runs through the health check framework; same pipelines marked FAILED on stale containers; health check exceptions caught and logged — never crash startup; fallback to original function on exception
        files:
          - orchestrator/cli.py
      - id: TASK-7-3
        description: Update create_pipeline_reconciliation_handler() in container_monitor.py to use runner. Wrap run_checks(trigger=RUNTIME_TICK) in try/except. On exception, log error and fall back to original _reconcile_container_state()
        acceptance: Container FAILED events trigger health check framework; state mutations via runner; health check exceptions caught and logged — never crash the container monitor thread; fallback to original function on exception
        files:
          - orchestrator/container_monitor.py
      - id: TASK-7-4
        description: Wire on_wave_complete callback in multi_agent.py to invoke runner.run_checks(trigger=WAVE_COMPLETE). Pass callback to execute_all_waves() (currently called without it at pipelines.py:3260). Callback wraps run_checks() in try/except; on exception, log and continue
        acceptance: "Wave completion triggers Tier 1 checks; DEGRADED escalates to Tier 2; results persisted; health check exceptions are caught and logged at the integration site — they never propagate to the pipeline execution thread"
        files:
          - orchestrator/multi_agent.py
      - id: TASK-7-5
        description: Invoke runner.run_checks(trigger=PHASE_COMPLETE) after phase completion in routes/pipelines.py. Wrap in try/except; on exception, log and continue. Act on results — FAIL_PIPELINE marks pipeline failed, RETRY triggers agent retry, ALERT emits event only, CONTINUE proceeds
        acceptance: "Phase completion triggers all checks including Tier 2; FAIL_PIPELINE result marks pipeline FAILED; RETRY triggers retry logic; health check exceptions are caught and logged at the integration site — they never propagate to _run_pipeline() or block phase transition"
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-7-6
        description: Add POST /api/v1/pipelines/<id>/health-check endpoint to routes/health.py for on-demand evaluation returning aggregated health results. Document the endpoint with docstring describing request/response format
        acceptance: Endpoint returns JSON array of HealthCheckResult for the pipeline; triggers all checks including Tier 2; returns 404 for unknown pipeline; endpoint documented with docstring
        files:
          - orchestrator/routes/health.py
  - id: 8
    name: Tests
    goal: Comprehensive test coverage for the health check framework including exception isolation, two-strike confirmation, HEALTH_CHECK_MODE, and performance; existing tests unchanged
    dependencies:
      - phase-7
    tasks:
      - id: TASK-8-1
        description: Write unit tests for types, enums, HealthResult creation, HealthCheckMode enum, and PipelineHealthContext lazy loading including local_branch_head in test_health_types.py and test_health_context.py
        acceptance: All enum values tested including HealthCheckMode; HealthResult creation and serialization tested; lazy fields verified not computed until accessed; local_branch_head tested
        files:
          - orchestrator/tests/test_health_types.py
          - orchestrator/tests/test_health_context.py
      - id: TASK-8-2
        description: "Write unit tests for each HealthCheck implementation in test_health_checks.py with mock PipelineHealthContext: startup reconciliation (mock Docker, verify HEALTHY/FAILED, verify allow_immediate_fail=True), container liveness (mock pipeline state, verify allow_immediate_fail=True), phase output presence (mock git/filesystem, verify DEGRADED on missing outputs, verify local-before-remote logic, verify allow_immediate_fail=False), state consistency (mock Docker vs state), repeated failure (mock execution history), agent inspector (mock LLM client, verify structured verdict parsing and graceful failure handling)"
        acceptance: Each check tested for HEALTHY, DEGRADED, and FAILED paths; edge cases covered (local commits but no remote for phase output); allow_immediate_fail values verified; mock dependencies only
        files:
          - orchestrator/tests/test_health_checks.py
      - id: TASK-8-3
        description: "Write unit tests for HealthCheckRunner in test_health_runner.py: trigger filtering, Tier 1-to-Tier 2 escalation, two-strike confirmation (first FAIL_PIPELINE -> ALERT, second -> enforce, allow_immediate_fail=True bypasses, non-FAIL clears pending), HEALTH_CHECK_MODE (enforce applies, observe emits only, disabled returns empty), exception isolation (check raising exception produces DEGRADED/CONTINUE), result application (event emission, state persistence, cap at 10), per-pipeline circuit breaker instances, concurrent execution safety"
        acceptance: Runner correctly filters by trigger; escalates on DEGRADED; two-strike confirmation logic verified; HEALTH_CHECK_MODE respected; exceptions handled gracefully; events emitted; result cap enforced; concurrent access safe
        files:
          - orchestrator/tests/test_health_runner.py
      - id: TASK-8-4
        description: Write integration tests for lifecycle wiring in test_health_integration.py — startup trigger, container failure trigger, wave complete trigger, phase complete trigger, on-demand REST endpoint. HEALTH_CHECK_MODE integration tests (observe emits but doesn't fail pipelines, disabled skips all checks)
        acceptance: Full lifecycle verified end-to-end with mocked Docker and LLM API; each trigger invokes correct checks; HEALTH_CHECK_MODE modes verified in integration
        files:
          - orchestrator/tests/test_health_integration.py
      - id: TASK-8-5
        description: "Write exception isolation tests in test_health_integration.py: verify health check exception does NOT crash _run_pipeline(); verify health check exception does NOT crash container monitor handler; verify health check exception does NOT crash on_wave_complete callback; verify health check timeout does NOT block phase transition"
        acceptance: Each integration site verified — exceptions from health checks are caught, logged, and do not propagate to calling code
        files:
          - orchestrator/tests/test_health_integration.py
      - id: TASK-8-6
        description: Write backward compatibility tests verifying migrated startup reconciliation and container monitor produce identical state changes as originals. Write performance test for RUNTIME_TICK frequency (100 triggers with ContainerLivenessCheck complete in under 1 second with no git operations triggered)
        acceptance: Same pipeline state mutations for identical input; error messages preserved; 100 RUNTIME_TICK triggers < 1 second; no lazy git properties accessed during RUNTIME_TICK
        files:
          - orchestrator/tests/test_health_integration.py
```
