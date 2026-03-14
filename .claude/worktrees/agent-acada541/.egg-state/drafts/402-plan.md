# Plan: Leverage Issue Labels for SDLC Workflow

> Issue: #402 | Phase: plan

## Summary

This plan implements phase-based labeling for the SDLC pipeline, replacing the single `egg-sdlc` trigger label with a set of mutually exclusive phase labels (`sdlc:refine`, `sdlc:plan`, `sdlc:implement`, `sdlc:pr`) plus an approval state modifier (`sdlc:awaiting-approval`). This enables visual status tracking on GitHub's issue list and supports filtering/querying by phase.

The implementation follows the approach approved in the refine phase analysis: Option A (Phase Labels) with the human decision to **replace** `egg-sdlc` with `sdlc:refine` as the initial trigger.

## Implementation Phases

### Phase 1: Create Labels and Setup Script

**Goal**: Establish the new label set in the repository and create a reusable setup script for label management.

**Tasks**:
- [TASK-1-1] Create label setup script that can create/update SDLC labels idempotently — Acceptance: Script creates all 5 labels with correct colors and descriptions; re-running is safe (idempotent)
- [TASK-1-2] Delete the existing `egg-sdlc` label from the repository — Acceptance: Label no longer exists; existing issues with the label have it removed

**Dependencies**: None

**Exit criteria**: All 5 new labels exist in the repository (`sdlc:refine`, `sdlc:plan`, `sdlc:implement`, `sdlc:pr`, `sdlc:awaiting-approval`); `egg-sdlc` label is removed.

### Phase 2: Update Pipeline Trigger Logic

**Goal**: Modify `sdlc-pipeline.yml` to trigger on `sdlc:refine` instead of `egg-sdlc`, and add label management during initialization.

**Tasks**:
- [TASK-2-1] Update pipeline trigger condition from `egg-sdlc` to `sdlc:refine` — Acceptance: Pipeline triggers when `sdlc:refine` label is added; does not trigger on other labels
- [TASK-2-2] Add logic to ensure correct phase label is applied during init — Acceptance: When workflow starts, the correct phase label is applied (e.g., `sdlc:refine` for new issues, or the current phase label if resuming)
- [TASK-2-3] Update PR label addition logic to use `sdlc:pr` instead of `egg-sdlc` — Acceptance: Created PRs get `sdlc:pr` label instead of `egg-sdlc`

**Dependencies**: Phase 1 (labels must exist)

**Exit criteria**: Pipeline triggers on `sdlc:refine`; correct phase label is applied on init; PRs receive `sdlc:pr` label.

### Phase 3: Add Label Transitions in HITL Workflow

**Goal**: Implement label transitions during phase approvals and HITL decisions in `sdlc-hitl.yml`.

**Tasks**:
- [TASK-3-1] Add `sdlc:awaiting-approval` label when posting phase completion comments — Acceptance: When refine/plan phase posts approval checkbox, `sdlc:awaiting-approval` is added
- [TASK-3-2] Remove `sdlc:awaiting-approval` and transition phase label on approval — Acceptance: When human approves, old phase label is removed, new phase label is added, `sdlc:awaiting-approval` is removed
- [TASK-3-3] Update handle-decision job to manage approval label for HITL decisions — Acceptance: When HITL decisions are pending, `sdlc:awaiting-approval` is present; removed when all resolved

**Dependencies**: Phase 1, Phase 2

**Exit criteria**: Labels correctly reflect phase and approval state throughout HITL interactions.

### Phase 4: Add Label Transitions in Main Pipeline

**Goal**: Ensure phase labels are updated during automatic phase transitions within `sdlc-pipeline.yml`.

**Tasks**:
- [TASK-4-1] Add label transition logic when advancing from implement to PR phase — Acceptance: When implement completes and PR is created, `sdlc:implement` is removed and `sdlc:pr` is added
- [TASK-4-2] Add `sdlc:awaiting-approval` when posting phase completion comments in pipeline — Acceptance: Refine and plan phase completion comments trigger adding `sdlc:awaiting-approval`
- [TASK-4-3] Add helper function/script for atomic label transitions (remove old + add new) — Acceptance: Reusable script handles label transitions with error handling; used by both workflows

**Dependencies**: Phase 1, Phase 2, Phase 3

