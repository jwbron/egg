# Plan: Run self-improvement workflow after every workflow completes

> Issue: #289 | Phase: plan

## Summary

This plan implements event-driven self-improvement analysis triggered by workflow failures, replacing the nightly cron schedule. The key innovation is a **per-workflow concurrency model** with **issue-based throttling**: once a self-improvement issue is created for a workflow, that workflow's failures are not re-analyzed until the issue is closed. This prevents duplicate analysis of recurring failures while ensuring fresh feedback on new issues.

The implementation adds a `workflow_run` trigger to `self-improvement.yml` that monitors all egg workflows (excluding test/lint PR checks), with logic to detect when the triggering workflow already has an open self-improvement issue. A separate follow-up issue will be created for smart log filtering as a future enhancement.

## Implementation Phases

### Phase 1: Core Workflow Trigger Infrastructure

**Goal**: Add `workflow_run` trigger with per-workflow concurrency and failure-only filtering.

**Tasks**:
- [TASK-1-1] Add `workflow_run` trigger to self-improvement.yml — Acceptance: Workflow triggers on completion of on-mention, on-pull-request, on-check-failure, on-review-feedback, on-merge-conflict, on-push-doc-updater, sdlc-pipeline, sdlc-work-loop, and self-improvement workflows
- [TASK-1-2] Add failure-only gate job — Acceptance: Job checks `github.event.workflow_run.conclusion == 'failure'` and skips analysis for successful runs
- [TASK-1-3] Add per-workflow concurrency group — Acceptance: Concurrency group `self-improvement-{workflow_name}` ensures only one analysis runs per workflow at a time
- [TASK-1-4] Remove nightly cron schedule — Acceptance: `schedule` trigger removed from workflow

**Dependencies**: None

**Exit criteria**: Workflow triggers on monitored workflow failures with proper concurrency isolation.

### Phase 2: Issue-Based Throttling

**Goal**: Implement mechanism to skip analysis when an open self-improvement issue already exists for the failing workflow.

**Tasks**:
- [TASK-2-1] Add issue search step to check for existing open issues — Acceptance: Step queries GitHub API for open issues with `self-improvement` label and workflow name in title/body
- [TASK-2-2] Add conditional skip logic based on open issue — Acceptance: If matching open issue exists, job is skipped with message indicating existing issue
- [TASK-2-3] Update collect.py to support single-run analysis mode — Acceptance: New `--run-id` parameter allows analyzing a specific run instead of time window
- [TASK-2-4] Pass workflow_run metadata to collect step — Acceptance: Collect step receives run ID, workflow name, and conclusion from triggering run

**Dependencies**: Phase 1

**Exit criteria**: Analysis is skipped when an open issue exists for the failing workflow; single-run analysis mode works correctly.

### Phase 3: Self-Improvement Loop Protection

**Goal**: Ensure self-improvement workflow failures are analyzed without creating infinite loops.

**Tasks**:
- [TASK-3-1] Add self-improvement to monitored workflows list — Acceptance: `workflow_run` trigger includes "egg: Self-Improvement Analysis" workflow
- [TASK-3-2] Add loop detection via consecutive failure count — Acceptance: If self-improvement has failed 3+ consecutive times, skip analysis and create a blocking issue requiring human intervention
- [TASK-3-3] Store/retrieve failure count in run artifacts or issue comments — Acceptance: Failure count persists across runs, resets on success

**Dependencies**: Phase 2

**Exit criteria**: Self-improvement failures are analyzed, but infinite loops are prevented through consecutive failure limit.

### Phase 4: Configuration and Cleanup

**Goal**: Update EGG_WORKFLOWS config and create follow-up issue for smart filtering.

**Tasks**:
- [TASK-4-1] Update EGG_WORKFLOWS in config.py — Acceptance: List includes all monitored workflows except test/lint
- [TASK-4-2] Update task prompt to reference single-run context — Acceptance: Prompt text updated to reflect analyzing single failed run vs. batch
- [TASK-4-3] Create GitHub issue for smart log filtering enhancement — Acceptance: Issue created describing future heuristic-based filtering (grep for errors, tool failures, etc.)
- [TASK-4-4] Add documentation comments in workflow — Acceptance: Workflow includes comments explaining concurrency model and throttling logic

**Dependencies**: Phase 3

**Exit criteria**: Configuration updated, documentation in place, follow-up issue created.

## Test Strategy

- **Unit tests**: Add tests for `collect.py --run-id` parameter parsing and single-run collection logic
- **Integration tests**: Test issue search query format against GitHub API schema
- **Manual testing**:
  1. Trigger a workflow failure (e.g., manually break a PR review)
  2. Verify self-improvement runs and creates an issue
  3. Trigger another failure in the same workflow
  4. Verify self-improvement skips due to existing open issue
  5. Close the issue
  6. Trigger another failure
  7. Verify self-improvement runs again
  8. Verify self-improvement failure triggers its own analysis
  9. Simulate 3 consecutive self-improvement failures and verify loop protection activates

## Rollback Plan

If issues arise after deployment:

