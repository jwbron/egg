# Plan: SDLC pipeline checks not re-run after autofixer

> Issue: #485 | Phase: plan

## Summary

This plan implements the **hybrid synchronous wait with re-dispatch** approach (Option C from analysis) to fix the broken check → fix → re-check loop. After the autofixer runs, the work loop will wait for completion synchronously using `gh run watch`, then re-dispatch itself in "checks-only" mode to verify fixes. The circuit breaker will be extended to track autofix attempts and prevent infinite loops.

Human answers from analysis:
1. Timeout for autofix wait: 30 minutes
2. Review phase re-evaluates the diff from the last review (not full diff)
3. Autofix counter resets on manual retry

## Implementation Phases

### Phase 1: Contract Schema Extension

**Goal**: Add autofix tracking fields to the contract schema and initialize them in the pipeline.

**Tasks**:
- [TASK-1-1] Add `autofix_attempts` and `max_autofix_attempts` fields to contract schema — Acceptance: Schema validates contracts with new fields; defaults to 0 and 3 respectively
- [TASK-1-2] Update `sdlc-pipeline.yml` to initialize autofix tracking in new contracts — Acceptance: New contracts have `autofix_attempts: 0` and `max_autofix_attempts: 3`
- [TASK-1-3] Reset autofix_attempts counter on manual workflow re-trigger — Acceptance: When work loop is triggered manually (workflow_dispatch), autofix_attempts resets to 0

**Dependencies**: None

**Exit criteria**: Contract schema validates with new fields; pipeline initializes them correctly.

### Phase 2: Synchronous Autofix Wait

**Goal**: Replace fire-and-forget autofix dispatch with synchronous wait using `gh run watch`.

**Tasks**:
- [TASK-2-1] Modify `check-fixer` job to wait for autofix workflow completion — Acceptance: Uses `gh run watch` with 30-minute timeout; outputs `fixed=true|false` based on completion status
- [TASK-2-2] Add error handling for autofix workflow not found or timeout — Acceptance: Outputs `fixed=false` and logs warning on timeout or workflow-not-found
- [TASK-2-3] Update autofix_attempts counter in contract after autofix completes — Acceptance: Increments `autofix_attempts` in contract; commits and pushes update

**Dependencies**: Phase 1

**Exit criteria**: `check-fixer` job waits for autofix completion and outputs accurate `fixed` status.

### Phase 3: Fix Aggregate Logic

**Goal**: Remove the flawed `pending` handling from aggregate-checks.

**Tasks**:
- [TASK-3-1] Remove `FIXER_STATUS != "pending"` condition from aggregate-checks — Acceptance: All three conditions (lint, test, integration) check only `*_PASSED` status, not fixer status
- [TASK-3-2] Add `autofix_succeeded` output to aggregate-checks — Acceptance: Outputs `true` if fixer ran and succeeded, `false` otherwise

**Dependencies**: Phase 2

**Exit criteria**: Aggregate logic correctly fails when checks fail regardless of fixer status.

### Phase 4: Re-dispatch After Autofix

**Goal**: Add a new job to re-dispatch the work loop in checks-only mode after successful autofix.

**Tasks**:
- [TASK-4-1] Add `mode` input to work loop workflow (default: "full") — Acceptance: Input accepts "full" or "checks-only"; workflow continues to work normally when "full"
- [TASK-4-2] Skip `work` job when mode is "checks-only" — Acceptance: Work job has `if: inputs.mode != 'checks-only'` condition
- [TASK-4-3] Add `re-dispatch-checks` job after aggregate-checks — Acceptance: Job runs when `autofix_succeeded=true` and `autofix_attempts < max_autofix_attempts`; re-dispatches work loop with `mode=checks-only`
- [TASK-4-4] Add circuit breaker for max autofix attempts — Acceptance: When `autofix_attempts >= max_autofix_attempts`, skip re-dispatch and proceed to escalate job

**Dependencies**: Phase 3

**Exit criteria**: After successful autofix, work loop re-runs checks; max attempts trigger escalation.

### Phase 5: Escalation for Autofix Failures

**Goal**: Extend escalation job to handle autofix failures distinctly from review failures.

**Tasks**:
- [TASK-5-1] Add `escalate-autofix-failure` job for max autofix attempts exceeded — Acceptance: Posts comment explaining autofix failed after N attempts; lists which checks are failing
- [TASK-5-2] Update escalate job condition to include autofix circuit breaker — Acceptance: Escalate job triggers on either review or autofix circuit breaker

**Dependencies**: Phase 4

**Exit criteria**: Human is notified when autofix fails repeatedly with actionable information.

### Phase 6: Testing and Validation

**Goal**: Verify the fix works end-to-end.

**Tasks**:
- [TASK-6-1] Add workflow syntax validation — Acceptance: `actionlint` passes on modified workflows
- [TASK-6-2] Document the new autofix retry behavior — Acceptance: Update `docs/guides/sdlc-pipeline.md` with autofix retry section

**Dependencies**: Phase 5

**Exit criteria**: All workflows validate; documentation is updated.

## Test Strategy

