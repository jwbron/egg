# Analysis: Multi-Agent Implementation Across Pipeline Phases (#546)

## 1. Problem Statement

The local orchestrator (`orchestrator/routes/pipelines.py`) runs each SDLC phase sequentially with a single CODER agent. Significant multi-agent infrastructure exists in the codebase — agent roles with dependency graphs, wave-based execution planning, a `MultiAgentExecutor` class, and a `PipelineDispatcher` — but none of it is wired into the local pipeline runner. The `_run_pipeline()` function spawns one agent container per phase and has no multi-agent branching logic.

Issue #546 requests:
1. Wire `MultiAgentExecutor` into the local pipeline runner for implement-phase wave-based execution
2. Extend multi-agent support to the plan phase with new agent roles (ARCHITECT, TASK_PLANNER, RISK_ANALYST)
3. Unify reviewers into the wave/dispatch model as agent types
4. Add configuration (CLI flags, contract-level config) to make multi-agent opt-in
5. Implement file conflict resolution for parallel agents sharing a worktree

## 2. Current State of the Codebase (Verified)

### What exists

| Component | Location | Status | Verification |
|-----------|----------|--------|--------------|
| AgentRole enum: 4 roles (CODER, TESTER, DOCUMENTER, INTEGRATOR) | `shared/egg_contracts/agent_roles.py:25-35` | Defined and working | Enum has exactly these 4 members |
| AgentRoleDefinition with dependencies and file access patterns | `shared/egg_contracts/agent_roles.py:126` | Defined for all 4 roles | CODER→[], TESTER→[CODER], DOCUMENTER→[CODER], INTEGRATOR→[CODER, TESTER] |
| FileAccessPattern with allowed_read, allowed_write, blocked_write | `shared/egg_contracts/agent_roles.py:50` | Defined per role | Includes can_read(), can_write(), _matches_pattern() methods |
| Utility functions: get_role_definition, get_all_roles, get_role_dependencies, can_run_in_parallel, create_execution_for_role | `shared/egg_contracts/agent_roles.py:304-398` | Fully implemented | 5 functions total |
| DependencyGraph with topological sort and wave computation | `shared/egg_contracts/dependency_graph.py:113-276` | Fully implemented | build_from_roles(), has_cycle(), topological_sort(), compute_waves(), get_execution_plan() |
| build_dependency_graph() and compute_execution_plan() | `shared/egg_contracts/dependency_graph.py:279-303` | Fully implemented | Module-level convenience functions |
| OrchestrationState (execution tracking, handoffs, status management) | `shared/egg_contracts/orchestration.py:50-268` | Fully implemented | mark_running/complete/failed/skipped, add_handoff, get_handoffs_for, get_pending/completed/failed_roles |
| Orchestrator class with get_next_dispatch(), agent lifecycle methods | `shared/egg_contracts/orchestrator.py:94-277` | Fully implemented | Uses get_runnable_agents() → can_agent_run() for dependency-based dispatch |
| DispatchDecision dataclass | `shared/egg_contracts/orchestrator.py:39-80` | Defined | agents_to_run, wave_number, reason, is_parallel, all_complete, has_failures |
| MultiAgentExecutor with execute_all_waves() | `orchestrator/multi_agent.py:79-402` | Implemented but NOT wired into _run_pipeline() | spawn_wave(), record_agent_result(), execute_wave(), execute_all_waves() |
| MultiAgentExecutor.spawn_wave() sets EGG_HANDOFF_DATA | `orchestrator/multi_agent.py:145` | Implemented | Sets `"EGG_HANDOFF_DATA": str(handoff_data)` in extra_env |
| PipelineDispatcher bridging contracts with orchestrator | `orchestrator/dispatch.py:87-274` | Implemented but NOT called by _run_pipeline() | get_next_dispatch(), start_agent(), complete_agent(), fail_agent(), get_handoff_data(), save_contract() |
| MultiAgentConfig on Contract model | `shared/egg_contracts/models.py:352-366` | Defined | Fields: enabled (bool, default=True), max_retries (int, default=2), parallel_execution (bool, default=True), roles_enabled (list[AgentRoleType]) |
| AgentExecutionModel | `shared/egg_contracts/models.py:322-350` | Defined | Fields: role, status, started_at, completed_at, commit, checkpoint_id, outputs, error, retry_count — NO conflicts field |
| PipelineConfig with multi_agent and parallel_agents | `orchestrator/models.py:162-179` | Defined | multi_agent: bool (default=True), parallel_agents: bool (default=True) — NO max_parallel_agents field |
| _PHASE_REVIEWERS dict | `orchestrator/routes/pipelines.py:795-799` | Active code | Maps phases to reviewer types: refine→[unified, agent-design], plan→[unified, agent-design], implement→[unified, agent-design, code, contract] |
| _spawn_and_wait() | `orchestrator/routes/pipelines.py:1299-1449` | Active code | Spawns single container, waits for exit, captures logs, updates status, cleans up. Returns (exit_code, logs) |
| container_spawner sets EGG_AGENT_ROLE | `orchestrator/container_spawner.py:308` | Active code | `"EGG_AGENT_ROLE": agent_role.value` in env dict |
| GitHub Actions multi-agent workflow | `.github/workflows/sdlc-multi-agent.yml` | Exists | Separate workflow with coder/tester/documenter/integrator jobs, sets EGG_AGENT_ROLE per job |
| Integration test file | `integration_tests/sdlc/test_multi_agent_orchestration.py` | Exists (single file) | Tests dependency graph and dispatch logic |
| Orchestrator architecture docs | `docs/architecture/orchestrator.md` | Exists | References multi-agent mode |