1. **Quick rollback**: Re-add the cron schedule and disable `workflow_run` trigger by commenting it out:
   ```yaml
   on:
     schedule:
       - cron: "0 2 * * *"
     # workflow_run:  # DISABLED - see issue #XXX
     #   workflows: [...]
   ```

2. **Full revert**: `git revert <commit-sha>` to restore the nightly-only behavior

3. **Partial fix**: If only the throttling is problematic, remove the issue-search step and allow duplicate analysis while fixing

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Infinite self-improvement loops | Medium | High | Consecutive failure counter with hard limit (3); human intervention issue created |
| Race condition in issue search | Low | Low | Per-workflow concurrency group prevents parallel searches for same workflow |
| Missed failures due to issue still open | Medium | Medium | Issue titles include workflow name for easy identification; closing issue re-enables analysis |
| API rate limiting from issue searches | Low | Medium | Issue search is lightweight (single API call); per-workflow concurrency limits frequency |
| Incorrect workflow names in trigger | Low | High | Verify exact workflow names from `name:` field in each workflow file |

## Migration Notes

- **Breaking change**: The nightly cron schedule is removed. Consumers who depended on the 24-hour batch analysis for pattern detection should be aware that analysis is now per-failure.
- **No database migrations**: All state is tracked via GitHub issues
- **Backward compatibility**: The `workflow_dispatch` input parameters remain unchanged for manual testing

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Trigger self-improvement on workflow failures"
  description: |
    Run self-improvement analysis when egg workflows fail, replacing the nightly
    cron schedule. Uses per-workflow concurrency and issue-based throttling to
    prevent duplicate analysis of recurring failures.

    Closes #289.
phases:
  - id: 1
    name: Core Workflow Trigger Infrastructure
    goal: Add workflow_run trigger with per-workflow concurrency and failure-only filtering
    tasks:
      - id: TASK-1-1
        description: Add workflow_run trigger to self-improvement.yml
        acceptance: Workflow triggers on completion of on-mention, on-pull-request, on-check-failure, on-review-feedback, on-merge-conflict, on-push-doc-updater, sdlc-pipeline, sdlc-work-loop, and self-improvement workflows
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-1-2
        description: Add failure-only gate job
        acceptance: Job checks github.event.workflow_run.conclusion == 'failure' and skips analysis for successful runs
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-1-3
        description: Add per-workflow concurrency group
        acceptance: Concurrency group self-improvement-{workflow_name} ensures only one analysis runs per workflow at a time
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-1-4
        description: Remove nightly cron schedule
        acceptance: schedule trigger removed from workflow
        files:
          - .github/workflows/self-improvement.yml
  - id: 2
    name: Issue-Based Throttling
    goal: Implement mechanism to skip analysis when an open self-improvement issue already exists for the failing workflow
    tasks:
      - id: TASK-2-1
        description: Add issue search step to check for existing open issues
        acceptance: Step queries GitHub API for open issues with self-improvement label and workflow name in title/body
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-2-2
        description: Add conditional skip logic based on open issue
        acceptance: If matching open issue exists, job is skipped with message indicating existing issue
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-2-3
        description: Update collect.py to support single-run analysis mode
        acceptance: New --run-id parameter allows analyzing a specific run instead of time window
        files:
          - sandbox/egg_lib/self_improvement/collect.py
      - id: TASK-2-4
        description: Pass workflow_run metadata to collect step
        acceptance: Collect step receives run ID, workflow name, and conclusion from triggering run
        files:
          - .github/workflows/self-improvement.yml
  - id: 3
    name: Self-Improvement Loop Protection
    goal: Ensure self-improvement workflow failures are analyzed without creating infinite loops
    tasks:
      - id: TASK-3-1
        description: Add self-improvement to monitored workflows list
        acceptance: workflow_run trigger includes egg Self-Improvement Analysis workflow
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-3-2
        description: Add loop detection via consecutive failure count
        acceptance: If self-improvement has failed 3+ consecutive times, skip analysis and create a blocking issue requiring human intervention
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-3-3
        description: Store/retrieve failure count in run artifacts or issue comments
        acceptance: Failure count persists across runs, resets on success
        files:
          - .github/workflows/self-improvement.yml
  - id: 4
    name: Configuration and Cleanup
    goal: Update EGG_WORKFLOWS config and create follow-up issue for smart filtering
    tasks:
      - id: TASK-4-1
        description: Update EGG_WORKFLOWS in config.py
        acceptance: List includes all monitored workflows except test/lint
        files:
          - sandbox/egg_lib/self_improvement/config.py
      - id: TASK-4-2
        description: Update task prompt to reference single-run context
        acceptance: Prompt text updated to reflect analyzing single failed run vs. batch
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-4-3
        description: Create GitHub issue for smart log filtering enhancement
        acceptance: Issue created describing future heuristic-based filtering (grep for errors, tool failures, etc.)
        files: []
      - id: TASK-4-4
        description: Add documentation comments in workflow
        acceptance: Workflow includes comments explaining concurrency model and throttling logic
        files:
          - .github/workflows/self-improvement.yml
```

---

*Authored-by: egg*
