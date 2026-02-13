# Plan: Checkpoint v2 with Rich Querying

> Issue: #530 | Phase: plan

## Summary

This plan implements a v2 checkpoint system that captures every agent session regardless of push activity, enables rich querying across multiple dimensions (session, issue, PR, commit, agent type, phase, status), and provides clear semantics distinguishing commit-linked vs session-end checkpoints. The implementation follows decisions from refinement: 30-second async buffer timeout, immediate switch to v2 (no dual-write), per-checkpoint index updates, no retention TTL, and increased transcript size limits. V1 checkpoints remain untouched in `egg/checkpoints/v1`; all new checkpoints go to `egg/checkpoints/v2`.

## Implementation Phases

### Phase 1: Core Contracts & Models

**Goal**: Define v2 data models with new enums, checkpoint structure, and multi-dimensional index.

**Tasks**:
- [TASK-1-1] Add `TriggerType`, `SessionStatus`, and `AgentType` enums to `checkpoints.py` — Acceptance: Enums defined with all values from the spec (`COMMIT`/`SESSION_END`, `COMPLETED`/`EXPIRED`/`FAILED`, `CODER`/`TESTER`/`DOCUMENTER`/`INTEGRATOR`/`REVIEWER`/`UNKNOWN`), unit tests pass
- [TASK-1-2] Add `CheckpointV2` model with optional `commit_sha`, required `trigger_type` and `session_id`, `session_status`, `agent_type`, `session_started_at`/`session_ended_at` timestamps — Acceptance: Model validates correctly for both commit-triggered and session-end checkpoints; `commit_sha` is optional; `schemaVersion` defaults to `"2.0"`
- [TASK-1-3] Add `CheckpointSummaryV2` model with all queryable fields — Acceptance: `from_checkpoint()` class method creates summary from `CheckpointV2`; includes `trigger_type`, `session_status`, `agent_type`, `pipeline_phase`
- [TASK-1-4] Add `CheckpointIndexV2` model with secondary indices (`by_session`, `by_issue`, `by_pr`, `by_commit`, `by_agent_type`, `by_phase`, `by_trigger`, `by_status`) — Acceptance: Index supports O(1) lookups for all dimensions; secondary indices are `dict[str, list[str]]` (or `dict[str, str]` for `by_commit`)
- [TASK-1-5] Add unit tests for all v2 models — Acceptance: Tests cover validation, serialization round-trip, edge cases (missing optional fields, invalid enum values), and `from_checkpoint()` conversion

**Dependencies**: None

**Exit criteria**: All v2 models defined and tested; `make test` passes for checkpoint tests

### Phase 2: Checkpoint Loader v2 Functions

**Goal**: Add v2 save/load functions with multi-index update logic to `checkpoint_loader.py`.

**Tasks**:
- [TASK-2-1] Add `generate_checkpoint_id_v2()` for session-end checkpoints — Acceptance: Generates deterministic IDs from `session_id` + timestamp (no `commit_sha` required); format matches `ckpt-{hex}`
- [TASK-2-2] Add `save_checkpoint_v2()` and `load_checkpoint_v2()` with atomic write — Acceptance: Atomic temp-file-rename pattern; directory sharding by ID prefix; handles `CheckpointV2` model
- [TASK-2-3] Add `load_checkpoint_index_v2()` and `save_checkpoint_index_v2()` — Acceptance: Loads/saves `CheckpointIndexV2`; returns empty index if file missing
- [TASK-2-4] Add `add_checkpoint_to_index_v2()` with multi-dimensional index updates — Acceptance: Updates primary list and all secondary indices atomically; deduplicates by checkpoint ID
- [TASK-2-5] Add v2 lookup helpers (`get_by_session`, `get_by_trigger`, `get_by_status`, `get_by_pr`) — Acceptance: O(1) lookups using secondary index dicts
- [TASK-2-6] Add unit tests for v2 loader functions — Acceptance: Tests cover save/load round-trip, index updates, deduplication, lookup helpers, and missing-file handling

