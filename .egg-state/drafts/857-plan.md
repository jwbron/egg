# Plan: Sync pipeline state branch to remote for durability

> Issue: #857 | Phase: plan

## Summary

The orchestrator's `egg/pipeline-state` branch is local-only. Volume loss or
host migration means total state loss with no recovery path. Old pipeline state
files with stale schemas (e.g., removed `reviewer_unified` role) cause
recurring Pydantic `ValidationError` warnings on every startup.

This plan delivers two capabilities in a single PR:

1. **Remote sync** — push `egg/pipeline-state` to the remote at phase
   boundaries (creation, phase start/complete, terminal states) and fetch
   from remote on startup for recovery. Uses the existing `GatewayClient`
   push/fetch infrastructure.

2. **Schema sanitization** — a `sanitize_pipeline_data()` function that
   normalizes unknown enum values and structural drift before Pydantic
   validation, making old pipeline state files loadable.

## Approach

### Remote sync: push at phase boundaries

The orchestrator already pushes *worktree branches* to remote via
`GatewayClient.push_worktree_branch()` at three points:
- After contract initialization (pipelines.py:4732)
- After phase completion (pipelines.py:5506)
- On pipeline failure (pipelines.py:5399)

We add parallel *state branch* pushes at these same call sites, plus at
pipeline creation and terminal completion. The push method on `StateStore`
delegates to `GatewayClient` with the state worktree path and state branch
refspec. Pushes are best-effort — failures log a warning but never block
pipeline execution.

On startup, before reconciliation, the orchestrator fetches
`egg/pipeline-state` from remote. If the remote branch exists, it resets the
local worktree to match (remote wins). If it doesn't exist or the fetch fails,
startup proceeds with local state — fully backwards-compatible.

### Schema sanitization: pre-validation normalization

A `sanitize_pipeline_data(data: dict) -> dict` function in `state_store.py`
runs before `Pipeline.model_validate()`. It:
- Collects valid values for each enum field (AgentRole, PipelineStatus, etc.)
- Drops agent executions with unknown roles (log warning)
- Maps unknown status enums to safe defaults (FAILED/CANCELLED)
- Removes unrecognized top-level keys

This keeps migration logic separate from the Pydantic models and handles
arbitrary schema drift — not just the known `reviewer_unified` gap.

## Phased Implementation

### Phase 1: Schema sanitization

Start with sanitization because it's self-contained, has no external
dependencies, and unblocks old pipeline loading — a value-add even without
remote sync.

**Tasks:**
1. Implement `sanitize_pipeline_data()` in `state_store.py`
2. Integrate sanitization into `load_pipeline()`
3. Write unit tests for sanitization and integration

### Phase 2: Remote sync — StateStore methods

Add `push_state_branch()` and `fetch_state_branch()` methods to `StateStore`,
wired through `GatewayClient`. These are standalone methods that can be tested
before wiring into the pipeline lifecycle.

**Tasks:**
4. Add `push_state_branch()` to `StateStore`
5. Add `fetch_state_branch()` to `StateStore`
6. Write unit tests for push/fetch methods

### Phase 3: Lifecycle integration

Wire the push/fetch methods into the pipeline lifecycle:
- Fetch on startup (before reconciliation)
- Push at phase boundaries in `_run_pipeline()`

**Tasks:**
7. Add fetch-on-startup to `cli.py`
8. Add push calls at phase boundaries in `routes/pipelines.py`
9. Update module docstring in `state_store.py` (local-only note is now stale)
10. Write integration-level tests for lifecycle hooks

## File Impact

| File | Changes |
|------|---------|
| `orchestrator/state_store.py` | `sanitize_pipeline_data()`, `push_state_branch()`, `fetch_state_branch()`, integrate sanitization into `load_pipeline()`, update docstring |
| `orchestrator/gateway_client.py` | May need thin wrappers for state branch push/fetch (or reuse existing methods with different params) |
| `orchestrator/cli.py` | Add `fetch_state_branch()` call on startup |
| `orchestrator/routes/pipelines.py` | Add `push_state_branch()` calls at phase boundaries |
| `orchestrator/tests/test_state_store.py` | Tests for `sanitize_pipeline_data()`, `push_state_branch()`, `fetch_state_branch()`, load_pipeline with old schemas |
| `orchestrator/tests/test_startup_reconciliation.py` | Test startup fetch behavior (no remote branch, fetch failure) |
| `orchestrator/tests/test_pipeline_failure_path.py` | Update existing push tests to also verify state branch push calls |

