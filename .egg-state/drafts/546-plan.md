# Implementation Plan: Multi-Agent Execution Across Pipeline Phases

> Issue: #546 | Phase: plan

## Overview

The local pipeline runner (`_run_pipeline()` in `orchestrator/routes/pipelines.py`) currently spawns a single CODER agent per phase, ignoring the existing multi-agent infrastructure in `shared/egg_contracts/` and `orchestrator/multi_agent.py`. This plan wires the `MultiAgentExecutor` into the local pipeline runner for wave-based parallel execution, extends it to the plan phase with new agent roles, adds file conflict resolution, integrates reviewers as agent types in the wave/dispatch model, and exposes configuration via CLI flags and contract settings.

**Approach**: Hybrid (Approach C from analysis) — the `MultiAgentExecutor` handles wave computation and agent dispatch using an injectable spawner callable, while `_run_pipeline()` retains responsibility for phase lifecycle (review cycles, status updates, HITL gates, phase transitions). This keeps wave execution consolidated in the executor and avoids duplicating the pipeline runner's lifecycle logic.

**Backward compatibility**: When `multi_agent` is false (or unset), all phases use the existing single-CODER path unchanged. Multi-agent mode is opt-in.

---

## Phase 1: Wire MultiAgentExecutor into Implement Phase (MVP)

### Task 1.1: Define spawner callable interface for MultiAgentExecutor

**File**: `orchestrator/multi_agent.py` — `MultiAgentExecutor.__init__()` (line 86)

**Current behavior**: The constructor accepts `pipeline`, `repo_path`, and `dispatcher`. Internally, `spawn_wave()` (line 123) uses `self.docker_client` to create and start containers directly, bypassing the pipeline runner's `_spawn_and_wait()` which handles gateway auth, cert volumes, repo volumes, session registration, and cleanup.

**Change**: Add a `spawn_fn` parameter to `__init__()` — a callable with a signature compatible with the pipeline's spawning needs. Modify `spawn_wave()` to call `spawn_fn` for each agent instead of using the raw Docker client.

**Implementation**:
```python
# In MultiAgentExecutor.__init__():
SpawnFn = Callable[
    [AgentRole, str, dict[str, str]],  # (role, prompt, extra_env) -> (exit_code, logs)
    tuple[int, str],
]

def __init__(
    self,
    pipeline: Pipeline,
    repo_path: Path,
    dispatcher: PipelineDispatcher | None = None,
    spawn_fn: SpawnFn | None = None,
):
    ...
    self.spawn_fn = spawn_fn
```

**In `spawn_wave()`**: If `self.spawn_fn` is set, use it to spawn each agent. Each agent in the wave is spawned in a separate thread (up to `max_parallel_agents` concurrent). The spawn function receives the agent role, the agent-specific prompt, and extra environment variables (`EGG_HANDOFF_DATA`, `EGG_WAVE_NUMBER`).

**In `execute_wave()`**: Replace the `docker_client.wait_for_container()` pattern with `spawn_fn` calls that block until completion (matching `_spawn_and_wait()` semantics — it already blocks).

**Acceptance criteria**:
- `MultiAgentExecutor.__init__()` accepts an optional `spawn_fn` callable
- `spawn_wave()` uses `spawn_fn` when provided, falls back to docker_client when not
- Each agent in a wave is spawned in its own thread with `EGG_AGENT_ROLE`, `EGG_HANDOFF_DATA`, and `EGG_WAVE_NUMBER` set
- Existing `execute_wave()` and `execute_all_waves()` work with the new spawn path

### Task 1.2: Add `max_parallel_agents` to PipelineConfig

**File**: `orchestrator/models.py` — `PipelineConfig` (line 162)

**Current state**: `PipelineConfig` has `multi_agent: bool = True` and `parallel_agents: bool = True` but no integer limit on concurrent agents.

**Change**: Add `max_parallel_agents: int = Field(default=10, ge=1)`.

**Implementation**:
```python
class PipelineConfig(BaseModel):
    auto_create_pr: bool = Field(default=True)
    multi_agent: bool = Field(default=True)
    parallel_agents: bool = Field(default=True)
    max_parallel_agents: int = Field(default=10, ge=1)  # NEW
    max_review_cycles: int = Field(default=3, ge=1)
    decision_timeout: int = Field(default=3600, ge=60)
    hitl_gates: bool = Field(default=True)
```

**Acceptance criteria**:
- `PipelineConfig` has `max_parallel_agents` field with default 10
- Existing tests and serialization still pass (new field has a default)

### Task 1.3: Add multi-agent branch in `_run_pipeline()`

**File**: `orchestrator/routes/pipelines.py` — `_run_pipeline()` (line 1721), specifically the worker spawn section (line 1959)

**Current behavior**: Lines 1959-2008 always spawn a single CODER agent via `_spawn_and_wait()`. There is no check on `pipeline.config.multi_agent`.

**Change**: Add a `_run_multi_agent_phase()` helper function. Before the worker spawn at line 1959, check `pipeline.config.multi_agent and current_phase.value == "implement"`. If true, call `_run_multi_agent_phase()` instead of the single-CODER spawn. If false, use the existing path unchanged.

