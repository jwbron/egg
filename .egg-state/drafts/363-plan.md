# Plan: Fix Comment Hider Logic for Issue Comments

> Issue: #363 | Phase: plan

## Summary

The comment hider logic improperly hid an analysis document in issue #359 because the hiding logic was designed for PR workflows but applied too broadly to issue comments. This plan implements role-based hiding with semantic markers: SDLC pipeline phase jobs will stop hiding comments on issues (preserving all substantive content), while PR-based workflows will use explicit `<!-- egg-status-comment -->` markers for precise targeting. Review bots will continue hiding prior reviews but with a counter showing how many were hidden.

The implementation follows the recommended approach from the analysis (Option D + Option A), incorporating the human feedback to keep refine status messages visible and add a hidden-comments counter for review cycles.

## Implementation Phases

### Phase 1: Remove Comment Hiding from SDLC Issue Phases

**Goal**: Stop the SDLC pipeline from hiding any comments during issue-based phases (init, refine, plan). This is the immediate fix that prevents substantive content from being hidden.

**Tasks**:
- [TASK-1-1] Remove "Minimize previous status comments" step from init job (line ~218-243) — Acceptance: Init job no longer calls minimizeComment; workflow file validates
- [TASK-1-2] Remove "Minimize previous pipeline comments" step from refine job (line ~1184-1207) — Acceptance: Refine job no longer calls minimizeComment
- [TASK-1-3] Remove "Minimize previous pipeline comments" step from plan job (line ~1905-1928) — Acceptance: Plan job no longer calls minimizeComment

**Dependencies**: None

**Exit criteria**: All three comment-minimizing steps are removed from issue-phase jobs in sdlc-pipeline.yml. The implement and finalize-pr jobs retain their comment hiding (they operate on PRs).

### Phase 2: Add Semantic Marker to Status Comments

**Goal**: Add the `<!-- egg-status-comment -->` marker to all status-only comments across workflows, enabling precise targeting for future hiding logic.

**Tasks**:
- [TASK-2-1] Add marker to SDLC pipeline status comments (init, phase completed, etc.) — Acceptance: All status comments in sdlc-pipeline.yml include the marker
- [TASK-2-2] Add marker to SDLC HITL status comments (decision resolved, phase approved) — Acceptance: All status comments in sdlc-hitl.yml include the marker
- [TASK-2-3] Add marker to reusable-review.yml status comments — Acceptance: Status comments include marker; review content does NOT
- [TASK-2-4] Add marker to on-check-failure.yml status comments — Acceptance: Status comments include marker
- [TASK-2-5] Add marker to on-merge-conflict.yml status comments — Acceptance: Status comments include marker
- [TASK-2-6] Add marker to on-mention.yml status comments — Acceptance: Status comments include marker
- [TASK-2-7] Add marker to on-review-feedback.yml status comments — Acceptance: Status comments include marker

**Dependencies**: Phase 1 (conceptually independent but should be sequenced for clean commits)

**Exit criteria**: All status-only comments across all workflows include the `<!-- egg-status-comment -->` marker. Substantive content (analysis docs, reviews, PR descriptions) does NOT include the marker.

### Phase 3: Update PR Workflow Hiding to Use Markers

**Goal**: Update the comment hiding logic in PR-based workflows to target the semantic marker instead of pattern-matching on content.

**Tasks**:
- [TASK-3-1] Update implement job hiding logic to use marker — Acceptance: Hiding uses `contains("<!-- egg-status-comment -->")` instead of content patterns
- [TASK-3-2] Update finalize-pr job hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-3] Update checks-failed job hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-4] Update reusable-review.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-5] Update on-check-failure.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-6] Update on-merge-conflict.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-7] Update on-mention.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-8] Update on-review-feedback.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection
- [TASK-3-9] Update sdlc-hitl.yml hiding logic to use marker — Acceptance: Hiding uses marker-based selection

**Dependencies**: Phase 2 (markers must exist before hiding logic can target them)

**Exit criteria**: All comment hiding logic across all workflows uses the semantic marker for targeting. No pattern-matching on content remains.

### Phase 4: Add Hidden-Comments Counter for Reviews

**Goal**: When review bots hide prior reviews, display a count so users know review cycles occurred (per human feedback).

**Tasks**:
- [TASK-4-1] Update reusable-review.yml to count hidden comments — Acceptance: When posting new review status, include "N previous review(s) hidden" if N > 0
- [TASK-4-2] Update on-review-feedback.yml to count hidden comments — Acceptance: Include hidden count in status message

**Dependencies**: Phase 3

**Exit criteria**: When prior reviews are hidden, the new status comment includes a count of how many were hidden.

## Test Strategy

- **Unit tests**: Not applicable (workflow YAML changes)
- **Integration tests**:
  - Trigger SDLC pipeline on a test issue and verify no comments are hidden during refine/plan phases
  - Create a PR and verify status comments ARE hidden (using marker-based logic)
  - Trigger multiple review cycles and verify hidden-count appears
- **Manual testing**:
  1. Create issue with `egg-sdlc` label
  2. Let pipeline run through refine phase — verify analysis NOT hidden
  3. Approve to plan phase — verify plan NOT hidden
  4. Approve to implement phase — verify only marker-tagged status comments hidden
  5. Review PR multiple times — verify hidden count shows in status

