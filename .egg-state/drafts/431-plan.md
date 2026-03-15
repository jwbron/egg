# Plan: Update PR checks to use the SDLC work loop

> Issue: #431 | Phase: plan

## Summary

This plan unifies PR check mechanisms by converting `lint.yml`, `test.yml`, and `test-integration.yml` into reusable workflows that can be called from the SDLC work loop. The SDLC work loop's current `check-lint` and `check-test` jobs will be replaced with `uses:` directives that invoke these reusable workflows, eliminating both code duplication and the `act` dependency bug. Standalone PR triggers will be removed since all PRs should go through the SDLC pipeline (per human feedback: "1. Yes").

## Implementation Phases

### Phase 1: Convert check workflows to reusable format

**Goal**: Add `workflow_call` triggers to `lint.yml`, `test.yml`, and `test-integration.yml` so they can be called from the SDLC work loop while maintaining their current functionality.

**Tasks**:
- [TASK-1-1] Add `workflow_call` trigger to `lint.yml` with outputs for pass/fail status — Acceptance: Workflow can be called via `uses:` and reports aggregate pass/fail
- [TASK-1-2] Add `workflow_call` trigger to `test.yml` with outputs for pass/fail status — Acceptance: Workflow can be called via `uses:` and reports aggregate pass/fail
- [TASK-1-3] Add `workflow_call` trigger to `test-integration.yml` with outputs for pass/fail status — Acceptance: Workflow can be called via `uses:` and reports aggregate pass/fail

**Dependencies**: None

**Exit criteria**: All three workflows have `workflow_call` triggers and expose job outputs that indicate overall success/failure.

### Phase 2: Update SDLC work loop to call reusable workflows

**Goal**: Replace the inline `check-lint` and `check-test` jobs in `sdlc-work-loop.yml` with calls to the reusable workflows, and add a new job for integration tests.

**Tasks**:
- [TASK-2-1] Replace `check-lint` job with `uses: ./.github/workflows/lint.yml` — Acceptance: Job calls lint workflow and captures pass/fail output
- [TASK-2-2] Replace `check-test` job with `uses: ./.github/workflows/test.yml` — Acceptance: Job calls test workflow and captures pass/fail output
- [TASK-2-3] Add `check-integration` job with `uses: ./.github/workflows/test-integration.yml` — Acceptance: Job calls integration test workflow and captures pass/fail output
- [TASK-2-4] Update `aggregate-checks` job to read outputs from reusable workflow jobs — Acceptance: Aggregation correctly combines results from all three check workflows

**Dependencies**: Phase 1 (workflows must have `workflow_call` triggers)

**Exit criteria**: SDLC work loop calls reusable workflows instead of running `make lint`/`make test`, and all check results are correctly aggregated.

### Phase 3: Remove standalone PR triggers

**Goal**: Remove `push` and `pull_request` triggers from the check workflows, making the SDLC pipeline the single entry point for all PR checks.

**Tasks**:
- [TASK-3-1] Remove `push` and `pull_request` triggers from `lint.yml` — Acceptance: Workflow only triggers via `workflow_call`
- [TASK-3-2] Remove `push` and `pull_request` triggers from `test.yml` — Acceptance: Workflow only triggers via `workflow_call`
- [TASK-3-3] Remove `push` and `pull_request` triggers from `test-integration.yml` — Acceptance: Workflow only triggers via `workflow_call`

**Dependencies**: Phase 2 (SDLC work loop must be calling these workflows)

**Exit criteria**: All check workflows only respond to `workflow_call` triggers. No independent PR triggers remain.

### Phase 4: Update check-fixer integration

**Goal**: Ensure the `check-fixer` job in the SDLC work loop correctly detects failures from the reusable workflows and triggers autofix appropriately.

**Tasks**:
- [TASK-4-1] Update `check-fixer` job conditionals to use outputs from reusable workflow jobs — Acceptance: Fixer triggers when lint or test reusable workflow fails
- [TASK-4-2] Verify autofix workflow receives correct `failed_workflow` parameter — Acceptance: Autofix correctly identifies which check failed (lint, test, or integration)

**Dependencies**: Phases 2 and 3

**Exit criteria**: Auto-fix workflow triggers correctly when any check workflow fails.

## Test Strategy

- **Unit tests**: No new unit tests required; this is a workflow refactoring
- **Integration tests**: The integration test workflow itself will be tested as part of the changes
- **Manual testing**:
  1. Create a test branch with a deliberate lint error, verify lint workflow fails and fixer triggers
  2. Create a test branch with a deliberate test failure, verify test workflow fails and fixer triggers
  3. Create a clean branch, verify all checks pass and aggregate-checks reports success
  4. Verify that pushing directly to a non-SDLC branch does NOT trigger the old standalone workflows
  5. Verify the SDLC work loop correctly runs all checks during the implement phase

