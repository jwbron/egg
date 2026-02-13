# Plan: Add Token Usage Tracking

> Issue: #519 | Phase: plan

## Summary

This plan implements token usage tracking across multiple dimensions (session, job, workflow, issue, PR) using the existing orphaned branch pattern established for checkpoints. Based on the analysis phase decisions, we will store pre-computed JSON aggregate files in the `egg/checkpoints/v1` branch alongside existing checkpoint data, with backfill-on-PR-creation for PR number association.

The implementation extends the checkpoint storage system to maintain aggregate usage summaries that are updated atomically when checkpoints are added, enabling O(1) lookups for usage by any dimension.

## Implementation Phases

### Phase 1: Data Models and Schema

**Goal**: Define the Pydantic models and JSON schema for token usage aggregates

**Tasks**:
- [TASK-1-1] Create usage aggregate Pydantic models — Acceptance: Models defined with proper validation, type hints, and docstrings; unit tests pass
- [TASK-1-2] Create JSON schema for usage aggregates — Acceptance: Schema validates sample usage documents; consistent with checkpoint schema patterns
- [TASK-1-3] Add `pr_number` field to Checkpoint model — Acceptance: Checkpoint and CheckpointSummary models include optional `pr_number` field; schema updated

**Dependencies**: None

**Exit criteria**: All models defined, schemas created, and unit tests for model validation pass

### Phase 2: Usage Storage Infrastructure

**Goal**: Implement the storage layer for usage aggregates in the orphaned branch

**Tasks**:
- [TASK-2-1] Create usage loader module with atomic read/write — Acceptance: `load_*_usage()` and `save_*_usage()` functions implemented with temp-file-rename pattern; handles missing files gracefully
- [TASK-2-2] Add usage directory structure to checkpoint branch — Acceptance: `usage/by-issue/`, `usage/by-session/`, `usage/by-workflow/`, `usage/by-pr/` directories created on first write
- [TASK-2-3] Implement incremental update logic — Acceptance: Existing aggregates are loaded, updated with new checkpoint data, and saved atomically; handles concurrent access with retry logic

**Dependencies**: Phase 1

**Exit criteria**: Can programmatically read and write usage aggregates to the orphaned branch

### Phase 3: Checkpoint Handler Integration

**Goal**: Extend the checkpoint handler to update usage aggregates on each checkpoint store

**Tasks**:
- [TASK-3-1] Create usage update helper in checkpoint handler — Acceptance: Helper function extracts usage data from checkpoint and calls appropriate update functions
- [TASK-3-2] Integrate usage update into `store_checkpoint()` — Acceptance: After storing checkpoint, usage aggregates for session, issue, and workflow are updated
- [TASK-3-3] Add error handling for usage update failures — Acceptance: Usage update failures are logged but do not block checkpoint storage; implements graceful degradation

**Dependencies**: Phase 2

**Exit criteria**: New checkpoints automatically update their associated usage aggregates

### Phase 4: PR Association and Backfill

**Goal**: Implement PR number tracking and backfill logic

**Tasks**:
- [TASK-4-1] Add PR number capture to checkpoint creation — Acceptance: `EGG_PR_NUMBER` environment variable is read and stored in checkpoint if available
- [TASK-4-2] Implement PR usage backfill function — Acceptance: Function updates all checkpoints for an issue's branch with PR number and creates PR usage aggregate
- [TASK-4-3] Create backfill trigger hook — Acceptance: When PR is created, backfill is triggered to associate existing checkpoints; callable from gateway or CLI

**Dependencies**: Phase 3

**Exit criteria**: PR numbers are captured on checkpoints when available; backfill works for existing checkpoints

### Phase 5: Query API and CLI

**Goal**: Provide programmatic and command-line access to usage data

**Tasks**:
- [TASK-5-1] Add usage query functions to loader — Acceptance: Functions to query usage by issue, session, workflow, PR with filtering options
- [TASK-5-2] Create `egg-usage` CLI command — Acceptance: CLI supports `--issue`, `--session`, `--workflow`, `--pr` flags; outputs JSON or human-readable format
- [TASK-5-3] Add usage summary to existing checkpoint CLI — Acceptance: `egg-checkpoint show` includes token usage breakdown

