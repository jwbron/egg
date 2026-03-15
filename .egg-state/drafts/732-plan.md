# Plan: Support parallel phase-level dispatch for implement phase

> Issue: #732 | Phase: plan | Pipeline: issue-732

## Approach

This PR adds Tier 3 (high-complexity) dispatch to the SDLC pipeline, following the
architect's recommended hybrid approach (Option C). The work is organized into two
logical stages within a single PR:

**Stage 1 — Sequential phase cycling foundation:** Extend complexity assessment to
3 tiers, add phase dependencies to the contract schema, implement composite
`(phase_id, role)` execution tracking, build a phase-level dependency graph, and
wire up sequential per-phase implement cycles with agentic review and retry. Give
the integrator conditional write access in Tier 3 mode.

**Stage 2 — Parallel dispatch (opt-in):** Add per-phase worktree management in the
gateway, enable parallel execution of independent phases on sub-branches, update the
integrator to merge sub-branches, and gate everything behind a
`PipelineConfig.enable_parallel_phases` feature flag.

This ordering de-risks the delivery: Stage 1 validates the orchestration foundation
(composite keys, phase DAG, per-phase review) without concurrent execution
complexity. Stage 2 adds parallelism as an optimization once the foundation is stable.

### Key design decisions

1. **Composite key `(phase_id, AgentRole)`** for execution tracking — the minimal
   change that lets the contract hold multiple CODER executions. `phase_id` is
   optional (`None` for Tier 2) for backward compatibility.
2. **Phase dependencies stored in the `Phase` model** as `dependencies: list[str]`.
   The plan parser already parses this field; we just propagate it.
3. **Tier 3 signaled by refine agent** with the same HITL override model as Tier 1.
   No separate approval gate.
4. **Integrator write access conditional on Tier 3 only.** In Tier 2, the integrator
   remains read-only.
5. **No new `PipelinePhase.INTEGRATE`** — the implement phase manages cycle-then-
   integrate internally.
6. **Gateway prefix check already supports sub-branches** (`egg/feature/phase-1`
   passes the existing `startswith('egg/')` check). Only worktree lifecycle needs
   extension.

### Backward compatibility

Tier 1 (short-circuit) and Tier 2 (standard multi-agent waves) continue working
unchanged. All schema changes use optional fields with defaults. The
`OrchestrationState` falls back to role-only keying when `phase_id` is `None`.

## Phase breakdown

### Phase 1: Contract schema and model extensions

**Goal:** Establish the data model foundation that all subsequent phases build on.

The contract `Phase` model gains a `dependencies` field. `AgentExecutionModel` gains
a `phase_id` field. The orchestrator-side `Pipeline` model gains a `complexity_tier`
field. The contract JSON schema is updated. The plan parser propagates the
`dependencies` field it already parses into the contract `Phase` model.

**Files:**
- `shared/egg_contracts/models.py` — Add `dependencies: list[str]` to Phase,
  `phase_id: str | None` to AgentExecutionModel
- `shared/egg_contracts/plan_parser.py` — Update `to_contract_phase()` to propagate
  `dependencies`
- `.egg/schemas/contract.schema.json` — Add `dependencies` to Phase schema,
  `phase_id` to agent execution schema
- `orchestrator/models.py` — Add `complexity_tier` field to Pipeline/PipelineConfig

### Phase 2: 3-tier complexity assessment

**Goal:** The refine phase distinguishes low / mid / high complexity. The
orchestrator detects Tier 3 signals and stores the tier on the pipeline.

The refine prompt is updated to signal `complexity_tier: high` and
`parallel_phases: true` in the YAML metadata block. A new
`_check_high_complexity_signal()` function detects this. The pipeline's
`complexity_tier` is set from the detected signal.

**Files:**
- `orchestrator/routes/pipelines.py` — Update refine prompt, add Tier 3 detection
- `orchestrator/models.py` — Wire `complexity_tier` into Pipeline model

### Phase 3: Composite execution tracking and phase dependency graph

