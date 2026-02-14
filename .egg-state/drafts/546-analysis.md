# Analysis: Multi-Agent Implementation Across Pipeline Phases (#546)

## 1. Problem Statement

The local orchestrator (`orchestrator/routes/pipelines.py`) has significant multi-agent infrastructure already wired into its pipeline execution path. The `_run_multi_agent_phase()` function dispatches agents in dependency-ordered waves via threading, and `_run_pipeline()` conditionally branches to it when `pipeline.config.multi_agent` is true and the phase is `implement` or `plan`.

However, several features required by the acceptance criteria are incomplete or partially integrated. There are two parallel implementations of wave-based execution (the inline `_run_multi_agent_phase()` in pipelines.py and the standalone `MultiAgentExecutor` class), the reviewer mechanism still uses the sequential `_PHASE_REVIEWERS` dict rather than wave-based dispatch, there is no file conflict resolution for parallel agents, cross-phase handoff (plan outputs flowing to implement agents) is not implemented, and the contract-level `MultiAgentConfig` is not consulted by the pipeline runner.

This issue requests completing the remaining gaps so that multi-agent execution is fully operational across implement and plan phases.

## 2. Current State of the Codebase

### What exists and works

| Component | Location | Status |
|-----------|----------|--------|
| Agent role definitions — all 11 roles (CODER, TESTER, DOCUMENTER, INTEGRATOR, ARCHITECT, TASK_PLANNER, RISK_ANALYST, REVIEWER_UNIFIED, REVIEWER_CODE, REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN) | `shared/egg_contracts/agent_roles.py` | Fully defined with file access patterns, dependencies, handoff declarations |
| `get_roles_for_phase(phase, include_reviewers)` | `agent_roles.py:517` | Returns correct role lists for implement and plan phases |
| `detect_write_overlaps(roles)` | `agent_roles.py:569` | Implemented but NOT called in the pipeline path |
| Dependency graph with topological sort and wave computation | `shared/egg_contracts/dependency_graph.py` | Fully implemented, tested |
| Orchestration state management (executions, handoffs, waves) | `shared/egg_contracts/orchestration.py` | Fully implemented |
| `MultiAgentExecutor` (spawn waves, wait, record results, retry, revision cycles) | `orchestrator/multi_agent.py` | Fully implemented but NOT used by `_run_pipeline()` |
| `PipelineDispatcher` (bridges contracts with orchestrator, verdict aggregation) | `orchestrator/dispatch.py` | Fully implemented but NOT used by `_run_pipeline()` |
| `_run_multi_agent_phase()` inline implementation | `pipelines.py:1569-1807` | Active code path — uses threading + `_spawn_and_wait()` directly |
| Multi-agent conditional branch in `_run_pipeline()` | `pipelines.py:1981` | Checks `pipeline.config.multi_agent` and phase in `{implement, plan}` |
| Fallback to single-agent on `no_multi_agent_roles` error | `pipelines.py:2003-2010` | Working |
| `EGG_AGENT_ROLE`, `EGG_WAVE_NUMBER`, `EGG_HANDOFF_DATA` env vars | `pipelines.py:1658-1677` | Set per container in multi-agent path |
| `PipelineConfig` with `multi_agent` (default False) and `max_parallel_agents` (default 10) | `orchestrator/models.py:174-193` | Defined and read by pipeline runner |
| `MultiAgentConfig` on Contract model | `shared/egg_contracts/models.py:362-383` | Defined with `enabled`, `phase_overrides`, `roles_enabled`, etc. |
| `AgentExecutionModel` with `conflicts` field | `shared/egg_contracts/models.py:329-360` | Defined, `conflicts: list[str]` field exists but never populated |
| CLI flags `--multi-agent`, `--no-multi-agent`, `--max-parallel` | `orchestrator/cli.py:402-420` | Implemented and mapped to PipelineConfig |
| Claude prompts for plan-phase agents | `shared/prompts/architect.md`, `task_planner.md`, `risk_analyst.md` | Exist |
| Integration tests for orchestration layer | `integration_tests/sdlc/test_multi_agent_orchestration.py` | 437 lines, tests dependency graph and dispatch logic |
| E2E tests for `_run_multi_agent_phase()` | `integration_tests/sdlc/test_multi_agent_pipeline_e2e.py` | Tests wave execution with mocked spawner |
| Phase-specific test for roles | `integration_tests/sdlc/test_multi_agent_phases.py` | Tests role definitions and wave computation |
| Multi-agent documentation | `docs/multi-agent.md` | Exists |