**Dependencies**: Phase 4

**Exit criteria**: Usage data is queryable via Python API and CLI

### Phase 6: Testing and Cleanup

**Goal**: Comprehensive testing and cleanup of checkpoint branch

**Tasks**:
- [TASK-6-1] Write unit tests for usage models and loader — Acceptance: >90% coverage on new modules; edge cases tested
- [TASK-6-2] Write integration tests for checkpoint→usage flow — Acceptance: End-to-end test verifies checkpoint storage updates usage aggregates correctly
- [TASK-6-3] Clean up existing checkpoint branch — Acceptance: Per owner's direction, delete existing checkpoints in `egg/checkpoints/v1` and start fresh

**Dependencies**: Phases 1-5

**Exit criteria**: All tests pass; existing checkpoint data cleaned up

## Test Strategy

- **Unit tests**:
  - Pydantic model validation (valid/invalid data)
  - Usage loader atomic operations (concurrent writes, missing files)
  - Aggregation logic (adding checkpoints, cost calculations)
  - PR backfill logic (issue→PR mapping)

- **Integration tests**:
  - End-to-end checkpoint storage → usage update flow
  - PR creation triggers backfill correctly
  - CLI commands produce expected output

- **Manual testing**:
  - Verify usage files appear in orphaned branch after checkpoint storage
  - Confirm `egg-usage --issue 519` returns expected data
  - Test PR backfill by creating a PR and verifying checkpoint updates

## Rollback Plan

1. **Feature flag**: Usage tracking can be disabled via `USAGE_TRACKING_ENABLED=false` environment variable (similar to existing `CHECKPOINT_ENABLED`)

2. **No schema migration**: Existing checkpoint format is preserved; new `pr_number` field is optional with null default

3. **Independent storage**: Usage aggregates are stored in separate `usage/` directory; can be deleted without affecting checkpoint data

4. **Rollback steps**:
   ```bash
   # Disable usage tracking
   export USAGE_TRACKING_ENABLED=false

   # If needed, remove usage data from checkpoint branch
   git checkout egg/checkpoints/v1
   rm -rf usage/
   git commit -m "Rollback: remove usage tracking data"
   git push origin egg/checkpoints/v1
   ```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Concurrent update conflicts on usage files | Medium | Low | Implement optimistic locking with retry (read version, check before write, retry on conflict) |
| Usage update failure blocks checkpoint storage | Low | High | Catch and log errors; usage update runs in try/except, never blocks checkpoint |
| Large usage files cause slow updates | Low | Medium | Limit checkpoint references stored in aggregates; use summary data not full checkpoint IDs |
| PR backfill misses checkpoints | Low | Medium | Query by branch as well as issue number; log warnings for unassociated checkpoints |

## Migration Notes

- **No database migrations required**: All data stored in Git orphaned branch
- **Backward compatibility**: Existing checkpoints continue to work; new fields are optional
- **Cleanup**: Per owner direction, existing checkpoint branch data will be deleted and system starts fresh

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add token usage tracking across sessions and workflows"
  description: |
    Implements token usage tracking per session, job, workflow, issue, and PR.
    Uses pre-computed JSON aggregate files stored in the egg/checkpoints/v1
    orphaned branch alongside existing checkpoint data. Includes CLI for
    querying usage and automatic backfill when PRs are created.

    Closes #519
