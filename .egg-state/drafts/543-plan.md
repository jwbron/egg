# Plan: Unify Local-mode and Issue-mode Pipelines

> Issue: #543 | Phase: plan

## Summary

This implementation unifies local-mode and issue-mode pipelines by introducing phase-based push restrictions (replacing the blanket local-mode block), enabling state persistence at phase boundaries, prefixing file paths with pipeline IDs, adding contract CLI instructions to both modes, enabling checkpoints for local mode, and populating container/agent tracking in phase state. The approach builds on existing infrastructure (`PhaseFilter`, `phase-permissions.json`) to minimize new code while maintaining the security model.

## Implementation Phases

### Phase 1: Unify Git Push Restrictions

**Goal**: Replace the blanket local-mode push block with phase-based file restrictions that apply to both modes.

**Tasks**:
- [TASK-1-1] Add phase-based file restrictions to `phase-permissions.json` — Acceptance: Config includes `phase_file_restrictions` section with per-phase allowed/blocked patterns
- [TASK-1-2] Extend `PhaseFilter` to check phase-based file restrictions — Acceptance: New method `check_phase_file_restrictions(phase, files)` returns allow/block result
- [TASK-1-3] Update gateway push handler to apply phase restrictions — Acceptance: Push handler checks session phase and enforces file restrictions before allowing push
- [TASK-1-4] Remove blanket local-mode push block in `gateway.py` — Acceptance: Lines 521-533 removed, local-mode pushes allowed subject to phase restrictions
- [TASK-1-5] Ensure checkpoint branch pushes always bypass restrictions — Acceptance: Pushes to `egg/checkpoints/v1` branch succeed from any phase/mode

**Dependencies**: None

**Exit criteria**: Local-mode pipelines can push `.egg-state/` files during refine/plan phases and source code during implement/pr phases, with the same restrictions as issue-mode.

### Phase 2: Unify State Persistence

**Goal**: Commit pipeline state to git for local pipelines at phase boundaries.

**Tasks**:
- [TASK-2-1] Add `commit_at_phase_boundary` flag to `StateStore.save_pipeline()` — Acceptance: Method accepts flag to override mode-based commit skip
- [TASK-2-2] Modify orchestrator phase transitions to commit local pipeline state — Acceptance: State committed to git when phase changes (not on every save)
- [TASK-2-3] Update `delete_pipeline()` to handle local pipeline git cleanup — Acceptance: Local pipeline deletion commits the removal to git

**Dependencies**: Phase 1 (push must be allowed for state to be pushed)

**Exit criteria**: Local pipeline state files are committed to git at phase boundaries and visible in git history.

### Phase 3: Unify File Paths

**Goal**: Use pipeline ID prefixes for local-mode draft and review files to support concurrent pipelines.

**Tasks**:
- [TASK-3-1] Update `_get_draft_path()` to use pipeline ID prefix for local mode — Acceptance: Local mode uses `.egg-state/drafts/{pipeline_id}-{phase}.md` format
- [TASK-3-2] Update `_verdict_path_for_type()` to use pipeline ID prefix for local mode — Acceptance: Local mode uses `.egg-state/reviews/{pipeline_id}-{phase}-{type}.json` format
- [TASK-3-3] Update `_read_phase_draft()` to handle prefixed paths — Acceptance: Draft reading works with new path format
- [TASK-3-4] Update agent prompts to reference correct prefixed paths — Acceptance: Phase prompts show correct file paths for local mode

**Dependencies**: None (can run parallel to Phase 1)

**Exit criteria**: Local pipelines use `{pipeline_id}-` prefixed paths for all `.egg-state/` files.

### Phase 4: Unify Contract Usage

**Goal**: Include contract CLI instructions in local-mode agent prompts.

**Tasks**:
- [TASK-4-1] Update `_build_phase_prompt()` to include contract CLI for local mode — Acceptance: Contract CLI instructions appear in prompts for both modes
- [TASK-4-2] Update `create_local_contract()` to set `contract_synced=False` — Acceptance: New local contracts have `contract_synced=False` in pipeline state
- [TASK-4-3] Ensure contract file uses pipeline ID as key for local mode — Acceptance: Contract saved as `.egg-state/contracts/{pipeline_id}.json`

