# Analysis: Handle merge conflicts in contract push-with-retry logic

> Issue: #385 | Phase: refine

## Problem Statement

The push-with-retry logic in the SDLC pipeline workflows fails when git rebase encounters a merge conflict on the contract JSON file. When the egg agent pushes commits to the branch while a workflow is running, the workflow's subsequent push attempt fails. The retry logic attempts `git pull --rebase`, but when both sides have modified the same contract JSON file, the rebase produces a merge conflict that leaves git in a broken state. Subsequent retry attempts fail because the worktree is in an unresolved mid-rebase state.

**Current state**: Push failures with concurrent branch modifications leave the pipeline stuck, losing the review state update.

**Desired outcome**: The retry logic should recover gracefully from merge conflicts by re-applying the contract transformation on top of the current remote HEAD.

## Current Behavior

The push-with-retry pattern appears in **9 locations** across two workflow files:

### sdlc-pipeline.yml (6 locations)

| Line | Context | Has Retry? | Notes |
|------|---------|------------|-------|
| ~201 | Initial branch push | Yes | Standard retry pattern |
| ~363 | Populate contract tasks | **No** | Direct push, no retry |
| ~436 | Checkpoint before timeout | Yes | Soft failure (warns, continues) |
| ~938 | Advance to PR phase | Yes | Standard retry pattern |
| ~1463 | Update refine review state | Yes | Weaker error handling (warns on rebase fail) |
| ~1782 | Populate contract from plan | Yes | Standard retry pattern |
| ~2142 | Update plan review state | Yes | Weaker error handling |

### sdlc-hitl.yml (3 locations)

| Line | Context | Has Retry? |
|------|---------|------------|
| ~228 | Resolve HITL decision | Yes |
| ~329 | Advance to next phase | Yes |
| ~550 | Approve and advance phase | Yes |

All retry-enabled locations use essentially the same pattern:

```bash
MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
  if git push origin "${BRANCH_NAME}"; then
    break
  elif [[ $i -eq $MAX_RETRIES ]]; then
    echo "Push failed after $MAX_RETRIES attempts"
    exit 1
  else
    git pull --rebase origin "${BRANCH_NAME}" || {
      echo "Rebase failed, cannot resolve conflict automatically"
      exit 1
    }
  fi
done
```

**Failure mode**: When rebase hits a conflict on the contract JSON file:
1. `git pull --rebase` exits non-zero
2. Git is left in a "rebasing" state with unmerged files
3. The error handler may exit (most locations) or warn and continue (some locations)
4. If it continues, subsequent push attempts fail with "non-fast-forward"
5. Subsequent `git pull --rebase` attempts fail with "cannot pull with rebase: you have unmerged files"

**Root cause**: The pattern assumes rebase will always succeed cleanly. JSON files are particularly conflict-prone since concurrent modifications to the same file will almost always conflict.

## Constraints

- **Atomicity**: Contract updates must be based on the current state of the contract, not stale data. Re-reading and re-applying the transformation ensures correctness.
- **Idempotency**: The same transformation applied twice should produce the same result. Most jq transformations in these workflows are idempotent (setting phase, adding audit entries with timestamps).
- **Workflow complexity**: There are 9 locations with similar but not identical patterns. Changes should be consistent across all locations.
- **No existing helper scripts**: The `.github/scripts/` directory does not exist. Creating it introduces a new pattern.
- **Testing**: GitHub Actions workflows are difficult to test in isolation. Changes should be conservative.

## Options Considered

### Option A: Reset-and-Reapply Pattern (Inline)

**Approach**: Modify each push-with-retry block to abort any in-progress rebase, reset to remote HEAD, and re-apply the jq transformation inline before retrying.

```bash
MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
  if git push origin "${BRANCH_NAME}"; then
    break
  elif [[ $i -eq $MAX_RETRIES ]]; then
    echo "::error::Push failed after $MAX_RETRIES attempts"
    exit 1
  else
    echo "Push failed (attempt $i/$MAX_RETRIES), resetting to remote..."
    git rebase --abort 2>/dev/null || true
    git fetch origin "${BRANCH_NAME}"
    git reset --hard "origin/${BRANCH_NAME}"
    # Re-apply the jq transformation
    jq '.current_phase = "pr"' "$CONTRACT_PATH" > /tmp/contract.json
    mv /tmp/contract.json "$CONTRACT_PATH"
    git add "$CONTRACT_PATH"
    git commit -m "Advance to PR phase for issue #${ISSUE_NUMBER}"
  fi
done
```

**Pros**:
- No new files or dependencies
- Changes are localized to each affected location
- Easy to understand the retry logic in context

**Cons**:
- Duplicates the jq transformation logic (once before loop, once in retry)
- Harder to maintain consistency across 9 locations
- Some transformations are complex (multi-step jq with audit entries)
- Increases code duplication in already large workflow files

### Option B: Shared Helper Script

**Approach**: Create `.github/scripts/push-contract-update.sh` that encapsulates the fetch-transform-commit-push-retry logic. Each workflow step calls the helper with parameters for the transformation.