## Test Strategy

**Unit tests (test_state_store.py):**
- `sanitize_pipeline_data` with unknown AgentRole drops agent from agents list
- `sanitize_pipeline_data` with unknown PipelineStatus maps to FAILED
- `sanitize_pipeline_data` with unknown ContainerStatus maps to FAILED
- `sanitize_pipeline_data` with unknown AgentExecutionStatus maps to FAILED
- `sanitize_pipeline_data` with unknown DecisionStatus maps to CANCELLED
- `sanitize_pipeline_data` with valid data passes through unchanged
- `sanitize_pipeline_data` with deeply nested unknown enums in PhaseExecution
- `load_pipeline` successfully loads old pipeline with `reviewer_unified` after sanitization
- `push_state_branch` success returns True
- `push_state_branch` failure returns False and logs warning
- `fetch_state_branch` success syncs worktree
- `fetch_state_branch` with no remote branch is a graceful no-op
- `fetch_state_branch` failure proceeds with local state

**Integration tests (test_pipeline_failure_path.py / test_startup_reconciliation.py):**
- State branch push is called alongside worktree branch push at phase completion
- State branch push is called on pipeline failure
- Startup fetch restores state when local is empty
- Startup proceeds when remote fetch fails

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Push failure leaves remote state stale | Low | Best-effort, non-blocking. Next phase boundary catches up. |
| Startup fetch overwrites local unpushed changes | Low | Only if last push failed AND volume lost (double failure). Remote is more recent than empty. |
| Sanitization drops data from old pipelines | Low | Only drops unrecognizable agent executions, not whole pipelines. Originals in git history. |
| Force-push conflict with concurrent pushes | Low | Single-orchestrator architecture. Documented as constraint. |

## Key Decisions

1. **Reuse GatewayClient infrastructure** — no new gateway endpoints. State
   branch push uses the same session-based push mechanism as worktree branches.
2. **Remote wins on startup** — if remote has state, local is reset to match.
   The purpose of remote sync is durability.
3. **Drop unknown agent roles** — rather than mapping `reviewer_unified` to
   `reviewer_code`, we drop the execution. Old pipelines are terminal; accuracy
   matters less than loadability.
4. **Sanitize before validation, not inside the model** — keeps migration
   logic separate from Pydantic models. Easier to test, doesn't accumulate
   debt in model definitions.

---

*Authored-by: egg*