**Exit criteria**: All automatic phase transitions update labels atomically.

### Phase 5: Update Cleanup Workflow

**Goal**: Update `on-issue-closed.yml` to handle new label set.

**Tasks**:
- [TASK-5-1] Update cleanup trigger condition to check for any `sdlc:*` label — Acceptance: Cleanup runs for issues with any SDLC phase label, not just `egg-sdlc`
- [TASK-5-2] Remove all SDLC labels on issue close (optional cleanup) — Acceptance: Closed SDLC issues have phase labels removed for cleanliness

**Dependencies**: Phase 1

**Exit criteria**: Cleanup workflow handles new label scheme.

### Phase 6: Update Contract Verification Workflow

**Goal**: Update `on-pull-request-contract-verify.yml` to use new labels.

**Tasks**:
- [TASK-6-1] Update PR contract verification trigger to check for `sdlc:pr` label — Acceptance: Contract verification triggers on PRs with `sdlc:pr` label

**Dependencies**: Phase 2

**Exit criteria**: PR contract verification works with new label scheme.

### Phase 7: Documentation and Testing

**Goal**: Update documentation and verify the implementation works end-to-end.

**Tasks**:
- [TASK-7-1] Update `docs/guides/sdlc-pipeline.md` with new label documentation — Acceptance: Documentation describes all SDLC labels, their meanings, and how they're managed
- [TASK-7-2] Update any references to `egg-sdlc` in other documentation — Acceptance: No stale references to `egg-sdlc` remain
- [TASK-7-3] Add integration test for label transitions — Acceptance: Test verifies labels transition correctly through mock phase changes

**Dependencies**: All previous phases

**Exit criteria**: Documentation is updated; integration tests pass.

## Test Strategy

