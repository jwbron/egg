# Analysis: Have code fully ready and reviewed before opening a PR for SDLC workflow

> Issue: #416 | Phase: refine

## Problem Statement

The current SDLC pipeline creates draft PRs during the implementation phase to leverage GitHub's PR-based CI integration (linting, testing, code review). While draft PRs are less visible than regular PRs, they still appear in the GitHub PR list, generating noise for human reviewers who only want to see polished, ready-for-merge work.

**Current state**: Implementation pushes commits to a branch, then immediately creates a draft PR to trigger CI workflows (lint, test, review). All refinement happens on this visible draft PR.

**Desired outcome**: Keep the entire refinement process invisible until the work is fully complete and reviewed. Humans should only see the final polished PR when it's ready for merge.

## Current Behavior

The SDLC pipeline currently works as follows:

1. **Implementation phase** (`sdlc-pipeline.yml:624-715`): After the agent completes implementation, a draft PR is created with `gh pr create --draft` and labeled with `sdlc:pr`.

2. **CI triggers on PR events**: The following workflows trigger on `pull_request` events:
   - `lint.yml` - Runs on `push` to main and `pull_request` to main
   - `test.yml` - Runs on `push` to main and `pull_request` to main
   - `on-pull-request.yml` - Code review on PR opened/synchronized/ready_for_review
   - `on-pull-request-contract-verify.yml` - Contract verification when `sdlc:pr` label is present
   - `on-check-failure.yml` - Triggers via `workflow_run` when Lint or Test workflows fail on a PR

3. **Feedback loop**: If code review requests changes, `on-review-feedback.yml` triggers the agent to address feedback, push new commits, which triggers re-review.

4. **Wait for checks** (`sdlc-pipeline.yml:786-799`): The pipeline waits for all CI checks to pass before marking the PR ready for review.

5. **Draft → Ready transition**: Only after all checks pass is the PR marked ready via `gh pr ready`.

The key limitation is that workflows like `on-pull-request.yml`, `on-check-failure.yml`, and the contract verification all depend on PR existence to trigger.

## Constraints

### Technical Constraints
- **GitHub Actions limitations**: `workflow_run` events (used by `on-check-failure.yml`) only trigger for workflows that run on `push` or `pull_request` events - not for arbitrary branch pushes
- **Review API requirements**: `reusable-review.yml` uses `gh pr review` which requires a PR to exist
- **Contract verification**: Currently checks PR metadata and uses PR-based review posting
- **Check status API**: GitHub's check runs are associated with commits, not branches, so theoretically they can work without a PR

### Compatibility Constraints
- Must maintain backward compatibility with non-SDLC workflows that still use normal PR flow
- Other repos using the reusable workflows should not be affected
- The gateway sidecar only allows pushing to `egg/` prefixed branches (already the case)

### Process Constraints
- Human approval checkpoints must still work (HITL mechanism)
- Internal review must still happen before human sees the work
- Merge blocking must remain in place (humans must merge)

## Options Considered

### Option A: Branch-Based Workflow Triggers

**Approach**: Add `push` triggers to existing workflows for branches matching a specific pattern (e.g., `egg/sdlc/*` or `egg/issue-*`).

**Changes required**:
1. Modify `lint.yml` and `test.yml` to trigger on push to `egg/**` branches
2. Create new branch-based equivalents of PR workflows:
   - `on-branch-push.yml` - Trigger review/autofix without requiring a PR
   - `on-branch-check-failure.yml` - Handle failures on branch pushes
3. Adapt `reusable-review.yml` to work without a PR (post results to issue comments instead)
4. Create PR only at the very end when all checks pass

**Pros**:
- Minimal changes to existing workflow structure
- Reuses existing workflow logic
- Clear separation: branches for development, PRs for human review
- GitHub natively supports branch-based triggers

**Cons**:
- Requires duplicating some workflow logic for branch vs PR contexts
- Review feedback would need to go to issue comments instead of PR comments (different UX)
- `workflow_run` events may not work the same way for branch pushes
- More complex trigger conditions across multiple files

### Option B: Internal Check Runs API (No PR Until Ready)

**Approach**: Use GitHub's Check Runs API directly to run and report checks on commits without creating a PR. The SDLC pipeline orchestrates all checks internally, only creating a PR when everything passes.

**Changes required**:
1. Modify `sdlc-pipeline.yml` to:
   - Call lint/test as reusable workflows directly (via `workflow_call`)
   - Run code review as an internal step using the agent
   - Track all feedback and iterations within the pipeline
2. Create `reusable-lint.yml` and `reusable-test.yml` for calling from the pipeline
3. Post check run statuses to the commit using the Checks API
4. Only create PR when all internal checks pass

**Pros**:
- Complete control over when PR is created
- All refinement is truly invisible to humans
- Cleaner architecture - pipeline is the single orchestrator
- No noise in PR list at all

**Cons**:
- Major refactoring of the pipeline
- Loses the benefit of GitHub's native PR-based workflow UX
- Check runs API is more complex to use than PR triggers
- Internal review comments wouldn't benefit from GitHub's code review UI
- Feedback loop becomes custom rather than using GitHub's review infrastructure

### Option C: Delayed PR Creation with Branch CI

**Approach**: A hybrid approach where branch-based triggers run CI (lint, test), but the internal review and feedback loop still uses a PR - just created later, after CI passes.

