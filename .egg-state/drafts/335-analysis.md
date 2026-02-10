# Analysis: Re-enable and Dial in Self-Reflection Workflow

> Issue: #335 | Phase: refine

## Problem Statement

The self-improvement workflow currently runs only on a nightly schedule (2 AM UTC), which means issues discovered from failed workflows can be up to 24 hours stale. Issue #289 proposed running self-improvement after every workflow completes to create a tighter feedback loop. Issue #335 takes a conservative first step: **only run self-reflection on failed workflows** to capture the highest-value feedback while limiting cost and noise.

**Current state:** Self-improvement runs nightly, analyzing all egg workflow runs from the past 24 hours.

**Desired outcome:** Self-improvement also runs immediately after any egg workflow fails, providing timely analysis while the context is fresh.

## Current Behavior

The self-improvement workflow (`self-improvement.yml`) currently:

1. **Triggers:** Nightly at 2 AM UTC via `schedule`, or manually via `workflow_dispatch`
2. **Collects:** All egg workflow runs (on-mention, on-pull-request, on-check-failure, self-improvement) from the past 24 hours
3. **Partitions:** Splits runs into batches (max 5 per partition) for parallel analysis
4. **Analyzes:** Examines both failed AND successful runs for issues, patterns, and concerns
5. **Creates issues:** Files GitHub issues with `self-improvement` label for actionable findings

Key code locations:
- Workflow: `.github/workflows/self-improvement.yml`
- Collection: `sandbox/egg_lib/self_improvement/collect.py:132-181`
- Config: `sandbox/egg_lib/self_improvement/config.py:15-20` (defines `EGG_WORKFLOWS`)

## Constraints

**Technical:**
- GitHub's `workflow_run` trigger can filter by completion type (`completed`) but cannot natively filter by conclusion (`failure` vs `success`) in the trigger itself — filtering must happen in a job step
- The workflow already supports `workflow_call` for reuse (lines 24-49)
- The data collection module collects both failed and successful runs; a failure-only analysis would need scoping

**Cost:**
- Each self-improvement run spins up an egg instance with API usage
- High-volume repos could see significant cost increase if triggering on every completion
- Failure-only trigger significantly reduces volume (failures are the minority case)

**Concurrency:**
- Multiple workflows failing near-simultaneously could spawn parallel self-improvement runs
- Existing `max-parallel: 3` limits parallelism within a single run but not across triggered runs

**Dependencies:**
- The `on-check-failure.yml` workflow already demonstrates the pattern for filtering `workflow_run` by conclusion (lines 31-56)

## Options Considered

### Option A: Add `workflow_run` Trigger with Failure Filter

**Approach:** Add a `workflow_run` trigger to `self-improvement.yml` that fires on completion of egg workflows. Use a gate job (similar to `on-check-failure.yml`) to check `github.event.workflow_run.conclusion == 'failure'` and skip if not a failure.

**Implementation:**
1. Add `workflow_run` trigger watching `["egg: On Mention", "egg: On Pull Request", "egg: Autofix Check Failures"]`
2. Add a gate job that checks conclusion and outputs whether to run
3. Modify the `collect` job to only fetch the triggering run when invoked via `workflow_run`
4. Keep nightly schedule for batch/pattern analysis

**Pros:**
- Immediate feedback on failures (within minutes)
- Uses proven pattern from `on-check-failure.yml`
- Minimal cost impact (only triggers on failures)
- Keeps nightly run for cross-run pattern detection

**Cons:**
- Adds complexity to the workflow
- May need concurrency group to prevent parallel runs
- Need to scope collection to single run for `workflow_run` trigger

### Option B: Create Separate On-Failure Workflow

**Approach:** Create a new `self-improvement-on-failure.yml` workflow that only handles failure-triggered analysis. Keep `self-improvement.yml` unchanged for nightly batch analysis.

**Implementation:**
1. New workflow with `workflow_run` trigger + failure filter
2. Passes single run to existing collection/analysis infrastructure
3. Uses `workflow_call` to reuse `self-improvement.yml` with `since_hours: 1`

**Pros:**
- Clean separation of concerns
- Easier to disable/tune independently
- No changes to battle-tested nightly workflow

