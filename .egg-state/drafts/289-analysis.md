# Analysis: Run self-improvement workflow after every workflow completes

> Issue: #289 | Phase: refine

## Problem Statement

The self-improvement workflow currently runs on a nightly cron schedule (2 AM UTC), analyzing the previous 24 hours of workflow runs. This creates a delayed feedback loop where issues discovered by the analysis can be up to 24 hours stale. If a workflow fails at 3 AM, the analysis won't surface it until the next night's run, and similar failures during that day repeat without the benefit of insights.

The proposal is to trigger self-improvement on every workflow completion to create a tighter feedback loop, while adding heuristics to reduce token usage (e.g., focusing on failed runs, grepping for errors).

## Current Behavior

**Self-Improvement Workflow** (`.github/workflows/self-improvement.yml:9-12`):
- Triggered by cron schedule: `0 2 * * *` (2 AM UTC daily)
- Supports `workflow_dispatch` for manual testing and `workflow_call` for reusability
- Default analysis window: 24 hours (`since_hours` parameter)

**Data Collection** (`sandbox/egg_lib/self_improvement/collect.py`):
- Collects runs from configured workflows: `on-mention.yml`, `on-pull-request.yml`, `on-check-failure.yml`, `self-improvement.yml`
- Fetches logs via `gh run view --log`
- Partitions runs into batches (max 5 runs per partition, max 3 parallel egg instances)
- Analyzes ALL runs (not just failed) because successful runs may have tool errors, warnings, or concerning patterns

**Related Workflows** (names needed for `workflow_run` trigger):
- `on-mention.yml`: Named "egg: Respond to @mention"
- `on-pull-request.yml`: Named "egg: Code Review"
- `on-check-failure.yml`: Named "egg: Autofix Check Failures"

## Constraints

- **API Cost**: Each self-improvement run spawns egg instances consuming Anthropic API tokens. High-volume repos could see significant cost increases.
- **Compute Cost**: Each run spins up GitHub Actions runners, consuming workflow minutes.
- **Concurrency**: Multiple workflows completing simultaneously could spawn parallel self-improvement runs, potentially overwhelming resources.
- **Existing `max-parallel: 3`**: The workflow already limits parallelism within a single run, but doesn't guard against multiple triggered runs.
- **Pattern Detection**: Single-run analysis may miss patterns that only emerge across multiple runs—the nightly batch is better for cross-run analysis.
- **Workflow Names**: The `workflow_run` trigger requires exact workflow names (not file names), so we must use "egg: Respond to @mention", "egg: Code Review", "egg: Autofix Check Failures".

## Options Considered

### Option A: Full `workflow_run` Trigger (Every Completion)

**Approach**: Add a `workflow_run` trigger that fires on completion (both success and failure) of all egg workflows. Pass the triggering run ID to scope analysis.

```yaml
on:
  workflow_run:
    workflows:
      - "egg: Respond to @mention"
      - "egg: Code Review"
      - "egg: Autofix Check Failures"
    types: [completed]
```

**Pros**:
- Immediate feedback on every run
- Fresh context for analysis
- Issues caught as they occur

**Cons**:
- High cost for active repos (runs on every completion)
- Most successful runs don't need analysis
- May create noise from analyzing transient issues
- Could overwhelm with parallel runs

### Option B: Failure-Only `workflow_run` Trigger

**Approach**: Trigger only on workflow failures, not all completions. Keep the nightly cron for cross-run pattern detection on successful runs.

```yaml
on:
  schedule:
    - cron: "0 2 * * *"
  workflow_run:
    workflows:
      - "egg: Respond to @mention"
      - "egg: Code Review"
      - "egg: Autofix Check Failures"
    types: [completed]
```
With a condition in the job:
```yaml
if: github.event.workflow_run.conclusion == 'failure' || github.event_name == 'schedule'
```