phases:
  - id: 1
    name: Data Models and Schema
    goal: Define Pydantic models and JSON schema for token usage aggregates
    tasks:
      - id: TASK-1-1
        description: Create usage aggregate Pydantic models
        acceptance: Models defined with proper validation, type hints, and docstrings; unit tests pass
        files:
          - shared/egg_contracts/usage.py
      - id: TASK-1-2
        description: Create JSON schema for usage aggregates
        acceptance: Schema validates sample usage documents; consistent with checkpoint schema patterns
        files:
          - .egg/schemas/usage.schema.json
      - id: TASK-1-3
        description: Add pr_number field to Checkpoint model
        acceptance: Checkpoint and CheckpointSummary models include optional pr_number field; schema updated
        files:
          - shared/egg_contracts/checkpoints.py
          - .egg/schemas/checkpoint.schema.json
  - id: 2
    name: Usage Storage Infrastructure
    goal: Implement the storage layer for usage aggregates in the orphaned branch
    tasks:
      - id: TASK-2-1
        description: Create usage loader module with atomic read/write
        acceptance: load_*_usage() and save_*_usage() functions implemented with temp-file-rename pattern; handles missing files gracefully
        files:
          - shared/egg_contracts/usage_loader.py
      - id: TASK-2-2
        description: Add usage directory structure to checkpoint branch
        acceptance: usage/by-issue/, usage/by-session/, usage/by-workflow/, usage/by-pr/ directories created on first write
        files:
          - shared/egg_contracts/usage_loader.py
      - id: TASK-2-3
        description: Implement incremental update logic
        acceptance: Existing aggregates are loaded, updated with new checkpoint data, and saved atomically; handles concurrent access with retry logic
        files:
          - shared/egg_contracts/usage_loader.py
  - id: 3
    name: Checkpoint Handler Integration
    goal: Extend checkpoint handler to update usage aggregates on each checkpoint store
    tasks:
      - id: TASK-3-1
        description: Create usage update helper in checkpoint handler
        acceptance: Helper function extracts usage data from checkpoint and calls appropriate update functions
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-3-2
        description: Integrate usage update into store_checkpoint()
        acceptance: After storing checkpoint, usage aggregates for session, issue, and workflow are updated
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-3-3
        description: Add error handling for usage update failures
        acceptance: Usage update failures are logged but do not block checkpoint storage; implements graceful degradation
        files:
          - gateway/checkpoint_handler.py
  - id: 4
    name: PR Association and Backfill
    goal: Implement PR number tracking and backfill logic
    tasks:
      - id: TASK-4-1
        description: Add PR number capture to checkpoint creation
        acceptance: EGG_PR_NUMBER environment variable is read and stored in checkpoint if available
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-4-2
        description: Implement PR usage backfill function
        acceptance: Function updates all checkpoints for an issue's branch with PR number and creates PR usage aggregate
        files:
          - shared/egg_contracts/usage_loader.py
          - gateway/checkpoint_handler.py
      - id: TASK-4-3
        description: Create backfill trigger hook
        acceptance: When PR is created, backfill is triggered to associate existing checkpoints; callable from gateway or CLI
        files:
          - gateway/gateway.py
          - shared/egg_contracts/usage_cli.py
  - id: 5
    name: Query API and CLI
    goal: Provide programmatic and command-line access to usage data
    tasks:
      - id: TASK-5-1
        description: Add usage query functions to loader
        acceptance: Functions to query usage by issue, session, workflow, PR with filtering options
        files:
          - shared/egg_contracts/usage_loader.py
      - id: TASK-5-2
        description: Create egg-usage CLI command
        acceptance: CLI supports --issue, --session, --workflow, --pr flags; outputs JSON or human-readable format
        files:
          - shared/egg_contracts/usage_cli.py
      - id: TASK-5-3
        description: Add usage summary to existing checkpoint CLI
        acceptance: egg-checkpoint show includes token usage breakdown
        files:
          - shared/egg_contracts/checkpoint_cli.py
  - id: 6
    name: Testing and Cleanup
    goal: Comprehensive testing and cleanup of checkpoint branch
    tasks:
      - id: TASK-6-1
        description: Write unit tests for usage models and loader
        acceptance: >90% coverage on new modules; edge cases tested
        files:
          - tests/egg_contracts/test_usage.py
          - tests/egg_contracts/test_usage_loader.py
      - id: TASK-6-2
        description: Write integration tests for checkpoint to usage flow
        acceptance: End-to-end test verifies checkpoint storage updates usage aggregates correctly
        files:
          - tests/gateway/test_checkpoint_usage_integration.py
      - id: TASK-6-3
        description: Clean up existing checkpoint branch
        acceptance: Per owner's direction, delete existing checkpoints in egg/checkpoints/v1 and start fresh
        files: []
```

---

*Authored-by: egg*
