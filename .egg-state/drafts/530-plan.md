# Plan: Checkpoint v2 with Rich Querying

> Issue: #530 | Phase: plan

## Summary

This plan implements a v2 checkpoint system that captures every agent session regardless of push activity, enables rich querying across multiple dimensions (session, issue, PR, commit, agent type, phase), and provides clear semantics distinguishing commit-linked vs session-end checkpoints. The implementation follows the decisions from refinement: 30-second async buffer timeout, immediate switch to v2 (no dual-write), per-checkpoint index updates, no retention TTL, and increased transcript size limits.

## Implementation Phases

### Phase 1: Core Contracts & Models

**Goal**: Define v2 data models with new enums, checkpoint structure, and multi-dimensional index.

**Tasks**:
- [TASK-1-1] Add `TriggerType`, `SessionStatus`, and `AgentType` enums to checkpoints.py — Acceptance: Enums defined with all values from spec, unit tests pass
- [TASK-1-2] Add `CheckpointV2` model with optional `commit_sha`, required `trigger_type` and `session_id`, and new timestamp fields — Acceptance: Model validates correctly, supports both commit and session-end checkpoints
- [TASK-1-3] Add `CheckpointSummaryV2` model with all queryable fields — Acceptance: Summary captures all fields needed for index lookups
- [TASK-1-4] Add `CheckpointIndexV2` model with secondary indices (by_session, by_issue, by_pr, by_commit, by_agent_type, by_phase, by_trigger, by_status) — Acceptance: Index model supports O(1) lookups for all dimensions
- [TASK-1-5] Add unit tests for v2 models — Acceptance: Tests cover validation, serialization, and edge cases

**Dependencies**: None

**Exit criteria**: All v2 models defined and tested, `make test` passes for checkpoint tests

### Phase 2: Checkpoint Loader v2 Functions

**Goal**: Add v2 save/load functions with multi-index update logic.

**Tasks**:
- [TASK-2-1] Add `generate_checkpoint_id_v2()` function for session-end checkpoints (not commit-based) — Acceptance: Generates unique IDs without requiring commit_sha
- [TASK-2-2] Add `save_checkpoint_v2()` with atomic write support — Acceptance: Writes to v2 directory structure, atomic file operations
- [TASK-2-3] Add `load_checkpoint_v2()` and `load_checkpoint_index_v2()` functions — Acceptance: Correctly loads v2 checkpoints and index
- [TASK-2-4] Add `add_checkpoint_to_index_v2()` with multi-index updates — Acceptance: Updates all secondary indices atomically (by_session, by_issue, by_pr, etc.)
- [TASK-2-5] Add v2-specific lookup helpers (get_by_session, get_by_trigger, get_by_status) — Acceptance: O(1) lookups using secondary indices
- [TASK-2-6] Add unit tests for v2 loader functions — Acceptance: Tests cover save/load, index updates, and concurrent access patterns

**Dependencies**: Phase 1

**Exit criteria**: All v2 loader functions implemented and tested

### Phase 3: Session Metadata Enhancement

**Goal**: Store issue/PR numbers at session registration for reliable checkpoint linkage.

**Tasks**:
- [TASK-3-1] Add `issue_number` and `pr_number` optional fields to `Session` dataclass — Acceptance: Fields persisted to disk, backward compatible with existing sessions
- [TASK-3-2] Update `register_session()` to accept optional `issue_number` and `pr_number` parameters — Acceptance: Parameters stored in Session, passed to gateway API
- [TASK-3-3] Update `Session.to_dict_for_persistence()` and `from_persistence()` for new fields — Acceptance: New fields round-trip correctly through persistence
- [TASK-3-4] Add unit tests for session metadata — Acceptance: Tests cover new fields, backward compatibility

**Dependencies**: None (can be done in parallel with Phase 1-2)

**Exit criteria**: Session model extended, persistence tested

### Phase 4: Transcript Size Limit Increase

**Goal**: Increase transcript extraction size limits for session-end checkpoints capturing longer sessions.

**Tasks**:
- [TASK-4-1] Increase `max_content_length` from 10,000 to 25,000 characters — Acceptance: Longer messages preserved, existing tests pass
- [TASK-4-2] Increase `max_param_length` from 1,000 to 2,500 characters — Acceptance: Tool parameters less aggressively truncated
- [TASK-4-3] Increase `max_result_length` from 500 to 1,500 characters — Acceptance: Tool results more complete
- [TASK-4-4] Increase `MAX_TRANSCRIPT_SIZE` in checkpoint_handler.py from 1MB to 3MB — Acceptance: Larger transcripts accepted
- [TASK-4-5] Update constants documentation — Acceptance: Comments reflect new limits and rationale

**Dependencies**: None (can be done in parallel with Phase 1-3)

**Exit criteria**: Size limits increased, tests pass

### Phase 5: Session-End Checkpoint Capture

**Goal**: Implement checkpoint capture for session termination with async buffer preservation.