**Dependencies**: None

**Exit criteria**: Local-mode agents see contract CLI instructions and can use `egg-contract show/add-commit` commands.

### Phase 5: Enable Checkpoints for Local Mode

**Goal**: Local pipelines produce checkpoints on successful pushes.

**Tasks**:
- [TASK-5-1] Verify checkpoint capture triggers on local-mode pushes — Acceptance: `capture_and_store_checkpoints_for_push()` called after local-mode push succeeds
- [TASK-5-2] Update phase restrictions to always allow checkpoint file pushes — Acceptance: `.egg-state/checkpoints/*` files pushable from all phases
- [TASK-5-3] Test checkpoint creation for local pipeline pushes — Acceptance: Checkpoint JSON appears on `egg/checkpoints/v1` branch after local-mode push

**Dependencies**: Phase 1 (push must be allowed)

**Exit criteria**: Local pipelines produce checkpoints with the same format as issue-mode pipelines.

### Phase 6: Container and Agent Tracking

**Goal**: Populate `PhaseExecution.containers` and `PhaseExecution.agents` during pipeline execution.

**Tasks**:
- [TASK-6-1] Ensure all `_spawn_and_wait()` calls pass `store` parameter — Acceptance: All call sites in `pipelines.py` pass state store for tracking
- [TASK-6-2] Add agent tracking in multi-agent execution — Acceptance: `PhaseExecution.agents` populated when spawning multi-agent containers
- [TASK-6-3] Update agent status on completion/failure — Acceptance: `AgentExecution` records updated with `completed_at`, `commit`, `error` fields
- [TASK-6-4] Verify API exposes container/agent state — Acceptance: GET `/pipelines/{id}` returns populated `containers` and `agents` arrays

**Dependencies**: None

**Exit criteria**: Pipeline status API shows active containers and agent execution state for both modes.

### Phase 7: Integration Tests

**Goal**: Comprehensive test coverage for unified pipeline behavior.

**Tasks**:
- [TASK-7-1] Test local pipeline push restrictions per phase — Acceptance: Test verifies refine/plan can push `.egg-state/`, implement can push code
- [TASK-7-2] Test checkpoint creation for local pipelines — Acceptance: Test verifies checkpoint JSON created on push
- [TASK-7-3] Test contract CLI in local mode — Acceptance: Test verifies `egg-contract show/add-commit` work in local pipeline
- [TASK-7-4] Test concurrent pipelines with prefixed paths — Acceptance: Two local pipelines run concurrently without file conflicts
- [TASK-7-5] Test container/agent tracking via API — Acceptance: Test verifies running containers appear in API response
- [TASK-7-6] Test state persistence at phase boundaries — Acceptance: Test verifies local pipeline state committed to git on phase transition

**Dependencies**: All previous phases

**Exit criteria**: All integration tests pass, covering the unified behavior between local and issue modes.

## Test Strategy

- **Unit tests**:
  - `PhaseFilter.check_phase_file_restrictions()` with various phase/file combinations
  - `StateStore.save_pipeline()` with `commit_at_phase_boundary` flag
  - `_get_draft_path()` and `_verdict_path_for_type()` with local mode pipeline IDs

- **Integration tests**:
  - Full local pipeline execution from refine to PR with push verification
  - Checkpoint capture verification after local-mode push
  - Concurrent pipeline isolation test
  - Container tracking visibility during execution

- **Manual testing**:
  1. Run `egg local "Test task"` and verify push works in each phase
  2. Check `.egg-state/contracts/{pipeline_id}.json` exists
  3. Verify checkpoints appear on `egg/checkpoints/v1` branch
  4. Query `/api/v1/pipelines/{id}` and verify containers array populated
  5. Run two local pipelines concurrently and verify no path conflicts

## Rollback Plan

If issues arise after deployment:

1. **Immediate rollback**: Revert the commits that:
   - Removed the local-mode push block (TASK-1-4)
   - Changed state persistence behavior (TASK-2-1, TASK-2-2)