## Rollback Plan

If something goes wrong after deployment:

1. **Immediate rollback**: Revert the changes to `sdlc-work-loop.yml` to restore the inline `check-lint` and `check-test` jobs
2. **Re-add standalone triggers**: If SDLC pipeline has issues, temporarily restore `push`/`pull_request` triggers to the check workflows
3. **Specific commands**:
   ```bash
   git revert <commit-hash>  # Revert the problematic commit
   git push origin main
   ```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Reusable workflow outputs not accessible to calling workflow | Low | High | Test output access pattern on a feature branch before merging; GitHub Actions supports this via `jobs.<job_id>.outputs` |
| Aggregate-checks logic fails to correctly read outputs | Medium | Medium | Add explicit logging in aggregate-checks to show received values; test with known pass/fail scenarios |
| Check-fixer triggers incorrectly or not at all | Low | Medium | Verify fixer conditionals match the new output structure; test with deliberate failures |
| Removal of standalone triggers breaks expected behavior | Low | Low | Per human feedback, all PRs should use SDLC pipeline; no external dependencies on standalone triggers |

## Migration Notes

- **Breaking change for non-SDLC PRs**: After Phase 3, PRs that don't go through the SDLC pipeline will not have automatic lint/test checks. This is the intended behavior per the issue requirements.
- **No database migrations**: This change only affects GitHub Actions workflows.
- **No config changes**: No changes to application configuration.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Unify PR checks under SDLC work loop"
  description: |
    Replaces standalone PR check triggers with reusable workflows called from the
    SDLC work loop. This eliminates code duplication and fixes the `act` dependency
    bug where `make lint`/`make test` fail on GitHub Actions runners.

    Closes #431
phases:
  - id: 1
    name: Convert check workflows to reusable format
    goal: Add workflow_call triggers to lint.yml, test.yml, and test-integration.yml
    tasks:
      - id: TASK-1-1
        description: Add workflow_call trigger to lint.yml with outputs for pass/fail status
        acceptance: Workflow can be called via uses: and reports aggregate pass/fail
        files:
          - .github/workflows/lint.yml
      - id: TASK-1-2
        description: Add workflow_call trigger to test.yml with outputs for pass/fail status
        acceptance: Workflow can be called via uses: and reports aggregate pass/fail
        files:
          - .github/workflows/test.yml
      - id: TASK-1-3
        description: Add workflow_call trigger to test-integration.yml with outputs for pass/fail status
        acceptance: Workflow can be called via uses: and reports aggregate pass/fail
        files:
          - .github/workflows/test-integration.yml
  - id: 2
    name: Update SDLC work loop to call reusable workflows
    goal: Replace inline check jobs with calls to reusable workflows
    tasks:
      - id: TASK-2-1
        description: Replace check-lint job with uses ./.github/workflows/lint.yml
        acceptance: Job calls lint workflow and captures pass/fail output
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-2
        description: Replace check-test job with uses ./.github/workflows/test.yml
        acceptance: Job calls test workflow and captures pass/fail output
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-3
        description: Add check-integration job with uses ./.github/workflows/test-integration.yml
        acceptance: Job calls integration test workflow and captures pass/fail output
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-4
        description: Update aggregate-checks job to read outputs from reusable workflow jobs
        acceptance: Aggregation correctly combines results from all three check workflows
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 3
    name: Remove standalone PR triggers
    goal: Make SDLC pipeline the single entry point for PR checks
    tasks:
      - id: TASK-3-1
        description: Remove push and pull_request triggers from lint.yml
        acceptance: Workflow only triggers via workflow_call
        files:
          - .github/workflows/lint.yml
      - id: TASK-3-2
        description: Remove push and pull_request triggers from test.yml
        acceptance: Workflow only triggers via workflow_call
        files:
          - .github/workflows/test.yml
      - id: TASK-3-3
        description: Remove push and pull_request triggers from test-integration.yml
        acceptance: Workflow only triggers via workflow_call
        files:
          - .github/workflows/test-integration.yml
  - id: 4
    name: Update check-fixer integration
    goal: Ensure auto-fix triggers correctly from reusable workflow failures
    tasks:
      - id: TASK-4-1
        description: Update check-fixer job conditionals to use outputs from reusable workflow jobs
        acceptance: Fixer triggers when lint or test reusable workflow fails
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-2
        description: Verify autofix workflow receives correct failed_workflow parameter
        acceptance: Autofix correctly identifies which check failed
        files:
          - .github/workflows/sdlc-work-loop.yml
```

---

*Authored-by: egg*