**Goal:** The orchestration state supports `(phase_id, role)` composite keys,
and a phase-level dependency graph determines implement cycle ordering.

This is the riskiest change — it touches the core state management. The
`OrchestrationState.executions` dict is extended to support composite keys.
`can_agent_run()` and `get_runnable_agents()` gain phase-scoped variants. A new
`PhaseDependencyGraph` class computes phase waves from `Phase.dependencies`.

**Files:**
- `shared/egg_contracts/orchestration.py` — Composite key support in
  `OrchestrationState`, phase-scoped `can_agent_run()`
- `shared/egg_contracts/orchestrator.py` — Phase-aware `get_next_dispatch()`
- `shared/egg_contracts/dependency_graph.py` — New `PhaseDependencyGraph` class

### Phase 4: Sequential phase cycling in implement phase

**Goal:** Tier 3 implement runs N sequential cycles (one per plan phase in
dependency order). Each cycle: coder → tester → agentic review with retry.

This is the largest behavioral change. A new `_run_tier3_implement()` function
loops through phases in dependency order (using `PhaseDependencyGraph` waves
sequentially). Each iteration spawns coder → tester → agentic reviewers for that
phase's tasks. If a reviewer rejects, the coder retries within that phase. Per-phase
prompts are scoped to the current phase's tasks and `files_affected`.

**Files:**
- `orchestrator/routes/pipelines.py` — `_run_tier3_implement()`, phase-scoped
  prompt building
- `orchestrator/multi_agent.py` — Phase-level execute support in
  `MultiAgentExecutor`
- `orchestrator/dispatch.py` — Per-phase dispatching, phase-scoped handoff data

### Phase 5: Integrator write access for Tier 3

**Goal:** The integrator can modify source, tests, and docs in Tier 3 mode to
fix integration issues. In Tier 2, it remains read-only.

The `INTEGRATOR_ROLE` file access is made dynamic based on `complexity_tier`. The
gateway's phase filter is updated to allow integrator writes when Tier 3 is active.
The integrator prompt is updated: run the full test suite, fix integration issues,
report results.

**Files:**
- `shared/egg_contracts/agent_roles.py` — Dynamic file access for INTEGRATOR_ROLE
- `gateway/phase_filter.py` — Allow integrator writes in Tier 3
- `gateway/agent_restrictions.py` — Tier-aware restriction computation

### Phase 6: Per-phase worktrees and parallel dispatch (Stage 2)

**Goal:** Independent plan phases run in parallel on sub-branches. The integrator
merges sub-branches. All gated behind `enable_parallel_phases` feature flag.

The gateway's `WorktreeManager` gains `create_phase_worktree()` for sub-worktrees
from the pipeline worktree. Branch naming: `egg/<feature>/phase-N`. The
`MultiAgentExecutor` spawns concurrent implement cycles for independent phases.
The integrator receives sub-branch references and merges them.

**Files:**
- `gateway/worktree_manager.py` — `create_phase_worktree()`, cleanup lifecycle
- `orchestrator/routes/pipelines.py` — Parallel dispatch in `_run_tier3_implement()`
- `orchestrator/multi_agent.py` — Concurrent phase execution
- `orchestrator/models.py` — `enable_parallel_phases` config flag
- `shared/egg_contracts/agent_roles.py` — Integrator sub-branch merge instructions

### Phase 7: Tests

**Goal:** Comprehensive test coverage for all changes. Existing Tier 1 and Tier 2
tests continue passing.

**Files:**
- `orchestrator/tests/test_tier3_dispatch.py` — Sequential phase cycling flow
- `orchestrator/tests/test_short_circuit.py` — Verify Tier 1 unchanged
- `orchestrator/tests/test_dispatch.py` — Verify Tier 2 unchanged
- `shared/egg_contracts/tests/test_phase_dependency_graph.py` — Phase DAG computation
- `shared/egg_contracts/tests/test_composite_execution.py` — `(phase_id, role)` tracking
- `shared/egg_contracts/tests/test_plan_parser_dependencies.py` — Dependencies preserved
- `gateway/tests/test_worktree_manager.py` — Phase worktree lifecycle (extend existing)
- `gateway/tests/test_phase_filter.py` — Integrator Tier 3 write access (extend existing)

