# Plan: Improve Orchestration Integration Tests

> Issue: #568 | Phase: plan

## Summary

This plan expands the local orchestration integration test suite to comprehensively cover edge cases, concurrent operations, error recovery, and complex workflow behaviors. Building on the existing mock sandbox infrastructure, we will add 25-35 new integration tests organized into logical categories. The approach focuses on high-value scenarios first (concurrent pipelines, HITL edge cases, error recovery) while enhancing the mock sandbox to support additional failure modes.

Based on human feedback from the analysis phase:
- **Scope**: Comprehensive coverage of local orchestration workflows
- **Priority**: Thoroughness over execution speed
- **Timeouts**: Use short configurable timeouts for timeout-related tests (not pytest markers)

## Implementation Phases

### Phase 1: Mock Sandbox Enhancement

**Goal**: Extend the mock sandbox (`phase-runner.sh`) to support additional failure modes and behaviors needed for new test scenarios.

**Tasks**:
- [TASK-1-1] Add `SLOW_PHASE` support for configurable phase delays — Acceptance: Mock sandbox sleeps for `SLOW_PHASE_DURATION` seconds when `SLOW_PHASE` keyword present in prompt; default 30s, env-configurable
- [TASK-1-2] Add `FAIL_ON_PHASE=<phase>` for targeted phase failures — Acceptance: Mock sandbox exits code 1 only when `EGG_PIPELINE_PHASE` matches the specified phase
- [TASK-1-3] Add `REVIEWER_MIXED_VERDICT` for reviewer disagreement simulation — Acceptance: First reviewer returns `approved`, second returns `needs_revision` when keyword present
- [TASK-1-4] Add `HEARTBEAT_ONLY` mode for hang simulation — Acceptance: Mock sandbox writes heartbeat signals but never exits (useful for timeout tests)
- [TASK-1-5] Add `PARTIAL_FAILURE` for mid-phase failure simulation — Acceptance: Mock sandbox writes partial draft content then exits code 1

**Dependencies**: None

**Exit criteria**: All new mock sandbox behaviors documented in phase-runner.sh header comments and verified via manual test execution

### Phase 2: Concurrent Pipeline Tests

**Goal**: Verify that multiple pipelines can run simultaneously without interference and maintain proper isolation.

**Tasks**:
- [TASK-2-1] Create `test_concurrent_pipelines.py` test file with fixtures — Acceptance: File created with proper integration test markers and helper functions
- [TASK-2-2] Test two pipelines running simultaneously complete independently — Acceptance: Both pipelines reach `complete` status without cross-contamination of state files
- [TASK-2-3] Test pipeline ID isolation in contract files — Acceptance: Each pipeline's contract file contains only its own pipeline_id and state
- [TASK-2-4] Test concurrent pipelines with different prompts have isolated draft files — Acceptance: Draft files are prefixed with pipeline_id, no shared content
- [TASK-2-5] Test three concurrent pipelines to stress resource handling — Acceptance: All three pipelines complete successfully within extended timeout
- [TASK-2-6] Test concurrent pipeline creation race condition handling — Acceptance: Rapidly creating 5 pipelines results in 5 unique pipeline IDs with no collisions

**Dependencies**: Phase 1 (for slow phase simulation if needed)

**Exit criteria**: All concurrent pipeline tests pass; no flaky failures on 3 consecutive runs

### Phase 3: Error Recovery Tests

**Goal**: Verify the system handles partial failures, connection issues, and recovery scenarios gracefully.

**Tasks**:
- [TASK-3-1] Create `test_error_recovery.py` test file with fixtures — Acceptance: File created with proper integration test markers
- [TASK-3-2] Test partial phase failure (draft written, then crash) — Acceptance: Pipeline status reflects failure; partial draft preserved for debugging
- [TASK-3-3] Test phase failure mid-execution with `FAIL_ON_PHASE` — Acceptance: Pipeline fails on specified phase; earlier phases remain `complete`
- [TASK-3-4] Test container timeout handling with short timeout configuration — Acceptance: Pipeline fails with timeout error when container exceeds configured timeout
- [TASK-3-5] Test state file corruption detection — Acceptance: Pipeline reports error when contract JSON is malformed; does not crash
- [TASK-3-6] Test orphaned container cleanup on pipeline failure — Acceptance: No orphaned `egg-sandbox-*` containers remain after pipeline failure
- [TASK-3-7] Test pipeline deletion during running state — Acceptance: DELETE returns appropriate status; running container is stopped and removed

**Dependencies**: Phase 1 (TASK-1-2 for `FAIL_ON_PHASE`, TASK-1-5 for partial failure)

**Exit criteria**: All error recovery tests pass; container cleanup verified via docker inspection

### Phase 4: HITL Decision Edge Cases

