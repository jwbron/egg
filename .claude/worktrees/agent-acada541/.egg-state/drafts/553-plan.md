# Plan: Add Integration Tests for Worktree Management

> Issue: #553 | Phase: plan

## Summary

This plan adds integration tests for worktree management that exercise the full worktree lifecycle through realistic SDLC pipeline scenarios. Based on the analysis phase and human feedback from PR #569, we focus on testing scenarios that have caused production issues: empty worktrees, root-owned worktrees, and permission problems in orchestrator-managed pipelines.

The tests will use the existing `local_pipeline` test infrastructure with a mock sandbox, allowing us to verify worktree behavior end-to-end without requiring Claude API calls.

## Implementation Phases

### Phase 1: Test Infrastructure Setup

**Goal**: Create the test file and fixtures needed for worktree integration testing within the local_pipeline test suite.

**Tasks**:
- [TASK-1-1] Create `test_worktree_integration.py` in `integration_tests/local_pipeline/` — Acceptance: File exists with pytest boilerplate and imports from conftest
- [TASK-1-2] Add worktree verification helper functions — Acceptance: Helpers can check worktree validity (non-empty, correct ownership, writable)
- [TASK-1-3] Add mock sandbox verification for worktree mounts — Acceptance: Mock sandbox can report worktree mount status via exit code or output file

**Dependencies**: None (builds on existing local_pipeline infrastructure)

**Exit criteria**: Test file runs without errors (even if tests are empty/skipped)

### Phase 2: Worktree Lifecycle Tests

**Goal**: Test the full worktree create/use/delete lifecycle through the gateway API, integrated with pipeline execution.

**Tasks**:
- [TASK-2-1] Test worktree creation returns valid paths — Acceptance: Gateway `/api/v1/worktree/create` returns paths that exist and are non-empty
- [TASK-2-2] Test worktree has correct ownership (uid/gid) — Acceptance: Created worktree is owned by specified uid:gid, not root
- [TASK-2-3] Test worktree is writable by container — Acceptance: Mock sandbox can create files in the mounted worktree
- [TASK-2-4] Test worktree deletion cleans up properly — Acceptance: After delete, worktree path no longer exists

**Dependencies**: Phase 1

**Exit criteria**: All lifecycle tests pass with the local_pipeline stack

### Phase 3: SDLC Pipeline Worktree Tests

**Goal**: Test worktree behavior in realistic SDLC pipeline scenarios as requested by the human reviewer.

**Tasks**:
- [TASK-3-1] Test pipeline containers share worktrees — Acceptance: Multiple phase containers see each other's file changes
- [TASK-3-2] Test worktree survives phase transitions — Acceptance: Files created in refine phase exist in plan phase
- [TASK-3-3] Test worktree cleanup on pipeline completion — Acceptance: Worktrees are removed when pipeline reaches terminal state
- [TASK-3-4] Test worktree cleanup on pipeline failure — Acceptance: Worktrees are removed even when pipeline fails

**Dependencies**: Phase 2

**Exit criteria**: All SDLC pipeline tests pass

### Phase 4: Edge Cases and Regression Tests

**Goal**: Cover edge cases and specific bugs from PR #569 (empty worktrees, permission issues).

**Tasks**:
- [TASK-4-1] Test worktree not empty after creation — Acceptance: Worktree contains expected git files (.git file pointing to gitdir, at minimum one file from repo)
- [TASK-4-2] Test worktree not root-owned — Acceptance: `stat` on worktree shows uid/gid matching HOST_UID/HOST_GID, not 0:0
- [TASK-4-3] Test orphaned worktree cleanup on gateway restart — Acceptance: Orphaned worktrees from crashed containers are cleaned up
- [TASK-4-4] Test worktree with Docker pre-created .git directory — Acceptance: When Docker creates empty .git directory before worktree creation, worktree still works

**Dependencies**: Phase 2

**Exit criteria**: All edge case tests pass

## Test Strategy