```yaml
# yaml-tasks
pr:
  title: "Sync pipeline state branch to remote for durability"
  description: |
    Push the orchestrator's egg/pipeline-state branch to remote at phase
    boundaries and fetch on startup for recovery after volume loss or host
    migration. Add a pre-validation sanitization layer that normalizes old
    pipeline state files with stale schemas (e.g., removed reviewer_unified
    role) so they load without perpetual ValidationError warnings.
phases:
  - id: 1
    name: Schema sanitization
    goal: Make old pipeline state files with stale schemas loadable without errors
    tasks:
      - id: TASK-1-1
        description: >
          Implement sanitize_pipeline_data(data: dict) -> dict function in
          orchestrator/state_store.py. Builds sets of valid enum values from
          model enums. Drops agent executions with unknown AgentRole values.
          Maps unknown PipelineStatus/ContainerStatus/AgentExecutionStatus to
          FAILED. Maps unknown DecisionStatus to CANCELLED. Removes
          unrecognized top-level keys. Logs what was sanitized.
        acceptance: >
          Function exists, handles all enum fields in the Pipeline model,
          returns sanitized dict. Unknown AgentRole causes agent drop.
          Unknown status enums map to safe defaults. Valid data passes
          through unchanged.
        files:
          - orchestrator/state_store.py
      - id: TASK-1-2
        description: >
          Integrate sanitize_pipeline_data() into load_pipeline() in
          orchestrator/state_store.py. Call sanitize before
          Pipeline.model_validate(). Log at INFO level when sanitization
          modifies data.
        acceptance: >
          load_pipeline() calls sanitize_pipeline_data() before validation.
          A pipeline JSON with reviewer_unified role loads successfully
          instead of raising StateValidationError.
        files:
          - orchestrator/state_store.py
      - id: TASK-1-3
        description: >
          Write unit tests for sanitize_pipeline_data() and the
          load_pipeline() integration in orchestrator/tests/test_state_store.py.
          Cover: unknown AgentRole drops agent, unknown PipelineStatus maps
          to FAILED, unknown ContainerStatus maps to FAILED, unknown
          DecisionStatus maps to CANCELLED, valid data unchanged, deeply
          nested enums in PhaseExecution, load_pipeline with old
          reviewer_unified schema.
        acceptance: >
          All new tests pass. At least 8 test methods covering the
          sanitization function and integration path.
        files:
          - orchestrator/tests/test_state_store.py
  - id: 2
    name: Remote sync methods
    goal: Add push/fetch methods for the state branch via GatewayClient
    tasks:
      - id: TASK-2-1
        description: >
          Add push_state_branch(gateway_client) method to StateStore in
          orchestrator/state_store.py. Uses gateway_client to push the
          egg/pipeline-state branch from the state worktree directory.
          Best-effort: returns True/False, logs warning on failure. Uses
          the existing push_worktree_branch pattern with state branch
          refspec and state worktree path.
        acceptance: >
          Method exists on StateStore. Calls gateway_client with correct
          repo_path (state worktree) and branch (egg/pipeline-state).
          Returns bool. Does not raise on failure.
        files:
          - orchestrator/state_store.py
      - id: TASK-2-2
        description: >
          Add fetch_state_branch(gateway_client) method to StateStore in
          orchestrator/state_store.py. Fetches egg/pipeline-state from
          remote via gateway_client, then resets local worktree to match
          remote (git reset --hard). If remote branch does not exist, no-op.
          If fetch fails, log warning and continue with local state.
        acceptance: >
          Method exists on StateStore. On success, local worktree matches
          remote state. On failure or missing remote branch, returns False
          gracefully. Does not raise.
        files:
          - orchestrator/state_store.py
      - id: TASK-2-3
        description: >
          Write unit tests for push_state_branch() and fetch_state_branch()
          in orchestrator/tests/test_state_store.py. Cover: push success,
          push failure (returns False, logs), fetch success, fetch with no
          remote branch (no-op), fetch failure (returns False, logs).
          Mock GatewayClient to avoid real network calls.
        acceptance: >
          All new tests pass. At least 5 test methods covering push/fetch
          success and failure cases.
        files:
          - orchestrator/tests/test_state_store.py
  - id: 3
    name: Lifecycle integration
    goal: Wire push/fetch into pipeline startup and phase boundaries
    tasks:
      - id: TASK-3-1
        description: >
          Add fetch_state_branch() call to orchestrator/cli.py cmd_serve
          startup, BEFORE startup reconciliation (line ~132). Create
          GatewayClient, call store.fetch_state_branch(). Wrap in
          try/except — fetch failure must not prevent startup.
        acceptance: >
          On startup, orchestrator fetches state branch from remote before
          reconciliation runs. If fetch fails, startup proceeds normally.
          Backwards-compatible with environments that have no remote state
          branch.
        files:
          - orchestrator/cli.py
      - id: TASK-3-2
        description: >
          Add push_state_branch() calls in orchestrator/routes/pipelines.py
          at phase boundaries. Push state branch alongside existing
          push_worktree_branch calls: (1) after contract init (~line 4736),
          (2) on pipeline failure (~line 5403), (3) after phase completion
          (~line 5510). Also add pushes at: (4) pipeline terminal completion
          (~line 5678). All pushes are best-effort with try/except. Access
          GatewayClient via spawner.gateway. Access StateStore via
          get_state_store(). Push uses the state worktree path from the
          store instance.
        acceptance: >
          State branch is pushed to remote at all phase boundary points.
          Push failures are caught and logged without affecting pipeline
          execution. Existing worktree branch push behavior is unchanged.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-3
        description: >
          Update the module docstring at the top of
          orchestrator/state_store.py to reflect that the state branch is
          now pushed to remote at phase boundaries (remove the
          "local-only" characterization at line 10).
        acceptance: >
          Docstring accurately describes the new behavior: state branch is
          pushed to remote at phase boundaries for durability, with
          fetch-on-startup for recovery.
        files:
          - orchestrator/state_store.py
      - id: TASK-3-4
        description: >
          Write tests for lifecycle integration. In
          test_startup_reconciliation.py: test that fetch is called before
          reconciliation. In test_pipeline_failure_path.py: verify
          push_state_branch is called alongside push_worktree_branch at
          phase completion and on failure. Test that push failures don't
          block pipeline execution.
        acceptance: >
          All new tests pass. Lifecycle integration points are covered.
          Existing tests continue to pass.
        files:
          - orchestrator/tests/test_startup_reconciliation.py
          - orchestrator/tests/test_pipeline_failure_path.py
```