**Implementation of `_run_multi_agent_phase()`**:
```python
def _run_multi_agent_phase(
    pipeline_id: str,
    pipeline: Pipeline,
    phase: str,
    spawner: ContainerSpawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store: StateStore,
    certs_volume: str | None,
    worktree_repo_path: Path,
    review_feedback: str | None = None,
    review_cycle: int = 0,
) -> tuple[int, str]:
    """Run a phase using multi-agent wave-based execution.

    Returns (exit_code, combined_logs) — 0 on success.
    """
```

This function:
1. Creates a `PipelineDispatcher` for the pipeline
2. Builds a spawner callable that wraps `_spawn_and_wait()` with the correct env/volumes/certs
3. Creates a `MultiAgentExecutor` with the spawner callable
4. Calls `executor.execute_all_waves()`
5. On any wave failure, returns non-zero exit code
6. On all waves complete, returns 0 with combined logs

The spawner callable bridges `_spawn_and_wait()`:
```python
def make_spawn_fn(spawner, pipeline_id, ...):
    def spawn_fn(role: AgentRole, prompt: str, extra_env: dict[str, str]) -> tuple[int, str]:
        command = ["claude", "--dangerously-skip-permissions", "--print", ...]
        merged_env = {**sandbox_env, **extra_env}
        return _spawn_and_wait(
            spawner=spawner,
            pipeline_id=pipeline_id,
            agent_role=role,
            issue_number=pipeline.issue_number,
            repo_volumes=repo_volumes,
            gateway_mode=gateway_mode,
            repos=repos,
            phase=phase,
            sandbox_env=merged_env,
            sandbox_command=command + [prompt],
            store=store,
            certs_volume=certs_volume,
        )
    return spawn_fn
```

**In the main `_run_pipeline()` review loop** (line 1953), the branch becomes:
```python
if pipeline.config.multi_agent and current_phase.value == "implement":
    exit_code, container_logs = _run_multi_agent_phase(...)
else:
    # existing single-CODER path (unchanged)
    exit_code, container_logs = _spawn_and_wait(...)
```

**Acceptance criteria**:
- When `multi_agent=true` and phase is `implement`, `_run_multi_agent_phase()` is called
- When `multi_agent=false`, existing single-CODER path executes (no behavior change)
- `_run_multi_agent_phase()` creates a `MultiAgentExecutor` with the pipeline spawner
- `execute_all_waves()` runs agents in dependency-ordered waves: CODER → TESTER + DOCUMENTER → INTEGRATOR
- Each container receives `EGG_AGENT_ROLE`, `EGG_HANDOFF_DATA`, `EGG_WAVE_NUMBER`
- `max_parallel_agents` is respected via threading semaphore
- Phase failure on any agent failure propagates correctly

### Task 1.4: Build agent-specific prompts for implement-phase roles

**File**: `orchestrator/routes/pipelines.py` — new helper, called from `_run_multi_agent_phase()`

**Current behavior**: `_build_phase_prompt()` (line 1063) builds a single prompt for the CODER. There are no role-specific prompts for TESTER, DOCUMENTER, or INTEGRATOR.

**Change**: Add `_build_agent_prompt()` that takes `role`, `phase`, and context, and returns a role-specific prompt. For the CODER role, delegate to the existing `_build_phase_prompt()`. For TESTER, DOCUMENTER, INTEGRATOR, build role-appropriate prompts.

**Implementation**: Each role prompt includes the pipeline context (issue, repo, branch) plus role-specific instructions:
- **CODER**: Existing `_build_phase_prompt()` output (unchanged)
- **TESTER**: Instructions to write/run tests for changes made by CODER. Receives CODER's changed_files via handoff data.
- **DOCUMENTER**: Instructions to update documentation. Receives CODER's changed_files via handoff data.
- **INTEGRATOR**: Instructions to verify integration, run full test suite, resolve any conflicts. Receives outputs from CODER and TESTER.

**Acceptance criteria**:
- `_build_agent_prompt(role, phase, ...)` returns role-appropriate prompts
- CODER prompt matches existing `_build_phase_prompt()` output
- TESTER/DOCUMENTER/INTEGRATOR prompts include role-specific instructions and handoff data
- All prompts include pipeline context (issue, repo, branch, review feedback)

### Task 1.5: Add integration tests for pipeline-level multi-agent execution

**File**: `integration_tests/sdlc/test_multi_agent_pipeline.py` (new file)

**Tests**:
1. **`test_multi_agent_implement_phase_dispatches_waves`**: Mock spawner returns success. Verify `execute_all_waves()` is called, agents spawn in correct wave order (CODER first, then TESTER+DOCUMENTER, then INTEGRATOR).
2. **`test_multi_agent_single_agent_fallback`**: Set `multi_agent=false`. Verify the single-CODER `_spawn_and_wait()` path is taken.
3. **`test_multi_agent_wave_failure_propagates`**: Mock TESTER agent to fail. Verify the phase fails and INTEGRATOR is never spawned.
4. **`test_handoff_data_injected`**: Mock spawner. Verify TESTER receives `EGG_HANDOFF_DATA` containing CODER outputs.
5. **`test_max_parallel_agents_respected`**: Set `max_parallel_agents=1`. Verify agents in a wave execute sequentially (one at a time).

