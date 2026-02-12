# Plan: Fix merge fixer bot — switch to merge commits and improve context analysis

> Issue: #427 | Phase: plan

## Summary

The merge fixer bot (conflict resolver) has been causing issues with PR history and introducing incorrect changes. Per human direction, we will switch from the current rebase-based approach to merge commits, which allows easier retry of failed resolutions. We will also enhance the agent's context-gathering to enable more intelligent conflict resolution with minimal human escalation.

The key insight is that merge commits are non-destructive—if the agent makes a mistake, we can revert the merge commit and try again without losing the PR's original history.

## Implementation Phases

### Phase 1: Core Workflow Changes — Rebase to Merge

**Goal**: Replace rebase-based conflict resolution with merge-based resolution, preserving PR commit history and enabling easy retry.

**Tasks**:
- [TASK-1-1] Update `action/build-conflict-prompt.sh` to use merge instead of rebase — Acceptance: Prompt instructs agent to use `git merge origin/<base>` instead of `git rebase origin/<base>`
- [TASK-1-2] Update `action/conflict-conventions.md` to document merge-based workflow — Acceptance: Documentation reflects the new merge approach with examples
- [TASK-1-3] Modify `reusable-conflict-resolve.yml` to handle regular push (not force-push) — Acceptance: Workflow uses `git push` without `--force-with-lease` after merge
- [TASK-1-4] Add retry capability — allow the bot to revert a failed merge and try again — Acceptance: Prompt includes instructions for reverting merge commits if resolution introduces errors

**Dependencies**: None

**Exit criteria**: Conflict resolver uses merge commits, can push without force, and supports revert-and-retry pattern.

### Phase 2: Enhanced Context Gathering

**Goal**: Provide the agent with richer context to make intelligent conflict resolution decisions, reducing incorrect resolutions.

**Tasks**:
- [TASK-2-1] Enhance prompt builder to include PR description and recent commit messages — Acceptance: Prompt contains PR body and commit messages from both sides of the conflict
- [TASK-2-2] Add contextual file analysis to the prompt — include surrounding code context for conflicting files — Acceptance: For each conflicting file, agent receives the full file content (or relevant sections) before attempting resolution
- [TASK-2-3] Include any existing code review comments on the PR — Acceptance: Prompt contains reviewer feedback that may inform conflict resolution
- [TASK-2-4] Add semantic analysis instructions to help agent understand intent — Acceptance: Prompt includes specific guidance for analyzing what each side of a conflict is trying to achieve

**Dependencies**: Phase 1 (core workflow must be in place)

**Exit criteria**: Agent receives comprehensive context including PR metadata, commit messages, file context, and review comments.

### Phase 3: Improved Conflict Detection and Analysis

**Goal**: Help the agent better identify and categorize conflicts to make appropriate resolution decisions.

**Tasks**:
- [TASK-3-1] Add pre-merge conflict preview to the prompt — Acceptance: Agent runs `git merge --no-commit origin/<base>` first to preview conflicts before committing
- [TASK-3-2] Update prompt to categorize conflicts before resolving — Acceptance: Agent explicitly categorizes each conflict (additive, semantic, lock file, etc.) before taking action
- [TASK-3-3] Improve instructions for handling semantic conflicts — Acceptance: Agent has clear guidance on when semantic conflicts can be resolved vs. need human input
- [TASK-3-4] Add conflict summary output — agent posts clear summary of what was resolved and how — Acceptance: Post-resolution comment includes specific changes made for each conflict

**Dependencies**: Phase 2 (enhanced context is needed for proper analysis)

**Exit criteria**: Agent previews conflicts, categorizes them, and provides clear resolution summaries.

### Phase 4: Retry and Recovery Mechanism

**Goal**: Enable easy recovery from incorrect resolutions without human intervention.