**Tasks**:
- [TASK-5-1] Add `capture_session_end_checkpoint()` function to checkpoint_handler.py — Acceptance: Captures checkpoint with `trigger_type=SESSION_END`, uses v2 schema
- [TASK-5-2] Add `store_checkpoint_v2()` method to `CheckpointHandler` for v2 branch (`egg/checkpoints/v2`) — Acceptance: Stores to v2 branch with proper index updates
- [TASK-5-3] Implement async capture with buffer preservation (30-second timeout) — Acceptance: Buffer not deleted until capture completes or timeout, non-blocking for API
- [TASK-5-4] Add `truncated_reason` field handling for crashed sessions — Acceptance: Transcript marked with `truncated_reason="container_crash"` when applicable
- [TASK-5-5] Update `capture_checkpoint()` to produce v2 checkpoints with `trigger_type=COMMIT` — Acceptance: Existing push-triggered checkpoints use v2 format

**Dependencies**: Phases 1, 2, 3

**Exit criteria**: Session-end checkpoint capture implemented with proper async/timeout behavior

### Phase 6: Integration with Session Deletion Paths

**Goal**: Hook checkpoint capture into all session termination paths.

**Tasks**:
- [TASK-6-1] Update `delete_session()` to capture checkpoint with `status=COMPLETED` before buffer cleanup — Acceptance: Graceful deletions produce checkpoints
- [TASK-6-2] Update `delete_session_by_container()` to capture checkpoint with `status=COMPLETED` — Acceptance: Container-based deletions produce checkpoints
- [TASK-6-3] Update `prune_expired_sessions()` to capture checkpoint with `status=EXPIRED` for each expired session — Acceptance: TTL expirations produce checkpoints
- [TASK-6-4] Add session lookup before worktree cleanup in `cleanup_orphaned_worktrees()` — Acceptance: Session metadata available for orphan checkpoints
- [TASK-6-5] Update `cleanup_orphaned_worktrees()` to capture checkpoint with `status=FAILED` — Acceptance: Crashed containers produce checkpoints with `truncated_reason="container_crash"`
- [TASK-6-6] Refactor `_cleanup_transcript_buffer()` to await checkpoint capture completion — Acceptance: Buffer preserved until capture finishes or 30s timeout

**Dependencies**: Phases 3, 5

**Exit criteria**: All deletion paths capture checkpoints, buffer lifecycle correct

### Phase 7: Update Push-Triggered Checkpoints

**Goal**: Migrate push-triggered checkpoints to v2 format and branch.

**Tasks**:
- [TASK-7-1] Update `CHECKPOINT_BRANCH` constant to `egg/checkpoints/v2` — Acceptance: New checkpoints written to v2 branch
- [TASK-7-2] Update `capture_and_store_checkpoint()` to use v2 capture and storage — Acceptance: Push checkpoints use v2 models
- [TASK-7-3] Update `capture_and_store_checkpoints_for_push()` to produce v2 checkpoints — Acceptance: Multi-commit pushes produce v2 checkpoints
- [TASK-7-4] Remove v1 imports and references (clean break) — Acceptance: No v1 code paths remain active

**Dependencies**: Phases 1, 2, 5

**Exit criteria**: All push-triggered checkpoints use v2 format on v2 branch

### Phase 8: Integration Testing & Documentation

**Goal**: Ensure end-to-end functionality and document the v2 system.

**Tasks**:
- [TASK-8-1] Add integration tests for session-end checkpoint flow — Acceptance: Test covers graceful deletion, TTL expiration, and crash scenarios
- [TASK-8-2] Add integration tests for commit checkpoint flow with v2 — Acceptance: Push-triggered checkpoints work with v2
- [TASK-8-3] Add integration tests for multi-dimensional index queries — Acceptance: Queries by session, issue, PR, status all work
- [TASK-8-4] Update checkpoint docstrings and module-level documentation — Acceptance: Code is well-documented for future maintainers
- [TASK-8-5] Verify no regressions in existing gateway functionality — Acceptance: Full test suite passes, manual smoke test succeeds

**Dependencies**: All previous phases

**Exit criteria**: All tests pass, documentation complete

## Test Strategy

- **Unit tests**:
  - v2 model validation (CheckpointV2, CheckpointSummaryV2, CheckpointIndexV2)
  - Enum values and serialization
  - Loader functions (save/load, index updates)
  - Session metadata persistence with new fields

- **Integration tests**:
  - Session deletion triggers checkpoint capture
  - TTL expiration triggers checkpoint capture
  - Orphan cleanup triggers checkpoint capture with FAILED status
  - Push triggers v2 checkpoint capture
  - Multi-index queries return correct results
  - Async capture with timeout works correctly
  - Buffer preservation during async capture

- **Manual testing**:
  - Start a container, make changes, gracefully delete → verify checkpoint
  - Let a session expire via TTL → verify checkpoint with EXPIRED status
  - Simulate container crash → verify checkpoint with FAILED status
  - Push commits → verify v2 checkpoint created
  - Query checkpoints by various dimensions (issue, session, status)

## Rollback Plan

