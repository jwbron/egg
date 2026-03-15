# Analysis: SDLC pipeline checks not re-run after autofixer

> Issue: #485 | Phase: refine

## Problem Statement

When lint, test, or integration checks fail during the implement phase of the SDLC pipeline, the autofixer is triggered but checks are never re-run to verify the fixes. The pipeline proceeds to the review phase as if checks passed, meaning broken code can reach human review.

**Current state**: Check failures trigger autofix, but autofix completion doesn't trigger re-validation.

**Desired outcome**: The pipeline should re-run checks after autofix completes, only proceeding to review when checks actually pass (or escalating after max retries).

## Current Behavior

The check-fix-recheck loop in `sdlc-work-loop.yml` is broken in three places:

### 1. Fire-and-forget autofix dispatch (`check-fixer` job, lines 626-678)

The fixer triggers `reusable-autofix.yml` via `gh workflow run` which is asynchronous:

```yaml
gh workflow run reusable-autofix.yml \
  --repo "${{ github.repository }}" \
  --field pr_number="${ACTUAL_PR_NUMBER}" \
  --field failed_workflow="${FAILED_CHECKS}"
echo "fixed=pending" >> "$GITHUB_OUTPUT"
```

The job immediately outputs `fixed=pending` without waiting for the autofix workflow to complete or succeed. There is no mechanism to:
- Wait for the autofix workflow to finish
- Capture whether autofix succeeded or failed
- Get the commit SHA of any fixes pushed

### 2. Aggregate treats "pending" as passing (`aggregate-checks` job, lines 744-756)

When `fixer_status == "pending"`, the aggregate job skips marking lint/test as failed:

```bash
if [[ "$LINT_PASSED" == "false" && "$FIXER_STATUS" != "pending" ]]; then
  ALL_PASSED="false"
fi
```

This logic means:
- `LINT_PASSED=false` + `FIXER_STATUS=pending` → `ALL_PASSED=true` (incorrect)
- The same applies to test and integration checks

The aggregate job outputs `all_passed=true` even though checks failed and the fixer hasn't completed yet.

### 3. No re-check dispatch after autofix (`reusable-autofix.yml`, lines 232-250)

After the autofix agent pushes fixes, the workflow posts a comment and exits:

```yaml
- name: Post result comment
  if: always() && !cancelled() && steps.skip-check.outputs.skip != 'true'
  run: |
    # ... posts comment about autofix completion ...
```

There is no step to:
- Re-trigger lint/test/integration checks
- Re-dispatch the work loop with a "checks-only" mode
- Notify the work loop that autofix is complete

The `sdlc-work-loop.yml` is only triggered via `workflow_dispatch`/`workflow_call`, not by push events, so the push from the autofixer does not automatically re-trigger checks.

## Constraints

- **GitHub Actions limitations**: `workflow_run` can trigger on workflow completion, but this pattern is already used by `on-check-failure.yml` for the standalone autofix flow (outside SDLC pipeline)
- **Concurrency**: The work loop uses `concurrency: sdlc-work-loop-${{ inputs.issue_number }}` with `cancel-in-progress: false`, which means a re-dispatch won't cancel an in-progress run
- **Circuit breaker exists**: The contract already has a `circuit_breaker` structure with `total_cycles` and `max_total_cycles` (default 10), but this only applies to review cycles, not autofix cycles
- **Autofix has no retry limit**: Currently there is no max attempts for autofix - it could theoretically loop forever if fixes don't resolve the issue
- **Parallel checks**: Lint, test, and integration run in parallel, so a single failure could trigger autofix while other checks are still running

## Options Considered

### Option A: Re-dispatch from autofix workflow

**Approach**: After the autofix agent pushes fixes, have `reusable-autofix.yml` trigger a new run of the work loop (or just the check jobs) with a "checks-only" flag.

**Implementation**:
1. Add a step to `reusable-autofix.yml` that dispatches `sdlc-work-loop.yml` after successful push
2. Add a new input `mode: "checks-only"` to `sdlc-work-loop.yml` that skips the work phase and only runs checks
3. Track autofix attempts in the contract to enforce a max retry limit
4. Update `aggregate-checks` to fail when `fixer_status=pending` (remove the special case)