**Acceptance criteria**:
- All 5 tests pass
- Tests use mocked spawner (no real Docker containers)
- Tests cover the primary multi-agent code paths in `_run_multi_agent_phase()`

---

## Phase 2: Add Plan-Phase Agent Roles

### Task 2.1: Define ARCHITECT, TASK_PLANNER, RISK_ANALYST roles

**File**: `shared/egg_contracts/agent_roles.py` — `AgentRole` enum (line 25) and role definitions

**Change**: Add three new enum members and their `AgentRoleDefinition` instances:

```python
class AgentRole(StrEnum):
    CODER = "coder"
    TESTER = "tester"
    DOCUMENTER = "documenter"
    INTEGRATOR = "integrator"
    ARCHITECT = "architect"          # NEW
    TASK_PLANNER = "task_planner"    # NEW
    RISK_ANALYST = "risk_analyst"    # NEW
```

**Role definitions**:
- **ARCHITECT**: Dependencies=[], writes to `.egg-state/drafts/`, `.egg-state/agent-outputs/`. Produces: `["architecture_analysis", "technical_decisions"]`
- **TASK_PLANNER**: Dependencies=[ARCHITECT], writes to `.egg-state/drafts/`, `.egg-state/agent-outputs/`. Requires: `["architecture_analysis"]`. Produces: `["task_breakdown", "acceptance_criteria"]`
- **RISK_ANALYST**: Dependencies=[ARCHITECT], writes to `.egg-state/drafts/`, `.egg-state/agent-outputs/`. Requires: `["architecture_analysis"]`. Produces: `["risk_assessment", "mitigation_plan"]`

**Also update**: `shared/egg_contracts/models.py` — `AgentRoleType` enum (line 313) to include the three new roles, so the contract model can track them.

**Acceptance criteria**:
- `AgentRole` enum has 7 members (4 existing + 3 new)
- `AgentRoleType` enum matches (for contract model)
- Role definitions include correct dependencies: ARCHITECT→[], TASK_PLANNER→[ARCHITECT], RISK_ANALYST→[ARCHITECT]
- `DependencyGraph.compute_waves()` produces: Wave 1=[ARCHITECT], Wave 2=[TASK_PLANNER, RISK_ANALYST]
- File access patterns restrict plan-phase agents to plan-related files
- Existing implement-phase wave computation is unaffected

### Task 2.2: Add `get_roles_for_phase()` function

**File**: `shared/egg_contracts/agent_roles.py` — new function after existing utility functions (after line 398)

**Implementation**:
```python
_PHASE_ROLES: dict[str, list[AgentRole]] = {
    "implement": [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER, AgentRole.INTEGRATOR],
    "plan": [AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
}

def get_roles_for_phase(phase: str) -> list[AgentRole]:
    """Return the agent roles for a given pipeline phase.

    Args:
        phase: Pipeline phase name (e.g., "implement", "plan")
    Returns:
        List of AgentRole values for that phase.
    Raises:
        ValueError: If phase has no defined roles.
    """
    roles = _PHASE_ROLES.get(phase)
    if roles is None:
        raise ValueError(f"No agent roles defined for phase: {phase}")
    return list(roles)
```

**Acceptance criteria**:
- `get_roles_for_phase("implement")` returns [CODER, TESTER, DOCUMENTER, INTEGRATOR]
- `get_roles_for_phase("plan")` returns [ARCHITECT, TASK_PLANNER, RISK_ANALYST]
- `get_roles_for_phase("refine")` raises `ValueError`
- Function is exported from the module

### Task 2.3: Create plan-phase agent prompts

**Files**: New directory `shared/prompts/` with three files:
- `shared/prompts/architect.md` — System prompt for ARCHITECT: analyze the issue and codebase, produce architecture analysis, identify key files, recommend approach, document technical decisions
- `shared/prompts/task_planner.md` — System prompt for TASK_PLANNER: receive architecture analysis, decompose into discrete tasks with acceptance criteria, define dependency order
- `shared/prompts/risk_analyst.md` — System prompt for RISK_ANALYST: receive architecture analysis, assess technical risks, identify rollback strategies, flag areas needing human review

Each prompt follows the pattern of existing phase prompts (context injection, structured output expectations, file write targets).

**Acceptance criteria**:
- Three prompt files exist in `shared/prompts/`
- Each prompt references the handoff data it expects from dependencies
- Each prompt specifies where to write outputs (`.egg-state/agent-outputs/`)
- Prompts are loadable from `_build_agent_prompt()` in pipelines.py

### Task 2.4: Extend multi-agent branch to plan phase

**File**: `orchestrator/routes/pipelines.py` — the multi-agent condition in `_run_pipeline()`

**Current condition (from Task 1.3)**:
```python
if pipeline.config.multi_agent and current_phase.value == "implement":
```

**Change**:
```python
if pipeline.config.multi_agent and current_phase.value in {"implement", "plan"}:
```

**Also**: Update `_run_multi_agent_phase()` to call `get_roles_for_phase(phase)` to determine which roles to use, rather than hardcoding implement-phase roles. The `PipelineDispatcher` and `DependencyGraph` already work generically given a set of roles.

