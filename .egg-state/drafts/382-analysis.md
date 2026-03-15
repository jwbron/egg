# Analysis: Address-feedback bot should only run once all auto-reviewers have run

> Issue: #382 | Phase: refine

## Problem Statement

Currently, the address-feedback bot (`on-review-feedback.yml`) triggers **immediately** when any auto-reviewer posts feedback. This creates a race condition where:

1. Multiple reviewers (e.g., `review`, `agent-mode-design`, `contract-verification`) trigger in parallel on a PR
2. The first reviewer to post feedback triggers the address-feedback bot
3. The address-feedback bot starts working before other reviewers have finished
4. When other reviewers post their feedback, the bot either:
   - Gets cancelled (due to `cancel-in-progress: true` concurrency), wasting work
   - Misses feedback from reviewers that haven't finished yet

The desired behavior is for the address-feedback bot to **wait until all auto-reviewers have completed** before starting to address their combined feedback in a single pass.

## Current Behavior

The address-feedback workflow is defined in `.github/workflows/on-review-feedback.yml`.

**Trigger mechanism (lines 10-20):**
- `pull_request_review.submitted` - triggers when a formal review is posted
- `issue_comment.created` - triggers when a self-review is posted as a comment
- Both events trigger the workflow immediately

**Trigger filter (lines 33-47):**
```yaml
if: >-
  github.event_name == 'workflow_dispatch' ||
  (
    github.event_name == 'pull_request_review' &&
    (github.event.review.user.login == 'james-in-a-box' || ...) &&
    contains(github.event.review.body, 'egg-automated-review') &&
    github.event.review.state != 'approved'
  ) || ...
```
The workflow triggers for **any single automated review** containing the `egg-automated-review` marker.

**Concurrency (lines 54-56):**
```yaml
concurrency:
  group: egg-feedback-${{ github.event.pull_request.number || ... }}
  cancel-in-progress: true
```
This cancels in-progress runs when a new review arrives, but doesn't wait for all reviewers.