**Pros**:
- Minimal changes to existing job structure
- Autofix workflow remains reusable for both SDLC and standalone use
- Clear separation of concerns (autofix doesn't need to know about work loop internals)
- Avoids long-running synchronous waits in the workflow

**Cons**:
- Requires the autofix workflow to know which workflow to re-dispatch (coupling)
- Multiple workflow runs for a single issue (harder to follow in UI)
- Need to handle race conditions if original work loop hasn't finished yet
- Context about which checks failed is lost between runs

### Option B: Synchronous fixer with inline re-check loop

**Approach**: Instead of dispatching autofix as a separate workflow, run the fixer inline within `sdlc-work-loop.yml` and loop back to re-run checks within the same workflow run.

**Implementation**:
1. Replace `gh workflow run reusable-autofix.yml` with inline steps that call the autofix action directly
2. Add a retry loop around the check jobs that runs checks → fix → checks until passing or max attempts
3. Track attempt count in job outputs (not persistent contract state)
4. Only dispatch the work loop once; all retries happen within that run

**Pros**:
- Single workflow run contains the entire fix-recheck cycle (easier to debug/monitor)
- No race conditions between workflows
- Simpler state management (no cross-workflow coordination)
- Context is preserved (failed check outputs available to fixer)

**Cons**:
- Significant refactoring of work loop job structure
- GitHub Actions doesn't support true job loops; would need matrix/reusable-workflow tricks
- Longer-running workflow (may hit timeout limits)
- Autofix logic duplicated between inline and reusable versions
- The inline approach limits future parallelization

### Option C: Hybrid - Synchronous wait with re-dispatch

**Approach**: Keep autofix as a separate workflow, but have the `check-fixer` job wait for it to complete synchronously using `gh run watch`, then re-dispatch the work loop.

**Implementation**:
1. After `gh workflow run reusable-autofix.yml`, immediately find the triggered run and use `gh run watch` to wait for completion
2. If autofix succeeded, increment attempt counter and re-dispatch work loop in checks-only mode
3. If autofix failed or max attempts reached, escalate
4. Track autofix attempts in the contract

**Pros**:
- Autofix remains a reusable workflow
- Work loop controls the re-dispatch logic (better encapsulation)
- Clear failure handling in one place
- Similar pattern already used in `check-multi-agent` step (lines 385-420)

**Cons**:
- Long-running job while waiting for autofix (may hit job timeouts)
- If autofix hangs, the wait blocks the workflow
- Still results in multiple workflow runs

## Recommended Approach

**Option C (Hybrid - Synchronous wait with re-dispatch)** is recommended for the following reasons:

1. **Proven pattern**: The codebase already uses `gh run watch` for synchronous waiting in the multi-agent step (`sdlc-work-loop.yml:385-420`), so this pattern is established.

2. **Preserves reusability**: The `reusable-autofix.yml` workflow remains usable for both SDLC and standalone flows without modification to its core logic.

3. **Centralized control**: The work loop controls retry logic, which means:
   - Easier to enforce max attempts
   - Consistent escalation behavior
   - All retry state in one workflow

4. **Clear semantics**: Instead of `fixed=pending` (ambiguous), we get `fixed=true|false` after waiting, making aggregate logic straightforward.

### High-Level Implementation Plan

1. **Update `check-fixer` job** to:
   - After triggering autofix, use `gh run watch` to wait for completion (with timeout)
   - Output `fixed=true` on success, `fixed=false` on failure
   - Track autofix attempts in the contract

2. **Update `aggregate-checks` job** to:
   - Remove the `FIXER_STATUS != "pending"` special case
   - Simply check if all checks passed; if fixer ran and checks still fail, `all_passed=false`

3. **Add a `re-dispatch-checks` job** that:
   - Runs after aggregate-checks when `needs_fixer=true` and `fixed=true`
   - Re-dispatches `sdlc-work-loop.yml` with a new `mode: checks-only` input
   - Respects max autofix attempts from contract (suggest: 3 attempts)

4. **Update contract schema** to track:
   - `autofix_attempts: number` - current count of autofix attempts for this phase
   - `max_autofix_attempts: number` - configurable limit (default: 3)

5. **Add `checks-only` mode** to work loop:
   - When `mode=checks-only`, skip the `work` job entirely
   - Run only: `check-lint`, `check-test`, `check-integration`, `aggregate-checks`, `review-setup`, etc.

## Open Questions

The following questions should be addressed before implementation:

1. **Timeout for autofix wait**: The multi-agent pattern uses an implicit timeout via `gh run watch`. Should autofix have an explicit timeout? Suggested default: 30 minutes.

2. **What happens if checks pass after autofix but new issues are introduced?** Should the review phase re-evaluate the full diff, or trust that passing checks means the code is ready?

3. **Should autofix attempts persist across manual retries?** If a human manually re-triggers the pipeline, should the autofix counter reset?

---

*Authored-by: egg*
