# Plan: SDLC PRs aren't being taken out of draft mode when checks pass

> Issue: #387 | Phase: plan

## Summary

Replace the synchronous busy-wait polling in `wait-for-checks` with an event-driven `on-check-success.yml` workflow that triggers on `workflow_run` events. When all relevant checks pass and no automated reviewers have requested changes, the new workflow will mark the draft PR as ready-for-review and post to the SDLC issue. This eliminates up to 30 minutes of wasted runner time per PR and provides near-instant reaction to check completion.

## Implementation Phases

### Phase 1: Create Event-Driven Workflow

**Goal**: Implement `on-check-success.yml` that triggers when CI workflows complete and marks eligible draft PRs as ready.

**Tasks**:
- [TASK-1-1] Create `on-check-success.yml` workflow file with `workflow_run` trigger for Lint, Test, "egg: Code Review", and "egg: Contract Verification" — Acceptance: workflow file exists with correct trigger configuration
- [TASK-1-2] Implement SDLC PR detection logic (check for `egg-sdlc` label and draft state) — Acceptance: workflow correctly identifies SDLC draft PRs and skips non-SDLC PRs
- [TASK-1-3] Implement check aggregation logic to verify all required checks have passed — Acceptance: workflow correctly queries check-runs API and filters self-checks
- [TASK-1-4] Implement automated review verification (no `CHANGES_REQUESTED` from egg-automated-review markers) — Acceptance: workflow correctly detects review blockers
- [TASK-1-5] Add concurrency group to prevent duplicate runs for same PR — Acceptance: concurrent triggers for same PR are properly deduplicated

**Dependencies**: None

**Exit criteria**: New workflow file exists and passes linting

### Phase 2: Implement PR Finalization Logic

**Goal**: Add the PR finalization steps to `on-check-success.yml` (mark ready, update body, post to issue).

**Tasks**:
- [TASK-2-1] Implement merge conflict detection (poll mergeability status) — Acceptance: workflow detects conflicts and posts appropriate comment
- [TASK-2-2] Implement PR title update from contract file — Acceptance: PR title is updated from `.egg-state/contracts/{issue}.json` if available
- [TASK-2-3] Implement PR body update with "Ready for human review" section — Acceptance: PR body is appended with status section and reviewer mention
- [TASK-2-4] Implement `gh pr ready` call to mark PR as ready-for-review — Acceptance: draft PR is converted to ready state
- [TASK-2-5] Implement reviewer assignment and contract phase update — Acceptance: reviewer is assigned and contract `current_phase` is updated to "pr"
- [TASK-2-6] Implement comment minimization for old pipeline status comments — Acceptance: old status comments are minimized via GraphQL
- [TASK-2-7] Implement issue comment posting ("Pull request ready for review") — Acceptance: completion comment is posted to SDLC issue

**Dependencies**: Phase 1

**Exit criteria**: All finalization logic implemented and passes linting

### Phase 3: Remove Polling from SDLC Pipeline

**Goal**: Remove the `wait-for-checks` and `finalize-pr` jobs from `sdlc-pipeline.yml` since they're replaced by the event-driven workflow.

**Tasks**:
- [TASK-3-1] Remove `wait-for-checks` job from `sdlc-pipeline.yml` — Acceptance: job definition removed, no dangling references
- [TASK-3-2] Remove `finalize-pr` job from `sdlc-pipeline.yml` — Acceptance: job definition removed, no dangling references
- [TASK-3-3] Remove `checks-failed` job from `sdlc-pipeline.yml` — Acceptance: job definition removed, no dangling references
- [TASK-3-4] Update job dependency chain to end at `implement` job — Acceptance: implement job completes pipeline, outputs are preserved for event-driven workflow
- [TASK-3-5] Add informational comment after implement job explaining async handoff — Acceptance: comment documents the event-driven continuation

**Dependencies**: Phases 1 and 2

**Exit criteria**: `sdlc-pipeline.yml` no longer contains polling logic and passes linting

### Phase 4: Handle Edge Cases and Failure Modes

**Goal**: Ensure robust handling of edge cases identified in the analysis.

**Tasks**:
- [TASK-4-1] Add timeout fallback using scheduled workflow (optional safety net) — Acceptance: cron job runs every 15 minutes to catch stuck PRs
- [TASK-4-2] Handle "zero checks" edge case (no external checks configured) — Acceptance: workflow proceeds correctly when no checks exist
- [TASK-4-3] Add check-failure handling (update issue when checks fail permanently) — Acceptance: failure state is communicated to SDLC issue
- [TASK-4-4] Update `on-check-failure.yml` trigger list if needed for consistency — Acceptance: autofix workflow triggers on same workflow set

**Dependencies**: Phases 1-3

**Exit criteria**: Edge cases handled, all workflows pass linting

## Test Strategy

- **Unit tests**: None required (workflow YAML, tested via actionlint)
- **Integration tests**:
  - Trigger test PR with `egg-sdlc` label
  - Verify Lint/Test completion triggers `on-check-success.yml`
  - Verify draft PR is marked ready after all checks pass
  - Verify issue receives completion comment
- **Manual testing**:
  1. Create a draft PR with `egg-sdlc` label
  2. Push commits that pass all checks
  3. Observe `on-check-success.yml` workflow triggers
  4. Verify PR transitions from draft to ready-for-review
  5. Verify issue comment is posted
  6. Test failure case: push commits that fail checks, verify PR stays draft
  7. Test review blocker: have automated reviewer request changes, verify PR stays draft