- **Unit tests**: No new unit tests required (workflow YAML doesn't have unit tests)
- **Integration tests**:
  - Add test in `integration_tests/sdlc/` that mocks label API calls and verifies correct labels are added/removed during phase transitions
  - Test idempotency: running label setup twice produces same result
- **Manual testing**:
  1. Create a test issue, add `sdlc:refine` label
  2. Verify pipeline starts and `sdlc:refine` label is present
  3. Approve refine phase, verify `sdlc:refine` → `sdlc:plan` transition
  4. Continue through pipeline, verifying labels at each phase
  5. Verify `sdlc:awaiting-approval` appears/disappears at HITL checkpoints

## Rollback Plan

1. **Immediate rollback**: Revert the PR (all changes are in workflow YAML files)
2. **Label restoration**:
   ```bash
   gh label create "egg-sdlc" --description "Trigger SDLC pipeline" --color "0e8a16"
   ```
3. **In-flight issues**: Manually add `egg-sdlc` label to any issues that were mid-pipeline
4. **New labels**: Can be left in place or removed; they won't affect anything if workflows are reverted

The rollback is straightforward because:
- All changes are in workflow YAML files (single PR revert)
- Label operations are idempotent (can recreate old label)
- No database migrations or data transformations

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Workflow trigger loops (label change triggers workflow that changes label) | Medium | High | Use `types: [labeled]` filter and only trigger on `sdlc:refine`; other phase labels are internal state only |
| Stale labels if workflow fails mid-transition | Low | Low | Best-effort approach; next workflow run will correct labels based on contract state |
| Concurrent label modifications causing race conditions | Low | Medium | Use GitHub API's `add-label` and `remove-label` which are atomic; accept eventual consistency |
| Existing `egg-sdlc` issues lose their label during migration | Medium | Medium | Before deleting `egg-sdlc`, query for all issues with it and document them; manually triage any active ones |

## Migration Notes

**Breaking change**: The `egg-sdlc` label will no longer trigger the pipeline. Users must use `sdlc:refine` instead.

**Migration steps** (to be done by human before/during deploy):
1. Check for any issues currently using `egg-sdlc` label: `gh issue list --label "egg-sdlc" --state open`
2. For active pipeline issues: wait for completion or manually transition to new labels
3. For issues not yet started: remove `egg-sdlc`, add `sdlc:refine` to trigger

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add phase-based labels for SDLC workflow state tracking"
  description: |
    Implements phase-based labeling for the SDLC pipeline to provide visual status
    tracking and enable filtering issues by pipeline state. Replaces the single
    `egg-sdlc` trigger label with phase labels (`sdlc:refine`, `sdlc:plan`,
    `sdlc:implement`, `sdlc:pr`) plus an approval modifier (`sdlc:awaiting-approval`).

    Closes #402.
phases:
  - id: 1
    name: Create Labels and Setup Script
    goal: Establish the new label set in the repository
    tasks:
      - id: TASK-1-1
        description: Create label setup script for SDLC labels (idempotent)
        acceptance: Script creates all 5 labels with correct colors and descriptions; re-running is safe
        files:
          - .github/scripts/setup-sdlc-labels.sh
      - id: TASK-1-2
        description: Delete the existing egg-sdlc label from the repository
        acceptance: Label no longer exists; existing issues have it removed
        files: []
  - id: 2
    name: Update Pipeline Trigger Logic
    goal: Modify sdlc-pipeline.yml to trigger on sdlc:refine
    tasks:
      - id: TASK-2-1
        description: Update pipeline trigger condition from egg-sdlc to sdlc:refine
        acceptance: Pipeline triggers when sdlc:refine label is added
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-2
        description: Add logic to ensure correct phase label is applied during init
        acceptance: Correct phase label is applied when workflow starts
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-3
        description: Update PR label addition logic to use sdlc:pr instead of egg-sdlc
        acceptance: Created PRs get sdlc:pr label
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 3
    name: Add Label Transitions in HITL Workflow
    goal: Implement label transitions during phase approvals
    tasks:
      - id: TASK-3-1
        description: Add sdlc:awaiting-approval label when posting phase completion comments
        acceptance: When refine/plan phase posts approval checkbox, label is added
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-2
        description: Remove sdlc:awaiting-approval and transition phase label on approval
        acceptance: On approval, old phase label removed, new added, awaiting-approval removed
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-3-3
        description: Update handle-decision job to manage approval label for HITL decisions
        acceptance: sdlc:awaiting-approval present when decisions pending, removed when resolved
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 4
    name: Add Label Transitions in Main Pipeline
    goal: Ensure phase labels are updated during automatic phase transitions
    tasks:
      - id: TASK-4-1
        description: Add label transition logic when advancing from implement to PR phase
        acceptance: When PR is created, sdlc:implement removed and sdlc:pr added
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-2
        description: Add sdlc:awaiting-approval when posting phase completion comments in pipeline
        acceptance: Phase completion comments trigger adding sdlc:awaiting-approval
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-3
        description: Add helper script for atomic label transitions
        acceptance: Reusable script handles label transitions with error handling
        files:
          - .github/scripts/transition-sdlc-label.sh
  - id: 5
    name: Update Cleanup Workflow
    goal: Update on-issue-closed.yml to handle new label set
    tasks:
      - id: TASK-5-1
        description: Update cleanup trigger condition to check for any sdlc:* label
        acceptance: Cleanup runs for issues with any SDLC phase label
        files:
          - .github/workflows/on-issue-closed.yml
      - id: TASK-5-2
        description: Remove all SDLC labels on issue close
        acceptance: Closed SDLC issues have phase labels removed
        files:
          - .github/workflows/on-issue-closed.yml
  - id: 6
    name: Update Contract Verification Workflow
    goal: Update on-pull-request-contract-verify.yml to use new labels
    tasks:
      - id: TASK-6-1
        description: Update PR contract verification trigger to check for sdlc:pr label
        acceptance: Contract verification triggers on PRs with sdlc:pr label
        files:
          - .github/workflows/on-pull-request-contract-verify.yml
  - id: 7
    name: Documentation and Testing
    goal: Update documentation and verify implementation
    tasks:
      - id: TASK-7-1
        description: Update docs/guides/sdlc-pipeline.md with new label documentation
        acceptance: Documentation describes all SDLC labels and their management
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-7-2
        description: Update any references to egg-sdlc in other documentation
        acceptance: No stale references to egg-sdlc remain
        files:
          - docs/guides/sdlc-pipeline.md
          - CLAUDE.md
      - id: TASK-7-3
        description: Add integration test for label transitions
        acceptance: Test verifies labels transition correctly through phase changes
        files:
          - integration_tests/sdlc/test_label_transitions.py
```

---

## Phase Approval

### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