**Dependencies**: Phase 1

**Exit criteria**: All v2 loader functions implemented and tested

### Phase 3: Session Metadata Enhancement

**Goal**: Store issue/PR numbers on the `Session` dataclass for reliable checkpoint linkage at session-end time.

**Tasks**:
- [TASK-3-1] Add optional `issue_number: int | None` and `pr_number: int | None` fields to `Session` dataclass — Acceptance: Fields default to `None`; existing session loading succeeds (backward compatible)
- [TASK-3-2] Update `register_session()` to accept optional `issue_number` and `pr_number` parameters — Acceptance: Parameters stored in Session object
- [TASK-3-3] Update `to_dict_for_persistence()` and `from_persistence()` for new fields — Acceptance: New fields round-trip correctly; missing fields in persisted data default to `None`
- [TASK-3-4] Add unit tests for session metadata — Acceptance: Tests cover new fields, backward compatibility with old persisted sessions

**Dependencies**: None (parallel with Phases 1-2)

**Exit criteria**: Session model extended with issue/PR fields, persistence tested

### Phase 4: Transcript Size Limit Increase

**Goal**: Increase transcript extraction size limits so session-end checkpoints capture more complete context from longer sessions.

**Tasks**:
- [TASK-4-1] Increase `max_content_length` from 10,000 to 25,000 characters in `transcript_extractor.py` — Acceptance: Longer messages preserved, existing tests pass
- [TASK-4-2] Increase `max_param_length` from 1,000 to 2,500 characters — Acceptance: Tool parameters less aggressively truncated
- [TASK-4-3] Increase `max_result_length` from 500 to 1,500 characters — Acceptance: Tool results more complete
- [TASK-4-4] Increase `MAX_TRANSCRIPT_SIZE` in `checkpoint_handler.py` from 1MB to 3MB — Acceptance: Larger transcripts accepted without triggering size truncation
- [TASK-4-5] Update constants documentation/comments — Acceptance: Comments reflect new limits and rationale

**Dependencies**: None (parallel with Phases 1-3)

**Exit criteria**: Size limits increased, all existing tests pass

### Phase 5: Session-End Checkpoint Capture

**Goal**: Implement the core checkpoint capture function for session termination, with async buffer preservation.

**Tasks**:
- [TASK-5-1] Add `capture_session_end_checkpoint()` function to `checkpoint_handler.py` — Acceptance: Captures checkpoint with `trigger_type=SESSION_END` and appropriate `session_status`; extracts transcript from proxy buffer; uses v2 schema; handles missing buffer gracefully (creates minimal checkpoint)
- [TASK-5-2] Add `store_checkpoint_v2()` method to `CheckpointHandler` for the `egg/checkpoints/v2` branch — Acceptance: Stores checkpoint to v2 branch with proper v2 index updates via `add_checkpoint_to_index_v2()`
- [TASK-5-3] Implement async capture with buffer preservation (30-second timeout) — Acceptance: Returns an `Event`/callback; buffer cleanup blocked until capture signals completion or 30s timeout; non-blocking for the calling API
- [TASK-5-4] Handle `truncated_reason` for crashed sessions — Acceptance: Transcript marked with `truncated=True` and `truncation_reason="container_crash"` when `session_status=FAILED`
- [TASK-5-5] Update `capture_checkpoint()` to produce v2 checkpoints with `trigger_type=COMMIT` — Acceptance: Existing push-triggered checkpoints use `CheckpointV2` model with `trigger_type=COMMIT`; backward compatible behavior

**Dependencies**: Phases 1, 2, 3

**Exit criteria**: Session-end checkpoint capture implemented with proper async/timeout behavior

### Phase 6: Integration with Session Deletion Paths

**Goal**: Hook checkpoint capture into all session termination paths so no session is lost.