## Rollback Plan

1. Revert the commit that removes `wait-for-checks`/`finalize-pr` from `sdlc-pipeline.yml`
2. Delete or disable `on-check-success.yml` workflow
3. The polling approach will resume immediately

Specific commands:
```bash
git revert <commit-sha>  # Revert removal of polling jobs
git rm .github/workflows/on-check-success.yml  # Remove new workflow
git commit -m "Rollback: Restore polling-based PR finalization"
git push origin egg/issue-387
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `workflow_run` doesn't fire for some check types | Low | High | All relevant checks are in-repo workflows, which are supported. External GitHub App checks would need `status` event handling. |
| Race condition: PR pushed while processing | Medium | Low | Concurrency group with `cancel-in-progress: true` ensures latest state is processed |
| Workflow names change breaking trigger | Low | Medium | Document required workflow name stability; add test that verifies trigger list |
| Multiple workflows complete simultaneously | Medium | Low | Concurrency group deduplicates; check aggregation is idempotent |
| Merge conflict detection timing | Low | Low | Existing poll logic (12 retries) handles GitHub's async mergeability computation |

## Migration Notes

- **No database migrations**: This is purely workflow changes
- **No config changes**: No new secrets or environment variables required
- **Breaking changes**: None for users; internal workflow structure change only
- **Backwards compatibility**: Any in-flight SDLC PRs using the old polling approach will complete normally. New PRs will use the event-driven approach once merged.
- **Transition period**: Consider keeping old jobs disabled (commented out) for one sprint before full removal

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Replace busy-wait polling with event-driven PR finalization"
  description: |
    Replace the synchronous busy-wait polling in `wait-for-checks` with an
    event-driven `on-check-success.yml` workflow. When all checks pass and
    no automated reviewers have requested changes, the workflow marks the
    draft PR as ready-for-review and posts to the SDLC issue.

    This eliminates up to 30 minutes of wasted runner time per PR.

    Fixes #387
phases:
  - id: 1
    name: Create Event-Driven Workflow
    goal: Implement on-check-success.yml that triggers when CI workflows complete
    tasks:
      - id: TASK-1-1
        description: Create on-check-success.yml with workflow_run trigger
        acceptance: Workflow file exists with correct trigger configuration
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-1-2
        description: Implement SDLC PR detection logic
        acceptance: Workflow correctly identifies SDLC draft PRs
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-1-3
        description: Implement check aggregation logic
        acceptance: Workflow correctly queries check-runs API and filters self-checks
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-1-4
        description: Implement automated review verification
        acceptance: Workflow correctly detects review blockers
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-1-5
        description: Add concurrency group for deduplication
        acceptance: Concurrent triggers for same PR are deduplicated
        files:
          - .github/workflows/on-check-success.yml
  - id: 2
    name: Implement PR Finalization Logic
    goal: Add PR finalization steps to on-check-success.yml
    tasks:
      - id: TASK-2-1
        description: Implement merge conflict detection
        acceptance: Workflow detects conflicts and posts appropriate comment
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-2-2
        description: Implement PR title update from contract file
        acceptance: PR title is updated from contract if available
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-2-3
        description: Implement PR body update with ready section
        acceptance: PR body is appended with status section
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-2-4
        description: Implement gh pr ready call
        acceptance: Draft PR is converted to ready state
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-2-5
        description: Implement reviewer assignment and contract update
        acceptance: Reviewer assigned and contract phase updated to pr
        files:
          - .github/workflows/on-check-success.yml
          - .github/scripts/push-contract-update.sh
      - id: TASK-2-6
        description: Implement comment minimization
        acceptance: Old status comments are minimized via GraphQL
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-2-7
        description: Implement issue comment posting
        acceptance: Completion comment posted to SDLC issue
        files:
          - .github/workflows/on-check-success.yml
  - id: 3
    name: Remove Polling from SDLC Pipeline
    goal: Remove wait-for-checks and finalize-pr jobs from sdlc-pipeline.yml
    tasks:
      - id: TASK-3-1
        description: Remove wait-for-checks job
        acceptance: Job definition removed, no dangling references
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-2
        description: Remove finalize-pr job
        acceptance: Job definition removed, no dangling references
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-3
        description: Remove checks-failed job
        acceptance: Job definition removed, no dangling references
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-4
        description: Update job dependency chain
        acceptance: Implement job completes pipeline
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-5
        description: Add comment explaining async handoff
        acceptance: Comment documents event-driven continuation
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 4
    name: Handle Edge Cases and Failure Modes
    goal: Ensure robust handling of edge cases
    tasks:
      - id: TASK-4-1
        description: Add optional timeout fallback cron job
        acceptance: Cron job runs every 15 minutes to catch stuck PRs
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-4-2
        description: Handle zero checks edge case
        acceptance: Workflow proceeds when no checks exist
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-4-3
        description: Add check-failure handling
        acceptance: Failure state communicated to SDLC issue
        files:
          - .github/workflows/on-check-success.yml
      - id: TASK-4-4
        description: Update on-check-failure.yml trigger list
        acceptance: Autofix triggers on same workflow set
        files:
          - .github/workflows/on-check-failure.yml
```

---

*Authored-by: egg*