- **Unit tests**: Existing unit tests in `gateway/tests/test_worktree_manager.py` remain unchanged; this plan adds integration tests only
- **Integration tests**: New tests in `integration_tests/local_pipeline/test_worktree_integration.py` that:
  - Use the existing `local_pipeline_stack` fixture for full gateway+orchestrator stack
  - Use the existing mock sandbox image for container execution without Claude
  - Verify actual filesystem state via gateway container exec or docker volume inspection
- **Manual testing**: Run tests locally with `PYTHONPATH=shared pytest integration_tests/local_pipeline/test_worktree_integration.py -v -m integration`

## Rollback Plan

1. If tests introduce flakiness, they can be marked with `@pytest.mark.skip` or `@pytest.mark.xfail` until fixed
2. Tests are additive only — no changes to existing code or tests
3. If infrastructure changes are needed (mock sandbox modifications), they can be reverted by restoring the original `phase-runner.sh`

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Tests flaky due to timing | Medium | Medium | Use explicit waits, poll for expected state with timeouts |
| Mock sandbox can't verify worktree state | Low | High | Add simple verification script to mock sandbox that writes status file |
| Docker socket access issues in CI | Low | Medium | Tests already run with Docker socket access in existing local_pipeline tests |
| Worktree volume mount path differences | Medium | Medium | Use gateway's `translate_to_host_path()` consistently; verify paths in both gateway and orchestrator contexts |

## Migration Notes

No migration required. This is purely additive test coverage.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add integration tests for worktree management"
  description: |
    Adds integration tests for worktree management in realistic SDLC pipeline
    scenarios. Tests cover the full worktree lifecycle (create, use, delete),
    permission handling, and edge cases from PR #569 (empty/root-owned worktrees).

    Closes #553
phases:
  - id: 1
    name: Test Infrastructure Setup
    goal: Create test file and fixtures for worktree integration testing
    tasks:
      - id: TASK-1-1
        description: Create test_worktree_integration.py with pytest boilerplate
        acceptance: File exists with imports and pytest markers
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-1-2
        description: Add worktree verification helper functions
        acceptance: Helpers can check worktree validity (non-empty, correct ownership, writable)
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-1-3
        description: Add mock sandbox verification for worktree mounts
        acceptance: Mock sandbox can report worktree mount status
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
  - id: 2
    name: Worktree Lifecycle Tests
    goal: Test full worktree create/use/delete lifecycle through gateway API
    tasks:
      - id: TASK-2-1
        description: Test worktree creation returns valid paths
        acceptance: Gateway endpoint returns paths that exist and are non-empty
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-2-2
        description: Test worktree has correct ownership (uid/gid)
        acceptance: Created worktree is owned by specified uid:gid, not root
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-2-3
        description: Test worktree is writable by container
        acceptance: Mock sandbox can create files in mounted worktree
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-2-4
        description: Test worktree deletion cleans up properly
        acceptance: After delete, worktree path no longer exists
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
  - id: 3
    name: SDLC Pipeline Worktree Tests
    goal: Test worktree behavior in realistic SDLC pipeline scenarios
    tasks:
      - id: TASK-3-1
        description: Test pipeline containers share worktrees
        acceptance: Multiple phase containers see each other's file changes
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-3-2
        description: Test worktree survives phase transitions
        acceptance: Files created in refine phase exist in plan phase
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-3-3
        description: Test worktree cleanup on pipeline completion
        acceptance: Worktrees removed when pipeline reaches terminal state
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-3-4
        description: Test worktree cleanup on pipeline failure
        acceptance: Worktrees removed even when pipeline fails
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
  - id: 4
    name: Edge Cases and Regression Tests
    goal: Cover edge cases and specific bugs from PR #569
    tasks:
      - id: TASK-4-1
        description: Test worktree not empty after creation
        acceptance: Worktree contains expected git files
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-4-2
        description: Test worktree not root-owned
        acceptance: stat shows uid/gid matching HOST_UID/HOST_GID
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-4-3
        description: Test orphaned worktree cleanup on gateway restart
        acceptance: Orphaned worktrees from crashed containers are cleaned up
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
      - id: TASK-4-4
        description: Test worktree with Docker pre-created .git directory
        acceptance: Worktree works even when Docker creates empty .git directory first
        files:
          - integration_tests/local_pipeline/test_worktree_integration.py
```

---

*Authored-by: egg*