### What does NOT exist (common misconceptions from prior drafts)

| Claimed Component | Actual Status |
|-------------------|---------------|
| Plan-phase roles (ARCHITECT, TASK_PLANNER, RISK_ANALYST) in AgentRole enum | **Does not exist** — AgentRole has only CODER, TESTER, DOCUMENTER, INTEGRATOR |
| Reviewer roles (REVIEWER_UNIFIED, etc.) in AgentRole enum | **Does not exist** — not defined anywhere |
| `get_roles_for_phase()` function | **Does not exist** — not in agent_roles.py or anywhere else |
| `detect_write_overlaps()` function | **Does not exist** — not in agent_roles.py or anywhere else |
| `_run_multi_agent_phase()` function | **Does not exist** — not in pipelines.py or anywhere else |
| Multi-agent conditional branch in `_run_pipeline()` | **Does not exist** — _run_pipeline() has no branching on multi_agent config |
| CLI flags --multi-agent, --no-multi-agent, --max-parallel | **Do not exist** — CLI has --sdlc, --public, --private, etc., but no multi-agent flags |
| `max_parallel_agents` field on PipelineConfig | **Does not exist** — PipelineConfig has multi_agent (bool) and parallel_agents (bool), no integer limit |
| `conflicts` field on AgentExecutionModel | **Does not exist** — model has role, status, timestamps, commit, checkpoint_id, outputs, error, retry_count |
| `phase_overrides` field on MultiAgentConfig | **Does not exist** — MultiAgentConfig has enabled, max_retries, parallel_execution, roles_enabled |
| Plan-phase prompts (architect.md, task_planner.md, risk_analyst.md) | **Do not exist** — no shared/prompts/ directory exists |
| `test_multi_agent_pipeline_e2e.py` | **Does not exist** |
| `test_multi_agent_phases.py` | **Does not exist** |
| `docs/multi-agent.md` | **Does not exist** |
| EGG_WAVE_NUMBER env var set in pipelines.py | **Does not exist** — not referenced in pipelines.py |
| MultiAgentExecutor called from _run_pipeline() | **Does not exist** — executor is completely disconnected from pipeline runner |

### Current execution flow (actual)