**Goal**: Thoroughly test human-in-the-loop decision handling including timeouts, rejections, and multiple pending decisions.

**Tasks**:
- [TASK-4-1] Create `test_hitl_edge_cases.py` test file with fixtures — Acceptance: File created with proper integration test markers
- [TASK-4-2] Test decision rejection flow (resolution="reject") — Acceptance: Pipeline transitions to cancelled state on rejection; final status reflects rejection reason
- [TASK-4-3] Test custom "Other" option with free-text input — Acceptance: Resolution with custom input is recorded in decision; pipeline continues appropriately
- [TASK-4-4] Test decision timeout with short configurable timeout — Acceptance: Pipeline transitions to timeout state when decision not resolved within `decision_timeout_seconds` config
- [TASK-4-5] Test invalid decision resolution (non-existent decision ID) — Acceptance: API returns 404; pipeline state unchanged
- [TASK-4-6] Test resolving already-resolved decision — Acceptance: API returns 409 conflict; pipeline state unchanged
- [TASK-4-7] Test concurrent decision resolution race condition — Acceptance: One resolution succeeds; other returns 409; no state corruption

**Dependencies**: None (uses existing HITL infrastructure)

**Exit criteria**: All HITL edge case tests pass; decision state transitions verified

### Phase 5: Signal Handling Tests

**Goal**: Verify the orchestrator correctly processes signals (heartbeat, progress, error) from sandbox containers.

**Tasks**:
- [TASK-5-1] Create `test_signals.py` test file with fixtures — Acceptance: File created with proper integration test markers
- [TASK-5-2] Test heartbeat signal extends container timeout — Acceptance: Container with `HEARTBEAT_ONLY` mode stays alive beyond default timeout while sending heartbeats
- [TASK-5-3] Test progress signal updates pipeline status — Acceptance: Progress signals reflected in GET /status endpoint; percentage updates correctly
- [TASK-5-4] Test error signal triggers pipeline failure — Acceptance: Error signal with severity=critical causes immediate pipeline failure
- [TASK-5-5] Test signal from invalid/unknown container — Acceptance: API returns 404; no state changes to existing pipelines
- [TASK-5-6] Test signal API rate limiting (if configured) — Acceptance: Excessive signals from same container are rate-limited; pipeline not affected

**Dependencies**: Phase 1 (TASK-1-4 for `HEARTBEAT_ONLY` mode)

**Exit criteria**: All signal tests pass; signal API contracts verified

### Phase 6: API Validation Tests

**Goal**: Ensure orchestrator API endpoints handle invalid requests correctly with proper error responses.

**Tasks**:
- [TASK-6-1] Create `test_api_validation.py` test file with fixtures — Acceptance: File created with proper integration test markers
- [TASK-6-2] Test POST /pipelines with invalid mode — Acceptance: Returns 400 with clear error message about valid modes
- [TASK-6-3] Test POST /pipelines with missing required fields — Acceptance: Returns 400 with field-specific validation errors
- [TASK-6-4] Test GET /pipelines/{id} with non-existent ID — Acceptance: Returns 404 with appropriate message
- [TASK-6-5] Test DELETE /pipelines/{id} with non-existent ID — Acceptance: Returns 404; idempotent behavior
- [TASK-6-6] Test PATCH /pipelines/{id} with invalid config values — Acceptance: Returns 400; config not modified
- [TASK-6-7] Test pagination for GET /pipelines list endpoint — Acceptance: Returns correct page size; pagination metadata accurate

**Dependencies**: None

**Exit criteria**: All API validation tests pass; response schemas match documented API spec

### Phase 7: Review Cycle Edge Cases

**Goal**: Test complex review scenarios including reviewer disagreement, revision limits, and review state transitions.

**Tasks**:
- [TASK-7-1] Add reviewer disagreement tests to existing test file — Acceptance: Tests added to `test_local_pipeline.py` under new test class
- [TASK-7-2] Test multi-reviewer with mixed verdicts (some approve, some reject) — Acceptance: Pipeline handles mixed verdicts according to policy (majority or consensus)
- [TASK-7-3] Test revision cycle with max_review_cycles=0 (no revisions allowed) — Acceptance: Any `needs_revision` verdict immediately triggers circuit breaker
- [TASK-7-4] Test review cycle counter accuracy — Acceptance: `review_cycles` count matches actual revision iterations
- [TASK-7-5] Test reviewer verdict file path isolation per pipeline — Acceptance: Concurrent pipelines have distinct verdict file paths; no cross-contamination

**Dependencies**: Phase 1 (TASK-1-3 for `REVIEWER_MIXED_VERDICT`)

**Exit criteria**: All review cycle tests pass; review state machine verified

## Test Strategy