**Acceptance criteria**:
- Plan phase uses wave-based execution when `multi_agent=true`
- ARCHITECT runs in Wave 1, TASK_PLANNER + RISK_ANALYST run in Wave 2
- Plan phase falls back to single-CODER when `multi_agent=false`
- Implement phase continues to work correctly

### Task 2.5: Add cross-phase handoff data flow

**File**: `orchestrator/routes/pipelines.py` — in `_run_pipeline()`, between plan and implement phases

**Current behavior**: After the plan phase completes, `_run_pipeline()` populates the contract from plan output (line ~2300+). There is no mechanism to pass plan-phase agent outputs to implement-phase agents.

**Change**: After plan phase completes in multi-agent mode, persist the plan-phase agent outputs (ARCHITECT analysis, TASK_PLANNER breakdown, RISK_ANALYST assessment) to `.egg-state/agent-outputs/`. When the implement phase starts in multi-agent mode, load these outputs and inject them as `EGG_HANDOFF_DATA` for Wave 1 agents (CODER).

**Implementation**: Use the existing `save_agent_output()` and `collect_handoff_data()` functions from `shared/egg_contracts/orchestrator.py` (lines 330-380). Plan-phase agents write their outputs via these functions. When implement phase starts, read plan-phase outputs and include them in the initial handoff.

**Acceptance criteria**:
- Plan-phase agent outputs are persisted to `.egg-state/agent-outputs/`
- Implement-phase CODER receives plan-phase outputs in `EGG_HANDOFF_DATA`
- Cross-phase handoff works when both plan and implement use multi-agent mode
- Cross-phase handoff degrades gracefully when only one phase uses multi-agent

### Task 2.6: Add tests for plan-phase roles and cross-phase handoff

**File**: Extend `integration_tests/sdlc/test_multi_agent_orchestration.py` and `test_multi_agent_pipeline.py`

**Tests**:
1. **`test_plan_phase_role_definitions`**: Verify ARCHITECT, TASK_PLANNER, RISK_ANALYST have correct dependencies and file access patterns.
2. **`test_plan_phase_wave_computation`**: Verify `compute_waves()` returns [[ARCHITECT], [TASK_PLANNER, RISK_ANALYST]].
3. **`test_get_roles_for_phase`**: Test for "implement", "plan", and invalid phase.
4. **`test_plan_phase_multi_agent_execution`**: Mock spawner. Run plan phase with multi_agent=true. Verify correct wave dispatch.
5. **`test_cross_phase_handoff`**: Run plan phase, verify outputs persisted. Start implement phase, verify CODER receives plan outputs.

**Acceptance criteria**:
- All tests pass
- Plan-phase role graph is tested independently
- Cross-phase handoff tested end-to-end with mocked spawner

---

## Phase 3: File Conflict Resolution

### Task 3.1: Add `detect_write_overlaps()` function

**File**: `shared/egg_contracts/agent_roles.py` — new function

**Implementation**:
```python
def detect_write_overlaps(
    roles: list[AgentRole],
) -> list[tuple[AgentRole, AgentRole, list[str]]]:
    """Detect overlapping write patterns between agents that may run in parallel.

    Returns list of (role1, role2, overlapping_patterns) tuples.
    Only checks roles that can run in the same wave (share no dependency edge).
    """
```

Compare `FileAccessPattern.allowed_write` across all role pairs that could be in the same wave. A pattern overlaps if any concrete path could match both patterns. For glob patterns, use conservative overlap detection (e.g., `*.py` overlaps with `src/**/*.py`).