1. **Immediate rollback**: Revert the commit on `egg/issue-530` branch
2. **Branch-level isolation**: v2 checkpoints are on a separate branch (`egg/checkpoints/v2`), v1 remains untouched
3. **Feature flag fallback**: If needed, add `CHECKPOINT_V2_ENABLED` env var to disable v2 capture and fall back to v1-only behavior (would require minimal additional code)
4. **Data recovery**: Since v1 branch is unchanged, all existing checkpoint data remains accessible

If rollback is needed:
```bash
# Revert the feature branch
git checkout main
git branch -D egg/issue-530  # or revert specific commits

# v1 checkpoints continue to work unchanged
# v2 branch can be deleted if desired (data loss for v2 checkpoints only)
git push origin --delete egg/checkpoints/v2
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Session metadata not available for orphan cleanup | Medium | Medium | Pre-populate metadata at registration, graceful degradation to null linkage |
| Buffer deleted before capture completes | Low | High | Implement async capture with explicit buffer preservation and 30s timeout |
| Index file corruption from concurrent writes | Low | Medium | Atomic write pattern already in place, single-threaded index updates |
| Increased checkpoint storage costs | Medium | Low | No TTL requested; can add pruning later if needed |
| Breaking change impacts existing tooling | Medium | Medium | Separate v2 branch, no v1 modifications, clear documentation |
| Gateway crash during async capture loses checkpoint | Low | Medium | Fire timeout, log warning; checkpoint loss is acceptable for rare edge case |

## Migration Notes

- **No database migrations**: Checkpoints are stored as JSON files in git
- **No config changes**: Uses existing environment variables, new optional params at registration
- **Breaking change**: v2 checkpoints written to `egg/checkpoints/v2` branch, not backward compatible with v1 readers
- **Parallel operation**: v1 checkpoints remain in place on `egg/checkpoints/v1` branch, but no new checkpoints written there
- **Session persistence**: New fields (`issue_number`, `pr_number`) are optional, existing sessions load correctly

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
    agent type, phase, and status. Resolves #530.
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
        acceptance: Model validates correctly, supports both commit and session-end checkpoints
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-3
        description: Add CheckpointSummaryV2 model with all queryable fields
        acceptance: Summary captures all fields needed for index lookups
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
          - shared/egg_contracts/tests/test_checkpoints.py
  - id: 2
    name: Checkpoint Loader v2 Functions
    goal: Add v2 save/load functions with multi-index update logic
    tasks:
      - id: TASK-2-1
        description: Add generate_checkpoint_id_v2() function for session-end checkpoints
        acceptance: Generates unique IDs without requiring commit_sha
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-2
        description: Add save_checkpoint_v2() with atomic write support
        acceptance: Writes to v2 directory structure, atomic file operations
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-3
        description: Add load_checkpoint_v2() and load_checkpoint_index_v2() functions
        acceptance: Correctly loads v2 checkpoints and index
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-4
        description: Add add_checkpoint_to_index_v2() with multi-index updates
        acceptance: Updates all secondary indices atomically
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-5
        description: Add v2-specific lookup helpers (get_by_session, get_by_trigger, get_by_status)
        acceptance: O(1) lookups using secondary indices
        files:
          - shared/egg_contracts/checkpoint_loader.py
      - id: TASK-2-6
        description: Add unit tests for v2 loader functions
        acceptance: Tests cover save/load, index updates, and concurrent access patterns
        files:
          - shared/egg_contracts/tests/test_checkpoint_loader.py
  - id: 3
    name: Session Metadata Enhancement
    goal: Store issue/PR numbers at session registration for reliable checkpoint linkage
    tasks:
      - id: TASK-3-1
        description: Add issue_number and pr_number optional fields to Session dataclass
        acceptance: Fields persisted to disk, backward compatible with existing sessions
        files:
          - gateway/session_manager.py
      - id: TASK-3-2
        description: Update register_session() to accept optional issue_number and pr_number
        acceptance: Parameters stored in Session, passed to gateway API
        files:
          - gateway/session_manager.py
      - id: TASK-3-3
        description: Update Session persistence methods for new fields
        acceptance: New fields round-trip correctly through persistence
        files:
          - gateway/session_manager.py
      - id: TASK-3-4
        description: Add unit tests for session metadata
        acceptance: Tests cover new fields, backward compatibility
        files:
          - gateway/tests/test_session_manager.py
  - id: 4
    name: Transcript Size Limit Increase
    goal: Increase transcript extraction size limits for session-end checkpoints
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
        acceptance: Stores to egg/checkpoints/v2 branch with proper index updates
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
    name: Update Push-Triggered Checkpoints
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
        description: Remove v1 imports and references
        acceptance: No v1 code paths remain active
        files:
          - gateway/checkpoint_handler.py
  - id: 8
    name: Integration Testing & Documentation
    goal: Ensure end-to-end functionality and document the v2 system
    tasks:
      - id: TASK-8-1
        description: Add integration tests for session-end checkpoint flow
        acceptance: Test covers graceful deletion, TTL expiration, and crash scenarios
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
