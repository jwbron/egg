# Plan: Address-feedback bot should only run once all auto-reviewers have run

> Issue: #382 | Phase: plan

## Summary

The address-feedback bot currently triggers immediately when any auto-reviewer posts feedback, causing race conditions where the bot starts before all reviewers finish. This plan implements a wait mechanism using a standardized reviewer job naming convention (`egg-reviewer-{name}`) that allows the feedback workflow to poll check runs and wait for all reviewers to complete before addressing their combined feedback.

This approach was chosen over alternatives (coordination workflow, debouncing, workflow_run events) because it adapts proven polling patterns already in the codebase (`reusable-review.yml`, `sdlc-pipeline.yml`) and handles dynamic reviewer sets naturally (path filters, label gates cause some reviewers to not run at all).

## Implementation Phases

### Phase 1: Standardize Reviewer Job Names

**Goal**: Establish a consistent naming convention for reviewer jobs that the feedback workflow can detect.

**Tasks**:
- [TASK-1-1] Update `reusable-review.yml` to use standardized job name format `egg-reviewer-{bot_name}` — Acceptance: Job name follows the pattern (visible in GitHub Actions UI and check runs API)
- [TASK-1-2] Update `on-pull-request.yml` to use standardized job name — Acceptance: Code Review job appears as `egg-reviewer-review` in check runs
- [TASK-1-3] Update `on-pull-request-agent-mode-design.yml` to use standardized job name — Acceptance: Design Review job appears as `egg-reviewer-agent-mode-design` in check runs
- [TASK-1-4] Update `on-pull-request-contract-verify.yml` to use standardized job name — Acceptance: Contract Verification job appears as `egg-reviewer-contract-verification` in check runs

**Dependencies**: None

**Exit criteria**: All reviewer workflows use the `egg-reviewer-{name}` naming pattern visible in check runs.

### Phase 2: Add Wait-for-Reviewers Logic to Feedback Workflow

**Goal**: Modify the address-feedback workflow to wait for all `egg-reviewer-*` checks to complete before proceeding.

**Tasks**:
- [TASK-2-1] Add a "Wait for all reviewers" step after the initial trigger filter but before addressing feedback — Acceptance: Step polls check runs API and logs progress
- [TASK-2-2] Implement polling loop that looks for checks matching `egg-reviewer-*` pattern — Acceptance: Correctly identifies all reviewer checks regardless of which ones run
- [TASK-2-3] Add timeout handling (10 minutes max wait) with graceful proceed — Acceptance: Workflow proceeds after timeout with warning annotation
- [TASK-2-4] Handle edge case: no reviewer checks found (docs-only PR, etc.) — Acceptance: Workflow exits early with success when no `egg-reviewer-*` checks exist after polling period
- [TASK-2-5] Handle edge case: reviewer checks exist but all skip/neutral — Acceptance: Workflow proceeds when all reviewer checks are completed (regardless of conclusion)

**Dependencies**: Phase 1

**Exit criteria**: Feedback workflow waits for all reviewer checks before running egg.

### Phase 3: Update Exclusion Lists

**Goal**: Ensure wait-for-checks loops in other workflows correctly exclude the new reviewer job names.

**Tasks**:
- [TASK-3-1] Update `reusable-review.yml` wait-for-checks regex to exclude `egg-reviewer-*` pattern — Acceptance: Review workflows don't wait for each other
- [TASK-3-2] Update `sdlc-pipeline.yml` wait-for-checks to use the new pattern for reviewer detection — Acceptance: SDLC pipeline can identify completed reviews by the standardized name

**Dependencies**: Phase 2

**Exit criteria**: No deadlock scenarios between reviewer workflows and wait loops.

### Phase 4: Testing and Validation

**Goal**: Verify the implementation works correctly in various scenarios.

**Tasks**:
- [TASK-4-1] Test scenario: Multiple reviewers trigger concurrently, feedback bot waits for all — Acceptance: Feedback runs once after all reviewers complete
- [TASK-4-2] Test scenario: Only one reviewer triggers (path filters exclude others) — Acceptance: Feedback runs after single reviewer completes without waiting forever
- [TASK-4-3] Test scenario: No reviewers trigger (docs-only change with no applicable filters) — Acceptance: Feedback workflow exits gracefully
- [TASK-4-4] Test scenario: Reviewer times out or fails — Acceptance: Feedback still runs for completed reviews

**Dependencies**: Phase 3

**Exit criteria**: All test scenarios pass.

## Test Strategy

- **Unit tests**: N/A (workflow YAML configuration changes)
- **Integration tests**: Manual testing by triggering the workflows on test PRs
- **Manual testing**:
  1. Create a PR that triggers all three reviewers (touches action/ and has egg-sdlc label)
  2. Verify feedback bot waits until all three complete before running
  3. Create a docs-only PR that triggers only the general code review
  4. Verify feedback bot runs after just that one reviewer completes
  5. Create a minimal PR that triggers no reviewers (e.g., .gitignore change)
  6. Verify feedback bot exits gracefully