**Pros**:
- Immediate feedback on failures (highest-value cases)
- Lower volume than Option A
- Retains nightly for pattern detection across all runs
- Balanced cost/benefit tradeoff

**Cons**:
- May miss issues in successful runs (tool errors, warnings)
- Still needs concurrency control for burst failures

### Option C: Intelligent Filtering with Selective Heuristics

**Approach**: Trigger on all completions but add intelligent pre-filtering before spawning egg. Use lightweight checks (grep logs for errors, check conclusion) to decide whether full analysis is warranted.

**Implementation**:
1. Add `workflow_run` trigger for all completions
2. Add a "triage" job that quickly scans logs for:
   - Failed conclusion → always analyze
   - Error patterns in logs (tool failures, exceptions) → analyze
   - Clean successful run → skip analysis
3. Only spawn egg if triage finds something worth analyzing
4. Keep nightly cron as a comprehensive sweep

**Pros**:
- Captures both failures and problematic successful runs
- Reduces cost by filtering clean runs early
- Maintains comprehensive nightly analysis

**Cons**:
- More complex implementation
- Heuristics may miss subtle issues
- Triage job still consumes runner time

### Option D: Throttled Trigger with Cooldown

**Approach**: Trigger on failures with a cooldown/concurrency mechanism to prevent burst spawning.

```yaml
concurrency:
  group: self-improvement-triggered
  cancel-in-progress: false
```

Combined with a rate-limiting approach (e.g., skip if another self-improvement run completed within the last hour).

**Pros**:
- Prevents resource exhaustion from burst failures
- Still provides timely feedback
- Simple to implement

**Cons**:
- May delay analysis during failure bursts
- Doesn't address successful-run issues

## Recommended Approach

**Option B + Concurrency Controls**: Failure-only `workflow_run` trigger with the existing nightly cron retained.

**Rationale**:
1. **Highest-value feedback loop**: Failed runs are the primary source of actionable insights. Analyzing them immediately while context is fresh provides the most value.
2. **Cost-effective**: Only failures trigger immediate analysis, keeping API and compute costs proportional to problem frequency.
3. **Pattern detection preserved**: The nightly cron continues to analyze all runs (including successful ones with tool errors), providing cross-run pattern detection.
4. **Simple implementation**: Minimal changes to the existing workflow—just add the trigger and a condition.

**Implementation details**:
1. Add `workflow_run` trigger to `self-improvement.yml`
2. Add conditional logic to detect trigger type
3. For `workflow_run` triggers: analyze only the triggering run (not full 24-hour window)
4. For `schedule` triggers: continue analyzing full 24-hour window
5. Add concurrency group to prevent parallel triggered runs

**Scope for triggered runs**:
- Pass the `workflow_run.id` to the collect script
- Add `--run-id` parameter to collect only that specific run
- Set `since_hours=1` or similar small window as fallback

**Concurrency**:
```yaml
concurrency:
  group: self-improvement-${{ github.event_name }}
  cancel-in-progress: false
```
This allows scheduled and triggered runs to coexist but prevents multiple triggered runs from stacking.

## Open Questions

For questions that require human input before proceeding, please provide guidance:

1. **Cost tolerance**: What is the acceptable increase in API/compute costs for more frequent self-improvement runs? Should we add usage monitoring?

2. **Failure-only vs. all completions**: The proposal mentions analyzing failed runs for cost savings. Should we:
   - Start with failure-only triggers (Option B) and expand later if needed?
   - Implement intelligent filtering (Option C) from the start?

3. **Nightly scope change**: When the nightly cron runs, should it:
   - Continue analyzing ALL runs from the past 24 hours (current behavior)?
   - Exclude runs already analyzed by triggered runs (to avoid duplication)?

4. **Additional workflows**: Should other workflows be included in the trigger list? For example:
   - `on-merge-conflict.yml`
   - `on-review-feedback.yml`
   - SDLC workflows (`sdlc-pipeline.yml`, `sdlc-work-loop.yml`)

---

*Authored-by: egg*