2. **Partial rollback** (if only specific features fail):
   - Phase restrictions can be disabled by setting `phase_file_restrictions: {}` in `phase-permissions.json`
   - State persistence can revert to disk-only by re-adding the `is_local` check in `save_pipeline()`

3. **Recovery commands**:
   ```bash
   # Revert to previous gateway behavior
   git revert <commit-hash-for-task-1-4>

   # Clear corrupted local pipeline state
   rm -rf .egg-state/pipelines/local-*
   ```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Phase restriction bypass via edge cases | Medium | High | Comprehensive testing, fail-closed on file check errors |
| Git history pollution from frequent state commits | Low | Low | Only commit at phase boundaries per human feedback |
| Checkpoint branch push conflicts | Low | Medium | Checkpoint pushes use unique per-commit paths |
| Breaking existing issue-mode behavior | Low | High | Run existing issue-mode tests before merge |
| Race condition during phase transition | Medium | Medium | Use optimistic locking (already implemented) |

## Migration Notes

- **No backwards compatibility required**: Per human feedback, existing local pipelines with unprefixed paths do not need migration support.
- **No config changes for users**: Phase restrictions are enforced server-side; no client updates needed.
- **Contract file location**: Local contracts already use `{pipeline_id}.json` naming, no migration needed.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Unify local-mode and issue-mode pipeline behavior"
  description: |
    Unifies local-mode and issue-mode pipelines so both follow the same
    contract/checkpoint/push discipline. Replaces blanket local-mode push
    blocking with phase-based file restrictions, enables state persistence
    at phase boundaries, and adds container/agent tracking.

    Fixes #543
