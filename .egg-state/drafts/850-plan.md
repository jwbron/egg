# Implementation Plan: Holistic Pipeline Failure Detection

## Issue

[#850](https://github.com/jwbron/egg/issues/850) — Holistic pipeline failure detection: programmatic checks + agent inspector

## Problem Summary

The orchestrator's failure detection is a collection of one-off mechanisms (`startup_reconciliation`, `ContainerMonitor`, `max_waves`/`max_review_cycles`) with no shared abstraction. None can detect **semantic failures** — cases where infrastructure looks healthy but agents didn't accomplish meaningful work (e.g., issue-835: reviewers exited cleanly but had nothing to review because coders never pushed).

## Approach

Following the architect's recommended approach (Approach A — HealthCheck Protocol + HealthCheckRunner + adapter migration), implement a two-tier health check framework:

- **Tier 1 (Programmatic):** Fast, deterministic checks that run on every lifecycle event — container liveness, startup state, phase output presence, state consistency.
- **Tier 2 (Agent Inspector):** An LLM-based evaluator that runs when Tier 1 flags DEGRADED or at phase completion — reads agent outputs, git log/diff, and determines whether meaningful progress occurred.

Both tiers produce a `HealthResult` with status (`HEALTHY`/`DEGRADED`/`FAILED`), reasoning, and a suggested action (`CONTINUE`/`FAIL_PIPELINE`/`ALERT`).

## Key Design Decisions (from Architect)

| ID | Decision | Rationale |
|----|----------|-----------|
| DD-1 | `HealthCheck` as `typing.Protocol`, not ABC | Structural subtyping; adapters/dataclasses/functions all conform |
| DD-2 | Explicit `HealthCheckRunner` over decorator/registry | Testable, no import-time side effects, injectable |
| DD-3 | Adapter pattern for existing mechanisms | 23 existing tests preserved; no rewrite risk |
| DD-4 | Tier 2 via direct Anthropic SDK call, not sandbox | Fast (~2-5s), no container overhead, no recursive monitoring |
| DD-5 | Three-valued Action: CONTINUE/FAIL_PIPELINE/ALERT | Graduated response; ALERT is intermediate between ignore and kill |
| DD-6 | Tier 2 gated: WAVE_COMPLETE only if DEGRADED, PHASE_COMPLETE always | Cost control (~$0.02-0.10 per call) |
| DD-7 | Lazy `PipelineHealthContext` properties | Git ops only execute when Tier 2 accesses them |
| DD-8 | Results via EventBus + SSE | No new transport; HEALTH_CHECK type already exists |
| DD-9 | Tier 2 advisory-only (ALERT), Tier 1 can enforce (FAIL_PIPELINE) | LLM may have false positives; can tune later |

## Package Structure

```
orchestrator/health_checks/
├── __init__.py                     # Re-exports core types
├── types.py                        # HealthCheck protocol, HealthResult, enums
├── context.py                      # PipelineHealthContext with lazy properties
├── runner.py                       # HealthCheckRunner: dispatch, escalation, events
├── tier1/
│   ├── __init__.py
│   ├── container_liveness.py       # Adapter wrapping ContainerMonitor
│   ├── startup_state.py            # Adapter wrapping reconcile_stale_containers
│   ├── phase_output.py             # NEW: verify phase artifacts exist
│   └── state_consistency.py        # NEW: orchestrator vs Docker vs contract
└── tier2/
    ├── __init__.py
    └── agent_inspector.py          # LLM-based semantic evaluation
```

## Modified Files

| File | Change |
|------|--------|
| `orchestrator/events.py` | Add HEALTH_CHECK_STARTED, HEALTH_CHECK_COMPLETED, HEALTH_CHECK_DEGRADED, HEALTH_CHECK_FAILED event types |
| `orchestrator/models.py` | Add HealthCheckResultModel; optional health_check_results field on PhaseExecution |
| `orchestrator/cli.py` | Initialize HealthCheckRunner, register all checks, run STARTUP trigger after existing reconciliation |
| `orchestrator/container_monitor.py` | Add optional runner integration for RUNTIME_TICK on state changes |
| `orchestrator/multi_agent.py` | Add WAVE_COMPLETE health check trigger after wave completion; break on FAIL_PIPELINE |
| `orchestrator/routes/pipelines.py` | Add PHASE_COMPLETE health check trigger before phase advancement |
| `orchestrator/routes/health.py` | Add GET /api/v1/pipelines/{id}/health on-demand endpoint |

## Phased Implementation

### Phase 1: Core Framework + Tier 1 Checks

Build the foundation: shared interfaces, all programmatic checks, lifecycle wiring. This phase delivers immediate value by unifying existing mechanisms and adding the missing phase-output and state-consistency checks that would have caught issue-835.

### Phase 2: Tier 2 Agent Inspector + Observability

Build the LLM-based semantic evaluator, wire Tier 2 escalation into the runner, surface results through events/SSE. This phase adds the ability to detect novel failure patterns without enumerating them in advance.

## Trigger Matrix

| Trigger | Tier 1 | Tier 2 | Location |
|---------|--------|--------|----------|
| STARTUP | always | no | `cli.py:cmd_serve()` |
| RUNTIME_TICK | on state change | no | `container_monitor.py:_monitor_loop()` |
| WAVE_COMPLETE | always | if Tier 1 DEGRADED | `multi_agent.py:execute_all_waves()` |
| PHASE_COMPLETE | always | always | `routes/pipelines.py` |
| ON_DEMAND | always | always | `routes/health.py` (new endpoint) |

## Constraints

- Existing `startup_reconciliation.py` and `container_monitor.py` must remain functionally unchanged (23 combined tests must pass without modification)
- Health checks are **read-only** — they inspect state but never mutate pipeline state directly. Only the caller acts on results.
- Tier 2 must handle API failures gracefully (return HEALTHY with warning, never block the pipeline)
- State mutations must respect optimistic locking via `state_store`
- Tier 2 uses `claude-sonnet-4-20250514` for cost efficiency
- Health check infrastructure failure must never block or crash the pipeline

## Test Strategy

- **Unit tests**: Each check class tested in isolation with mocked dependencies (Docker client, state store, git subprocess, Claude API). Follow the existing pattern of `sys.modules` docker mocking from `test_container_monitor.py`.
- **Runner tests**: Verify trigger filtering, Tier 1→2 escalation, result aggregation, and event emission.
- **Integration touchpoints**: Verify that `multi_agent.py`, `routes/pipelines.py`, and `cli.py` call the runner at the correct lifecycle points (mock runner to verify invocation).
- **Tier 2**: Mock at the Anthropic SDK / httpx level. Cover healthy, degraded, failed, API error, and malformed response cases.
- **Run**: `PYTHONPATH=shared pytest orchestrator/tests/test_health_check_types.py orchestrator/tests/test_health_check_tier1.py orchestrator/tests/test_health_check_tier2.py -v`

## What This Would Have Caught in Issue-835

- **Tier 1 phase_output check**: Implement phase completed, but no new commits on remote branch → DEGRADED. This alone would have flagged the problem before reviewers were spawned.
- **Tier 2 agent_inspector**: Reviewer output files empty, git diff shows no changes reviewed → FAILED with reasoning explaining the disconnect.

---

```yaml
# yaml-tasks
pr:
  title: "Add two-tier health check framework for pipeline failure detection"
  description: |
    Introduces a unified HealthCheck interface with programmatic (Tier 1)
    and LLM-based semantic (Tier 2) health checks. Migrates existing
    startup reconciliation and container monitor to the new interface via
    adapters, adds phase-output and state-consistency checks, and implements
    an agent inspector that detects semantic failures like issue-835 where
    agents exit cleanly but produce no meaningful work.
phases:
  - id: 1
    name: Core Framework + Tier 1 Checks
    goal: Define shared interfaces, implement all programmatic checks, wire into lifecycle hooks
    tasks:
      - id: TASK-1-1
        description: Define core types and interfaces — HealthCheck protocol, HealthResult dataclass, HealthStatus/Tier/Trigger/Action enums, PipelineHealthContext with lazy properties
        acceptance: All types importable from orchestrator/health_checks/; PipelineHealthContext lazy properties only execute expensive operations (git, file reads) when accessed; unit tests pass
        files:
          - orchestrator/health_checks/__init__.py
          - orchestrator/health_checks/types.py
          - orchestrator/health_checks/context.py
      - id: TASK-1-2
        description: Implement HealthCheckRunner with trigger dispatch, Tier escalation logic, and EventBus integration; add new EventType values to events.py
        acceptance: Runner registers checks and dispatches by trigger; Tier 1 runs before Tier 2; WAVE_COMPLETE escalates to Tier 2 only when DEGRADED; PHASE_COMPLETE always runs both; events emitted for each result; new EventType values added; unit tests pass
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/events.py
      - id: TASK-1-3
        description: Create ContainerLivenessCheck and StartupStateCheck adapter classes wrapping existing ContainerMonitor and reconcile_stale_containers without modifying their code
        acceptance: Both conform to HealthCheck protocol; delegate to existing code; existing 23 tests pass unchanged; adapter tests verify correct HealthResult generation
        files:
          - orchestrator/health_checks/tier1/__init__.py
          - orchestrator/health_checks/tier1/container_liveness.py
          - orchestrator/health_checks/tier1/startup_state.py
      - id: TASK-1-4
        description: Implement PhaseOutputPresenceCheck — verify commits on remote, agent output files, contract fields based on phase type
        acceptance: Implement phase checks for commits on remote; plan phase checks for architect-output.json; returns DEGRADED when agents succeed but artifacts missing; HEALTHY when all present; unit tests with mocked git/filesystem
        files:
          - orchestrator/health_checks/tier1/phase_output.py
      - id: TASK-1-5
        description: Implement StateConsistencyCheck — cross-reference orchestrator state vs Docker reality vs contract
        acceptance: Detects RUNNING agents with missing containers (FAILED); detects COMPLETE agents with PENDING contract tasks (DEGRADED); unit tests with mocked Docker client and state store
        files:
          - orchestrator/health_checks/tier1/state_consistency.py
      - id: TASK-1-6
        description: Wire HealthCheckRunner into lifecycle hooks — cli.py (STARTUP), container_monitor.py (RUNTIME_TICK), multi_agent.py (WAVE_COMPLETE), routes/pipelines.py (PHASE_COMPLETE); add on-demand endpoint and HealthCheckResultModel to models.py
        acceptance: STARTUP checks run after existing reconciliation; RUNTIME_TICK runs on state changes; WAVE_COMPLETE runs after each wave with FAIL_PIPELINE break; PHASE_COMPLETE runs before advancement; GET /api/v1/pipelines/{id}/health works; integration tests verify trigger->check->event flow
        files:
          - orchestrator/cli.py
          - orchestrator/container_monitor.py
          - orchestrator/multi_agent.py
          - orchestrator/routes/pipelines.py
          - orchestrator/routes/health.py
          - orchestrator/models.py
      - id: TASK-1-7
        description: Write comprehensive unit tests for core types, runner, and all Tier 1 checks
        acceptance: All tests pass; each check has healthy/degraded/failed test cases; runner trigger filtering and escalation tested; mock patterns follow test_container_monitor.py conventions
        files:
          - orchestrator/tests/test_health_check_types.py
          - orchestrator/tests/test_health_check_tier1.py
  - id: 2
    name: Tier 2 Agent Inspector + Observability
    goal: Implement LLM-based semantic health evaluation and surface results through events/SSE
    tasks:
      - id: TASK-2-1
        description: Implement Tier 2 context assembly — enrich PipelineHealthContext with git log/diff, agent output files, contract state; only populate for Tier 2 triggers
        acceptance: Context factory populates git_log, git_diff_stat, agent_outputs, contract when tier=AGENT; capped at ~4000 tokens by truncating diffs; unit tests verify lazy loading
        files:
          - orchestrator/health_checks/tier2/__init__.py
          - orchestrator/health_checks/context.py
      - id: TASK-2-2
        description: Implement AgentInspectorCheck — structured prompt to Claude API (claude-sonnet-4-20250514), JSON verdict parsing, timeout/retry handling, graceful degradation
        acceptance: Produces structured HEALTHY/DEGRADED/FAILED verdict; returns ALERT action (not FAIL_PIPELINE); handles API timeout/failure gracefully (returns HEALTHY with warning); context capped; mock API tests cover all scenarios
        files:
          - orchestrator/health_checks/tier2/agent_inspector.py
      - id: TASK-2-3
        description: Wire Tier 2 escalation into HealthCheckRunner — run Tier 2 when Tier 1 flags DEGRADED at WAVE_COMPLETE or unconditionally at PHASE_COMPLETE; surface full result payloads via HEALTH_CHECK events for SSE
        acceptance: Tier 2 runs only when escalation conditions met; events contain full payload (status, tier, check name, reasoning, action); SSE clients receive them; integration tests pass
        files:
          - orchestrator/health_checks/runner.py
          - orchestrator/routes/pipelines.py
      - id: TASK-2-4
        description: Write comprehensive unit tests for Tier 2 — context assembly, prompt construction, verdict parsing, escalation logic, event emission, edge cases
        acceptance: All tests pass; API mocked at SDK/httpx level; covers healthy, degraded, failed, API timeout, malformed response, empty outputs
        files:
          - orchestrator/tests/test_health_check_tier2.py
```
