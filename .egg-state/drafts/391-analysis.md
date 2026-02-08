# Analysis: Don't re-run PR checks when a draft PR is marked as ready for review

> Issue: #391 | Phase: refine

## Problem Statement

When a draft PR is marked as ready for review via `gh pr ready` or the GitHub UI, certain workflow runs are triggered again even though they already ran successfully on the same commit. This results in redundant CI/CD usage and wasted compute resources.

The user reports: "all checks run on draft PRs" - meaning the checks already executed when the PR was in draft state, so re-running them when the PR becomes ready is unnecessary.

## Current Behavior

The repository has two categories of PR checks:

### 1. Base CI Workflows (NOT affected by this issue)
These workflows use the default `pull_request` event types (`opened`, `synchronize`, `reopened`) and do **NOT** include `ready_for_review`:

| Workflow | File | Behavior |
|----------|------|----------|
| Unit Tests | `.github/workflows/test.yml:6-7` | Does NOT trigger on `ready_for_review` |
| Lint | `.github/workflows/lint.yml:6-7` | Does NOT trigger on `ready_for_review` |
| Integration Tests | `.github/workflows/test-integration.yml:6-7` | Does NOT trigger on `ready_for_review` |

These are correctly configured and do not re-run when a draft becomes ready.

### 2. AI Review Workflows (AFFECTED by this issue)
These workflows explicitly include `ready_for_review` in their event types:

| Workflow | File | Event Types |
|----------|------|-------------|
| Code Review | `.github/workflows/on-pull-request.yml:5` | `[opened, synchronize, ready_for_review, reopened]` |
| Design Review | `.github/workflows/on-pull-request-agent-mode-design.yml:5` | `[opened, synchronize, ready_for_review, reopened]` |
| Contract Verification | `.github/workflows/on-pull-request-contract-verify.yml:7` | `[opened, synchronize, ready_for_review, reopened, labeled]` |

**The Problem**: These workflows run on `opened` (when a draft PR is created) AND again on `ready_for_review` (when the draft becomes ready). If no commits were pushed between these events, this is a redundant run on the same code.

The SDLC pipeline (`sdlc-pipeline.yml:906`) calls `gh pr ready "${PR_NUMBER}"` to mark PRs as ready, which triggers the `ready_for_review` event and causes these duplicate runs.

## Constraints

- **Performance**: AI review workflows are expensive (API costs, compute time). Avoiding redundant runs saves significant resources.
- **Correctness**: We must still run checks when:
  - A PR is first opened (draft or not)
  - Code is pushed to an existing PR (`synchronize`)
  - A PR is reopened after being closed
- **Race conditions**: There's a known GitHub behavior where the transition from draft to ready takes a few seconds, during which old checks may still show as "passed" (potential for accidental merges).
- **SDLC pipeline integration**: The pipeline creates draft PRs and marks them ready automatically. The solution must work with this flow.

## Options Considered

### Option A: Skip review if same commit was already reviewed

**Approach**: In the `reusable-review.yml` `should-run` job, check if the current HEAD commit was already reviewed by looking for the `<!-- egg-automated-review bot=<name> commit=<sha> -->` marker in existing reviews.

**Implementation**: Add a step that:
1. Gets the current PR HEAD SHA
2. Searches existing reviews/comments for the automated review marker with that SHA
3. If found, skip the review

**Pros**:
- Surgically targets the problem without changing trigger behavior
- Works for both manual `ready_for_review` and SDLC pipeline flow
- Preserves ability to re-review via `workflow_dispatch`
- Already has infrastructure for marker detection (`.github/workflows/reusable-review.yml:233-318`)

**Cons**:
- Adds API calls to check for existing reviews
- Small additional complexity in the should-run logic

### Option B: Only run on draft → ready transition if new commits exist

**Approach**: For `ready_for_review` events, compare the current HEAD with the commit from the last workflow run and skip if unchanged.

**Implementation**:
1. Use GitHub's check runs API to find the last successful run's commit
2. Compare with current HEAD
3. Skip if identical

**Pros**:
- Generic solution that would work for any workflow

**Cons**:
- More complex to implement
- Requires additional API calls
- May miss edge cases where the check run completed but review was interrupted

### Option C: Skip workflows entirely on draft PRs

**Approach**: Add `if: github.event.pull_request.draft == false` to all AI review workflows, so they ONLY run when a PR is not a draft.

**Implementation**:
```yaml
jobs:
  review:
    if: github.event.pull_request.draft == false
```

**Pros**:
- Simple, one-line change per workflow
- Clear semantic: "don't review drafts"
- No additional API calls needed

**Cons**:
- Fundamentally changes behavior: draft PRs get NO AI review until ready
- May delay feedback that could be useful during drafting
- `ready_for_review` event is still needed to trigger the first review

### Option D: Remove `ready_for_review` from event types and rely on push events

**Approach**: Remove `ready_for_review` from the trigger types entirely. PRs will get reviewed on `opened` and `synchronize`.

**Implementation**: Change triggers to `types: [opened, synchronize, reopened]`

**Pros**:
- Simplest change - just remove the event type

**Cons**:
- **Breaks the SDLC pipeline**: Draft PRs created by the pipeline wouldn't get reviewed until a commit is pushed
- If a user creates a draft and never pushes, then marks ready, no review would run
- Defeats the original purpose of gating reviews on "ready" status

## Recommended Approach

**Option A: Skip review if same commit was already reviewed**

This is the recommended approach because:

1. **Precision**: It directly addresses the issue (duplicate reviews for the same code) without changing when workflows trigger
2. **Safety**: It preserves the ability to review PRs at any point in their lifecycle
3. **Existing infrastructure**: The marker detection logic already exists in `reusable-review.yml:233-318` for the "last review commit" feature
4. **Minimal risk**: If the detection fails, the worst case is a redundant review (not a missing review)

**Implementation location**: Add to the `should-run` job in `.github/workflows/reusable-review.yml`, between the `[skip-review]` check and the final output.

**Logic**:
```bash
# Skip if this commit was already reviewed by this bot
if [[ -n "$ALREADY_REVIEWED_SHA" && "$ALREADY_REVIEWED_SHA" == "$CURRENT_HEAD_SHA" ]]; then
  echo "Commit $CURRENT_HEAD_SHA was already reviewed, skipping"
  echo "run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
```

## Open Questions

1. **Should users be able to force a re-review of the same commit?** The `workflow_dispatch` trigger already provides this capability. Is that sufficient, or should there be a PR comment command like `/re-review`?

2. **Log visibility**: When a review is skipped due to "already reviewed", should we post a comment to the PR explaining why? This could help with transparency but adds noise.

---

*Authored-by: egg*