### Current execution flow (accurate)

```
_run_pipeline(pipeline_id, repo_path):
  for each phase:
    if pipeline.config.multi_agent AND phase in {"implement", "plan"}:
      ─── Multi-agent path ───
      1. _run_multi_agent_phase():
         a. get_roles_for_phase(phase) → [CODER, TESTER, DOCUMENTER, INTEGRATOR]
         b. build_dependency_graph(roles) → graph
         c. graph.compute_waves() → [[CODER], [TESTER, DOCUMENTER], [INTEGRATOR]]
         d. For each wave:
            - Spawn containers in parallel (threading + _spawn_and_wait)
            - Set EGG_AGENT_ROLE, EGG_WAVE_NUMBER, EGG_HANDOFF_DATA
            - Wait for all containers to exit
            - Abort on any failure
         e. Return (success, error)
      2. If success AND phase in _REVIEWED_PHASES:
         ─── Sequential reviewer loop (same as single-agent) ───
         a. For each reviewer in _PHASE_REVIEWERS[phase]:
            - Spawn reviewer container sequentially
            - Read verdict file
         b. Aggregate verdicts
         c. If needs_revision: loop back to step 1
    else:
      ─── Single-agent path ───
      1. Spawn CODER container → wait
      2. Checker/autofixer loop (implement phase)
      3. Sequential reviewer loop (same as above)
```

### What's actually missing (the real gaps)

1. **Reviewers not integrated into wave model** — After `_run_multi_agent_phase()` completes, reviewers are spawned sequentially via the `_PHASE_REVIEWERS` dict (pipelines.py:727-731), not through the wave/dispatch system. Reviewer role definitions exist in `agent_roles.py` with dependencies on INTEGRATOR, but `_run_multi_agent_phase()` only dispatches worker agents (CODER, TESTER, etc.), not reviewers. The `get_roles_for_phase(phase, include_reviewers=True)` path is never used.

2. **`MultiAgentExecutor` and `PipelineDispatcher` are orphaned** — `_run_multi_agent_phase()` reimplements wave logic using direct threading + `_spawn_and_wait()` instead of calling `MultiAgentExecutor.execute_all_waves()`. The executor and dispatcher are fully implemented classes that are never invoked in the pipeline path. This creates two parallel implementations of the same concept.

3. **No file conflict resolution** — Parallel agents in a wave share the same worktree. `detect_write_overlaps()` exists in `agent_roles.py` but is never called in the pipeline path. After a wave completes, there is no git merge step, no overlap detection, and the `conflicts` field on `AgentExecutionModel` is never populated.

4. **No retry logic in `_run_multi_agent_phase()`** — `MultiAgentExecutor.execute_all_waves()` has retry logic for non-conflict failures, but the inline implementation in `_run_multi_agent_phase()` does not. Any agent failure immediately aborts the wave.

5. **No cross-phase handoff** — Plan-phase agent outputs (architecture decisions, task breakdown, risk assessment) are not persisted or passed to implement-phase agents. The handoff mechanism (`EGG_HANDOFF_DATA`, `collect_handoff_data()`) works within a single phase across waves, but there's no persistence mechanism between phases.

6. **Contract `MultiAgentConfig` not consulted** — `_run_pipeline()` checks `pipeline.config.multi_agent` (from PipelineConfig in orchestrator/models.py) but NOT `contract.multi_agent_config` (from shared/egg_contracts/models.py). The `PipelineDispatcher.is_multi_agent_enabled_for_phase()` method reads the contract config but is never called. Per-phase overrides in the contract (`phase_overrides`) are ignored.

7. **Status endpoint lacks wave/agent detail** — `get_pipeline_status()` (pipelines.py:520) returns high-level status (id, phase, pending_decisions) but does not include wave progress, active agents, or per-agent execution status.