## Rollback Plan

If issues arise after deployment:

1. **Immediate rollback**: Revert the PR to restore original workflow files
   ```bash
   git revert <merge-commit-sha>
   git push origin main
   ```

2. **Partial rollback**: If only the wait logic is problematic, remove the wait step from `on-review-feedback.yml` while keeping the standardized job names (job names are backwards-compatible)

3. **Debug mode**: Add `workflow_dispatch` trigger to manually re-run the feedback workflow for testing

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Reviewer check names don't match regex due to GitHub's formatting | Low | High | Test with actual check runs API output; use exact match where possible |
| Timeout too short for slow reviewers | Medium | Low | Use 10-minute timeout; log warnings; allow proceed on timeout |
| No reviewer checks found erroneously | Low | Medium | Poll for 60 seconds before assuming no reviewers; log the decision |
| Race between last reviewer completing and feedback starting | Low | Low | Use `cancel-in-progress` concurrency to handle late triggers |

## Migration Notes

- **No breaking changes**: Existing functionality is preserved
- **Job name changes**: The job names in GitHub Actions UI will change from "AI Code Review" to "egg-reviewer-review", etc. This is cosmetic and doesn't affect functionality
- **Backwards compatibility**: Old review comments with existing markers will still be processed correctly

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Wait for all reviewers before addressing feedback"
  description: |
    The address-feedback bot currently triggers on each reviewer's feedback,
    causing race conditions. This change adds a wait step that polls for all
    `egg-reviewer-*` checks to complete before running.

    Fixes #382
phases:
  - id: 1
    name: Standardize Reviewer Job Names
    goal: Establish consistent naming convention for reviewer jobs
    tasks:
      - id: TASK-1-1
        description: Update reusable-review.yml to use standardized job name format
        acceptance: Job name follows egg-reviewer-{bot_name} pattern
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-2
        description: Update on-pull-request.yml to use standardized job name
        acceptance: Code Review job appears as egg-reviewer-review in check runs
        files:
          - .github/workflows/on-pull-request.yml
      - id: TASK-1-3
        description: Update on-pull-request-agent-mode-design.yml to use standardized job name
        acceptance: Design Review job appears as egg-reviewer-agent-mode-design
        files:
          - .github/workflows/on-pull-request-agent-mode-design.yml
      - id: TASK-1-4
        description: Update on-pull-request-contract-verify.yml to use standardized job name
        acceptance: Contract Verification job appears as egg-reviewer-contract-verification
        files:
          - .github/workflows/on-pull-request-contract-verify.yml
  - id: 2
    name: Add Wait-for-Reviewers Logic
    goal: Modify feedback workflow to wait for all egg-reviewer-* checks
    tasks:
      - id: TASK-2-1
        description: Add wait step after trigger filter in on-review-feedback.yml
        acceptance: Step polls check runs API and logs progress
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-2-2
        description: Implement polling loop for egg-reviewer-* pattern matching
        acceptance: Correctly identifies all reviewer checks
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-2-3
        description: Add timeout handling with graceful proceed
        acceptance: Workflow proceeds after 10-minute timeout with warning
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-2-4
        description: Handle edge case when no reviewer checks found
        acceptance: Workflow exits early when no egg-reviewer-* checks exist
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-2-5
        description: Handle edge case when all reviewer checks skip/neutral
        acceptance: Workflow proceeds when all checks complete
        files:
          - .github/workflows/on-review-feedback.yml
  - id: 3
    name: Update Exclusion Lists
    goal: Prevent deadlock between reviewer workflows
    tasks:
      - id: TASK-3-1
        description: Update reusable-review.yml wait-for-checks to exclude egg-reviewer-*
        acceptance: Review workflows don't wait for each other
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-3-2
        description: Update sdlc-pipeline.yml to use new pattern for reviewer detection
        acceptance: SDLC pipeline identifies reviews by standardized name
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 4
    name: Testing and Validation
    goal: Verify implementation in various scenarios
    tasks:
      - id: TASK-4-1
        description: Test multiple reviewers trigger concurrently
        acceptance: Feedback runs once after all reviewers complete
        files: []
      - id: TASK-4-2
        description: Test single reviewer scenario (path filters)
        acceptance: Feedback runs after single reviewer without waiting forever
        files: []
      - id: TASK-4-3
        description: Test no reviewers scenario
        acceptance: Feedback workflow exits gracefully
        files: []
      - id: TASK-4-4
        description: Test reviewer timeout or failure scenario
        acceptance: Feedback still runs for completed reviews
        files: []
```

---

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