## Test strategy

1. **Unit tests** for each new component:
   - `PhaseDependencyGraph`: wave computation, cycle detection, single-node graphs
   - Composite execution tracking: `(phase_id, role)` keying, backward compat with `None` phase_id
   - 3-tier complexity signal detection and parsing
   - Plan parser `dependencies` field propagation
   - Dynamic integrator file access based on `complexity_tier`

2. **Integration tests** for end-to-end flows:
   - Tier 3 sequential cycling: 3 phases → 3 cycles → integrator
   - Tier 3 with dependencies: Phase 4 waits for Phase 1
   - Agentic review rejection → coder retry within phase
   - Mixed tiers: Tier 1 and Tier 2 unchanged after changes

3. **Backward compatibility tests**:
   - Existing `test_short_circuit.py` passes (Tier 1)
   - Existing `test_dispatch.py` passes (Tier 2)
   - Contracts without `dependencies` or `phase_id` deserialize correctly

4. **Schema validation tests**:
   - Contracts with new fields validate against updated schema
   - Contracts without new fields still validate (optional fields)

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Composite key migration breaks existing pipeline state | Medium | High | `phase_id` defaults to `None`. Backward compat tests. |
| Per-phase prompts leak cross-phase context | Low | Medium | Phase-filtered prompt function. Test prompt isolation. |
| Integrator write access security concern | Low | Medium | Conditional on Tier 3. Runs after agentic reviews, before human review. |
| Sub-branch merge conflicts (Stage 2) | Medium | Medium | Sub-branch isolation primary. `files_affected` safety net. Integrator handles conflicts. |
| 4500-line pipelines.py becomes harder to maintain | Medium | Low | New functions are self-contained. Consider extraction in follow-up. |