**Changes required**:
1. Add `push` triggers to `lint.yml` and `test.yml` for `egg/issue-*` branches
2. Modify `sdlc-pipeline.yml` to:
   - Wait for lint/test to pass on the branch (via commit status checks)
   - Only then create a draft PR for internal code review
   - Mark ready for review after internal review passes
3. Add concurrency controls to prevent duplicate runs when PR is eventually created
4. Update `on-check-failure.yml` to also trigger on branch workflow failures

**Pros**:
- Leverages existing GitHub infrastructure for both CI and code review
- PR is still used for code review (better UX for line-level comments)
- Simpler than Option B - uses existing mechanisms
- Humans don't see the PR until CI has already passed

**Cons**:
- Still creates a PR (though later in the process)
- Requires coordinating between branch-based and PR-based triggers
- May cause duplicate workflow runs when PR is created
- Doesn't fully eliminate draft PR visibility (just delays it)

### Option D: Repository Dispatch with Invisible Refinement

**Approach**: Use `repository_dispatch` events to orchestrate all checks internally. The SDLC pipeline triggers CI checks via dispatch, collects results, and only creates a PR when ready.

**Changes required**:
1. Add `repository_dispatch` triggers to lint.yml and test.yml
2. Create a dispatch orchestrator in the SDLC pipeline that:
   - Triggers lint/test via dispatch with commit SHA
   - Polls for completion and collects results
   - Runs internal review as a pipeline step
   - Only creates PR when all checks pass
3. Ensure check statuses are posted to the correct commit

**Pros**:
- Complete control over the process
- Can fully hide refinement from humans
- Uses existing workflow definitions (just adds a trigger)
- Pipeline remains the orchestrator

**Cons**:
- `repository_dispatch` requires custom tooling to trigger and track
- More complex state management (tracking which dispatches are for which issue)
- May not integrate as cleanly with GitHub's UI for showing check status
- Polling for completion is less elegant than event-driven

## Recommended Approach

**Option A: Branch-Based Workflow Triggers** is recommended, with some elements from Option C.

### Rationale

1. **Simplest path to the goal**: Adding branch triggers is a well-understood pattern in GitHub Actions. The change is additive rather than requiring major refactoring.

2. **Leverages existing infrastructure**: Lint and test workflows already have the `push` trigger for main - extending this to SDLC branches is minimal.

3. **Maintains code review quality**: By keeping the PR creation for the review phase (but only after CI passes), we preserve GitHub's excellent code review UX with line-level comments.

4. **Clear semantics**:
   - `egg/issue-*` branch pushes → CI only (lint, test)
   - PR creation → Code review (internal first, then human)
   - PR ready → Human merge decision

### Implementation Outline

1. **Update lint.yml and test.yml**:
   ```yaml
   on:
     push:
       branches:
         - main
         - 'egg/issue-*'  # SDLC branches
     pull_request:
       branches: [main]
   ```

2. **Update on-check-failure.yml**: Add ability to detect failures on branch pushes (not just PRs) and trigger autofix.

3. **Modify sdlc-pipeline.yml implement phase**:
   - After implementation, wait for lint/test to pass on the branch (poll commit status)
   - Only create draft PR after CI passes
   - Continue existing review → feedback → re-implement loop

4. **Add deduplication**: Use `concurrency` groups tied to commit SHA to prevent duplicate runs when PR is created.

5. **Optional enhancement**: Create an internal review step that runs before PR creation, posting feedback to the issue instead of a PR.

This approach provides the cleanest path to reducing PR noise while maintaining the benefits of GitHub's review infrastructure.

## Open Questions

### Question 1: Branch prefix for SDLC workflow triggers

Should we use a specific sub-prefix for SDLC branches that get branch-based CI, or should all `egg/issue-*` branches get this treatment?

Options:
- **`egg/sdlc/issue-*`**: More explicit, only affects SDLC pipeline branches
- **`egg/issue-*`**: Simpler, all issue branches get CI
- Current branches are already `egg/issue-{N}` so the latter would require no branch naming changes

```
egg-contract add-decision --question "Which branch pattern should trigger branch-based CI for SDLC workflows?" \
  --options "egg/sdlc/issue-* (explicit SDLC prefix)" "egg/issue-* (current pattern, no change)" --format markdown
```

### Question 2: Internal review before or after PR creation

The analysis mentions potentially doing internal code review before creating a PR. This would mean all feedback during refinement goes to issue comments, not PR comments. Once the PR is created, it would be nearly final.

Alternatively, we keep internal review on the draft PR but delay PR creation until after CI passes.

```
egg-contract add-decision --question "When should internal code review happen relative to PR creation?" \
  --options "Before PR (review on issue, PR only when ready)" "After PR creation but after CI passes (draft PR for review only)" --format markdown
```

### Question 3: Handling on-check-failure for branch pushes

The current `on-check-failure.yml` uses `workflow_run` events which include PR context. For branch-based CI failures, we'd need a different mechanism to:
1. Detect the failure
2. Determine which issue/branch it relates to
3. Trigger the autofix agent

Options:
- Poll for failures in the SDLC pipeline itself
- Use `workflow_run` and extract branch from the workflow run context
- Create a separate branch-failure handler workflow

This is a technical detail that can be resolved during implementation, but input is welcome.

---

*Authored-by: egg*