**Tasks**:
- [TASK-6-1] Update `delete_session()` to capture checkpoint with `status=COMPLETED` before buffer cleanup — Acceptance: Graceful deletions produce session-end checkpoints
- [TASK-6-2] Update `delete_session_by_container()` to capture checkpoint with `status=COMPLETED` — Acceptance: Container-based deletions produce session-end checkpoints
- [TASK-6-3] Update `prune_expired_sessions()` to capture checkpoint with `status=EXPIRED` for each expired session — Acceptance: TTL expirations produce session-end checkpoints
- [TASK-6-4] Add session lookup before worktree cleanup in `cleanup_orphaned_worktrees()` — Acceptance: Session metadata (if still available) retrieved before worktree removal
- [TASK-6-5] Update `cleanup_orphaned_worktrees()` to capture checkpoint with `status=FAILED` — Acceptance: Crashed containers produce checkpoints with `truncation_reason="container_crash"`
- [TASK-6-6] Refactor `_cleanup_transcript_buffer()` to await checkpoint capture completion — Acceptance: Buffer preserved until capture finishes or 30s timeout; on timeout, buffer cleaned up with warning logged

**Dependencies**: Phases 3, 5

**Exit criteria**: All deletion paths capture checkpoints; buffer lifecycle prevents data loss

### Phase 7: Update Push-Triggered Checkpoints to v2

**Goal**: Migrate push-triggered checkpoints to v2 format and branch — clean break from v1.

**Tasks**:
- [TASK-7-1] Update `CHECKPOINT_BRANCH` constant to `egg/checkpoints/v2` — Acceptance: New checkpoints written to v2 branch
- [TASK-7-2] Update `capture_and_store_checkpoint()` to use v2 capture and storage — Acceptance: Push checkpoints use v2 models and v2 storage
- [TASK-7-3] Update `capture_and_store_checkpoints_for_push()` to produce v2 checkpoints — Acceptance: Multi-commit pushes produce v2 checkpoints with `trigger_type=COMMIT`
- [TASK-7-4] Remove v1 imports and active v1 code paths in checkpoint_handler — Acceptance: No v1 code paths remain active; v1 models/loaders kept in shared library for potential reads but not used by the handler

**Dependencies**: Phases 1, 2, 5

**Exit criteria**: All push-triggered checkpoints use v2 format on v2 branch; v1 code paths inactive

### Phase 8: Integration Testing & Cleanup

**Goal**: End-to-end tests and documentation for the complete v2 system.

**Tasks**:
- [TASK-8-1] Add integration tests for session-end checkpoint flow — Acceptance: Tests cover graceful deletion (COMPLETED), TTL expiration (EXPIRED), and crash scenarios (FAILED); verify checkpoint content and index entries
- [TASK-8-2] Add integration tests for commit checkpoint flow with v2 — Acceptance: Push-triggered checkpoints work with v2 models and v2 branch
- [TASK-8-3] Add integration tests for multi-dimensional index queries — Acceptance: Queries by session, issue, PR, commit, status, agent_type, trigger_type all return correct results
- [TASK-8-4] Update checkpoint docstrings and module-level documentation — Acceptance: Module docstrings explain v2 architecture; key functions documented
- [TASK-8-5] Verify no regressions in existing gateway functionality — Acceptance: Full test suite passes (`make test`)

**Dependencies**: All previous phases

**Exit criteria**: All tests pass, documentation complete, `make test` and `make lint` pass

## Test Strategy

- **Unit tests**:
  - v2 model validation (`CheckpointV2`, `CheckpointSummaryV2`, `CheckpointIndexV2`)
  - All enum values and serialization round-trips
  - Loader functions: save/load, index updates, deduplication, lookups
  - Session metadata persistence with new `issue_number`/`pr_number` fields
  - Backward compatibility with existing persisted sessions

