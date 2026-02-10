# Analysis: Update PR checks to use the SDLC work loop

> Issue: #431 | Phase: refine

## Problem Statement

The repository currently has two separate mechanisms for running PR checks:
1. **Standalone workflows**: `lint.yml` and `test.yml` trigger independently on `pull_request` events
2. **SDLC work loop**: `sdlc-work-loop.yml` has its own `check-lint` and `check-test` jobs that call `make lint`/`make test`

This creates duplication and a critical bug: the SDLC work loop's check jobs call `make lint`/`make test`, which depend on `act` (a tool for running GitHub Actions locally), but `act` is not installed on GitHub Actions runners.

The desired outcome is a unified check mechanism where `lint.yml` and `test.yml` become reusable workflows called from the SDLC work loop, eliminating both the duplication and the `act` dependency bug.

## Current Behavior

### Standalone PR check workflows

**`lint.yml`** (`.github/workflows/lint.yml:1-207`):
- Triggers on `push` to main and `pull_request` to main
- Contains 6 parallel jobs: `python`, `shell`, `yaml`, `docker`, `actions`, `custom-checks`
- Runs linters directly (ruff, mypy, shellcheck, yamllint, hadolint, actionlint)

**`test.yml`** (`.github/workflows/test.yml:1-53`):
- Triggers on `push` to main and `pull_request` to main
- Contains 2 jobs: `unit` (pytest with coverage) and `security` (bandit)

### SDLC work loop check jobs

**`sdlc-work-loop.yml`** (`.github/workflows/sdlc-work-loop.yml:414-520`):
- `check-lint` job (lines 414-465): Calls `make lint` which requires `act`
- `check-test` job (lines 469-520): Calls `make test` which requires `act`

### The `act` dependency bug

**`Makefile`** (lines 159-167):
```makefile
lint: _require-act
	act -j lint

test: _require-act
	act -j unit
```

The `_require-act` target fails if `act` is not installed. This means the SDLC work loop's check jobs will always fail on GitHub Actions runners because `act` is a local development tool, not a CI tool.

### Existing reusable workflow pattern

The repository already has reusable workflows:
- `reusable-review.yml`: Uses `workflow_call` trigger, accepts inputs and secrets
- `reusable-autofix.yml`: Uses `workflow_call` trigger, accepts inputs and secrets

## Constraints

- **Backward compatibility**: Standalone `lint.yml` and `test.yml` should remain as separate files for local testing and direct invocation
- **Branch restrictions**: SDLC work loop only runs on `egg/` prefixed branches during implement phase
- **Concurrency**: The SDLC work loop uses concurrency groups to prevent parallel runs for the same issue
- **Job dependencies**: `check-lint` and `check-test` in the SDLC work loop depend on `work` and `check-merge-conflict` jobs

## Options Considered

### Option A: Add `workflow_call` trigger to lint.yml and test.yml

**Approach**: Modify `lint.yml` and `test.yml` to add `workflow_call` as an additional trigger alongside existing triggers. The SDLC work loop would then call these workflows using `uses:` syntax instead of running `make lint`/`make test`.

**Pros**:
- Single source of truth: the actual lint/test logic stays in dedicated workflow files
- Standalone workflows continue to work for direct PR triggers and manual runs
- Follows the existing pattern used by `reusable-review.yml` and `reusable-autofix.yml`
- Clean separation of concerns

**Cons**:
- Reusable workflows called via `uses:` run as separate workflow runs, which may complicate status reporting
- GitHub has limitations on passing outputs from reusable workflows back to the caller
- Need to carefully handle job-level vs workflow-level outputs

### Option B: Inline the lint/test logic directly in SDLC work loop

**Approach**: Copy the step definitions from `lint.yml` and `test.yml` into the `check-lint` and `check-test` jobs in `sdlc-work-loop.yml`. Remove the `make lint`/`make test` calls.

**Pros**:
- Simple implementation with no workflow-call complexity
- All SDLC logic stays in one file
- Easy to track job outputs and status

**Cons**:
- Duplicates lint/test logic in two places (standalone workflows and SDLC work loop)
- Changes to linting/testing need to be made in multiple places
- Violates DRY principle

### Option C: Call lint.yml and test.yml as reusable workflows with job outputs

**Approach**: Same as Option A, but add explicit outputs to the reusable workflows and use `needs.<job>.outputs` in the SDLC work loop to get pass/fail status.

**Pros**:
- All pros from Option A
- Enables the SDLC work loop to react to lint/test failures programmatically
- Supports the existing autofix mechanism that triggers on check failures

**Cons**:
- Slightly more complex to implement
- GitHub Actions reusable workflow outputs require explicit declaration

## Recommended Approach

**Option C: Call lint.yml and test.yml as reusable workflows with job outputs**

This approach is recommended because:

1. **Eliminates the `act` dependency bug** by removing `make lint`/`make test` calls entirely
2. **Maintains single source of truth** for lint/test logic in dedicated workflow files
3. **Follows existing patterns** in the codebase (see `reusable-review.yml`, `reusable-autofix.yml`)
4. **Enables programmatic handling** of check failures in the SDLC work loop
5. **Preserves backward compatibility** - standalone workflows still work for direct PR triggers

### Implementation outline

1. **Modify `lint.yml`**:
   - Add `workflow_call` trigger with optional inputs (e.g., `branch_name`)
   - Keep existing `push` and `pull_request` triggers
   - Add workflow-level outputs for pass/fail status

2. **Modify `test.yml`**:
   - Add `workflow_call` trigger with optional inputs
   - Keep existing `push` and `pull_request` triggers
   - Add workflow-level outputs for pass/fail status

3. **Modify `sdlc-work-loop.yml`**:
   - Replace `check-lint` job with a call to `lint.yml` using `uses: ./.github/workflows/lint.yml`
   - Replace `check-test` job with a call to `test.yml` using `uses: ./.github/workflows/test.yml`
   - Update `aggregate-checks` job to read outputs from the reusable workflows

4. **Remove `make lint`/`make test` from SDLC work loop**:
   - Delete the inline shell scripts that call these make targets

### Considerations

- **PR trigger removal**: The issue asks to remove standalone PR triggers from `lint.yml` and `test.yml`. However, this would mean PRs not going through the SDLC pipeline would have no automatic checks. We may want to keep the PR triggers for non-SDLC branches, or alternatively ensure all PRs go through the SDLC pipeline.

- **Integration tests**: `test-integration.yml` also has `pull_request` triggers. It's not mentioned in the issue but may need similar treatment for consistency.

## Open Questions

1. **Should standalone PR triggers be removed from lint.yml and test.yml?**

   The issue says "remove standalone PR triggers" but this would leave non-SDLC PRs without automatic checks. Options:
   - Remove PR triggers entirely (all PRs must use SDLC)
   - Keep PR triggers but add `if: github.event_name != 'workflow_call'` to avoid double-running
   - Keep PR triggers as-is (they'll run in parallel with SDLC-triggered runs)

2. **Should integration tests (test-integration.yml) be included in this change?**

   Currently only `lint.yml` and `test.yml` are mentioned, but `test-integration.yml` has the same pattern.

3. **What about the check-fixer job in SDLC work loop?**

   The current `check-fixer` job triggers `reusable-autofix.yml` when lint or test fails. This should continue to work, but we need to ensure the workflow outputs are compatible.

---

*Authored-by: egg*