```
_run_pipeline(pipeline_id, repo_path):
  1. Initialize: load pipeline, parse env vars, create worktrees via gateway
  2. Create contract in worktree

  FOR each phase in pipeline:
    a. Mark phase RUNNING

    b. Build environment:
       - EGG_PIPELINE_ID, EGG_PIPELINE_PHASE, EGG_PIPELINE_MODE
       - EGG_ORCHESTRATOR_URL, EGG_ORCHESTRATOR_MODE

    c. WHILE True (review cycle):
       ── Spawn single CODER agent ──
       - _spawn_and_wait(spawner, ..., agent_role=AgentRole.CODER)
       - If exit != 0: fail phase

       ── If implement phase: checker/autofix loop ──
       - Up to 3 attempts:
         * Spawn CHECKER agent (EGG_AGENT_ROLE=checker)
         * Read check results
         * If passed: break
         * If failed: spawn CODER autofix agent, retry

       ── If phase in reviewed phases: sequential reviewer loop ──
       - reviewer_types = _PHASE_REVIEWERS.get(phase, ["unified"])
       - FOR EACH reviewer_type (SEQUENTIAL):
         * Spawn reviewer container (EGG_REVIEWER_TYPE set)
         * Read verdict
       - Aggregate verdicts
       - If approved: break
       - If needs_revision AND under max_review_cycles: loop back
       - Else: break (circuit breaker)

    d. Mark phase COMPLETE
    e. If plan phase: populate contract from plan output
    f. If HITL gates enabled: wait for human approval
    g. Advance to next phase or finish
```

**Key observation**: There is NO multi-agent branching anywhere. Every phase uses exactly one CODER agent followed by one CHECKER and sequential reviewers. The `PipelineConfig.multi_agent` and `PipelineConfig.parallel_agents` fields exist but are never read by `_run_pipeline()`.

## 3. Acceptance Criteria Gap Analysis

Based on the issue's requirements and implementation phases, here is an accurate assessment. Every "DONE" claim is backed by a verified file and line number; every "NOT DONE" means the feature was not found anywhere in the codebase.