```yaml
# yaml-tasks
pr:
  title: "Add Tier 3 phase-level dispatch for implement phase"
  description: |
    Adds high-complexity (Tier 3) dispatch to the SDLC pipeline. In Tier 3,
    independent plan phases run as separate implement cycles (coder -> tester ->
    agentic review), with dependent phases running sequentially. An integrator
    with write access merges results and fixes integration issues before human
    review. Sequential cycling is the default; parallel dispatch on sub-branches
    is available behind a feature flag.
phases:
  - id: 1
    name: Contract schema and model extensions
    goal: Establish the data model foundation for phase dependencies, composite execution keys, and complexity tiers
    tasks:
      - id: TASK-1-1
        description: Add dependencies field (list[str], default empty) to Phase model in models.py
        acceptance: Phase model accepts and serializes a dependencies field; existing contracts without it deserialize with empty list
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-2
        description: Add phase_id field (str | None, default None) to AgentExecutionModel in models.py
        acceptance: AgentExecutionModel accepts phase_id; existing executions without it deserialize with None
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Update contract.schema.json with dependencies on Phase and phase_id on agent execution
        acceptance: JSON schema validates contracts with and without the new fields
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-4
        description: Update to_contract_phase() in plan_parser.py to propagate ParsedPhase.dependencies to Phase.dependencies
        acceptance: Parsed plan with phase dependencies produces contract phases with populated dependencies field
        files:
          - shared/egg_contracts/plan_parser.py
      - id: TASK-1-5
        description: Add complexity_tier field (str, default 'mid') to Pipeline and PipelineConfig models
        acceptance: Pipeline model stores and exposes complexity_tier with values low/mid/high
        files:
          - orchestrator/models.py
  - id: 2
    name: 3-tier complexity assessment
    goal: Extend refine phase to signal low/mid/high complexity; orchestrator detects and stores Tier 3
    dependencies:
      - phase-1
    tasks:
      - id: TASK-2-1
        description: Update refine prompt to instruct LLM to signal complexity_tier high and parallel_phases true for high-complexity tasks
        acceptance: Refine prompt includes instructions for all three complexity tiers with YAML metadata format
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: Add _check_high_complexity_signal() to detect Tier 3 from refine analysis draft
        acceptance: Function correctly parses complexity_tier from YAML metadata block; returns high/mid/low
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Set pipeline.complexity_tier from detected signal during refine-to-plan transition
        acceptance: Pipeline complexity_tier is set to the value detected from refine analysis
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/models.py
  - id: 3
    name: Composite execution tracking and phase dependency graph
    goal: Orchestration state supports (phase_id, role) keys; phase DAG computes execution order
    dependencies:
      - phase-1
    tasks:
      - id: TASK-3-1
        description: Extend OrchestrationState to support (phase_id, role) composite keys in executions dict
        acceptance: State correctly stores and retrieves executions by (phase_id, role); falls back to role-only when phase_id is None
        files:
          - shared/egg_contracts/orchestration.py
      - id: TASK-3-2
        description: Add phase-scoped can_agent_run() and get_runnable_agents() that check dependencies within a phase context
        acceptance: Dependencies are checked within phase scope; cross-phase dependencies respected
        files:
          - shared/egg_contracts/orchestration.py
      - id: TASK-3-3
        description: Update Orchestrator.get_next_dispatch() for phase-aware dispatch decisions
        acceptance: Dispatch returns correct agents for current phase; supports both Tier 2 (role-only) and Tier 3 (phase-scoped)
        files:
          - shared/egg_contracts/orchestrator.py
      - id: TASK-3-4
        description: Create PhaseDependencyGraph class that computes phase waves from Phase.dependencies
        acceptance: Graph correctly identifies independent phases (same wave) and dependent phases (later waves); handles cycles with error
        files:
          - shared/egg_contracts/dependency_graph.py
  - id: 4
    name: Sequential phase cycling in implement phase
    goal: Tier 3 implement runs N sequential cycles (one per plan phase) with coder -> tester -> agentic review and retry
    dependencies:
      - phase-2
      - phase-3
    tasks:
      - id: TASK-4-1
        description: Add _run_tier3_implement() that loops through phases in dependency order, running coder -> tester -> agentic review per phase
        acceptance: Tier 3 pipeline executes one implement cycle per plan phase in correct dependency order
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        description: Add phase-scoped prompt building that filters tasks and files_affected to the current phase
        acceptance: Coder prompt for phase N contains only phase N's tasks and files; no cross-phase leakage
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-3
        description: Update MultiAgentExecutor to support per-phase implement cycles with phase context
        acceptance: Executor runs coder -> tester -> reviewers for a single phase's tasks
        files:
          - orchestrator/multi_agent.py
      - id: TASK-4-4
        description: Add per-phase agentic review with retry logic (reviewer rejects -> coder retries within phase)
        acceptance: Reviewer rejection triggers coder retry; max retry count respected; escalation on exhaustion
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/multi_agent.py
      - id: TASK-4-5
        description: Update PipelineDispatcher for per-phase dispatching and phase-scoped handoff data
        acceptance: Dispatcher correctly scopes handoff data to current phase; cross-phase data not leaked
        files:
          - orchestrator/dispatch.py
  - id: 5
    name: Integrator write access for Tier 3
    goal: Integrator gains conditional write access to source/tests/docs in Tier 3 mode
    dependencies:
      - phase-1
    tasks:
      - id: TASK-5-1
        description: Make INTEGRATOR_ROLE file access dynamic based on complexity_tier (write access in Tier 3, read-only in Tier 2)
        acceptance: Integrator file_access.blocked_write is empty for Tier 3; unchanged for Tier 2
        files:
          - shared/egg_contracts/agent_roles.py
      - id: TASK-5-2
        description: Update gateway phase_filter to allow integrator writes when complexity_tier is high
        acceptance: Gateway permits integrator file writes in Tier 3; blocks them in Tier 2
        files:
          - gateway/phase_filter.py
          - gateway/agent_restrictions.py
      - id: TASK-5-3
        description: Update integrator prompt for Tier 3 responsibilities (run full test suite, fix integration issues, report)
        acceptance: Integrator prompt in Tier 3 includes merge/fix/test instructions
        files:
          - orchestrator/routes/pipelines.py
  - id: 6
    name: Per-phase worktrees and parallel dispatch
    goal: Independent phases run in parallel on sub-branches behind enable_parallel_phases flag
    dependencies:
      - phase-4
      - phase-5
    tasks:
      - id: TASK-6-1
        description: Add create_phase_worktree() to WorktreeManager for sub-worktrees from pipeline worktree
        acceptance: Phase worktrees created at correct paths with correct branch names (egg/<feature>/phase-N)
        files:
          - gateway/worktree_manager.py
      - id: TASK-6-2
        description: Add phase worktree cleanup lifecycle (cleanup after integrator merges)
        acceptance: Phase worktrees are removed after successful integration; orphan cleanup on failure
        files:
          - gateway/worktree_manager.py
      - id: TASK-6-3
        description: Enable parallel phase execution in _run_tier3_implement() behind enable_parallel_phases flag
        acceptance: Independent phases spawn concurrent implement cycles when flag is True; sequential when False
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/multi_agent.py
      - id: TASK-6-4
        description: Add enable_parallel_phases config flag to PipelineConfig (default False)
        acceptance: Config flag is persisted and accessible during implement phase dispatch
        files:
          - orchestrator/models.py
      - id: TASK-6-5
        description: Update integrator to merge sub-branches and resolve conflicts in parallel mode
        acceptance: Integrator receives sub-branch list, merges into feature branch, runs full test suite
        files:
          - orchestrator/routes/pipelines.py
          - shared/egg_contracts/agent_roles.py
  - id: 7
    name: Tests
    goal: Comprehensive test coverage for all Tier 3 changes; Tier 1 and Tier 2 remain unchanged
    dependencies:
      - phase-4
      - phase-5
      - phase-6
    tasks:
      - id: TASK-7-1
        description: Write unit tests for PhaseDependencyGraph (wave computation, cycle detection, single-node, empty)
        acceptance: All graph scenarios tested; cycle detection raises appropriate error
        files:
          - shared/egg_contracts/tests/test_phase_dependency_graph.py
      - id: TASK-7-2
        description: Write unit tests for composite (phase_id, role) execution tracking and backward compat
        acceptance: Tests cover creation, lookup, serialization, and None-phase_id fallback
        files:
          - shared/egg_contracts/tests/test_composite_execution.py
      - id: TASK-7-3
        description: Write unit tests for 3-tier complexity detection and signal parsing
        acceptance: Tests cover all three tiers, missing signals, malformed YAML
        files:
          - orchestrator/tests/test_tier3_dispatch.py
      - id: TASK-7-4
        description: Write integration tests for sequential phase cycling flow (3 phases, dependency ordering, retry)
        acceptance: End-to-end test verifies correct phase execution order, agentic review, retry on rejection
        files:
          - orchestrator/tests/test_tier3_dispatch.py
      - id: TASK-7-5
        description: Write tests for plan parser dependencies field propagation
        acceptance: Parsed plan with dependencies produces correct contract Phase.dependencies
        files:
          - shared/egg_contracts/tests/test_plan_parser_dependencies.py
      - id: TASK-7-6
        description: Write tests for integrator conditional write access (Tier 2 read-only, Tier 3 read-write)
        acceptance: Tests verify file access patterns change correctly based on complexity_tier
        files:
          - gateway/tests/test_phase_filter.py
      - id: TASK-7-7
        description: Write tests for phase worktree lifecycle and parallel dispatch
        acceptance: Tests cover worktree creation, cleanup, parallel spawn, sub-branch merge
        files:
          - gateway/tests/test_worktree_manager.py
          - orchestrator/tests/test_tier3_dispatch.py
      - id: TASK-7-8
        description: Verify existing Tier 1 and Tier 2 tests still pass
        acceptance: test_short_circuit.py and test_dispatch.py pass without modification
        files:
          - orchestrator/tests/test_short_circuit.py
          - orchestrator/tests/test_dispatch.py
```