**Current auto-reviewers (run in parallel):**
| Bot Name | Workflow | Trigger |
|----------|----------|---------|
| `review` | `on-pull-request.yml` | All PRs |
| `agent-mode-design` | `on-pull-request-agent-mode-design.yml` | Path-filtered (action/**, workflows, etc.) |
| `contract-verification` | `on-pull-request-contract-verify.yml` | Label-gated (`egg-sdlc`) |

Each reviewer runs independently via `reusable-review.yml` with its own concurrency group (`egg-${{ bot_name }}-${{ pr_number }}`), allowing parallel execution.

## Constraints

**Technical constraints:**
- GitHub Actions has no native "wait for other workflows" primitive
- The check runs API can list checks but requires polling
- Reviews are posted asynchronously as each reviewer completes
- Some reviewers may not run (path filters, label gates)
- Must handle reviewers that approve (no feedback to address)

**Existing patterns:**
- `reusable-review.yml` already has a `wait-for-checks` job that polls the check runs API
- `sdlc-pipeline.yml` has a similar wait loop that excludes review workflows to avoid deadlock

**Race condition window:**
- Between detecting "all reviews complete" and starting to address feedback, new reviews could arrive
- The `cancel-in-progress` concurrency should handle this, but there's a brief window

**Backwards compatibility:**
- The marker format (`<!-- egg-automated-review bot=<name> ... -->`) is stable and used for detection

## Options Considered

### Option A: Poll check runs before addressing feedback

**Approach**: Add a step to `on-review-feedback.yml` that polls the GitHub check runs API and waits for all review-related checks to complete before proceeding.

**Implementation:**
1. After the workflow triggers, add a "Wait for all reviewers" step
2. Poll `repos/{owner}/{repo}/commits/{sha}/check-runs` for checks matching review workflow names
3. Wait until all reviewer checks are completed or skipped
4. Then proceed to address the accumulated feedback

**Pros:**
- Minimal changes to existing workflow structure
- Reuses proven pattern from `reusable-review.yml` and `sdlc-pipeline.yml`
- No changes needed to reviewer workflows
- Naturally handles reviewers that don't run (path filters exclude them from check runs)

**Cons:**
- Requires polling (30-second intervals typical)
- First review trigger still starts a runner, then waits
- Check names must be kept in sync with workflow names
- Timeout handling needed for slow reviewers

### Option B: Aggregate reviews via a coordination workflow

**Approach**: Create a new coordination workflow that collects all automated reviews and triggers the address-feedback bot only after all reviewers have posted.

**Implementation:**
1. Create a `review-complete` custom event or use `workflow_run` events
2. Each reviewer workflow dispatches to the coordinator after posting
3. Coordinator tracks which reviewers have completed
4. Once all expected reviewers are done, triggers address-feedback

**Pros:**
- Event-driven rather than polling
- Clear separation of concerns
- Could enable future features like review aggregation summaries

**Cons:**
- Significant complexity increase
- Need to track "expected reviewers" per PR (path filters, labels)
- Custom event coordination is fragile
- More workflows to maintain

### Option C: Debounce with delayed start

**Approach**: When the address-feedback workflow triggers, wait a fixed debounce period (e.g., 60 seconds) before starting work. Cancel if a newer trigger arrives during the wait.

**Implementation:**
1. At the start of address-feedback, sleep for a debounce period
2. Use `cancel-in-progress: true` to ensure only the latest trigger proceeds
3. After debounce, the last trigger runs (by which time all reviewers should be done)

**Pros:**
- Very simple implementation (just add `sleep`)
- Works with existing concurrency settings
- No polling or coordination needed

**Cons:**
- Arbitrary delay slows feedback loop even when only one reviewer runs
- Doesn't guarantee all reviewers are done (just probabilistically)
- If reviewers are slow, debounce may not be long enough
- Wastes runner time on cancelled runs

### Option D: Use `workflow_run` event chaining

**Approach**: Change the address-feedback workflow to trigger on `workflow_run` completion events from the reviewer workflows, rather than on `pull_request_review` events.

**Implementation:**
1. Change trigger to `workflow_run` with conditions for review workflows completing
2. Check if all expected reviewer workflows have completed
3. If not all complete, exit early (next `workflow_run` will check again)

**Pros:**
- Uses native GitHub Actions event for workflow completion
- Avoids polling the check runs API directly

**Cons:**
- `workflow_run` only triggers for default branch workflows (may not work for fork PRs or feature branches)
- Still need logic to determine "all reviewers done"
- Complex conditions in `workflow_run` trigger

## Recommended Approach

**Option A: Poll check runs before addressing feedback** is recommended.

**Rationale:**
1. **Proven pattern**: The codebase already has two working implementations of this approach (`reusable-review.yml:80-169` and `sdlc-pipeline.yml:619-768`). We can adapt this tested logic.

2. **Minimal invasiveness**: Only modifies `on-review-feedback.yml`. No changes to reviewer workflows or new coordination mechanisms.

3. **Handles dynamic reviewer set**: Path filters and label gates mean different PRs have different reviewers. Polling check runs naturally handles this - it only waits for checks that actually exist.

4. **Existing timeout handling**: The pattern includes timeout logic for slow/stuck reviewers.

5. **Correct semantics**: The feedback bot wants to know "are all reviewers done?" - this is exactly what the check runs API answers.

**Implementation outline:**
```yaml
- name: Wait for all reviewer checks to complete
  env:
    GH_TOKEN: ${{ steps.bot-token.outputs.token }}
  run: |
    HEAD_SHA=$(gh api "repos/${{ github.repository }}/pulls/${PR_NUMBER}" --jq '.head.sha')
    MAX_WAIT=600  # 10 minutes
    INTERVAL=30

    while true; do
      # Get review-related checks (Code Review, Design Review, Contract Verification)
      reviewer_checks=$(gh api "repos/${{ github.repository }}/commits/${HEAD_SHA}/check-runs" \
        --jq '.check_runs | [.[] | select(.name | test("Code Review|Design Review|Contract Verification"))]')

      total=$(echo "$reviewer_checks" | jq 'length')
      completed=$(echo "$reviewer_checks" | jq '[.[] | select(.status == "completed")] | length')

      if [[ "$total" -gt 0 && "$completed" -eq "$total" ]]; then
        echo "All $total reviewer checks completed"
        break
      fi

      # ... timeout handling ...
      sleep $INTERVAL
    done
```

## Open Questions

The following questions may help clarify implementation details:

1. **Timeout behavior**: If a reviewer check times out or fails, should the address-feedback bot still run? Current recommendation is yes - address whatever feedback was posted.

2. **Reviewer registry**: Should the list of reviewer check names be centralized in a config file, or is hardcoding in the workflow acceptable? The current three reviewers (`Code Review`, `Design Review`, `Contract Verification`) rarely change.

3. **Edge case - no reviewers trigger**: For PRs where no path-filtered reviewers run (e.g., docs-only changes that only trigger the general `review` bot), the wait logic should proceed as soon as the only applicable reviewer completes. The check runs API naturally handles this.

---

*Authored-by: egg*