- **Unit tests**: No new unit tests required (workflow logic only)
- **Integration tests**: The existing SDLC pipeline integration tests will exercise the new code paths when triggered manually
- **Manual testing**:
  1. Create a PR with a lint error → verify autofix triggers → verify checks re-run → verify passes or escalates after 3 attempts
  2. Create a PR with a test failure → verify autofix triggers → verify checks re-run
  3. Manually re-trigger pipeline after autofix → verify autofix_attempts resets to 0
  4. Verify timeout behavior: trigger autofix and cancel it mid-run → verify `fixed=false` output

## Rollback Plan

If issues arise after merge:

1. **Immediate rollback**: Revert the merge commit
   ```bash
   git revert -m 1 <merge-commit-sha>
   git push origin main
   ```

2. **Partial rollback** (if only aggregate logic is broken):
   - Re-add the `FIXER_STATUS != "pending"` condition temporarily
   - This restores the current (broken but safe) behavior

3. **Contract cleanup**: Existing contracts with `autofix_attempts` field will be ignored by older code (Pydantic ignores extra fields)

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Infinite loop if re-dispatch logic has bug | Low | High | Circuit breaker with max 3 autofix attempts; max 10 total cycles |
| `gh run watch` timeout causes job failure | Medium | Medium | Catch timeout error and output `fixed=false` gracefully |
| Race condition with concurrent workflow runs | Low | Medium | Concurrency group already prevents concurrent runs for same issue |
| Autofix workflow changes output format | Low | Medium | Watch for run ID, not specific output; fail gracefully on changes |

## Migration Notes

- **No database migrations**: Contract schema changes are additive (new optional fields)
- **Backwards compatibility**: Existing contracts without `autofix_attempts` will use default value of 0
- **Config changes**: None required; defaults are sensible

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Fix check re-validation after autofixer runs"
  description: |
    Fixes #485. When lint/test/integration checks fail during implement phase,
    the autofixer was triggered but checks were never re-run to verify fixes.

    This PR implements synchronous autofix wait with re-dispatch: after autofix
    completes, the work loop re-runs in checks-only mode to verify fixes. A circuit
    breaker prevents infinite loops (max 3 autofix attempts per phase).
phases:
  - id: 1
    name: Contract Schema Extension
    goal: Add autofix tracking fields to the contract schema
    tasks:
      - id: TASK-1-1
        description: Add autofix_attempts and max_autofix_attempts fields to contract schema
        acceptance: Schema validates contracts with new fields; defaults to 0 and 3
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-2
        description: Update sdlc-pipeline.yml to initialize autofix tracking in new contracts
        acceptance: New contracts have autofix_attempts 0 and max_autofix_attempts 3
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-3
        description: Reset autofix_attempts counter on manual workflow re-trigger
        acceptance: When work loop is triggered manually, autofix_attempts resets to 0
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 2
    name: Synchronous Autofix Wait
    goal: Replace fire-and-forget autofix with synchronous wait
    tasks:
      - id: TASK-2-1
        description: Modify check-fixer job to wait for autofix workflow completion
        acceptance: Uses gh run watch with 30-minute timeout; outputs fixed true or false
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-2
        description: Add error handling for autofix workflow not found or timeout
        acceptance: Outputs fixed false and logs warning on timeout or workflow-not-found
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-3
        description: Update autofix_attempts counter in contract after autofix completes
        acceptance: Increments autofix_attempts in contract; commits and pushes update
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 3
    name: Fix Aggregate Logic
    goal: Remove flawed pending handling from aggregate-checks
    tasks:
      - id: TASK-3-1
        description: Remove FIXER_STATUS != pending condition from aggregate-checks
        acceptance: All check conditions verify only PASSED status, not fixer status
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-2
        description: Add autofix_succeeded output to aggregate-checks
        acceptance: Outputs true if fixer ran and succeeded, false otherwise
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 4
    name: Re-dispatch After Autofix
    goal: Add re-dispatch in checks-only mode after successful autofix
    tasks:
      - id: TASK-4-1
        description: Add mode input to work loop workflow (default full)
        acceptance: Input accepts full or checks-only; workflow works normally when full
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-2
        description: Skip work job when mode is checks-only
        acceptance: Work job has if condition to skip when mode is checks-only
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-3
        description: Add re-dispatch-checks job after aggregate-checks
        acceptance: Job runs when autofix_succeeded and attempts below max; re-dispatches with checks-only
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-4
        description: Add circuit breaker for max autofix attempts
        acceptance: When autofix_attempts >= max, skip re-dispatch and escalate
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 5
    name: Escalation for Autofix Failures
    goal: Extend escalation to handle autofix failures
    tasks:
      - id: TASK-5-1
        description: Add escalate-autofix-failure job for max attempts exceeded
        acceptance: Posts comment explaining autofix failed after N attempts with failing checks
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-2
        description: Update escalate job condition to include autofix circuit breaker
        acceptance: Escalate job triggers on either review or autofix circuit breaker
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 6
    name: Testing and Validation
    goal: Verify the fix works end-to-end
    tasks:
      - id: TASK-6-1
        description: Add workflow syntax validation
        acceptance: actionlint passes on modified workflows
        files:
          - .github/workflows/sdlc-work-loop.yml
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-6-2
        description: Document the new autofix retry behavior
        acceptance: Update docs/guides/sdlc-pipeline.md with autofix retry section
        files:
          - docs/guides/sdlc-pipeline.md
```

---

*Authored-by: egg*