## Rollback Plan

If issues arise after deployment:

1. **Immediate rollback**: Revert the commit(s) on main via `git revert <commit-hash>`
2. **Partial rollback**: If only marker logic is problematic, revert Phase 3 commits while keeping Phases 1-2
3. **Emergency fix**: If comments are being hidden incorrectly, temporarily remove all `minimizeComment` calls by reverting to pre-change state

Commands:
```bash
# View recent commits
git log --oneline -10

# Revert specific commit
git revert <commit-hash> --no-edit
git push origin main
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Marker accidentally added to substantive content | Low | Medium | Clear documentation; use marker only in explicit status messages |
| Existing hidden comments stay hidden | Low | Low | Already hidden comments cannot be un-hidden automatically; acceptable |
| Hiding logic fails silently | Low | Low | Keep `|| true` error handling; comments just won't hide |
| Workflow validation failures | Medium | Low | Test YAML syntax before commit; use `yamllint` |

## Migration Notes

- **Breaking changes**: None. This is purely behavioral — no API or interface changes
- **Database migrations**: None
- **Config changes**: None
- **User impact**: Users will see more comments visible on issues (intended behavior). Status comments on PRs will continue to be hidden as before.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Fix comment hider to only hide status on PRs"
  description: |
    Fixes #363. The comment hider was improperly hiding substantive content
    (like analysis documents) on issues because it used pattern matching that
    was too broad.

    This PR implements role-based hiding with semantic markers:
    - Removes comment hiding from SDLC issue phases (refine, plan)
    - Adds `<!-- egg-status-comment -->` marker to status-only comments
    - Updates hiding logic to target the marker instead of content patterns
    - Adds a counter showing how many prior reviews were hidden
phases:
  - id: 1
    name: Remove Comment Hiding from SDLC Issue Phases
    goal: Stop hiding comments during issue-based phases to prevent substantive content from being hidden
    tasks:
      - id: TASK-1-1
        description: Remove "Minimize previous status comments" step from init job
        acceptance: Init job no longer calls minimizeComment; workflow file validates
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-2
        description: Remove "Minimize previous pipeline comments" step from refine job
        acceptance: Refine job no longer calls minimizeComment
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-3
        description: Remove "Minimize previous pipeline comments" step from plan job
        acceptance: Plan job no longer calls minimizeComment
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 2
    name: Add Semantic Marker to Status Comments
    goal: Add marker to status-only comments for precise targeting
    tasks:
      - id: TASK-2-1
        description: Add marker to SDLC pipeline status comments
        acceptance: All status comments in sdlc-pipeline.yml include the marker
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-2
        description: Add marker to SDLC HITL status comments
        acceptance: All status comments in sdlc-hitl.yml include the marker
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-2-3
        description: Add marker to reusable-review.yml status comments
        acceptance: Status comments include marker; review content does NOT
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-2-4
        description: Add marker to on-check-failure.yml status comments
        acceptance: Status comments include marker
        files:
          - .github/workflows/on-check-failure.yml
      - id: TASK-2-5
        description: Add marker to on-merge-conflict.yml status comments
        acceptance: Status comments include marker
        files:
          - .github/workflows/on-merge-conflict.yml
      - id: TASK-2-6
        description: Add marker to on-mention.yml status comments
        acceptance: Status comments include marker
        files:
          - .github/workflows/on-mention.yml
      - id: TASK-2-7
        description: Add marker to on-review-feedback.yml status comments
        acceptance: Status comments include marker
        files:
          - .github/workflows/on-review-feedback.yml
  - id: 3
    name: Update PR Workflow Hiding to Use Markers
    goal: Update hiding logic to target semantic marker instead of content patterns
    tasks:
      - id: TASK-3-1
        description: Update implement job hiding logic to use marker
        acceptance: Hiding uses contains marker instead of content patterns
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-2
        description: Update finalize-pr job hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-3
        description: Update checks-failed job hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-4
        description: Update reusable-review.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-3-5
        description: Update on-check-failure.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/on-check-failure.yml
      - id: TASK-3-6
        description: Update on-merge-conflict.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/on-merge-conflict.yml
      - id: TASK-3-7
        description: Update on-mention.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/on-mention.yml
      - id: TASK-3-8
        description: Update on-review-feedback.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/on-review-feedback.yml
      - id: TASK-3-9
        description: Update sdlc-hitl.yml hiding logic to use marker
        acceptance: Hiding uses marker-based selection
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 4
    name: Add Hidden-Comments Counter for Reviews
    goal: Display count of hidden prior reviews so users know review cycles occurred
    tasks:
      - id: TASK-4-1
        description: Update reusable-review.yml to count hidden comments
        acceptance: Status message includes "N previous review(s) hidden" if N > 0
        files:
          - .github/workflows/reusable-review.yml
      - id: TASK-4-2
        description: Update on-review-feedback.yml to count hidden comments
        acceptance: Status message includes hidden count
        files:
          - .github/workflows/on-review-feedback.yml
```

---

## Phase Approval

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