**Tasks**:
- [TASK-4-1] Add merge commit detection in `on-merge-conflict.yml` — skip PRs with recent failed merge attempts — Acceptance: Workflow detects when a PR's last commit is a problematic merge and handles accordingly
- [TASK-4-2] Implement automatic revert of failed merges when CI fails — Acceptance: If post-merge CI fails, agent can revert the merge commit and post explanation
- [TASK-4-3] Add ability to retry conflict resolution after revert — Acceptance: After reverting, agent can attempt a new merge with adjusted approach
- [TASK-4-4] Update conflict rules to include learned failure patterns — Acceptance: `conflict-conventions.md` documents common failure patterns and how to avoid them

**Dependencies**: Phase 3 (proper conflict analysis enables better retry decisions)

**Exit criteria**: Failed merges can be automatically reverted and retried with improved approach.

### Phase 5: Testing and Validation

**Goal**: Validate the new conflict resolution approach works correctly across various conflict scenarios.

**Tasks**:
- [TASK-5-1] Create test fixtures with sample conflict scenarios — Acceptance: Test repository with branches that produce various conflict types
- [TASK-5-2] Add integration test for merge-based resolution — Acceptance: CI test that creates conflict, runs resolver, verifies correct resolution
- [TASK-5-3] Test retry mechanism with intentionally failing resolution — Acceptance: Test confirms revert-and-retry works correctly
- [TASK-5-4] Document testing and validation results — Acceptance: Test results and any edge cases documented

**Dependencies**: Phases 1-4

**Exit criteria**: All conflict resolution scenarios tested and working correctly.

## Test Strategy

- **Unit tests**: Add tests for conflict categorization logic in prompt builder
- **Integration tests**:
  - Test merge-based resolution on various conflict types (additive, semantic, lock files)
  - Test retry mechanism by simulating failed CI after merge
  - Test that merge commits are properly structured and can be reverted
- **Manual testing**:
  - Create a test PR with intentional conflicts against main
  - Trigger conflict resolution workflow manually
  - Verify merge commit is created (not rebase)
  - Verify PR history is preserved
  - Test revert flow if resolution is incorrect

## Rollback Plan

If the new merge-based approach causes issues:

1. **Immediate rollback**: Revert the commits on this PR to restore rebase-based resolution
2. **Partial rollback**: If only retry mechanism is problematic, disable the automatic revert (TASK-4-2) while keeping merge-based flow
3. **Emergency stop**: Add `[skip-conflict-fix]` to affected PR titles to prevent resolution attempts

The change from rebase to merge is structurally simple—we're replacing one git command with another—so rollback risk is low.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Merge commits clutter PR history | Medium | Low | GitHub's "squash and merge" eliminates this on final merge; reviewers can ignore merge commits |
| Agent still makes incorrect resolutions | Medium | Medium | Enhanced context and analysis should reduce this; retry mechanism recovers from failures |
| Automatic revert causes confusion | Low | Medium | Clear comments explaining what happened; human can intervene if needed |
| Merge conflicts harder to resolve than rebase | Low | Low | Merge conflicts are semantically equivalent; agent has same information either way |
| Regular push fails if branch is protected | Low | High | Ensure workflow has write permissions; document branch protection requirements |

## Migration Notes

**No breaking changes for users.** The conflict resolver behavior changes, but:
- The resolver still runs automatically on conflicting PRs
- The `[skip-conflict-fix]` marker still works
- Custom `.egg/conflict-rules.md` files are still respected

**Internal changes:**
- Force-push is replaced with regular push (requires `contents: write` permission already in place)
- Merge commits will appear in PR history instead of rebased commits
- If a resolution fails, a revert commit may appear followed by another merge attempt

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Switch conflict resolver from rebase to merge commits"
  description: |
    Fixes the merge fixer bot issues where it was corrupting PR history and
    introducing incorrect changes. Switches from rebase (force-push) to merge
    commits (regular push), enabling easy retry if resolution fails. Also
    enhances context gathering so the agent can make smarter resolution decisions.

    Closes #427