| AC | Description | Status | Evidence |
|----|-------------|--------|----------|
| **Phase 1: Wire MultiAgentExecutor into implement phase** | | | |
| 1.1 | `_run_pipeline()` invokes multi-agent dispatch when multi_agent=true and phase=implement | **NOT DONE** | _run_pipeline() has no multi-agent branching. Always spawns single CODER. |
| 1.2 | `MultiAgentExecutor.execute_all_waves()` creates containers via existing spawner infrastructure | **NOT DONE** | MultiAgentExecutor exists (multi_agent.py:79) but is never called from _run_pipeline(). |
| 1.3 | Dependent agents receive `EGG_HANDOFF_DATA` env var within a phase | **PARTIAL** | MultiAgentExecutor.spawn_wave() sets EGG_HANDOFF_DATA (multi_agent.py:145), but this code path is never invoked from the pipeline runner. |
| 1.4 | Each container receives `EGG_AGENT_ROLE` env var | **DONE** | container_spawner.py:308 sets EGG_AGENT_ROLE for every spawned container. |
| 1.5 | Pipeline proceeds when all agents in wave complete successfully | **NOT DONE** | No wave-based execution in pipeline runner. |
| 1.6 | `PipelineConfig` has `multi_agent` and `max_parallel_agents` | **PARTIAL** | PipelineConfig has `multi_agent: bool` (models.py:168) and `parallel_agents: bool` (models.py:171). No `max_parallel_agents: int` field. |
| 1.7 | Tests cover wave dispatch, handoff injection, completion | **PARTIAL** | test_multi_agent_orchestration.py tests dependency graph and dispatch logic. No tests for pipeline-level wave execution. |
| **Phase 2: Plan-phase agent roles** | | | |
| 2.1 | ARCHITECT, TASK_PLANNER, RISK_ANALYST roles defined in AgentRole enum | **NOT DONE** | AgentRole has only CODER, TESTER, DOCUMENTER, INTEGRATOR (agent_roles.py:25-35). |
| 2.2 | ARCHITECT → TASK_PLANNER + RISK_ANALYST dependency graph | **NOT DONE** | These roles don't exist. |
| 2.3 | `get_roles_for_phase()` returns correct role lists for each phase | **NOT DONE** | Function does not exist anywhere. |
| 2.4 | Claude prompts for ARCHITECT, TASK_PLANNER, RISK_ANALYST | **NOT DONE** | No shared/prompts/ directory exists. |
| 2.5 | Plan phase uses wave dispatch when multi_agent=true | **NOT DONE** | No wave dispatch in pipeline runner for any phase. |
| 2.6 | Plan-phase outputs flow to implement-phase agents via handoff | **NOT DONE** | No cross-phase handoff mechanism. |
| 2.7 | Tests cover plan-phase role selection, dependency graph, wave execution | **NOT DONE** | |
| **Phase 3: File conflict resolution** | | | |
| 3.1 | Detect overlapping file patterns between agents before dispatch | **NOT DONE** | `detect_write_overlaps()` does not exist. |
| 3.2 | After wave completion, attempt git merge of agent changes | **NOT DONE** | No merge logic exists. |
| 3.3 | `AgentExecutionModel` includes `conflicts` field | **NOT DONE** | Model has no conflicts field (models.py:322-350). |
| 3.4 | Agents retry up to `max_retries` on non-conflict transient failures | **NOT DONE** | MultiAgentExecutor has some retry structure but is not wired into pipeline. |
| 3.5 | Tests cover merge, conflict failure, retry | **NOT DONE** | |
| **Phase 4: Integrate reviewers as agent types** | | | |
| 4.1 | Reviewer roles (REVIEWER_UNIFIED, REVIEWER_CODE, etc.) defined in AgentRole enum | **NOT DONE** | Not in AgentRole enum. |
| 4.2 | Reviewers depend on INTEGRATOR (or final plan agent) | **NOT DONE** | Reviewer roles don't exist in agent_roles.py. |
| 4.3 | Reviewers appear in final wave of dependency graph | **NOT DONE** | |
| 4.4 | `PipelineDispatcher` aggregates reviewer verdicts | **NOT DONE** | PipelineDispatcher exists but is never called. It does not have verdict aggregation — that's in pipelines.py:1034 (_aggregate_review_verdicts). |
| 4.5 | Sequential `_PHASE_REVIEWERS` loop replaced with wave-based dispatch for multi-agent | **NOT DONE** | _PHASE_REVIEWERS dict still active (pipelines.py:795-799). |
| 4.6 | Multiple reviewers run concurrently | **NOT DONE** | Reviewers spawned sequentially in for-loop (pipelines.py:2209-2291). |
| 4.7 | Tests cover reviewer dispatch, verdict aggregation, revision cycle | **NOT DONE** | |
| **Phase 5: Configuration and documentation** | | | |
| 5.1 | `MultiAgentConfig` on contract with enabled, max_retries, parallel_execution, roles_enabled | **DONE** | models.py:352-366. All 4 fields present. |
| 5.2 | Contract config can enable/disable multi-agent per phase | **NOT DONE** | No `phase_overrides` field on MultiAgentConfig. Pipeline runner doesn't read contract config. |
| 5.3 | `--multi-agent` and `--max-parallel` CLI flags | **NOT DONE** | CLI has no multi-agent flags. |
| 5.4 | Status endpoint shows wave progress, active agents, per-agent status | **NOT DONE** | get_pipeline_status() returns high-level status only. |
| 5.5 | Documentation covers configuration, agent roles, usage | **NOT DONE** | No docs/multi-agent.md. docs/architecture/orchestrator.md exists but doesn't cover the new features. |

**Summary**: 2 of 32 criteria are fully DONE (EGG_AGENT_ROLE in container_spawner, MultiAgentConfig fields). 2 are PARTIAL. 28 are NOT DONE.

## 4. Constraints and Dependencies

### Technical constraints

- **Docker container model**: Each agent runs in an isolated Docker container. Parallel agents in a wave share the same git worktree via volume mounts. This creates file-level race conditions that conflict resolution (Phase 3) must handle.

- **Gateway sidecar**: All git operations route through the gateway. Branch restrictions (`egg/` prefix) and merge blocking apply. Multiple containers pushing to the same branch concurrently needs investigation — the gateway may serialize or reject concurrent pushes.