## 3. Acceptance Criteria Gap Analysis

| AC | Description | Status | Notes |
|----|-------------|--------|-------|
| ac-1 | PipelineConfig has `multi_agent` and `max_parallel_agents` | **DONE** | models.py:180,183 |
| ac-2 | Calls multi-agent path when `multi_agent=true` and phase is implement | **DONE** | pipelines.py:1981 |
| ac-3 | `execute_all_waves()` creates containers via existing infrastructure | **PARTIAL** | `_run_multi_agent_phase()` creates containers, but does NOT use `MultiAgentExecutor.execute_all_waves()`. Decision needed: keep inline implementation or migrate to executor. |
| ac-4 | Dependent agents receive `EGG_HANDOFF_DATA` env var | **DONE** | pipelines.py:1668-1670 |
| ac-5 | Each container receives `EGG_AGENT_ROLE` env var | **DONE** | pipelines.py:1660 |
| ac-6 | Pipeline proceeds when all agents in wave complete | **DONE** | Wave loop with thread join |
| ac-7 | Tests cover wave dispatch, handoff injection, completion | **DONE** | test_multi_agent_pipeline_e2e.py |
| ac-8 | New roles defined in agent_roles.py | **DONE** | All 11 roles defined |
| ac-9 | ARCHITECT → TASK_PLANNER + RISK_ANALYST dependency | **DONE** | agent_roles.py:349,377 |
| ac-10 | `get_roles_for_phase()` returns correct role list | **DONE** | agent_roles.py:517 |
| ac-11 | Prompts for ARCHITECT, TASK_PLANNER, RISK_ANALYST | **DONE** | shared/prompts/*.md |
| ac-12 | Plan phase uses wave dispatch when `multi_agent=true` | **DONE** | Plan is in `_multi_agent_phases` set |
| ac-13 | Plan-phase outputs flow to implement-phase agents | **NOT DONE** | No cross-phase handoff mechanism |
| ac-14 | Tests cover role selection, dependency graph, wave execution | **DONE** | test_multi_agent_phases.py, test_multi_agent_orchestration.py |
| ac-15 | Detect overlapping file patterns between agents | **NOT DONE** | `detect_write_overlaps()` exists but not called in pipeline |
| ac-16 | Merge agent changes; fail on merge conflicts | **NOT DONE** | No merge logic after wave completion |
| ac-17 | `AgentExecutionModel` includes `conflicts` field | **DONE** (field exists) | models.py:357, but never populated |
| ac-18 | Agents retry up to `max_retries` on non-conflict failures | **NOT DONE** | Inline `_run_multi_agent_phase()` has no retry logic |
| ac-19 | Tests cover merge, conflict failure, retry | **NOT DONE** | No tests for conflict resolution |
| ac-20 | Reviewer roles with read-only file access | **DONE** | agent_roles.py:399-472 |
| ac-21 | Reviewers depend on INTEGRATOR or final plan agent | **DONE** (in definitions) | Defined but not exercised in wave dispatch |
| ac-22 | Reviewers appear in final wave | **NOT DONE** | Reviewers use sequential `_PHASE_REVIEWERS` loop |
| ac-23 | `PipelineDispatcher` aggregates verdicts | **NOT DONE** | Method exists at dispatch.py:265 but not called |
| ac-24 | Reviewer loop replaced with dispatcher-based execution | **NOT DONE** | `_PHASE_REVIEWERS` dict still active |
| ac-25 | Multiple reviewers run concurrently | **NOT DONE** | Sequential spawning |
| ac-26 | Tests cover reviewer dispatch, verdict aggregation | **NOT DONE** | |
| ac-27 | `MultiAgentConfig` with `enabled`, `max_parallel_agents`, `roles_enabled` | **DONE** | models.py:362-383 |
| ac-28 | Contract can enable/disable multi-agent per phase | **NOT DONE** | `phase_overrides` field exists but pipeline doesn't read contract config |
| ac-29 | `--multi-agent` and `--max-parallel` CLI flags | **DONE** | cli.py:402-420 |
| ac-30 | Status shows wave progress, active agents, per-agent status | **NOT DONE** | Status endpoint returns minimal info |
| ac-31 | Docs explain configuration, agent roles, usage | **DONE** | docs/multi-agent.md exists |
| ac-32 | Full pipeline test with multi-agent implement phase | **DONE** | test_multi_agent_pipeline_e2e.py |

**Summary**: 18 of 32 criteria are done. 14 remain.

## 4. Constraints and Dependencies

### Technical constraints

- **Docker container model**: Each agent runs in an isolated Docker container. Parallel agents in a wave share the same git worktree via volume mounts. This creates file-level race conditions that the conflict resolution mechanism (ac-15 through ac-19) must handle.

- **Gateway sidecar**: All git operations go through the gateway. Branch restrictions (`egg/` prefix) and merge blocking apply to agent containers. Multi-agent containers all push to the same branch, so post-wave merge strategy needs to handle this.

- **Container spawning overhead**: Each agent container runs a full Claude session (`claude --print`). Spawning N containers means N concurrent LLM sessions. The `max_parallel_agents: 10` limit (already enforced at pipelines.py:1642) is essential.

- **Worktree sharing**: All containers in a pipeline share the same worktree. Parallel agents writing to the same worktree need either file-pattern enforcement (pre-dispatch overlap check) or a post-wave merge step.

- **Sequential wave constraint**: Waves must complete in order. Wave 2 agents depend on Wave 1 outputs. This is already enforced by the wave loop.

### Architectural decision: inline vs MultiAgentExecutor

The codebase has two implementations of wave execution:

1. **Inline** (`_run_multi_agent_phase()` in pipelines.py:1569-1807) — the ACTIVE code path. Uses threading + `_spawn_and_wait()` directly. Tightly coupled to the pipeline runner's container infrastructure.

2. **MultiAgentExecutor** (`orchestrator/multi_agent.py:78-468`) — NOT used by the pipeline. Has its own Docker client interface, retry logic, and revision cycle support.

This duplication needs to be resolved. Options:
- **A. Migrate to MultiAgentExecutor**: Refactor `_run_multi_agent_phase()` to delegate to the executor. Requires ensuring the executor uses the pipeline's existing spawner/container infrastructure.
- **B. Consolidate into inline**: Move missing features (retry, conflict resolution, reviewer dispatch) into the inline implementation. Remove MultiAgentExecutor.
- **C. Keep both**: Inline for the pipeline runner, executor for other contexts (e.g., remote orchestration). Accept the duplication.

**Recommendation**: Option A is cleanest but highest risk. Option B is pragmatic since the inline implementation is already battle-tested in the pipeline. The contract acceptance criteria (ac-3) says "`execute_all_waves()` creates containers via existing infrastructure" which implies the MultiAgentExecutor should be the active code path. This suggests Option A with careful integration.

### Backward compatibility

- `PipelineConfig.multi_agent` defaults to `False` — existing pipelines are unaffected.
- CLI must explicitly pass `--multi-agent` to enable.
- The single-agent path (`_run_pipeline()` else-branch) must remain unchanged.

## 5. Implementation Approaches

### Approach A: Complete the inline implementation

Add missing features (retry, conflict resolution, reviewer dispatch, cross-phase handoff, contract config integration) directly into `_run_multi_agent_phase()` and surrounding code.

**Pros**: Minimal refactoring risk. Builds on the proven inline path.
**Cons**: Does not satisfy ac-3 (which specifies `execute_all_waves()`). Leaves MultiAgentExecutor and PipelineDispatcher as dead code.

### Approach B: Migrate to MultiAgentExecutor (matches ac-3)

Refactor `_run_multi_agent_phase()` to delegate to `MultiAgentExecutor.execute_all_waves()`, passing the pipeline's spawner infrastructure. Then add missing features to the executor.

**Pros**: Satisfies ac-3 exactly. Consolidates logic. Executor already has retry support.
**Cons**: Higher integration risk. Need to bridge executor's Docker client interface with pipeline's `_spawn_and_wait()`.

### Approach C: Hybrid — use executor for waves, inline for pipeline integration

Use MultiAgentExecutor for wave execution but keep the pipeline-specific integration (reviewer handling, status tracking, phase advancement) in pipelines.py.

**Pros**: Leverages executor's tested wave logic. Keeps pipeline integration where it belongs.
**Cons**: Requires clear interface between executor and pipeline runner.

**Recommendation**: Approach C. This satisfies ac-3 while keeping the pipeline runner in control of phase lifecycle.

## 6. Recommended Approach — Phased Implementation

### Phase 1: Unify wave execution via MultiAgentExecutor

**Goal**: Replace the inline wave loop in `_run_multi_agent_phase()` with calls to `MultiAgentExecutor`, satisfying ac-3.

**Key changes**:
- Bridge `MultiAgentExecutor` with the pipeline's `_spawn_and_wait()` / spawner infrastructure
- Ensure executor uses the same env var injection (`EGG_AGENT_ROLE`, `EGG_WAVE_NUMBER`, `EGG_HANDOFF_DATA`)
- Add retry logic from executor into the active path (ac-18)
- Verify existing tests still pass with the unified path

**Files to modify**:
- `orchestrator/routes/pipelines.py` — refactor `_run_multi_agent_phase()` to use executor
- `orchestrator/multi_agent.py` — ensure executor accepts pipeline spawner interface

**Acceptance criteria addressed**: ac-3, ac-18 (partial)

### Phase 2: File conflict resolution

**Goal**: Detect and handle file overlaps between parallel agents in a wave.

**Key changes**:
- Call `detect_write_overlaps()` before dispatching each wave — log warnings for overlapping write patterns
- After wave completion, run git merge/conflict detection on the shared worktree
- Populate `AgentExecutionModel.conflicts` on merge failure
- Implement retry for non-conflict transient failures (complete ac-18)

**Files to modify**:
- `orchestrator/multi_agent.py` — pre-dispatch overlap detection, post-wave merge
- `orchestrator/routes/pipelines.py` — pass conflict info to phase execution state

**Acceptance criteria addressed**: ac-15, ac-16, ac-17 (populate field), ac-18, ac-19

### Phase 3: Integrate reviewers into wave dispatch

**Goal**: Replace sequential `_PHASE_REVIEWERS` loop with wave-based reviewer execution.

**Key changes**:
- Use `get_roles_for_phase(phase, include_reviewers=True)` so reviewers appear as the final wave(s) in the dependency graph
- Remove the sequential reviewer loop from `_run_pipeline()` for multi-agent phases
- Migrate verdict aggregation to use `PipelineDispatcher.aggregate_reviewer_verdicts()`
- Run multiple reviewers concurrently within same wave
- Preserve the revision cycle (needs_revision → re-run worker waves)

**Files to modify**:
- `orchestrator/routes/pipelines.py` — replace `_PHASE_REVIEWERS` loop with wave-based reviewers for multi-agent path
- `orchestrator/dispatch.py` — ensure verdict aggregation integrates with pipeline flow
- `orchestrator/multi_agent.py` — support `execute_with_revision_cycle()` in pipeline context

**Acceptance criteria addressed**: ac-21 (exercised), ac-22, ac-23, ac-24, ac-25, ac-26

**Risk**: Medium-high. The review→revision→review cycle is deeply embedded. The `_PHASE_REVIEWERS` mechanism must be preserved for single-agent mode.

### Phase 4: Cross-phase handoff and contract config integration

**Goal**: Plan-phase outputs flow to implement-phase agents; contract-level config is respected.

**Key changes**:
- After plan phase completes, persist agent outputs to `.egg-state/agent-outputs/` (already the convention)
- When implement phase starts, collect plan-phase outputs and inject as `EGG_HANDOFF_DATA` for Wave 1 agents
- Read `contract.multi_agent_config` in `_run_pipeline()` and reconcile with `pipeline.config.multi_agent`
- Honor `phase_overrides` from contract config

**Files to modify**:
- `orchestrator/routes/pipelines.py` — cross-phase handoff, contract config reading
- `orchestrator/dispatch.py` — `is_multi_agent_enabled_for_phase()` integration
- `shared/egg_contracts/orchestration.py` — cross-phase handoff helpers if needed

**Acceptance criteria addressed**: ac-13, ac-28

### Phase 5: Status UI and remaining polish

**Goal**: Expose multi-agent execution details in the status endpoint.

**Key changes**:
- Extend `get_pipeline_status()` to include wave progress, active agents, per-agent status when multi-agent is active
- Verify documentation in `docs/multi-agent.md` covers all new behaviors

**Files to modify**:
- `orchestrator/routes/pipelines.py` — status endpoint enhancement
- `docs/multi-agent.md` — verify/update documentation

**Acceptance criteria addressed**: ac-30, ac-31 (verify)

## 7. Key Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Migrating from inline to MultiAgentExecutor breaks existing wave execution | High | Run both paths in parallel behind flag during migration; comprehensive test coverage |
| Parallel containers writing to same worktree cause corruption | High | Phase 2 conflict resolution; `detect_write_overlaps()` pre-dispatch warning |
| Review unification breaks revision cycle | High | Preserve `_PHASE_REVIEWERS` for single-agent mode; feature flag for reviewer wave dispatch |
| Multi-agent increases cost (N containers = N LLM sessions) | Medium | `max_parallel_agents` limit; `multi_agent` defaults to False (opt-in) |
| Contract config and PipelineConfig disagree on multi-agent settings | Low | Define clear precedence: CLI flag > contract config > PipelineConfig default |
| Gateway doesn't support multiple containers pushing to same branch simultaneously | Medium | Verify gateway behavior; add serialization if needed |

## 8. Testing Strategy

Each phase should include:
1. **Unit tests** for new/modified functions
2. **Integration tests** extending the existing test files in `integration_tests/sdlc/`
3. **Backward compatibility test** — pipeline with `multi_agent=False` behaves identically

Existing tests to extend:
- `test_multi_agent_orchestration.py` — dependency graph and dispatch logic
- `test_multi_agent_pipeline_e2e.py` — wave execution with mocked spawner
- `test_multi_agent_phases.py` — role definitions and wave computation

New tests needed:
- Phase 1: Test MultiAgentExecutor integration with pipeline spawner
- Phase 2: Test conflict detection, merge resolution, retry behavior
- Phase 3: Test reviewer wave dispatch, concurrent reviewer execution, verdict aggregation, revision cycle
- Phase 4: Test cross-phase handoff persistence and injection
- Phase 5: Test status endpoint with wave/agent detail

## 9. Estimated Scope

| Phase | Files Modified | New Files | Complexity |
|-------|---------------|-----------|------------|
| Phase 1 | 2 | 0-1 (tests) | Medium |
| Phase 2 | 2 | 0-1 (tests) | Medium |
| Phase 3 | 3 | 0-1 (tests) | High |
| Phase 4 | 3 | 0-1 (tests) | Medium |
| Phase 5 | 2 | 0 | Low |

## 10. Open Questions

1. **Inline vs executor**: The contract's ac-3 says "`execute_all_waves()` creates containers via existing infrastructure." Should we strictly use `MultiAgentExecutor.execute_all_waves()`, or is the inline implementation acceptable if it satisfies the same behavior? (Recommendation: migrate to executor per ac-3.)

2. **Config precedence**: When `pipeline.config.multi_agent=True` but `contract.multi_agent_config.enabled=False`, which wins? (Recommendation: contract config overrides pipeline config, since the contract is the source of truth for per-issue behavior.)

3. **Reviewer dispatch scope**: Should reviewer wave dispatch only apply when multi-agent is enabled, or should it also replace the sequential reviewer loop for single-agent phases? (Recommendation: multi-agent only, to minimize risk.)

4. **Cross-phase handoff format**: Plan-phase outputs need a structured format that implement-phase agents can consume. Should this be JSON files in `.egg-state/agent-outputs/`, or should it go through the contract's `agent_executions[].outputs` field? (Recommendation: file-based in `.egg-state/agent-outputs/` for simplicity, mirroring within-phase handoff.)

5. **Gateway concurrent push**: Do multiple containers in a wave push independently, or does the wave coordinator merge and push once? (Needs investigation: verify gateway behavior with concurrent pushes from the same branch.)

---

Authored-by: egg
