# Plan: Don't re-run PR checks when a draft PR is marked as ready for review

> Issue: #391 | Phase: plan

## Summary

Implement Option A from the approved analysis: skip AI reviews when the current PR HEAD commit has already been reviewed by the same bot. This prevents redundant reviews when a draft PR is marked as ready for review (via `ready_for_review` event) without any new commits. The implementation adds a commit-already-reviewed check to the `should-run` job in `reusable-review.yml`, reusing the existing marker detection pattern.

## Implementation Phases

### Phase 1: Add Already-Reviewed Check to should-run Job

**Goal**: Modify the `should-run` job to detect if the current commit has already been reviewed by the same bot, and skip the review if so.

**Tasks**:
- [TASK-1-1] Add GitHub App token generation to should-run job — Acceptance: Token is generated and available for API calls
- [TASK-1-2] Add PR HEAD SHA retrieval step — Acceptance: Current HEAD SHA is captured in a job output
- [TASK-1-3] Add marker detection step to check for existing review of current commit — Acceptance: Step searches reviews and comments for `<!-- egg-automated-review bot=<name> commit=<sha> -->` marker matching the current HEAD
- [TASK-1-4] Integrate already-reviewed check into the check step decision logic — Acceptance: Review is skipped when marker with current commit SHA is found; workflow_dispatch bypasses this check

**Dependencies**: None (first phase)

**Exit criteria**: The `should-run` job outputs `run=false` when a commit has already been reviewed by the same bot, except for `workflow_dispatch` events.

### Phase 2: Add Informational Logging

**Goal**: Add clear log output when a review is skipped due to already being reviewed, to aid debugging and transparency.

**Tasks**:
- [TASK-2-1] Add informational output when skipping due to already-reviewed commit — Acceptance: Workflow logs clearly indicate why the review was skipped, including the bot name and commit SHA

**Dependencies**: Phase 1

**Exit criteria**: When a review is skipped, the workflow run summary clearly shows "Commit <sha> already reviewed by <bot_name>, skipping"

### Phase 3: Testing and Validation

**Goal**: Validate the implementation works correctly in all scenarios.

**Tasks**:
- [TASK-3-1] Test: New PR (draft) triggers review as expected — Acceptance: Opening a new draft PR triggers the review workflow
- [TASK-3-2] Test: ready_for_review skips when same commit already reviewed — Acceptance: Marking a draft PR ready does NOT re-run the review if no new commits
- [TASK-3-3] Test: synchronize event triggers new review — Acceptance: Pushing new commits to a PR triggers a new review
- [TASK-3-4] Test: workflow_dispatch always runs — Acceptance: Manual dispatch via workflow_dispatch re-runs the review regardless of previous reviews
- [TASK-3-5] Test: Different bots track separately — Acceptance: Code Review and Design Review track their own review markers independently

**Dependencies**: Phase 1, Phase 2

**Exit criteria**: All test scenarios pass; no regression in normal PR review behavior.

## Test Strategy

- **Unit tests**: No unit tests required; this is a workflow-level change tested via GitHub Actions execution.
- **Integration tests**: Manual testing via the test scenarios in Phase 3. These can be executed by:
  1. Creating a test PR as draft
  2. Observing review runs on `opened` event
  3. Running `gh pr ready` and observing skip behavior
  4. Pushing a new commit and observing re-review
- **Manual testing**: Follow the Phase 3 test plan on a test PR before merging.

## Rollback Plan

If the change causes issues:

1. **Quick revert**: Revert the commit that modified `reusable-review.yml`:
   ```bash
   git revert <commit-sha>
   git push origin main
   ```

2. **Behavior if detection fails**: The worst case is a redundant review runs (not a missing review), which is the current behavior anyway. This is a safe failure mode.

3. **Monitoring**: Watch workflow runs for the first few PRs after merge to ensure reviews trigger correctly on new commits.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| API rate limiting from additional API calls | Low | Low | The marker check adds 2 API calls (reviews + comments), well within rate limits |
| False positive skip (misdetects already reviewed) | Very Low | Medium | Marker format is specific and includes bot name + commit SHA; false matches are unlikely |
| False negative (fails to skip) | Low | Low | Results in redundant review, which is the current behavior. Safe failure mode. |
| Marker format changes breaking detection | Low | Medium | The marker format is documented and used elsewhere; changes would be coordinated |

## Migration Notes

No migration required. This is a pure behavioral improvement with no breaking changes:
- Existing PRs will work as before
- New PRs will benefit from skip detection immediately
- No configuration changes needed
- No database or state migrations

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Skip AI reviews when commit already reviewed"
  description: |
    Prevents redundant AI review runs when a draft PR is marked as ready for
    review. When the `ready_for_review` event fires, the workflow now checks
    if the current HEAD commit was already reviewed by the same bot and skips
    if so. This saves compute resources and API costs.

    Closes #391
phases:
  - id: 1
    name: Add Already-Reviewed Check
    goal: Modify should-run job to detect and skip already-reviewed commits
    tasks:
      - id: TASK-1-1
        description: Add GitHub App token generation to should-run job
        acceptance: Token is generated and available for API calls
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-2
        description: Add PR HEAD SHA retrieval step
        acceptance: Current HEAD SHA is captured in a job output
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-3
        description: Add marker detection step to check for existing review
        acceptance: Step searches reviews and comments for automated review marker matching current HEAD
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-1-4
        description: Integrate already-reviewed check into decision logic
        acceptance: Review skipped when marker found; workflow_dispatch bypasses check
        files:
          - .github/workflows/reusable-review.yml
  - id: 2
    name: Add Informational Logging
    goal: Provide clear log output when reviews are skipped
    tasks:
      - id: TASK-2-1
        description: Add informational output when skipping due to already-reviewed
        acceptance: Logs clearly indicate why review was skipped with bot name and commit SHA
        files:
          - .github/workflows/reusable-review.yml
  - id: 3
    name: Testing and Validation
    goal: Validate implementation across all scenarios
    tasks:
      - id: TASK-3-1
        description: Test new draft PR triggers review
        acceptance: Opening new draft PR triggers review workflow
        files: []
      - id: TASK-3-2
        description: Test ready_for_review skips when same commit reviewed
        acceptance: Marking draft ready does not re-run review if no new commits
        files: []
      - id: TASK-3-3
        description: Test synchronize event triggers new review
        acceptance: Pushing new commits triggers new review
        files: []
      - id: TASK-3-4
        description: Test workflow_dispatch always runs
        acceptance: Manual dispatch re-runs review regardless of previous reviews
        files: []
      - id: TASK-3-5
        description: Test different bots track separately
        acceptance: Code Review and Design Review track markers independently
        files: []
```

---

## Phase Approval

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