phases:
  - id: 1
    name: Core Workflow Changes
    goal: Replace rebase-based conflict resolution with merge-based resolution
    tasks:
      - id: TASK-1-1
        description: Update build-conflict-prompt.sh to use merge instead of rebase
        acceptance: Prompt instructs agent to use git merge instead of git rebase
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-1-2
        description: Update conflict-conventions.md to document merge-based workflow
        acceptance: Documentation reflects merge approach with examples
        files:
          - action/conflict-conventions.md
      - id: TASK-1-3
        description: Modify reusable-conflict-resolve.yml for regular push
        acceptance: Workflow uses git push without --force-with-lease after merge
        files:
          - .github/workflows/reusable-conflict-resolve.yml
      - id: TASK-1-4
        description: Add retry capability via merge commit revert
        acceptance: Prompt includes instructions for reverting failed merge commits
        files:
          - action/build-conflict-prompt.sh
  - id: 2
    name: Enhanced Context Gathering
    goal: Provide richer context for intelligent conflict resolution
    tasks:
      - id: TASK-2-1
        description: Include PR description and commit messages in prompt
        acceptance: Prompt contains PR body and commit messages from both sides
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-2-2
        description: Add contextual file analysis to the prompt
        acceptance: Agent receives file content for conflicting files
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-2-3
        description: Include existing code review comments on the PR
        acceptance: Prompt contains reviewer feedback
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-2-4
        description: Add semantic analysis instructions for understanding intent
        acceptance: Prompt includes guidance for analyzing conflict intent
        files:
          - action/build-conflict-prompt.sh
          - action/conflict-conventions.md
  - id: 3
    name: Improved Conflict Detection and Analysis
    goal: Help agent categorize and analyze conflicts before resolution
    tasks:
      - id: TASK-3-1
        description: Add pre-merge conflict preview to the prompt
        acceptance: Agent runs git merge --no-commit first to preview conflicts
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-3-2
        description: Update prompt to categorize conflicts before resolving
        acceptance: Agent categorizes each conflict before taking action
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-3-3
        description: Improve instructions for handling semantic conflicts
        acceptance: Agent has clear guidance on semantic conflict resolution
        files:
          - action/conflict-conventions.md
      - id: TASK-3-4
        description: Add conflict summary output in post-resolution comment
        acceptance: Comment includes specific changes made for each conflict
        files:
          - action/build-conflict-prompt.sh
  - id: 4
    name: Retry and Recovery Mechanism
    goal: Enable automatic recovery from incorrect resolutions
    tasks:
      - id: TASK-4-1
        description: Add merge commit detection in on-merge-conflict.yml
        acceptance: Workflow detects PRs with recent failed merge attempts
        files:
          - .github/workflows/on-merge-conflict.yml
      - id: TASK-4-2
        description: Implement automatic revert of failed merges when CI fails
        acceptance: Agent can revert merge commit if post-merge CI fails
        files:
          - action/build-conflict-prompt.sh
          - .github/workflows/reusable-conflict-resolve.yml
      - id: TASK-4-3
        description: Add ability to retry conflict resolution after revert
        acceptance: After reverting, agent can attempt new merge
        files:
          - action/build-conflict-prompt.sh
      - id: TASK-4-4
        description: Update conflict rules with learned failure patterns
        acceptance: Documentation includes common failure patterns
        files:
          - action/conflict-conventions.md
  - id: 5
    name: Testing and Validation
    goal: Validate the new conflict resolution approach
    tasks:
      - id: TASK-5-1
        description: Create test fixtures with sample conflict scenarios
        acceptance: Test repository with branches producing various conflict types
        files:
          - tests/fixtures/conflict-scenarios/
      - id: TASK-5-2
        description: Add integration test for merge-based resolution
        acceptance: CI test verifies correct resolution
        files:
          - tests/integration/test_conflict_resolution.py
      - id: TASK-5-3
        description: Test retry mechanism with intentionally failing resolution
        acceptance: Test confirms revert-and-retry works correctly
        files:
          - tests/integration/test_conflict_resolution.py
      - id: TASK-5-4
        description: Document testing and validation results
        acceptance: Test results and edge cases documented
        files:
          - action/conflict-conventions.md
```

---

*Authored-by: egg*
