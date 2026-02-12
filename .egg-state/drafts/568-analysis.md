# Analysis: Improve Orchestration Integration Tests

> Issue: #568 | Phase: refine

## Problem Statement

The current integration test suite for local orchestration workflows covers the basic happy-path scenarios but lacks comprehensive coverage of edge cases, error scenarios, and complex workflow behaviors. The request is to "significantly beef up" these tests to catch issues early.

**Current state**: ~30 integration tests across 2 test files (1,700+ lines) covering:
- Basic pipeline lifecycle (create/start/complete)
- Container environment validation
- HITL gate approval flow
- Review cycles with circuit breakers
- Container failure handling

**Desired outcome**: A more comprehensive test suite that exercises edge cases, concurrent operations, error recovery paths, and complex multi-agent interactions to catch regressions and subtle bugs before they reach production.

## Current Behavior

### Test Infrastructure

The integration tests use a Docker Compose stack with:
- **Gateway**: Real gateway container for session/auth management
- **Orchestrator**: Real orchestrator container managing pipeline state
- **Mock Sandbox**: Lightweight Alpine container (`mock-sandbox:latest`) that simulates agent behavior via `phase-runner.sh`

Key files:
- `integration_tests/local_pipeline/test_local_pipeline.py:1-1048` - Main integration tests
- `integration_tests/local_pipeline/test_unified_pipeline_behavior.py:1-325` - Unified behavior tests
- `integration_tests/local_pipeline/conftest.py:1-388` - Fixtures and helpers
- `integration_tests/local_pipeline/mock-sandbox/phase-runner.sh:1-187` - Mock agent behavior

### Current Test Coverage

| Area | Tests | Lines |
|------|-------|-------|
| Pipeline lifecycle | 4 | Create, start, poll, complete |
| Multi-phase execution | 5 | All 4 phases (refine, plan, implement, pr) |
| Container management | 3 | Env vars, volumes, failure |
| Review cycles | 3 | Approved, needs_revision, circuit breaker |
| Autofix loop | 2 | Fail-then-pass, circuit breaker |
| HITL gates | 1 | Pause/resume approval flow |
| Error handling | 2 | Container failure, log capture |
| Idempotency | 3 | Can't restart running/completed/awaiting |
| Contract/state | 3 | Creation, prefixed paths, syncing |
| Push restrictions | 2 | Local mode, phase-based control |

### Mock Sandbox Capabilities

The mock sandbox supports testing scenarios via prompt keywords and env vars:
- `FORCE_FAIL` - Exit with code 1 (container failure)
- `CHECK_FAIL` - Checker always fails
- `CHECK_FAIL_THEN_PASS` - Checker fails first, passes on retry
- `REVIEW_NEEDS_REVISION` - Reviewers request revision
- `MOCK_REVIEW_VERDICT` - Override verdict directly
- Exit codes 2/3/4 for env var validation failures

## Constraints

### Technical Constraints
- Tests require Docker and take 2-10 minutes each (container startup overhead)
- Mock sandbox can only simulate behaviors via exit codes and file writes
- Real Claude API not used (mocked behavior only)
- Session-scoped fixtures mean some tests can't be fully isolated

### Resource Constraints
- Each test spawns multiple containers (gateway, orchestrator, 4+ sandbox phases)
- Longer test runs in CI affect feedback time
- Parallel test execution may cause port/network conflicts

### Dependencies
- Docker and Docker Compose available
- Gateway and orchestrator images buildable
- Network subnets 172.40.x/172.41.x available

## Options Considered

### Option A: Expand Existing Mock Sandbox Capabilities

**Approach**: Add new prompt keywords and behaviors to `phase-runner.sh` to enable testing of more scenarios without changing the test infrastructure.

**New scenarios to add**:
1. Timeout simulation (`SLOW_PHASE` - sleep for configurable duration)
2. Partial failure modes (`FAIL_ON_PHASE=plan` - fail only on specific phase)
3. Intermittent failures (`FLAKY_FAIL` - randomly fail ~50% of the time)
4. Signal testing (`HEARTBEAT_ONLY` - send heartbeat signals but never complete)
5. Multi-reviewer disagreement (`REVIEWER_DISAGREE` - some approve, some reject)

**Pros**:
- Minimal infrastructure changes
- Easy to understand and maintain
- Reuses existing patterns

**Cons**:
- Limited to what shell script can simulate
- Can't test complex async behaviors
- No real API contract validation

### Option B: Comprehensive Test Expansion with Enhanced Mock

**Approach**: Significantly expand test coverage while enhancing the mock sandbox to support more complex scenarios. Focus on areas identified as gaps:

1. **Concurrent pipeline tests** (new)
   - Multiple pipelines running simultaneously
   - Pipeline ID isolation verification
   - Resource contention handling

2. **Error recovery tests** (new)
   - Partial phase failure mid-execution
   - Gateway connection loss recovery
   - Orchestrator restart during pipeline execution

3. **HITL decision edge cases** (expand)
   - Decision timeout handling
   - Multiple pending decisions
   - Decision rejection flow
   - Custom input via "Other" option

4. **Signal handling tests** (new)
   - Heartbeat signals during long operations
   - Progress signals and status updates
   - Error signals and cleanup

5. **Contract and state consistency** (expand)
   - Contract modification during execution
   - State file corruption recovery
   - Git commit failures for state persistence

6. **Phase boundary edge cases** (new)
   - Phase transition with pending reviewer verdicts
   - Re-entry into completed phases
   - Skip phase configurations