**Cons:**
- More files to maintain
- Some duplication of trigger/gate logic
- May still run nightly analysis on runs already analyzed

### Option C: Workflow Call from Each Egg Workflow on Failure

**Approach:** Modify each egg workflow (on-mention, on-pull-request, etc.) to call self-improvement as a reusable workflow when they fail.

**Implementation:**
1. Add a final job to each egg workflow that runs on failure
2. Uses `workflow_call` to invoke self-improvement with scoped parameters
3. Pass run ID or short time window to focus analysis

**Pros:**
- Self-contained failure handling per workflow
- Clear failure → analysis relationship
- No additional trigger complexity

**Cons:**
- Requires changes to multiple workflows
- Tightly couples workflows
- Harder to add/remove workflows from analysis

## Recommended Approach

**Option A: Add `workflow_run` Trigger with Failure Filter**

This is recommended because:

1. **Proven pattern:** The `on-check-failure.yml` workflow already demonstrates this exact approach, making implementation straightforward and low-risk.

2. **Minimal disruption:** The nightly workflow continues unchanged, providing a safety net for pattern detection across runs.

3. **Focused scope:** By checking `conclusion == 'failure'` in a gate job, we only incur costs when analysis is most valuable.

4. **Single file change:** All changes are contained in `self-improvement.yml`, reducing coordination overhead.

**Implementation details:**

```yaml
on:
  workflow_run:
    workflows:
      - "egg: On Mention"
      - "egg: On Pull Request"
      - "egg: Autofix Check Failures"
    types: [completed]
  schedule:
    - cron: "0 2 * * *"
  # ... existing workflow_dispatch and workflow_call
```

Add a gate job before `collect`:

```yaml
should-run:
  name: Check if analysis should run
  runs-on: ubuntu-latest
  outputs:
    run: ${{ steps.check.outputs.run }}
    scope: ${{ steps.check.outputs.scope }}
  steps:
    - name: Check trigger
      id: check
      run: |
        if [[ "${{ github.event_name }}" == "workflow_run" ]]; then
          if [[ "${{ github.event.workflow_run.conclusion }}" == "failure" ]]; then
            echo "run=true" >> "$GITHUB_OUTPUT"
            echo "scope=single_run" >> "$GITHUB_OUTPUT"
          else
            echo "run=false" >> "$GITHUB_OUTPUT"
          fi
        else
          echo "run=true" >> "$GITHUB_OUTPUT"
          echo "scope=batch" >> "$GITHUB_OUTPUT"
        fi
```

For the single-run scope, modify collection to fetch only the failed run ID via `${{ github.event.workflow_run.id }}`.

**Concurrency guard (optional):**

```yaml
concurrency:
  group: self-improvement-${{ github.event.workflow_run.id || github.run_id }}
  cancel-in-progress: false
```

## Open Questions

### Decision: Analyze Only the Failed Run or Recent Window?

When triggered by a workflow failure, should the analysis:

```
egg-contract add-decision --question "When triggered by workflow failure, what scope should be analyzed?" \
  --options "Only the single failed run" "Failed run + runs from last 1-2 hours" "Failed run + all runs from same workflow" --format markdown
```

**Context:**
- **Single run:** Fastest, most focused, lowest cost. May miss related patterns.
- **Recent window:** Captures potential cascading failures. Slightly higher cost.
- **Same workflow:** Good for detecting flaky tests. Medium cost.

### Decision: Include Self-Improvement in the Trigger List?

Should a failed self-improvement run trigger another self-improvement analysis?

```
egg-contract add-decision --question "Should failed self-improvement runs trigger self-analysis?" \
  --options "Yes, include self-improvement" "No, exclude to prevent loops" --format markdown
```

**Context:**
- Including it enables true self-reflection (analyzing why analysis failed).
- Risk of loops is low since the gate job only fires on failures.
- The nightly run already analyzes self-improvement failures.

### Open-Ended: Concurrency and Cooldown

Should there be a cooldown period or concurrency limit to prevent multiple failures in quick succession from spawning many parallel analysis runs?

If so, what parameters would be appropriate (e.g., 5-minute cooldown, max 1 concurrent run)?

---

*Authored-by: egg*