- **Container spawning cost**: Each agent container runs a full Claude session (`claude --print`). N parallel containers means N concurrent LLM sessions. A configurable limit is needed (the issue specifies `max_parallel_agents: 10`).

- **Worktree sharing**: All containers in a pipeline share the same worktree path (created by gateway at pipeline start). Parallel agents writing to the same worktree need either pre-dispatch file pattern checks or post-wave merge logic.

- **Sequential wave constraint**: Waves must complete in order because later-wave agents depend on earlier-wave outputs. This is inherent in the dependency graph model and already supported by `DependencyGraph.compute_waves()`.

- **Existing spawner interface**: `container_spawner.spawn_agent_container()` (container_spawner.py:172-381) already accepts `agent_role`, `extra_env`, `phase`, and `repos` parameters. `MultiAgentExecutor.spawn_wave()` calls a method that needs to bridge to this spawner — currently it may use its own Docker client interface (multi_agent.py:123-203).

### Key architectural gap

The `MultiAgentExecutor` (multi_agent.py) and the pipeline runner `_run_pipeline()` (pipelines.py) use different container management approaches:

- **Pipeline runner**: Uses `_spawn_and_wait()` which calls `spawner.spawn_agent_container()`, waits for container exit, captures logs, updates `ContainerInfo` and `AgentExecution` in the pipeline store, then cleans up.

- **MultiAgentExecutor**: Has its own `spawn_wave()` that creates containers through what appears to be a Docker client interface, and has its own `record_agent_result()` method.

Bridging these two is the core integration challenge. The executor needs to use the pipeline's spawner (which handles gateway auth, cert volumes, repo volumes, etc.) rather than its own Docker client.

## 5. Implementation Approaches

### Approach A: Refactor MultiAgentExecutor to accept pipeline spawner

