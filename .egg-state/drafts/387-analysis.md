# Analysis: SDLC PRs aren't being taken out of draft mode when checks pass

> Issue: #387 | Phase: refine

## Problem Statement

The SDLC pipeline currently uses a busy-wait (polling) pattern to wait for PR checks to complete before taking draft PRs out of draft mode. This approach has reliability issues and doesn't align with an async, event-driven architecture. The desired outcome is an event-driven workflow where:

1. PR checks run asynchronously
2. When all checks pass and review bots approve, the system reacts to this event
3. The PR is taken out of draft mode
4. A notification is posted on the SDLC issue

## Current Behavior

The SDLC pipeline (`sdlc-pipeline.yml`) implements a `wait-for-checks` job (lines 648-797) that polls the GitHub API every 30 seconds for up to 30 minutes:

```yaml
# sdlc-pipeline.yml:704-705
MAX_WAIT_SECONDS=1800
POLL_INTERVAL=30
```

The polling loop:
1. Fetches check runs from `repos/{repo}/commits/{sha}/check-runs`
2. Filters out the SDLC pipeline's own checks to avoid self-deadlock
3. Waits for all checks to complete with `status == "completed"`
4. Checks that no checks failed (excluding `skipped`, `neutral`, `cancelled`)
5. Verifies no automated reviewer has `CHANGES_REQUESTED`
6. Sets `passed=true` output when all conditions are met

The `finalize-pr` job (lines 801-937) depends on `wait-for-checks.outputs.all_passed == 'true'` and calls `gh pr ready "${PR_NUMBER}"` to convert the draft to ready-for-review.

**Issues with current approach:**
- **Resource waste**: Workflow runner is held for up to 30 minutes polling
- **Billing impact**: GitHub Actions minutes consumed during idle polling
- **Timeout risk**: If checks take longer than 30 minutes, the PR stays in draft
- **Race conditions**: Potential for missed state transitions between polls
- **Complexity**: The polling logic handles many edge cases (zero checks, autofix cycles, review states)

## Constraints

- **GitHub Actions limitations**: `check_suite` and `check_run` events only fire for the repository's own workflows, not for checks created by third-party apps or other repos
- **workflow_run limitations**: Only triggers on workflows defined in the same repository, not external status checks
- **Draft PR state**: Only the `gh pr ready` command (or equivalent API call) can convert draft PRs
- **Existing patterns**: The codebase already uses `workflow_run` events for check failure handling (`on-check-failure.yml`), which works well for known workflow names
- **Review bot coordination**: Automated reviewers post reviews with `egg-automated-review` marker; the system must wait for all reviewers before proceeding
- **Self-deadlock risk**: The pipeline must not wait on its own checks (currently handled by filtering `SELF_WORKFLOW`)

## Options Considered

### Option A: Event-driven workflow using workflow_run completed event

**Approach**: Create a new workflow that triggers on `workflow_run` events when the relevant CI workflows (Lint, Test, Code Review, Contract Verification) complete. This workflow would:
1. Check if all relevant workflows have completed successfully for the PR
2. Verify automated reviewers have approved (no `CHANGES_REQUESTED`)
3. If conditions are met, mark the PR ready and post to the issue

**Pros**:
- Fully async, no polling or busy-wait
- Immediate reaction to check completion
- No wasted runner time
- Follows existing pattern (`on-check-failure.yml` uses this approach)

**Cons**:
- Requires enumerating all workflow names that must pass (fragile if workflows are added/renamed)
- `workflow_run` only works for workflows in the same repo; external status checks from GitHub Apps wouldn't trigger it
- Multiple workflow completions would trigger multiple runs; need deduplication logic
- Complex coordination: must aggregate state across multiple workflow runs

### Option B: Periodic GitHub Actions cron job

**Approach**: Run a scheduled workflow (e.g., every 5 minutes) that scans all draft SDLC PRs and checks if their checks have passed.

**Pros**:
- Completely decoupled from the main pipeline
- Handles all edge cases uniformly
- Simple conceptual model

**Cons**:
- Up to 5-minute delay between checks passing and PR being marked ready
- Still requires polling, just at a different level
- Consumes runner minutes even when no PRs need processing
- Doesn't fundamentally solve the async problem

### Option C: Hybrid approach - Short poll in pipeline + event-driven fallback

**Approach**: Keep the `wait-for-checks` job but reduce the timeout to 5-10 minutes. Add a separate event-driven workflow (`on-check-success.yml`) that:
1. Triggers on `workflow_run: [completed]` for known CI workflows
2. Checks if this is an SDLC PR (has `egg-sdlc` label)
3. If all checks now pass and reviewers approve, marks PR ready
4. Posts to the issue

This acts as a "catch-up" mechanism for PRs that didn't get marked ready in time.

**Pros**:
- Graceful degradation: fast path still works for quick checks
- Fallback handles long-running checks
- Reduces but doesn't eliminate polling overhead
- Lower risk of breakage during transition

**Cons**:
- More complexity (two mechanisms)
- Some polling remains
- Transition period where both systems are active

### Option D: Repository dispatch or webhook-based approach

**Approach**: Have each CI workflow that completes successfully dispatch a `repository_dispatch` event. A single handler workflow listens for these events and aggregates them to determine when all checks pass.

**Pros**:
- Full control over event emission
- Can work with any workflow or external system that can dispatch events
- Clean separation of concerns

**Cons**:
- Requires modifying each CI workflow to emit events
- State aggregation is complex (which checks have passed for which PR?)
- Need persistent state storage (issue comments, workflow artifacts, or external DB)
- Over-engineered for the problem

## Recommended Approach

**Option C: Hybrid approach** is recommended because:

1. **Incremental migration**: Reduces risk by keeping the existing path functional while adding the async mechanism
2. **Immediate improvement**: Reduces polling timeout from 30 minutes to 5-10 minutes, saving runner minutes
3. **Event-driven foundation**: The new `on-check-success.yml` workflow establishes the async pattern
4. **Proven pattern**: Mirrors the existing `on-check-failure.yml` structure
5. **Graceful fallback**: If checks pass within the short window, the current path works; otherwise the event-driven path catches it

**Implementation outline:**

1. **Reduce `wait-for-checks` timeout** from 30 to 10 minutes (lines 704 in `sdlc-pipeline.yml`)
2. **Create `on-check-success.yml`** triggered by `workflow_run: [Lint, Test, "egg: Code Review", "egg: Contract Verification"] types: [completed]`
3. **In `on-check-success.yml`**:
   - Check if the triggering PR has `egg-sdlc` label
   - Check if PR is still in draft state
   - Verify all relevant checks have passed
   - Verify no automated reviewer has `CHANGES_REQUESTED`
   - If all conditions met, call `gh pr ready` and post to issue
4. **Add concurrency group** to prevent duplicate runs for the same PR

**Future evolution**: Once the event-driven path proves reliable, the polling timeout could be reduced further or eliminated entirely.

## Open Questions

- Are there any external status checks (from GitHub Apps or integrations outside this repo) that must pass before marking a PR ready? If so, Option A and C won't fully cover those cases, and we may need a different approach for external checks.

- Should the reduced timeout (10 minutes) be configurable per-issue or kept as a global default?

---

*Authored-by: egg*
