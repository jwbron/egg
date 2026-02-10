# Analysis: Run self-improvement workflow after every workflow completes

> Issue: #289 | Phase: refine

## Problem Statement

The self-improvement workflow currently runs only on a nightly cron schedule (2 AM UTC), analyzing the previous 24 hours of GitHub Actions runs. This creates a significant feedback delay: failures occurring at 3 AM aren't surfaced until the next night's run, and similar failures throughout the day repeat without benefit of analysis.

The goal is to create a tighter feedback loop by triggering self-improvement analysis on each workflow completion, while adding selective heuristics to reduce token usage and controlling for cost/concurrency concerns.

## Current Behavior

The self-improvement workflow (`self-improvement.yml:9-12`) uses:
- **Nightly cron**: `0 2 * * *` (2 AM UTC daily)
- **Manual trigger**: `workflow_dispatch` with configurable `since_hours` parameter

The workflow collects data from four egg workflows defined in `config.py:15-20`:
- `on-mention.yml`
- `on-pull-request.yml`
- `on-check-failure.yml`
- `self-improvement.yml` (self-referential)

Data collection in `collect.py` already supports:
- Variable time windows via `--since-hours`
- Partitioning runs for parallel egg instances (`MAX_RUNS_PER_PARTITION = 5`)
- Log truncation to manage context size

The `analyze` job uses `max-parallel: 3` to limit concurrent egg instances within a single self-improvement run (`self-improvement.yml:270`).

## Constraints

**Technical constraints:**
- GitHub Actions `workflow_run` trigger only fires for workflows in the default branch
- `workflow_run` event provides metadata about the triggering run via `github.event.workflow_run`
- Concurrent self-improvement runs could create race conditions for issue creation/updates
- Each egg invocation has API and compute costs (Anthropic API tokens, GitHub Actions minutes)

**Business constraints:**
- Must not regress nightly batch analysis capability (cross-run pattern detection)
- Should reduce per-run analysis scope to control costs
- Should avoid noisy duplicate issues for transient failures

**Dependencies:**
- Data collection module already supports scoped time windows
- The `on-check-failure.yml` workflow already demonstrates the `workflow_run` pattern for this repo

## Options Considered

### Option A: Trigger on All Workflow Completions

**Approach**: Add `workflow_run` trigger for all egg workflows (on-mention, on-pull-request, on-check-failure) that fires on completion (success or failure). Scope analysis to only the single run that just completed.

```yaml
on:
  workflow_run:
    workflows:
      - "egg: Respond to @mention"
      - "egg: Code Review"
      - "egg: Autofix Check Failures"
    types: [completed]
  schedule:
    - cron: "0 2 * * *"  # Keep nightly for cross-run pattern detection
```

**Pros**:
- Fastest feedback loop — issues surfaced immediately
- Every run gets analyzed while context is fresh
- Captures both failures and subtle issues in successful runs

**Cons**:
- Highest cost — every workflow completion spawns an egg instance
- May generate noise for transient failures
- Could overwhelm the self-improvement issue tracker

### Option B: Trigger Only on Workflow Failures

**Approach**: Add `workflow_run` trigger but filter to only run when the triggering workflow failed. Keep nightly for comprehensive analysis.

```yaml
on:
  workflow_run:
    workflows: [...]
    types: [completed]  # Must be completed; filter on conclusion in job
```

Gate job:
```yaml
if: github.event.workflow_run.conclusion == 'failure'
```

**Pros**:
- Significantly reduced cost — only failures trigger analysis
- Fastest feedback for the highest-value cases (failures)
- Maintains nightly batch for pattern detection in successful runs

**Cons**:
- Misses issues in successful runs (tool call errors, warnings, suboptimal behavior)
- Still may trigger frequently in high-churn periods

### Option C: Trigger on Failures + Error-Heuristic Filtering

**Approach**: Similar to Option B, but enhance the data collection phase to grep logs for error patterns before spawning egg. Only invoke egg if the logs contain actionable signals (e.g., tool failures, gateway errors, specific error keywords).

```yaml
# In collect step, before invoking egg:
if grep -qE "(ToolError|GatewayTimeout|APIError|CRITICAL)" logs.txt; then
  # Proceed with egg analysis
fi
```

**Pros**:
- Most cost-effective — filters out failures with no actionable content
- Reduces noise from infrastructure-level failures (e.g., GitHub rate limits)
- Still captures high-signal failures

**Cons**:
- Requires maintaining error pattern list (could miss new error types)
- Adds complexity to workflow
- False negatives if error patterns aren't comprehensive

### Option D: Debounced Trigger with Cooldown

**Approach**: Trigger on workflow completion but use a concurrency group with a debounce mechanism. If a self-improvement run is already in progress or recently completed, skip the new trigger.

```yaml
concurrency:
  group: self-improvement-realtime
  cancel-in-progress: false  # Queue instead of cancel
```

Combined with checking if last analysis was within N minutes (e.g., 30 min) and skipping if so.

**Pros**:
- Batches closely-spaced failures into single analysis
- Reduces cost in high-activity periods
- Still provides faster feedback than nightly-only

**Cons**:
- More complex implementation (need state tracking)
- May delay feedback for legitimate new failures
- Doesn't address the high-volume problem, just smooths it

## Recommended Approach

**Option B: Trigger Only on Workflow Failures** is recommended as the best balance of value and cost.

**Rationale:**
1. **Cost control**: Failed runs are a small subset of total runs, significantly reducing egg invocations
2. **Highest-value feedback**: Failures are where immediate feedback matters most
3. **Proven pattern**: `on-check-failure.yml` already uses `workflow_run` with conclusion filtering
4. **Maintains cross-run analysis**: Nightly schedule continues to catch patterns in successful runs
5. **Simple implementation**: No new dependencies or state tracking needed

**Implementation outline:**
1. Add `workflow_run` trigger to `self-improvement.yml`
2. Add gate job that checks `github.event.workflow_run.conclusion == 'failure'`
3. Pass `--since-hours 1` or scope to single run ID when triggered by `workflow_run`
4. Add concurrency group to prevent parallel self-improvement runs
5. Keep cron schedule with full 24-hour analysis window

**Concurrency strategy:**
```yaml
concurrency:
  group: self-improvement-${{ github.event_name == 'workflow_run' && 'realtime' || 'nightly' }}
  cancel-in-progress: false
```
This queues realtime runs (ensuring all failures get analyzed) while allowing nightly to run independently.

**Future enhancement path:** If Option B proves stable, Option C (error-heuristic filtering) could be layered on as an optimization to further reduce low-value invocations.

## Open Questions

1. **Scope for triggered runs**: When triggered by `workflow_run`, should the analysis:
   - (a) Analyze only the single failed run (lowest cost, most focused)
   - (b) Analyze a short time window (e.g., 1 hour) to catch related failures
   - (c) Analyze since last successful self-improvement run

2. **Cost budget**: Is there a maximum acceptable number of self-improvement runs per day? This would inform whether additional throttling is needed.

3. **Self-improvement of self-improvement**: Should self-improvement failures also trigger immediate re-analysis, or rely on nightly to avoid infinite loops?

---

*Authored-by: egg*