7. **API endpoint coverage** (new)
   - Invalid request validation (400 responses)
   - Not found handling (404 responses)
   - Rate limiting behavior
   - Pagination for pipeline listing

**Pros**:
- Comprehensive coverage of real-world scenarios
- Catches subtle bugs in async/concurrent code
- Validates API contracts

**Cons**:
- More complex mock sandbox required
- Longer test execution time
- More maintenance burden

### Option C: Unit Test Route Handlers + Focused Integration Tests

**Approach**: Add unit tests for route handlers (currently 0% coverage) and keep integration tests focused on end-to-end workflows only.

**Unit test additions**:
- `routes/pipelines.py` - Request validation, response formatting
- `routes/phases.py` - Phase transition logic
- `routes/decisions.py` - HITL decision handling
- `routes/signals.py` - Signal processing

**Integration test focus**:
- Happy path workflows only
- Critical failure modes
- Async behavior verification

**Pros**:
- Faster feedback from unit tests
- Better test isolation
- Easier to debug failures

**Cons**:
- Doesn't fulfill "beef up integration tests" goal directly
- May miss integration-level bugs that unit tests don't catch
- Requires mocking complex dependencies

### Option D: Hybrid Approach - Prioritized Test Expansion

**Approach**: Combine targeted unit test additions with prioritized integration test expansion, focusing on the highest-value scenarios first.

**Tier 1 - High Priority Integration Tests** (implement first):
1. Concurrent pipeline isolation
2. HITL decision timeout and rejection
3. Phase failure mid-execution recovery
4. Container timeout handling
5. Multi-reviewer verdict disagreement

**Tier 2 - Medium Priority Integration Tests**:
1. Gateway connection loss scenarios
2. State file corruption detection
3. Contract modification edge cases
4. API error response validation

**Tier 3 - Lower Priority / Unit Tests**:
1. Route handler unit tests
2. Response format validation
3. Request parameter validation

**Pros**:
- Focused on highest-impact areas first
- Balanced approach between integration and unit tests
- Manageable scope for initial implementation

**Cons**:
- Requires prioritization decisions
- Tier 3 may never get implemented

## Recommended Approach

**Option D: Hybrid Approach - Prioritized Test Expansion**

This approach balances thoroughness with practicality:

1. **Focuses on real-world scenarios**: Concurrent pipelines, timeouts, and failure recovery are the most likely sources of production bugs.

2. **Builds on existing infrastructure**: Reuses the mock sandbox pattern while adding targeted enhancements.

3. **Prioritizes high-value tests**: The tier system ensures we catch the most critical issues first.

4. **Leaves room for future expansion**: Unit tests for route handlers can be added incrementally.

### Implementation Summary

**Mock sandbox enhancements needed**:
- Add `SLOW_PHASE` support for timeout testing
- Add `FAIL_ON_PHASE=<phase>` for targeted failures
- Add `REVIEWER_MIXED_VERDICT` for disagreement scenarios
- Add heartbeat/signal simulation capabilities

**New test files**:
- `test_concurrent_pipelines.py` - Pipeline isolation and concurrency
- `test_error_recovery.py` - Failure modes and recovery paths
- `test_hitl_edge_cases.py` - Decision handling edge cases
- `test_signals.py` - Signal handling and status updates

**Existing file expansions**:
- `test_local_pipeline.py` - Add timeout and partial failure tests
- `conftest.py` - Add helpers for concurrent test setup

**Estimated test additions**: 25-40 new integration tests across 4-5 files

## Open Questions

For questions that require human input before proceeding, I've created formal HITL decisions below.

```
egg-contract add-decision --question "Which testing priority tier should we implement in the initial PR?" \
  --options "Tier 1 only (5-8 tests, focused)" "Tier 1 + Tier 2 (15-20 tests, comprehensive)" "All tiers (25-40 tests, full coverage)" --format markdown
```

**Decision required: Test scope for initial implementation**

- [ ] **Tier 1 only (5-8 tests, focused)** - Concurrent pipelines, HITL timeout, phase failure recovery, container timeout, reviewer disagreement
- [ ] **Tier 1 + Tier 2 (15-20 tests, comprehensive)** - Adds gateway loss scenarios, state corruption, contract edge cases, API validation
- [ ] **All tiers (25-40 tests, full coverage)** - Includes route handler unit tests and comprehensive request/response validation
- [ ] Other (explain in reply)

---

```
egg-contract add-decision --question "Should we create separate test files for new test categories or add to existing files?" \
  --options "Separate files (better organization)" "Existing files (less file sprawl)" "Mixed (separate for new categories, expand existing for related tests)" --format markdown
```

**Decision required: Test file organization**

- [ ] **Separate files (better organization)** - New files: `test_concurrent_pipelines.py`, `test_error_recovery.py`, `test_hitl_edge_cases.py`, `test_signals.py`
- [ ] **Existing files (less file sprawl)** - Add all new tests to `test_local_pipeline.py` and `test_unified_pipeline_behavior.py`
- [ ] **Mixed approach** - Separate files for entirely new categories (concurrency, signals), expand existing for related tests (error recovery, HITL)
- [ ] Other (explain in reply)

---

**Open-ended questions:**

1. Are there specific bugs or issues from production/staging that should inform which edge cases to prioritize?

2. What is the acceptable CI runtime increase for these additional tests? (Current suite ~5-10 minutes; new tests may add 10-20 minutes)

3. Should tests that require longer timeouts (e.g., timeout simulation tests) be marked with a separate pytest marker for optional execution?

---

*Authored-by: egg*