```bash
# .github/scripts/push-contract-update.sh
#!/bin/bash
set -euo pipefail

BRANCH_NAME="$1"
CONTRACT_PATH="$2"
COMMIT_MESSAGE="$3"
JQ_FILTER="$4"

MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
  # Apply transformation
  jq "$JQ_FILTER" "$CONTRACT_PATH" > /tmp/contract.json
  mv /tmp/contract.json "$CONTRACT_PATH"
  git add "$CONTRACT_PATH"
  git commit -m "$COMMIT_MESSAGE" --allow-empty || true

  if git push origin "${BRANCH_NAME}"; then
    break
  elif [[ $i -eq $MAX_RETRIES ]]; then
    echo "::error::Push failed after $MAX_RETRIES attempts"
    exit 1
  else
    echo "Push failed (attempt $i/$MAX_RETRIES), resetting to remote..."
    git rebase --abort 2>/dev/null || true
    git fetch origin "${BRANCH_NAME}"
    git reset --hard "origin/${BRANCH_NAME}"
  fi
done
```

**Pros**:
- Single source of truth for retry logic
- Easier to test in isolation
- Reduces workflow file size
- Encourages consistent behavior across all locations

**Cons**:
- Introduces a new file and pattern
- Complex jq transformations with multiple steps (e.g., update field + add audit entry) require careful parameterization
- Some transformations depend on variables computed earlier in the step
- Need to handle transformations that have conditional logic (e.g., circuit breaker)

### Option C: Hybrid Approach with Transformation Functions

**Approach**: Create a helper script that handles the retry logic, but define the transformation as a shell function within each workflow step. The step calls the helper, passing the function name.

```bash
# In workflow step:
update_contract() {
  jq --arg phase "$NEXT_PHASE" '.current_phase = $phase' "$CONTRACT_PATH" > /tmp/contract.json
  mv /tmp/contract.json "$CONTRACT_PATH"
}

.github/scripts/push-with-transform.sh "$BRANCH_NAME" "$CONTRACT_PATH" "Advance phase" update_contract
```

**Pros**:
- Transformations stay visible in workflow context
- Retry logic is centralized
- Functions can capture local variables from the step

**Cons**:
- Shell function export across scripts is fragile
- More complex invocation pattern
- Bash function scoping may cause issues in subshells

### Option D: Inline Reset-and-Reapply with Extracted Transform Variables

**Approach**: Keep everything inline but extract the jq filter and commit message to variables at the top of each step. This reduces duplication within each step while avoiding new files.

```bash
JQ_TRANSFORM='.current_phase = "pr"'
COMMIT_MSG="Advance to PR phase for issue #${ISSUE_NUMBER}"

# Apply and commit
jq "$JQ_TRANSFORM" "$CONTRACT_PATH" > /tmp/contract.json
mv /tmp/contract.json "$CONTRACT_PATH"
git add "$CONTRACT_PATH"
git commit -m "$COMMIT_MSG"

MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
  if git push origin "${BRANCH_NAME}"; then
    break
  elif [[ $i -eq $MAX_RETRIES ]]; then
    echo "::error::Push failed after $MAX_RETRIES attempts"
    exit 1
  else
    git rebase --abort 2>/dev/null || true
    git fetch origin "${BRANCH_NAME}"
    git reset --hard "origin/${BRANCH_NAME}"
    jq "$JQ_TRANSFORM" "$CONTRACT_PATH" > /tmp/contract.json
    mv /tmp/contract.json "$CONTRACT_PATH"
    git add "$CONTRACT_PATH"
    git commit -m "$COMMIT_MSG"
  fi
done
```

**Pros**:
- No new files
- Transformation logic visible in context
- Single variable holds the transform, used in both places
- Straightforward to implement

**Cons**:
- Still some duplication (the apply-add-commit block)
- Complex multi-step transforms (e.g., update + audit log) need multiple variables or compound jq
- Doesn't address the missing retry logic at line 363

## Recommended Approach

**Option D: Inline Reset-and-Reapply with Extracted Transform Variables**

This approach provides the best balance of:
1. **Minimal new patterns**: No new scripts or files to maintain
2. **Localized changes**: Each fix is self-contained within its step
3. **Visibility**: The transformation logic remains visible in workflow context
4. **Low risk**: Conservative changes with well-understood bash patterns

For the complex multi-step transformations (those with audit log entries), the jq filter can be stored in a heredoc variable to preserve readability.

**Additionally**:
- Add retry logic to the unprotected push at line 363 (populate contract tasks)
- Standardize error handling across all locations (some warn, some exit)

## Implementation Notes

1. Each affected location needs:
   - Extract jq filter and commit message to variables
   - Replace `git pull --rebase` with reset-and-reapply pattern
   - Ensure `git rebase --abort` is called before reset

2. The checkpoint location (~line 436) intentionally uses soft failure (warning + continue) - this should be preserved.

3. The line 363 push (populate contract tasks) needs retry logic added.

4. Consider whether `--allow-empty` is needed on the commit in the retry path (if the transform is truly idempotent, the second commit may have no diff).

## Open Questions

- Should failed retries trigger a Slack notification to alert operators? Currently the workflow just exits with an error.
- The checkpoint logic (~line 436) currently warns and continues on rebase failure. Should it also use the reset-and-reapply pattern, or is losing the checkpoint acceptable?

---

*Authored-by: egg*