phases:
  - id: 1
    name: Unify Git Push Restrictions
    goal: Replace blanket local-mode push block with phase-based file restrictions
    tasks:
      - id: TASK-1-1
        description: Add phase-based file restrictions to phase-permissions.json
        acceptance: Config includes phase_file_restrictions section with per-phase allowed/blocked patterns
        files:
          - .egg/phase-permissions.json
      - id: TASK-1-2
        description: Extend PhaseFilter to check phase-based file restrictions
        acceptance: New method check_phase_file_restrictions(phase, files) returns allow/block result
        files:
          - gateway/phase_filter.py
      - id: TASK-1-3
        description: Update gateway push handler to apply phase restrictions
        acceptance: Push handler checks session phase and enforces file restrictions
        files:
          - gateway/gateway.py
      - id: TASK-1-4
        description: Remove blanket local-mode push block in gateway.py
        acceptance: Lines 521-533 removed, local-mode pushes allowed subject to phase restrictions
        files:
          - gateway/gateway.py
      - id: TASK-1-5
        description: Ensure checkpoint branch pushes always bypass restrictions
        acceptance: Pushes to egg/checkpoints/v1 branch succeed from any phase/mode
        files:
          - gateway/gateway.py
  - id: 2
    name: Unify State Persistence
    goal: Commit pipeline state to git for local pipelines at phase boundaries
    tasks:
      - id: TASK-2-1
        description: Add commit_at_phase_boundary flag to StateStore.save_pipeline()
        acceptance: Method accepts flag to override mode-based commit skip
        files:
          - orchestrator/state_store.py
      - id: TASK-2-2
        description: Modify orchestrator phase transitions to commit local pipeline state
        acceptance: State committed to git when phase changes
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Update delete_pipeline() to handle local pipeline git cleanup
        acceptance: Local pipeline deletion commits the removal to git
        files:
          - orchestrator/state_store.py
  - id: 3
    name: Unify File Paths
    goal: Use pipeline ID prefixes for local-mode draft and review files
    tasks:
      - id: TASK-3-1
        description: Update _get_draft_path() to use pipeline ID prefix for local mode
        acceptance: Local mode uses .egg-state/drafts/{pipeline_id}-{phase}.md format
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-2
        description: Update _verdict_path_for_type() to use pipeline ID prefix for local mode
        acceptance: Local mode uses .egg-state/reviews/{pipeline_id}-{phase}-{type}.json format
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-3
        description: Update _read_phase_draft() to handle prefixed paths
        acceptance: Draft reading works with new path format
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-4
        description: Update agent prompts to reference correct prefixed paths
        acceptance: Phase prompts show correct file paths for local mode
        files:
          - orchestrator/routes/pipelines.py
  - id: 4
    name: Unify Contract Usage
    goal: Include contract CLI instructions in local-mode agent prompts
    tasks:
      - id: TASK-4-1
        description: Update _build_phase_prompt() to include contract CLI for local mode
        acceptance: Contract CLI instructions appear in prompts for both modes
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        description: Update create_local_contract() to set contract_synced=False
        acceptance: New local contracts have contract_synced=False in pipeline state
        files:
          - orchestrator/routes/pipelines.py
          - shared/egg_contracts/loader.py
      - id: TASK-4-3
        description: Ensure contract file uses pipeline ID as key for local mode
        acceptance: Contract saved as .egg-state/contracts/{pipeline_id}.json
        files:
          - shared/egg_contracts/loader.py
  - id: 5
    name: Enable Checkpoints for Local Mode
    goal: Local pipelines produce checkpoints on successful pushes
    tasks:
      - id: TASK-5-1
        description: Verify checkpoint capture triggers on local-mode pushes
        acceptance: capture_and_store_checkpoints_for_push() called after local-mode push succeeds
        files:
          - gateway/gateway.py
          - gateway/checkpoint_handler.py
      - id: TASK-5-2
        description: Update phase restrictions to always allow checkpoint file pushes
        acceptance: .egg-state/checkpoints/* files pushable from all phases
        files:
          - .egg/phase-permissions.json
      - id: TASK-5-3
        description: Test checkpoint creation for local pipeline pushes
        acceptance: Checkpoint JSON appears on egg/checkpoints/v1 branch after local-mode push
        files:
          - gateway/tests/test_checkpoint_handler.py
  - id: 6
    name: Container and Agent Tracking
    goal: Populate PhaseExecution.containers and PhaseExecution.agents during execution
    tasks:
      - id: TASK-6-1
        description: Ensure all _spawn_and_wait() calls pass store parameter
        acceptance: All call sites in pipelines.py pass state store for tracking
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-6-2
        description: Add agent tracking in multi-agent execution
        acceptance: PhaseExecution.agents populated when spawning multi-agent containers
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-6-3
        description: Update agent status on completion/failure
        acceptance: AgentExecution records updated with completed_at, commit, error fields
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-6-4
        description: Verify API exposes container/agent state
        acceptance: GET /pipelines/{id} returns populated containers and agents arrays
        files:
          - orchestrator/routes/pipelines.py
  - id: 7
    name: Integration Tests
    goal: Comprehensive test coverage for unified pipeline behavior
    tasks:
      - id: TASK-7-1
        description: Test local pipeline push restrictions per phase
        acceptance: Test verifies refine/plan can push .egg-state/, implement can push code
        files:
          - orchestrator/tests/test_pipeline_push_restrictions.py
      - id: TASK-7-2
        description: Test checkpoint creation for local pipelines
        acceptance: Test verifies checkpoint JSON created on push
        files:
          - gateway/tests/test_checkpoint_handler.py
      - id: TASK-7-3
        description: Test contract CLI in local mode
        acceptance: Test verifies egg-contract show/add-commit work in local pipeline
        files:
          - orchestrator/tests/test_local_pipeline_contracts.py
      - id: TASK-7-4
        description: Test concurrent pipelines with prefixed paths
        acceptance: Two local pipelines run concurrently without file conflicts
        files:
          - orchestrator/tests/test_concurrent_pipelines.py
      - id: TASK-7-5
        description: Test container/agent tracking via API
        acceptance: Test verifies running containers appear in API response
        files:
          - orchestrator/tests/test_container_tracking.py
      - id: TASK-7-6
        description: Test state persistence at phase boundaries
        acceptance: Test verifies local pipeline state committed to git on phase transition
        files:
          - orchestrator/tests/test_state_persistence.py
```

---

*Authored-by: egg*