- **Unit tests**: Not in scope for this issue (focused on integration tests per user request)
- **Integration tests**: 25-35 new tests across 6 new test files + additions to existing files
  - All tests marked with `@pytest.mark.integration`
  - Tests using extended timeouts marked clearly in docstrings
  - Concurrent tests designed to be independent and parallelizable where possible
- **Manual testing**:
  1. Run `make test-integration` to verify all tests pass
  2. Run tests 3 times consecutively to check for flakiness
  3. Verify no orphaned containers after test suite completion: `docker ps -a | grep egg-sandbox`

## Rollback Plan

All changes are additive (new test files, mock sandbox enhancements) and do not modify production code.

**To rollback:**
```bash
# Revert the PR branch
git revert <commit-sha>

# Or delete new test files if needed
rm integration_tests/local_pipeline/test_concurrent_pipelines.py
rm integration_tests/local_pipeline/test_error_recovery.py
rm integration_tests/local_pipeline/test_hitl_edge_cases.py
rm integration_tests/local_pipeline/test_signals.py
rm integration_tests/local_pipeline/test_api_validation.py

# Revert mock sandbox changes
git checkout HEAD~1 -- integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Test flakiness due to container timing | Medium | Medium | Use explicit waits and generous but bounded timeouts; document expected durations |
| CI runtime increase (est. +15-25 min) | High | Low | Acceptable per user guidance; focus on thoroughness |
| Port conflicts in concurrent tests | Low | Medium | Use Docker Compose network isolation; unique project names per test |
| Mock sandbox complexity growth | Medium | Low | Document all keywords/modes in header; add inline comments |
| Resource exhaustion with many containers | Low | Medium | Ensure cleanup in fixtures; add container count assertions |

## Migration Notes

No migration needed. This is a test-only change with no impact on production code or schemas.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Expand local orchestration integration test coverage"
  description: |
    Significantly improves integration test coverage for local orchestration workflows
    by adding 25-35 new tests covering concurrent pipelines, error recovery, HITL edge
    cases, signal handling, API validation, and review cycles.

    Enhances the mock sandbox to support additional failure modes needed for thorough
    testing. All tests use short configurable timeouts for timeout-related scenarios.

    Closes #568
phases:
  - id: 1
    name: Mock Sandbox Enhancement
    goal: Extend mock sandbox to support additional failure modes and behaviors
    tasks:
      - id: TASK-1-1
        description: Add SLOW_PHASE support for configurable phase delays
        acceptance: Mock sandbox sleeps for SLOW_PHASE_DURATION seconds when keyword present
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
      - id: TASK-1-2
        description: Add FAIL_ON_PHASE for targeted phase failures
        acceptance: Mock sandbox exits code 1 only when phase matches specified phase
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
      - id: TASK-1-3
        description: Add REVIEWER_MIXED_VERDICT for reviewer disagreement simulation
        acceptance: First reviewer approves, second returns needs_revision
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
      - id: TASK-1-4
        description: Add HEARTBEAT_ONLY mode for hang simulation
        acceptance: Mock sandbox writes heartbeat signals but never exits
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
      - id: TASK-1-5
        description: Add PARTIAL_FAILURE for mid-phase failure simulation
        acceptance: Mock sandbox writes partial draft then exits code 1
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
  - id: 2
    name: Concurrent Pipeline Tests
    goal: Verify multiple pipelines can run simultaneously without interference
    tasks:
      - id: TASK-2-1
        description: Create test_concurrent_pipelines.py with fixtures
        acceptance: File created with proper integration test markers
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
      - id: TASK-2-2
        description: Test two pipelines running simultaneously
        acceptance: Both pipelines complete without cross-contamination
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
      - id: TASK-2-3
        description: Test pipeline ID isolation in contract files
        acceptance: Each contract contains only its own pipeline_id
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
      - id: TASK-2-4
        description: Test concurrent pipelines have isolated draft files
        acceptance: Draft files prefixed with pipeline_id, no shared content
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
      - id: TASK-2-5
        description: Test three concurrent pipelines
        acceptance: All three complete successfully within extended timeout
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
      - id: TASK-2-6
        description: Test concurrent pipeline creation race condition
        acceptance: 5 rapid creates result in 5 unique IDs with no collisions
        files:
          - integration_tests/local_pipeline/test_concurrent_pipelines.py
  - id: 3
    name: Error Recovery Tests
    goal: Verify system handles partial failures and recovery scenarios
    tasks:
      - id: TASK-3-1
        description: Create test_error_recovery.py with fixtures
        acceptance: File created with proper integration test markers
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-2
        description: Test partial phase failure
        acceptance: Pipeline fails; partial draft preserved for debugging
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-3
        description: Test phase failure mid-execution with FAIL_ON_PHASE
        acceptance: Pipeline fails on specified phase; earlier phases complete
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-4
        description: Test container timeout handling
        acceptance: Pipeline fails with timeout error when container exceeds timeout
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-5
        description: Test state file corruption detection
        acceptance: Pipeline reports error on malformed contract JSON
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-6
        description: Test orphaned container cleanup on failure
        acceptance: No orphaned egg-sandbox containers after failure
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
      - id: TASK-3-7
        description: Test pipeline deletion during running state
        acceptance: DELETE returns appropriate status; container stopped
        files:
          - integration_tests/local_pipeline/test_error_recovery.py
  - id: 4
    name: HITL Decision Edge Cases
    goal: Thoroughly test human-in-the-loop decision handling
    tasks:
      - id: TASK-4-1
        description: Create test_hitl_edge_cases.py with fixtures
        acceptance: File created with proper integration test markers
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-2
        description: Test decision rejection flow
        acceptance: Pipeline transitions to cancelled on rejection
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-3
        description: Test custom Other option with free-text input
        acceptance: Custom input recorded; pipeline continues appropriately
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-4
        description: Test decision timeout with short configurable timeout
        acceptance: Pipeline transitions to timeout state when not resolved
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-5
        description: Test invalid decision resolution
        acceptance: API returns 404; pipeline state unchanged
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-6
        description: Test resolving already-resolved decision
        acceptance: API returns 409; pipeline state unchanged
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
      - id: TASK-4-7
        description: Test concurrent decision resolution race condition
        acceptance: One succeeds, other returns 409; no state corruption
        files:
          - integration_tests/local_pipeline/test_hitl_edge_cases.py
  - id: 5
    name: Signal Handling Tests
    goal: Verify orchestrator correctly processes signals from containers
    tasks:
      - id: TASK-5-1
        description: Create test_signals.py with fixtures
        acceptance: File created with proper integration test markers
        files:
          - integration_tests/local_pipeline/test_signals.py
      - id: TASK-5-2
        description: Test heartbeat signal extends container timeout
        acceptance: Container stays alive beyond default timeout with heartbeats
        files:
          - integration_tests/local_pipeline/test_signals.py
      - id: TASK-5-3
        description: Test progress signal updates pipeline status
        acceptance: Progress signals reflected in GET /status endpoint
        files:
          - integration_tests/local_pipeline/test_signals.py
      - id: TASK-5-4
        description: Test error signal triggers pipeline failure
        acceptance: Critical error signal causes immediate pipeline failure
        files:
          - integration_tests/local_pipeline/test_signals.py
      - id: TASK-5-5
        description: Test signal from invalid/unknown container
        acceptance: API returns 404; no state changes
        files:
          - integration_tests/local_pipeline/test_signals.py
      - id: TASK-5-6
        description: Test signal API rate limiting
        acceptance: Excessive signals rate-limited; pipeline unaffected
        files:
          - integration_tests/local_pipeline/test_signals.py
  - id: 6
    name: API Validation Tests
    goal: Ensure API endpoints handle invalid requests with proper errors
    tasks:
      - id: TASK-6-1
        description: Create test_api_validation.py with fixtures
        acceptance: File created with proper integration test markers
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-2
        description: Test POST /pipelines with invalid mode
        acceptance: Returns 400 with clear error about valid modes
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-3
        description: Test POST /pipelines with missing required fields
        acceptance: Returns 400 with field-specific validation errors
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-4
        description: Test GET /pipelines/{id} with non-existent ID
        acceptance: Returns 404 with appropriate message
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-5
        description: Test DELETE /pipelines/{id} with non-existent ID
        acceptance: Returns 404; idempotent behavior
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-6
        description: Test PATCH /pipelines/{id} with invalid config values
        acceptance: Returns 400; config not modified
        files:
          - integration_tests/local_pipeline/test_api_validation.py
      - id: TASK-6-7
        description: Test pagination for GET /pipelines list endpoint
        acceptance: Correct page size; pagination metadata accurate
        files:
          - integration_tests/local_pipeline/test_api_validation.py
  - id: 7
    name: Review Cycle Edge Cases
    goal: Test complex review scenarios including disagreement and limits
    tasks:
      - id: TASK-7-1
        description: Add reviewer disagreement tests to existing test file
        acceptance: Tests added to test_local_pipeline.py under new class
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
      - id: TASK-7-2
        description: Test multi-reviewer with mixed verdicts
        acceptance: Pipeline handles mixed verdicts per policy
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
      - id: TASK-7-3
        description: Test revision cycle with max_review_cycles=0
        acceptance: Any needs_revision triggers circuit breaker immediately
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
      - id: TASK-7-4
        description: Test review cycle counter accuracy
        acceptance: review_cycles count matches actual iterations
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
      - id: TASK-7-5
        description: Test reviewer verdict file path isolation
        acceptance: Concurrent pipelines have distinct verdict paths
        files:
          - integration_tests/local_pipeline/test_local_pipeline.py
```

---

*Authored-by: egg*
