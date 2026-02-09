# Analysis: Code review canceled by unrelated concurrency group collision

> Issue: #370 | Phase: refine

## Problem Statement

When a `pull_request_review` event occurs while an "egg: Code Review" workflow is in progress, the running code review can be canceled, wasting compute time and API calls. The reported example shows a review that ran for only 37 seconds before being canceled by an unrelated event.

The core issue is that GitHub Actions' concurrency groups with `cancel-in-progress: true` are designed to cancel stale runs when newer code is pushed, but the current configuration may cancel reviews for non-code events that don't actually invalidate the review.

## Current Behavior

The codebase has two main workflow paths that handle PR reviews:

### Code Review Path
- **Trigger**: `on-pull-request.yml` on `opened`, `synchronize`, `ready_for_review`, `reopened`
- **Calls**: `reusable-review.yml` with `bot_name: review`
- **Concurrency**: `egg-review-<pr_number>` (defined in `reusable-review.yml:188-190`)

### Review Feedback Path
- **Trigger**: `on-review-feedback.yml` on `pull_request_review` (submitted) or `issue_comment` (created)
- **Concurrency**: `egg-feedback-<pr_number>` (defined in `on-review-feedback.yml:54-56`)

**Current concurrency group separation:**
```yaml
# reusable-review.yml (line 188-190)
concurrency:
  group: egg-${{ inputs.bot_name }}-${{ inputs.pr_number }}
  cancel-in-progress: true

# on-review-feedback.yml (line 54-56)
concurrency:
  group: egg-feedback-${{ github.event.pull_request.number || ... }}
  cancel-in-progress: true
```

**Observation**: The current code shows *separate* concurrency groups (`egg-review-<pr>` vs `egg-feedback-<pr>`). This suggests either:
1. The issue was reported based on an older version of the code
2. There's a different collision scenario not immediately apparent
3. The fix for this issue has already been partially implemented

Regardless, the issue raises valid concerns about workflow efficiency that warrant analysis.

## Constraints

- **GitHub Actions limitations**: Concurrency groups are workflow-scoped; there's no built-in way to coordinate between different workflows
- **Claude API costs**: Long-running reviews consume significant API time; canceling near completion is wasteful
- **Review freshness**: Reviews should reflect the current code state; stale reviews on outdated commits are misleading
- **Feedback loop integrity**: The feedback addressing workflow should not interfere with active code reviews

## Options Considered

### Option A: Separate Concurrency Groups (Current State)

**Approach**: Use distinct concurrency group names for each workflow type, preventing cross-workflow cancellation.

Current groups:
- `egg-review-<pr>` for code reviews
- `egg-feedback-<pr>` for feedback addressing
- `egg-contract-verification-<pr>` for contract verification
- `egg-agent-mode-design-<pr>` for design reviews

**Pros**:
- Code reviews are not canceled by feedback events
- Feedback workflows are not canceled by new code pushes
- Simple to understand and maintain

**Cons**:
- Feedback might start on stale code if a push happens mid-review
- Multiple reviewers could run in parallel (may be desired or not)

### Option B: Event-Type Aware Cancellation

**Approach**: Only cancel running workflows when a `synchronize` event (new push) occurs. Non-code events like `pull_request_review` should not trigger cancellation of code reviews.

Implementation would involve:
1. Keep `cancel-in-progress: true` for code review workflows
2. Ensure feedback workflow uses a completely separate concurrency group
3. Optionally add a prefix to distinguish code-change vs. non-code-change triggers

**Pros**:
- New pushes correctly invalidate in-progress reviews
- Non-code events don't waste compute on running reviews
- Maintains the efficiency gains of canceling stale reviews

**Cons**:
- Requires careful event filtering logic
- May need workflow-level coordination for edge cases

### Option C: Disable Cancel-in-Progress for Expensive Jobs

**Approach**: Set `cancel-in-progress: false` for long-running, expensive workflows. Queue subsequent runs instead of canceling.

```yaml
concurrency:
  group: egg-review-${{ inputs.pr_number }}
  cancel-in-progress: false  # Let current review complete
```

**Pros**:
- No work is wasted; every started review completes
- Simpler mental model (FIFO queue behavior)

**Cons**:
- Potential queue buildup during rapid push iterations
- Completed reviews may be stale if code changed mid-review
- Longer feedback cycles for developers

### Option D: Hybrid Approach with Workflow-Specific Settings

**Approach**: Combine the best of Options A-C:
1. **Code reviews** (`reusable-review.yml`): Keep `cancel-in-progress: true` but only within the same workflow/bot_name
2. **Feedback workflows**: Use completely separate concurrency group with `cancel-in-progress: true` (cancel older feedback runs)
3. **Expensive reviews**: Consider adding a "completion check" that allows near-completion runs to finish

**Pros**:
- Targeted cancellation behavior per workflow type
- Efficient handling of rapid pushes while protecting expensive runs
- Clear separation of concerns

**Cons**:
- More complex configuration
- Requires understanding of each workflow's behavior

## Recommended Approach

**Option D: Hybrid Approach** is recommended.

The current codebase already implements part of this solution (separate concurrency groups for review vs. feedback). The remaining work is to:

1. **Verify concurrency isolation**: Confirm that the current `egg-review-<pr>` and `egg-feedback-<pr>` groups are truly independent and working as expected
2. **Add explicit documentation**: Document the concurrency design in workflow comments for future maintainability
3. **Consider `cancel-in-progress: false` for feedback addressing**: Since feedback addressing is an expensive operation that should complete, and multiple concurrent feedback runs are already prevented by the iteration counter, consider not canceling in-progress feedback runs

### Specific Changes

1. Add comments explaining concurrency group design in each workflow
2. Verify `pull_request_review` events for the feedback workflow don't share concurrency with code review
3. Consider adding a check in `reusable-review.yml` to skip if another review of the same type is already running (rather than relying solely on concurrency groups)

## Open Questions

1. **Was the collision recently fixed?** The current code shows separate concurrency groups (`egg-review-<pr>` vs `egg-feedback-<pr>`), but the issue reports they share `egg-<bot_name>-<pr_number>`. Was there a recent fix, or is there a different collision scenario?

2. **Should feedback addressing allow cancel-in-progress?** The feedback workflow already has iteration limits. Would it be better to let in-progress feedback runs complete rather than cancel them?

3. **Are there edge cases with multiple reviewer bots?** The `reusable-review.yml` uses `bot_name` in the concurrency group, meaning `egg-review-<pr>` and `egg-contract-verification-<pr>` are separate. Is this the desired behavior, or should all reviewers for a PR share a group?

---

*Authored-by: egg*