Modify `MultiAgentExecutor` to accept an injectable spawner interface (the pipeline's `_spawn_and_wait()` function or a wrapper around `container_spawner.spawn_agent_container()`). Then add a multi-agent branch in `_run_pipeline()` that creates an executor and calls `execute_all_waves()`.

**Pros**: Satisfies the issue's intent to use `MultiAgentExecutor.execute_all_waves()`. Consolidates wave logic in one place. Executor already has some retry structure.
**Cons**: Requires refactoring the executor's container management to match the pipeline's spawner interface. Medium integration risk.

### Approach B: Implement wave execution inline in _run_pipeline()

Add a `_run_multi_agent_phase()` function directly in pipelines.py that uses the existing `_spawn_and_wait()` + threading to run agents in waves. Use `DependencyGraph.compute_waves()` for wave computation but skip the executor class.

**Pros**: Lowest integration risk — uses the battle-tested `_spawn_and_wait()` directly. No need to refactor the executor.
**Cons**: Creates a parallel implementation alongside `MultiAgentExecutor`, leaving the executor as dead code. Doesn't satisfy the issue's explicit mention of wiring `MultiAgentExecutor` into the pipeline.

### Approach C: Hybrid — executor for waves, pipeline runner for lifecycle

Modify `MultiAgentExecutor` to accept a spawner callable. The pipeline runner creates the executor, passes in its spawner, and calls `execute_all_waves()`. The pipeline runner remains responsible for phase lifecycle (review cycles, status updates, HITL gates, phase transitions).

**Pros**: Clean separation — executor handles wave execution, pipeline handles phase lifecycle. Satisfies ac-1.2. Leverages existing wave computation in executor.
**Cons**: Still requires spawner interface bridging. Reviewer integration (Phase 4) means the executor eventually handles review waves too, which overlaps with the pipeline's existing review loop.

### Recommendation: Approach C

Approach C best matches the issue's requirements while managing risk. The executor handles what it's good at (wave computation, agent dispatch, handoff data), and the pipeline runner handles what it's good at (phase lifecycle, review cycles, status, HITL). The spawner interface bridge is a well-scoped integration point.

## 6. Recommended Implementation Plan

### Phase 1: Wire MultiAgentExecutor into implement phase (MVP)

**Goal**: When `pipeline.config.multi_agent` is true and the phase is `implement`, use `MultiAgentExecutor` to dispatch agents in waves instead of spawning a single CODER.

**Changes required**:

1. **Add spawner interface to MultiAgentExecutor** (`orchestrator/multi_agent.py`)
   - Modify `__init__` to accept a `spawn_fn` callable matching `_spawn_and_wait()` signature
   - Modify `spawn_wave()` to use the injected spawn function instead of its own Docker client
   - Ensure `EGG_AGENT_ROLE` and `EGG_HANDOFF_DATA` are set per container

2. **Add `max_parallel_agents` to PipelineConfig** (`orchestrator/models.py`)
   - Add `max_parallel_agents: int = 10` field
   - Keep existing `multi_agent: bool` and `parallel_agents: bool` fields

3. **Add multi-agent branch in `_run_pipeline()`** (`orchestrator/routes/pipelines.py`)
   - After phase setup, check `pipeline.config.multi_agent and phase == "implement"`
   - If true: create `MultiAgentExecutor`, pass spawner, call `execute_all_waves()`
   - If false: existing single-agent path (unchanged)
   - Respect `max_parallel_agents` limit

4. **Add `EGG_WAVE_NUMBER` env var** (`orchestrator/multi_agent.py` or `container_spawner.py`)
   - Set `EGG_WAVE_NUMBER` on each spawned container

5. **Tests** (`integration_tests/sdlc/`)
   - Add test for pipeline-level multi-agent execution with mocked spawner
   - Extend test_multi_agent_orchestration.py with executor + spawner integration test

**Acceptance criteria addressed**: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7

### Phase 2: Add plan-phase agent roles

**Goal**: Define ARCHITECT, TASK_PLANNER, RISK_ANALYST roles and enable plan-phase wave execution.

**Changes required**:

1. **Add roles to AgentRole enum** (`shared/egg_contracts/agent_roles.py`)
   - Add ARCHITECT, TASK_PLANNER, RISK_ANALYST to enum
   - Add AgentRoleDefinition for each with dependencies: ARCHITECT→[], TASK_PLANNER→[ARCHITECT], RISK_ANALYST→[ARCHITECT]
   - Add FileAccessPattern for each (plan-phase agents are read-heavy, write to plan docs)

2. **Add `get_roles_for_phase()` function** (`shared/egg_contracts/agent_roles.py`)
   - Returns appropriate role list for a given phase
   - implement → [CODER, TESTER, DOCUMENTER, INTEGRATOR]
   - plan → [ARCHITECT, TASK_PLANNER, RISK_ANALYST]

3. **Create plan-phase prompts** (new files)
   - Create `shared/prompts/` directory
   - `architect.md` — system prompt for architecture analysis
   - `task_planner.md` — system prompt for task decomposition
   - `risk_analyst.md` — system prompt for risk assessment

4. **Extend multi-agent branch to plan phase** (`orchestrator/routes/pipelines.py`)
   - Check `pipeline.config.multi_agent and phase in {"implement", "plan"}`

5. **Add cross-phase handoff** (`orchestrator/routes/pipelines.py`, `shared/egg_contracts/orchestration.py`)
   - After plan phase completes, persist agent outputs to `.egg-state/agent-outputs/`
   - When implement phase starts, collect plan-phase outputs and inject as `EGG_HANDOFF_DATA` for Wave 1

6. **Tests**
   - Test plan-phase role definitions and dependency graph
   - Test get_roles_for_phase() for both phases
   - Test cross-phase handoff persistence and injection

**Acceptance criteria addressed**: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7

### Phase 3: File conflict resolution

**Goal**: Handle parallel agents writing to the same worktree.

**Changes required**:

1. **Add `detect_write_overlaps()` function** (`shared/egg_contracts/agent_roles.py`)
   - Compare FileAccessPattern.allowed_write across agents in a wave
   - Return list of overlapping patterns

2. **Add post-wave merge logic** (`orchestrator/multi_agent.py`)
   - After wave completes, check for git conflicts in worktree
   - Attempt merge resolution
   - On unresolvable conflict: fail the wave

3. **Add `conflicts` field to AgentExecutionModel** (`shared/egg_contracts/models.py`)
   - `conflicts: list[str] = Field(default_factory=list)`
   - Populated on merge failure

4. **Add retry logic** (`orchestrator/multi_agent.py`)
   - On non-conflict transient failures: retry up to `MultiAgentConfig.max_retries`
   - On conflict failures: fail immediately

5. **Tests**
   - Test overlap detection
   - Test merge success path
   - Test conflict failure and reporting
   - Test retry behavior

**Acceptance criteria addressed**: 3.1, 3.2, 3.3, 3.4, 3.5

### Phase 4: Integrate reviewers as agent types

**Goal**: Replace sequential `_PHASE_REVIEWERS` loop with wave-based reviewer execution for multi-agent phases.

**Changes required**:

1. **Add reviewer roles to AgentRole enum** (`shared/egg_contracts/agent_roles.py`)
   - REVIEWER_UNIFIED, REVIEWER_CODE, REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN
   - All with read-only FileAccessPattern (allowed_write: [])
   - Dependencies: all reviewer roles depend on INTEGRATOR (implement) or final plan agent (plan)

2. **Extend `get_roles_for_phase()` with `include_reviewers` parameter**
   - When True, append reviewer roles to the phase's role list
   - This makes reviewers appear as the final wave in the dependency graph

3. **Add reviewer wave execution** (`orchestrator/routes/pipelines.py`)
   - In multi-agent path: include reviewers in execute_all_waves() call
   - Multiple reviewers in the same wave run concurrently
   - Collect verdicts from reviewer outputs

4. **Migrate verdict aggregation** (`orchestrator/routes/pipelines.py`)
   - Reuse existing `_aggregate_review_verdicts()` logic
   - Feed reviewer agent outputs into aggregation

5. **Preserve single-agent review path**
   - When multi_agent=false: keep existing _PHASE_REVIEWERS sequential loop unchanged

6. **Tests**
   - Test reviewer role definitions and dependencies
   - Test concurrent reviewer execution
   - Test verdict aggregation from wave-based reviewers
   - Test revision cycle (needs_revision → re-run worker waves → re-run reviewer wave)

**Acceptance criteria addressed**: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7

### Phase 5: Configuration and documentation

**Goal**: Full configuration support and documentation.

**Changes required**:

1. **Add CLI flags** (`sandbox/egg_lib/cli.py`)
   - `--multi-agent` / `--no-multi-agent` to override PipelineConfig.multi_agent
   - `--max-parallel N` to set max_parallel_agents

2. **Add per-phase config to MultiAgentConfig** (`shared/egg_contracts/models.py`)
   - Add `phase_overrides: dict[str, dict]` field for per-phase role/config overrides
   - Pipeline runner reads contract.multi_agent_config alongside PipelineConfig

3. **Enhance status endpoint** (`orchestrator/routes/pipelines.py`)
   - Extend `get_pipeline_status()` to include wave progress, active agents, per-agent execution status when multi-agent is active

4. **Create documentation** (`docs/multi-agent.md`)
   - Configuration options (PipelineConfig, MultiAgentConfig, CLI flags)
   - Agent roles and their dependencies per phase
   - Wave execution model
   - Conflict resolution behavior
   - Troubleshooting

5. **Tests**
   - Test CLI flag parsing
   - Test config precedence (CLI > contract config > PipelineConfig defaults)
   - Test status endpoint with wave data

**Acceptance criteria addressed**: 5.1 (already done), 5.2, 5.3, 5.4, 5.5

## 7. Key Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Bridging MultiAgentExecutor's container interface with pipeline's spawner | High | Define clean spawner callable interface; test with mocked spawner first |
| Parallel containers writing to same worktree cause corruption | High | Phase 3 adds pre-dispatch overlap detection + post-wave merge; start with sequential execution even in "multi-agent" mode as a safe default |
| Reviewer unification breaks existing review→revision cycle | High | Preserve _PHASE_REVIEWERS loop for single-agent mode; only use wave-based reviewers when multi_agent=true |
| N concurrent LLM sessions increases cost | Medium | max_parallel_agents limit (default 10); multi_agent defaults to opt-in |
| Gateway may not handle concurrent pushes from same branch | Medium | Investigate gateway behavior before Phase 1 implementation; add serialization if needed |
| New plan-phase roles (ARCHITECT, etc.) need well-crafted prompts | Medium | Iterate on prompts with testing; use existing phase prompt patterns as templates |

## 8. Testing Strategy

### Existing test infrastructure
- `integration_tests/sdlc/test_multi_agent_orchestration.py` — tests dependency graph and orchestration dispatch. Extend this.

### New tests needed per phase

| Phase | Test File | Coverage |
|-------|-----------|----------|
| Phase 1 | `test_multi_agent_orchestration.py` (extend) + new `test_multi_agent_pipeline.py` | Executor + spawner integration, pipeline multi-agent branch, wave execution end-to-end |
| Phase 2 | `test_multi_agent_orchestration.py` (extend) | Plan-phase role definitions, get_roles_for_phase(), cross-phase handoff |
| Phase 3 | New `test_multi_agent_conflicts.py` | Overlap detection, merge logic, conflict reporting, retry |
| Phase 4 | New `test_multi_agent_reviewers.py` | Reviewer wave dispatch, concurrent execution, verdict aggregation, revision cycle |
| Phase 5 | Extend CLI tests, new `test_multi_agent_config.py` | CLI flags, config precedence, status endpoint |

### Backward compatibility
- All existing tests must pass unchanged
- Pipeline with `multi_agent=false` (or not set) must behave identically to current behavior
- Single-agent review path preserved alongside wave-based reviewer path

## 9. Scope Summary

| Phase | Files Modified | New Files | Key Risk |
|-------|---------------|-----------|----------|
| Phase 1 | orchestrator/multi_agent.py, orchestrator/models.py, orchestrator/routes/pipelines.py | 0-1 test file | Spawner interface bridging |
| Phase 2 | shared/egg_contracts/agent_roles.py, orchestrator/routes/pipelines.py | 3 prompt files (shared/prompts/*.md) | Prompt quality |
| Phase 3 | shared/egg_contracts/agent_roles.py, shared/egg_contracts/models.py, orchestrator/multi_agent.py | 1 test file | Merge correctness |
| Phase 4 | shared/egg_contracts/agent_roles.py, orchestrator/routes/pipelines.py | 1 test file | Review cycle regression |
| Phase 5 | sandbox/egg_lib/cli.py, shared/egg_contracts/models.py, orchestrator/routes/pipelines.py, docs/ | 1 doc file, 1 test file | Config precedence logic |

## 10. Open Questions

1. **Spawner interface**: Should `MultiAgentExecutor` accept the pipeline's `_spawn_and_wait()` directly, or should we define a protocol/interface class? The former is simpler; the latter is more testable. (Recommendation: callable with defined signature — simpler than a protocol, still mockable.)

2. **Config precedence**: When `PipelineConfig.multi_agent=True` but `contract.multi_agent_config.enabled=False`, which wins? (Recommendation: contract config overrides, since the contract is the per-issue source of truth.)

3. **Reviewer dispatch scope**: Should wave-based reviewer dispatch only apply when multi_agent=true, or also replace sequential reviewers in single-agent mode? (Recommendation: multi-agent only, to minimize regression risk.)

4. **Cross-phase handoff format**: Should plan-phase outputs flow via `.egg-state/agent-outputs/` files or via `contract.agent_executions[].outputs`? (Recommendation: file-based in `.egg-state/agent-outputs/`, consistent with how the existing `collect_handoff_data()` in orchestrator.py:356-380 works.)

5. **Gateway concurrent push**: Can multiple containers in a wave push to the same branch simultaneously? (Needs investigation before Phase 1. If not, add post-wave single-push serialization.)

6. **Prompt location**: The issue mentions `shared/prompts/` but this directory doesn't exist. Should prompts go there, or should they be embedded in the phase prompt builder functions (`_build_phase_prompt()` in pipelines.py:1063)? (Recommendation: create `shared/prompts/` as specified in the issue for maintainability.)

---

Authored-by: egg