**Acceptance criteria**:
- `detect_write_overlaps([CODER, TESTER])` returns empty (they're in different waves)
- `detect_write_overlaps([TESTER, DOCUMENTER])` returns overlaps on `.egg-state/agent-outputs/` (both write there)
- The function only considers roles that could actually run concurrently (same wave)

### Task 3.2: Add post-wave merge logic

**File**: `orchestrator/multi_agent.py` — new method on `MultiAgentExecutor`

**Implementation**: After each wave completes (all agents in the wave finish), run:
1. Check for git conflicts in the shared worktree using `git status`
2. If conflicts detected, attempt `git merge` resolution
3. If merge succeeds, proceed to next wave
4. If merge fails, mark the wave as failed with conflict details

```python
def _resolve_wave_conflicts(self, wave: AgentWave) -> list[str]:
    """Attempt to resolve file conflicts after wave completion.

    Returns list of unresolvable conflict file paths (empty = success).
    """
```

This runs inside the orchestrator container (which has access to the worktree) via subprocess git commands. Each agent in a wave commits to its own temporary branch, and the orchestrator merges them sequentially into the working branch after the wave completes.

**Acceptance criteria**:
- After each wave, conflicts are checked and merge is attempted
- On successful merge, next wave proceeds
- On unresolvable conflicts, wave fails with file list in error
- No merge logic runs for waves with a single agent

### Task 3.3: Add `conflicts` field to AgentExecutionModel

**File**: `shared/egg_contracts/models.py` — `AgentExecutionModel` (line 322)

**Change**: Add `conflicts: list[str] = Field(default_factory=list)` to track files with merge conflicts.

**Also**: Add corresponding field to orchestrator's `AgentExecution` model (`orchestrator/models.py` line 108).

**Acceptance criteria**:
- `AgentExecutionModel.conflicts` field exists with default empty list
- Field serializes/deserializes correctly in contract JSON
- Existing code that creates `AgentExecutionModel` still works (field has default)

### Task 3.4: Add retry logic for transient failures

**File**: `orchestrator/multi_agent.py` — in `execute_wave()`

**Change**: When an agent in a wave fails with a non-conflict error, retry up to `MultiAgentConfig.max_retries` times. On conflict failures, fail immediately without retry.

**Implementation**: In `execute_wave()`, after an agent fails:
1. Check if `agent.retry_count < max_retries`
2. Check if failure is not a conflict (conflict errors are not retryable)
3. If retryable: increment `retry_count`, re-dispatch the agent
4. If not retryable: mark wave as failed

**Acceptance criteria**:
- Non-conflict failures are retried up to `max_retries` (default 2)
- Conflict failures fail immediately
- `retry_count` is incremented on each retry
- After exhausting retries, agent is marked failed
- Retry behavior is logged

### Task 3.5: Add conflict resolution tests

**File**: `integration_tests/sdlc/test_multi_agent_conflicts.py` (new file)

**Tests**:
1. **`test_detect_write_overlaps_concurrent_roles`**: Verify overlap detection for roles in the same wave.
2. **`test_no_overlaps_different_waves`**: Verify no false positives for roles in different waves.
3. **`test_wave_merge_success`**: Mock git operations. Two agents write non-overlapping files. Merge succeeds.
4. **`test_wave_merge_conflict_fails`**: Mock git operations. Two agents modify same file. Merge fails. Conflict files reported.
5. **`test_retry_on_transient_failure`**: Agent fails with non-conflict error. Verify retry and eventual success.
6. **`test_no_retry_on_conflict`**: Agent fails with conflict. Verify no retry.
7. **`test_conflicts_field_populated`**: Verify `AgentExecutionModel.conflicts` is populated on merge failure.

**Acceptance criteria**:
- All 7 tests pass
- Overlap detection, merge, retry, and conflict reporting are all covered

---

## Phase 4: Integrate Reviewers as Agent Types

### Task 4.1: Define reviewer roles in AgentRole enum

**File**: `shared/egg_contracts/agent_roles.py` — `AgentRole` enum and role definitions

**Change**: Add four reviewer roles:
```python
class AgentRole(StrEnum):
    ...
    REVIEWER_UNIFIED = "reviewer_unified"
    REVIEWER_CODE = "reviewer_code"
    REVIEWER_CONTRACT = "reviewer_contract"
    REVIEWER_AGENT_DESIGN = "reviewer_agent_design"
```

**Role definitions**: All reviewers have read-only file access (`allowed_write: []`). Dependencies:
- Implement phase: All reviewer roles depend on INTEGRATOR
- Plan phase: All reviewer roles depend on TASK_PLANNER and RISK_ANALYST

This is expressed by making reviewer dependencies phase-aware in `get_roles_for_phase()`.

**Also update**: `AgentRoleType` in `models.py`, and the orchestrator's `AgentRole` enum in `orchestrator/models.py` (which already has REVIEWER but needs the specific subtypes).

**Acceptance criteria**:
- Four reviewer roles defined in `AgentRole` enum
- File access is read-only (empty `allowed_write`)
- `AgentRoleType` includes all reviewer types
- Dependencies are correctly set so reviewers appear in the final wave

### Task 4.2: Extend `get_roles_for_phase()` with `include_reviewers` parameter

**File**: `shared/egg_contracts/agent_roles.py`

**Change**: Add optional `include_reviewers: bool = False` parameter:
```python
_PHASE_REVIEWERS: dict[str, list[AgentRole]] = {
    "implement": [
        AgentRole.REVIEWER_UNIFIED,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
    ],
    "plan": [
        AgentRole.REVIEWER_UNIFIED,
        AgentRole.REVIEWER_AGENT_DESIGN,
    ],
}

def get_roles_for_phase(
    phase: str,
    include_reviewers: bool = False,
) -> list[AgentRole]:
    roles = list(_PHASE_ROLES[phase])
    if include_reviewers:
        roles.extend(_PHASE_REVIEWERS.get(phase, []))
    return roles
```

When reviewers are included, `compute_waves()` places them in the final wave since they depend on the last worker agent.

**Acceptance criteria**:
- `get_roles_for_phase("implement", include_reviewers=True)` returns 8 roles (4 workers + 4 reviewers)
- `get_roles_for_phase("plan", include_reviewers=True)` returns 5 roles (3 workers + 2 reviewers)
- `get_roles_for_phase("implement", include_reviewers=False)` returns 4 roles (workers only)
- Reviewers appear in the final wave of `compute_waves()`

### Task 4.3: Add reviewer wave execution in multi-agent path

**File**: `orchestrator/routes/pipelines.py` — `_run_multi_agent_phase()`

**Change**: When running in multi-agent mode, include reviewers in `execute_all_waves()`. After the final reviewer wave completes, collect reviewer outputs and feed them into the existing `_aggregate_review_verdicts()` logic.

**Implementation**:
1. Call `get_roles_for_phase(phase, include_reviewers=True)` to get the full role set
2. Build a dependency graph including reviewers → they land in the last wave
3. `execute_all_waves()` runs all waves including the reviewer wave
4. After completion, extract reviewer verdicts from `.egg-state/agent-outputs/reviewer_*.json`
5. Feed into `_aggregate_review_verdicts()` (existing function at line 1034)
6. If `needs_revision`: return to the caller, which triggers the review cycle loop

**Reviewer agents need their own prompts**: Each reviewer type writes a structured verdict to `.egg-state/reviews/` (matching the existing review verdict format). The spawner callable builds a reviewer-specific prompt using the existing `_get_review_criteria_for_type()` function (line 802).

**Acceptance criteria**:
- In multi-agent mode, reviewers run concurrently in the final wave
- Reviewer verdicts are collected and aggregated using existing `_aggregate_review_verdicts()`
- If `needs_revision`, the review cycle loops back to re-run worker waves
- Each reviewer type produces a structured verdict file

### Task 4.4: Preserve single-agent review path

**File**: `orchestrator/routes/pipelines.py` — existing reviewer loop (lines 2209-2291)

**No change**: The existing sequential `_PHASE_REVIEWERS` loop is kept for `multi_agent=false`. The multi-agent reviewer path only activates when `multi_agent=true`.

**Change**: Rename the module-level `_PHASE_REVIEWERS` dict (line 795) to `_PHASE_REVIEWER_TYPES` to avoid confusion with the new `_PHASE_REVIEWERS` in `agent_roles.py` which uses `AgentRole` values.

**Acceptance criteria**:
- `multi_agent=false` uses the existing sequential reviewer loop (no behavior change)
- `multi_agent=true` uses wave-based concurrent reviewers
- No regression in single-agent review behavior

### Task 4.5: Migrate verdict aggregation to support both paths

**File**: `orchestrator/routes/pipelines.py` — `_aggregate_review_verdicts()` (line 1034)

**Current behavior**: Takes `dict[str, ReviewVerdict | None]` keyed by reviewer_type string.

**Change**: No signature change needed. Wave-based reviewers produce the same `ReviewVerdict` structure. The caller in `_run_multi_agent_phase()` reads reviewer outputs and constructs the same dict format before calling `_aggregate_review_verdicts()`.

**Acceptance criteria**:
- `_aggregate_review_verdicts()` works identically for both single-agent and multi-agent reviewer paths
- Wave-based reviewer outputs map cleanly to the existing verdict dict format

### Task 4.6: Add reviewer integration tests

**File**: `integration_tests/sdlc/test_multi_agent_reviewers.py` (new file)

**Tests**:
1. **`test_reviewer_roles_in_final_wave`**: Verify `compute_waves()` places reviewers after INTEGRATOR.
2. **`test_concurrent_reviewer_execution`**: Mock spawner. Verify all reviewers in the wave start concurrently.
3. **`test_reviewer_verdict_aggregation`**: Mock reviewer outputs. Verify aggregation matches existing `_aggregate_review_verdicts()` behavior.
4. **`test_revision_cycle_re_runs_workers`**: Reviewer returns needs_revision. Verify worker waves re-execute.
5. **`test_single_agent_reviewer_unchanged`**: Set `multi_agent=false`. Verify sequential reviewer loop still used.

**Acceptance criteria**:
- All 5 tests pass
- Both concurrent and sequential reviewer paths tested

---

## Phase 5: Configuration and Documentation

### Task 5.1: Add CLI flags `--multi-agent` and `--max-parallel`

**File**: `sandbox/egg_lib/cli.py` — argument parsing

**Change**: Add two new flags to the `--sdlc` argument group:
```python
sdlc_group.add_argument(
    "--multi-agent", dest="multi_agent",
    action=argparse.BooleanOptionalAction,
    default=None,
    help="Enable/disable multi-agent execution (--multi-agent / --no-multi-agent)"
)
sdlc_group.add_argument(
    "--max-parallel", type=int, default=None,
    help="Maximum parallel agents per wave (default: 10)"
)
```

These override `PipelineConfig.multi_agent` and `PipelineConfig.max_parallel_agents` respectively. When `None` (not specified), the pipeline config defaults apply.

**Acceptance criteria**:
- `egg --sdlc 546 --multi-agent` enables multi-agent mode
- `egg --sdlc 546 --no-multi-agent` disables multi-agent mode
- `egg --sdlc 546 --max-parallel 4` limits to 4 concurrent agents
- `egg --sdlc 546` uses pipeline config defaults (no override)
- Flags only valid with `--sdlc`

### Task 5.2: Add per-phase config to MultiAgentConfig

**File**: `shared/egg_contracts/models.py` — `MultiAgentConfig` (line 352)

**Change**: Add `phase_overrides` field:
```python
class MultiAgentConfig(BaseModel):
    enabled: bool = Field(default=True)
    max_retries: int = Field(default=2, ge=0)
    parallel_execution: bool = Field(default=True)
    roles_enabled: list[AgentRoleType] = Field(default_factory=lambda: list(AgentRoleType))
    phase_overrides: dict[str, PhaseAgentConfig] = Field(default_factory=dict)  # NEW

class PhaseAgentConfig(BaseModel):
    """Per-phase agent configuration override."""
    enabled: bool = Field(default=True)
    roles: list[AgentRoleType] | None = Field(default=None)  # None = use defaults
    max_parallel_agents: int | None = Field(default=None)  # None = use pipeline default
```

**Config precedence** (highest wins):
1. CLI flags (`--multi-agent`, `--max-parallel`)
2. Contract `multi_agent_config.phase_overrides[phase]`
3. Contract `multi_agent_config` (global)
4. `PipelineConfig` defaults

**Also**: Update `_run_multi_agent_phase()` to read contract config and apply precedence.

**Acceptance criteria**:
- `phase_overrides` allows per-phase role and concurrency configuration
- Config precedence is correctly applied: CLI > contract phase override > contract global > pipeline default
- Existing contracts without `phase_overrides` work unchanged (empty dict default)

### Task 5.3: Enhance status endpoint with wave progress

**File**: `orchestrator/routes/pipelines.py` — `get_pipeline_status()` route

**Change**: When multi-agent is active, include additional fields in the status response:
```json
{
  "multi_agent": {
    "enabled": true,
    "current_wave": 2,
    "total_waves": 3,
    "agents": [
      {"role": "coder", "status": "complete", "wave": 1},
      {"role": "tester", "status": "running", "wave": 2},
      {"role": "documenter", "status": "running", "wave": 2},
      {"role": "integrator", "status": "pending", "wave": 3}
    ]
  }
}
```

**Acceptance criteria**:
- Status endpoint includes wave progress when multi-agent is active
- Per-agent status, role, and wave number are visible
- Response is backward-compatible (new field, not breaking)

### Task 5.4: Create documentation

**File**: `docs/multi-agent.md` (new file)

**Contents**:
1. Overview of multi-agent execution model
2. Agent roles per phase (implement and plan) with dependency diagrams
3. Configuration options: PipelineConfig, MultiAgentConfig, CLI flags
4. Config precedence rules
5. Wave execution and handoff data model
6. File conflict resolution behavior
7. Reviewer integration in multi-agent mode
8. Troubleshooting common issues

**Acceptance criteria**:
- Documentation covers all configuration options
- Agent role dependency graphs for both implement and plan phases
- Troubleshooting section includes common failure modes

### Task 5.5: Add configuration tests

**File**: `integration_tests/sdlc/test_multi_agent_config.py` (new file)

**Tests**:
1. **`test_cli_flag_multi_agent`**: Parse `--multi-agent` and `--no-multi-agent`.
2. **`test_cli_flag_max_parallel`**: Parse `--max-parallel 4`.
3. **`test_config_precedence_cli_over_contract`**: CLI `--no-multi-agent` overrides contract `enabled=True`.
4. **`test_config_precedence_phase_override`**: Contract phase override overrides global config.
5. **`test_status_endpoint_with_wave_data`**: Mock pipeline with active waves. Verify status response includes multi-agent fields.
6. **`test_phase_agent_config_serialization`**: Verify `PhaseAgentConfig` serializes/deserializes in contract JSON.

**Acceptance criteria**:
- All 6 tests pass
- Config precedence logic tested exhaustively

---

## Orchestrator `AgentRole` Mapping Updates

**File**: `orchestrator/dispatch.py` — `map_contract_role_to_agent_role()` and `map_agent_role_to_contract_role()` (lines 50-84)

**Change**: Extend the mapping functions to handle the new roles (ARCHITECT, TASK_PLANNER, RISK_ANALYST, REVIEWER_UNIFIED, REVIEWER_CODE, REVIEWER_CONTRACT, REVIEWER_AGENT_DESIGN). Add these roles to the orchestrator's own `AgentRole` enum in `orchestrator/models.py`.

This is a cross-cutting change needed across Phases 2 and 4. Implement in Phase 2 for plan-phase roles and extend in Phase 4 for reviewer roles.

**Acceptance criteria**:
- All new egg_contracts roles have corresponding orchestrator roles
- Mapping functions handle all new roles correctly
- Orchestrator `AgentRole` enum includes all 11 roles

---

## Risk Assessment and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| **Spawner interface mismatch**: `MultiAgentExecutor` callable doesn't match `_spawn_and_wait()` behavior | High — executor fails to spawn agents | Medium | Define explicit `SpawnFn` type alias; test with mock spawner before real Docker; keep `_spawn_and_wait()` as the backing implementation |
| **Concurrent worktree writes**: Parallel agents corrupt the shared git worktree | High — data loss | Medium | Phase 3 adds branch-per-agent-per-wave and post-wave merge; Phase 1 starts with sequential execution within waves as safe default |
| **Gateway concurrent push rejection**: Gateway serializes or rejects concurrent pushes from same pipeline | Medium — wave execution blocked | Medium | Investigate gateway behavior before Phase 1; add serialized push if needed (agents commit locally, orchestrator pushes once per wave) |
| **Review cycle regression**: Wave-based reviewers break the revision loop | High — review quality degrades | Low | Phase 4 preserves single-agent path; wave-based path only for `multi_agent=true`; test revision cycle explicitly |
| **N concurrent LLM sessions cost**: 10 parallel agents = 10 Claude sessions | Medium — cost spike | High (by design) | `max_parallel_agents` limit enforced; default 10; configurable down to 1 |
| **New plan-phase prompts quality**: ARCHITECT/TASK_PLANNER/RISK_ANALYST prompts may not produce useful output | Medium — poor plan quality | Medium | Iterate on prompts in dedicated testing; use existing phase prompt patterns as baseline |
| **Contract schema migration**: Adding fields to `AgentRoleType`, `AgentExecutionModel`, `MultiAgentConfig` | Low — backward compat | Low | All new fields have defaults; existing contracts parse without changes |

## Rollback Strategy

Each phase is independently deployable and revertable:

- **Phase 1**: Revert by setting `multi_agent=false` in `PipelineConfig` (default behavior unchanged). Or revert the `_run_multi_agent_phase()` function and the condition in `_run_pipeline()`.
- **Phase 2**: Revert by removing plan-phase roles from `get_roles_for_phase()`. Plan phase falls back to single-CODER.
- **Phase 3**: Revert by removing merge logic from `execute_wave()`. Parallel agents still work but without conflict resolution (acceptable for non-overlapping file patterns).
- **Phase 4**: Revert by removing `include_reviewers=True` from the multi-agent path. Reviewers fall back to sequential `_PHASE_REVIEWERS` loop.
- **Phase 5**: Config flags are additive. Removing them just means defaults apply.

## Test Strategy Summary

| Phase | New Test File(s) | Test Count | Key Coverage |
|-------|-----------------|------------|--------------|
| Phase 1 | `test_multi_agent_pipeline.py` | 5 | Executor+spawner integration, wave dispatch, handoff, fallback |
| Phase 2 | Extend existing + `test_multi_agent_pipeline.py` | 5 | Plan-phase roles, wave computation, cross-phase handoff |
| Phase 3 | `test_multi_agent_conflicts.py` | 7 | Overlap detection, merge, retry, conflict reporting |
| Phase 4 | `test_multi_agent_reviewers.py` | 5 | Reviewer waves, concurrent execution, verdict aggregation, revision cycle |
| Phase 5 | `test_multi_agent_config.py` | 6 | CLI flags, config precedence, status endpoint, serialization |
| **Total** | **4 new files + extend 1** | **28** | |

**Backward compatibility requirement**: All existing tests must pass unchanged after each phase. The pipeline with `multi_agent=false` must behave identically to current behavior.

---

## Files Modified Summary

| File | Phases | Changes |
|------|--------|---------|
| `orchestrator/multi_agent.py` | 1, 3 | Spawner callable, conflict resolution, retry logic |
| `orchestrator/models.py` | 1, 4 | `max_parallel_agents` field, new agent roles in enum |
| `orchestrator/routes/pipelines.py` | 1, 2, 4, 5 | Multi-agent branch, `_run_multi_agent_phase()`, reviewer wave, status endpoint |
| `orchestrator/dispatch.py` | 2, 4 | Role mapping for new roles |
| `shared/egg_contracts/agent_roles.py` | 2, 3, 4 | New roles, `get_roles_for_phase()`, `detect_write_overlaps()`, reviewer roles |
| `shared/egg_contracts/models.py` | 2, 3, 5 | `AgentRoleType` expansion, `conflicts` field, `PhaseAgentConfig`, `phase_overrides` |
| `sandbox/egg_lib/cli.py` | 5 | `--multi-agent`, `--max-parallel` flags |

## New Files

| File | Phase | Purpose |
|------|-------|---------|
| `shared/prompts/architect.md` | 2 | ARCHITECT agent system prompt |
| `shared/prompts/task_planner.md` | 2 | TASK_PLANNER agent system prompt |
| `shared/prompts/risk_analyst.md` | 2 | RISK_ANALYST agent system prompt |
| `docs/multi-agent.md` | 5 | Documentation |
| `integration_tests/sdlc/test_multi_agent_pipeline.py` | 1 | Pipeline-level multi-agent tests |
| `integration_tests/sdlc/test_multi_agent_conflicts.py` | 3 | Conflict resolution tests |
| `integration_tests/sdlc/test_multi_agent_reviewers.py` | 4 | Reviewer integration tests |
| `integration_tests/sdlc/test_multi_agent_config.py` | 5 | Configuration tests |

---

## Open Questions (Recommendations Included)

1. **Gateway concurrent push**: Can multiple containers push to the same branch simultaneously? **Recommendation**: Investigate before Phase 1 implementation. If not supported, use branch-per-agent with orchestrator merge (same mechanism as Phase 3 conflict resolution). This investigation should be the first task in Phase 1.

2. **Config precedence**: When `PipelineConfig.multi_agent=True` but `contract.multi_agent_config.enabled=False`, which wins? **Recommendation**: Contract config wins — it's the per-issue source of truth. Document this.

3. **Prompt loading**: Should plan-phase prompts live in `shared/prompts/` as separate files or be embedded in `_build_agent_prompt()`? **Recommendation**: Separate files in `shared/prompts/` for maintainability. Load at runtime.

---

Authored-by: egg