- **Integration tests**:
  - Session deletion → checkpoint capture (graceful, TTL, crash)
  - Push → v2 checkpoint capture
  - Multi-commit push → multiple v2 checkpoints
  - Async capture with timeout behavior
  - Buffer preservation during async capture
  - Multi-dimensional index queries across multiple checkpoints

- **Manual testing**:
  - Start a container, make changes, gracefully delete → verify checkpoint on v2 branch
  - Let a session expire via TTL → verify checkpoint with `EXPIRED` status
  - Simulate container crash → verify checkpoint with `FAILED` status
  - Push commits → verify v2 checkpoint created on v2 branch
  - Query checkpoints by various dimensions

## Rollback Plan

1. **Branch-level isolation**: v2 checkpoints are on `egg/checkpoints/v2`; v1 remains completely untouched on `egg/checkpoints/v1`
2. **Immediate rollback**: Revert the feature branch commits on `main`
3. **Feature flag fallback**: If partial rollback needed, add `CHECKPOINT_V2_ENABLED=false` env var to disable v2 capture and fall back to v1 behavior (requires minimal additional code)
4. **Data recovery**: v1 branch unchanged, all existing checkpoint data accessible

```bash
# If rollback needed:
# 1. Revert the feature branch on main
# 2. v1 checkpoints continue to work unchanged
# 3. Optionally delete v2 branch (v2 data loss only):
git push origin --delete egg/checkpoints/v2
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Session metadata unavailable for orphan cleanup | Medium | Medium | Hybrid approach: use Session metadata if available, graceful degradation to null linkage for orphans |
| Buffer deleted before async capture completes | Low | High | Explicit buffer preservation with 30s timeout before cleanup proceeds |
| Index file corruption from concurrent writes | Low | Medium | Atomic write pattern (temp + rename) already in place; index updates single-threaded per checkpoint |
| Increased checkpoint volume | Medium | Low | No TTL requested; can add pruning later if volume becomes a problem |
| Breaking change impacts existing checkpoint consumers | Medium | Medium | Separate v2 branch; v1 data untouched; v1 models/loaders remain in shared library |
| Gateway crash during async capture loses checkpoint | Low | Medium | Acceptable trade-off; checkpoint loss on crash is rare and non-critical |
| Deadlock from checkpoint capture within RLock-held deletion paths | Low | High | Use `RLock` (already in use) and ensure checkpoint capture does not acquire the same lock; async handoff avoids lock contention |

## Migration Notes

- **No database migrations**: All data stored as JSON in git orphan branches
- **No config changes**: Uses existing environment variables; new optional params at session registration
- **Breaking change**: v2 checkpoints on `egg/checkpoints/v2` branch, not backward compatible with v1 readers
- **Parallel operation**: v1 checkpoints remain on `egg/checkpoints/v1` branch; no new data written there
- **Session persistence**: New `issue_number`/`pr_number` fields are optional; existing serialized sessions load correctly (missing fields default to `None`)

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Checkpoint v2: capture all sessions with rich querying"
  description: |
    Implements a v2 checkpoint system that captures every agent session regardless
    of push activity. Adds session-end checkpoints (COMPLETED, EXPIRED, FAILED)
    with multi-dimensional index for querying by session, issue, PR, commit,
    agent type, phase, and status. Increases transcript size limits. Stores all
    new checkpoints on egg/checkpoints/v2 branch; v1 remains untouched.

    Closes #530
phases:
  - id: 1
    name: Core Contracts & Models
    goal: Define v2 data models with new enums, checkpoint structure, and multi-dimensional index
    tasks:
      - id: TASK-1-1
        description: Add TriggerType, SessionStatus, and AgentType enums to checkpoints.py
        acceptance: Enums defined with all values from spec, unit tests pass
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-2
        description: Add CheckpointV2 model with optional commit_sha, required trigger_type and session_id
        acceptance: Model validates correctly for both commit and session-end checkpoints
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-3
        description: Add CheckpointSummaryV2 model with all queryable fields
        acceptance: Summary captures all fields needed for index lookups, from_checkpoint() works
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-4
        description: Add CheckpointIndexV2 model with secondary indices
        acceptance: Index model supports O(1) lookups for all dimensions
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-5
        description: Add unit tests for v2 models
        acceptance: Tests cover validation, serialization, and edge cases
        files:
          - tests/shared/egg_contracts/test_checkpoints.py
  - id: 2
    name: Checkpoint Loader v2 Functions
    goal: Add v2 save/load functions with multi-index update logic
    tasks:
      - id: TASK-2-1
        description: Add generate_checkpoint_id_v2() for session-end checkpoints
        acceptance: Generates deterministic IDs from session_id + timestamp
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-2
        description: Add save_checkpoint_v2() and load_checkpoint_v2() with atomic write
        acceptance: Atomic temp-file-rename pattern, directory sharding by prefix
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-3
        description: Add load_checkpoint_index_v2() and save_checkpoint_index_v2()
        acceptance: Loads/saves CheckpointIndexV2, returns empty index if missing
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-4
        description: Add add_checkpoint_to_index_v2() with multi-dimensional index updates
        acceptance: Updates primary list and all secondary indices atomically
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-5
        description: Add v2 lookup helpers (get_by_session, get_by_trigger, get_by_status, get_by_pr)
        acceptance: O(1) lookups using secondary index dicts
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-6
        description: Add unit tests for v2 loader functions
        acceptance: Tests cover save/load, index updates, deduplication, lookups
        files:
          - tests/shared/egg_contracts/test_checkpoint_loader.py
  - id: 3
    name: Session Metadata Enhancement
    goal: Store issue/PR numbers on Session for reliable checkpoint linkage
    tasks:
      - id: TASK-3-1
        description: Add issue_number and pr_number optional fields to Session dataclass
        acceptance: Fields default to None, backward compatible with existing sessions
        files:
          - gateway/session_manager.py
      - id: TASK-3-2
        description: Update register_session() to accept issue_number and pr_number
        acceptance: Parameters stored in Session object
        files:
          - gateway/session_manager.py
      - id: TASK-3-3
        description: Update to_dict_for_persistence() and from_persistence() for new fields
        acceptance: New fields round-trip correctly, missing fields default to None
        files:
          - gateway/session_manager.py
      - id: TASK-3-4
        description: Add unit tests for session metadata
        acceptance: Tests cover new fields, backward compatibility
        files:
          - gateway/tests/test_session_manager.py
  - id: 4
    name: Transcript Size Limit Increase
    goal: Increase transcript extraction size limits for longer session-end checkpoints
    tasks:
      - id: TASK-4-1
        description: Increase max_content_length from 10,000 to 25,000 characters
        acceptance: Longer messages preserved, existing tests pass
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-4-2
        description: Increase max_param_length from 1,000 to 2,500 characters
        acceptance: Tool parameters less aggressively truncated
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-4-3
        description: Increase max_result_length from 500 to 1,500 characters
        acceptance: Tool results more complete
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-4-4
        description: Increase MAX_TRANSCRIPT_SIZE from 1MB to 3MB
        acceptance: Larger transcripts accepted
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-4-5
        description: Update constants documentation
        acceptance: Comments reflect new limits and rationale
        files:
          - shared/egg_contracts/transcript_extractor.py
          - gateway/checkpoint_handler.py
  - id: 5
    name: Session-End Checkpoint Capture
    goal: Implement checkpoint capture for session termination with async buffer preservation
    tasks:
      - id: TASK-5-1
        description: Add capture_session_end_checkpoint() function
        acceptance: Captures checkpoint with trigger_type=SESSION_END, uses v2 schema
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-5-2
        description: Add store_checkpoint_v2() method for v2 branch
        acceptance: Stores to egg/checkpoints/v2 branch with proper v2 index updates
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-5-3
        description: Implement async capture with buffer preservation (30s timeout)
        acceptance: Buffer not deleted until capture completes or timeout
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-5-4
        description: Add truncated_reason field handling for crashed sessions
        acceptance: Transcript marked with truncated_reason when applicable
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-5-5
        description: Update capture_checkpoint() to produce v2 checkpoints
        acceptance: Existing push-triggered checkpoints use v2 format
        files:
          - gateway/checkpoint_handler.py
  - id: 6
    name: Integration with Session Deletion Paths
    goal: Hook checkpoint capture into all session termination paths
    tasks:
      - id: TASK-6-1
        description: Update delete_session() to capture checkpoint with status=COMPLETED
        acceptance: Graceful deletions produce checkpoints
        files:
          - gateway/session_manager.py
      - id: TASK-6-2
        description: Update delete_session_by_container() to capture checkpoint
        acceptance: Container-based deletions produce checkpoints
        files:
          - gateway/session_manager.py
      - id: TASK-6-3
        description: Update prune_expired_sessions() to capture checkpoints with status=EXPIRED
        acceptance: TTL expirations produce checkpoints
        files:
          - gateway/session_manager.py
      - id: TASK-6-4
        description: Add session lookup before worktree cleanup
        acceptance: Session metadata available for orphan checkpoints
        files:
          - gateway/worktree_manager.py
      - id: TASK-6-5
        description: Update cleanup_orphaned_worktrees() to capture checkpoint with status=FAILED
        acceptance: Crashed containers produce checkpoints
        files:
          - gateway/worktree_manager.py
      - id: TASK-6-6
        description: Refactor _cleanup_transcript_buffer() to await checkpoint completion
        acceptance: Buffer preserved until capture finishes or 30s timeout
        files:
          - gateway/session_manager.py
  - id: 7
    name: Update Push-Triggered Checkpoints to v2
    goal: Migrate push-triggered checkpoints to v2 format and branch
    tasks:
      - id: TASK-7-1
        description: Update CHECKPOINT_BRANCH constant to egg/checkpoints/v2
        acceptance: New checkpoints written to v2 branch
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-7-2
        description: Update capture_and_store_checkpoint() to use v2 capture and storage
        acceptance: Push checkpoints use v2 models
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-7-3
        description: Update capture_and_store_checkpoints_for_push() to produce v2 checkpoints
        acceptance: Multi-commit pushes produce v2 checkpoints
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-7-4
        description: Remove v1 imports and active v1 code paths
        acceptance: No v1 code paths remain active in handler
        files:
          - gateway/checkpoint_handler.py
  - id: 8
    name: Integration Testing & Cleanup
    goal: End-to-end tests and documentation for the complete v2 system
    tasks:
      - id: TASK-8-1
        description: Add integration tests for session-end checkpoint flow
        acceptance: Tests cover graceful deletion, TTL expiration, and crash scenarios
        files:
          - gateway/tests/test_checkpoint_handler.py
      - id: TASK-8-2
        description: Add integration tests for commit checkpoint flow with v2
        acceptance: Push-triggered checkpoints work with v2
        files:
          - gateway/tests/test_checkpoint_handler.py
      - id: TASK-8-3
        description: Add integration tests for multi-dimensional index queries
        acceptance: Queries by session, issue, PR, status all work
        files:
          - gateway/tests/test_checkpoint_handler.py
      - id: TASK-8-4
        description: Update checkpoint docstrings and module-level documentation
        acceptance: Code is well-documented for future maintainers
        files:
          - shared/egg_contracts/checkpoints.py
          - gateway/checkpoint_handler.py
      - id: TASK-8-5
        description: Verify no regressions in existing gateway functionality
        acceptance: Full test suite passes, manual smoke test succeeds
        files: []
```

---

*Authored-by: egg*
