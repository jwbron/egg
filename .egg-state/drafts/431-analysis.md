# Analysis: Update PR Checks to Use the SDLC Work Loop

> Issue: #431 | Phase: refine

## Problem Statement

The `check-lint` and `check-test` jobs in `sdlc-work-loop.yml` (lines 445-519) call `make lint` and `make test` to run checks during the implement phase. However, these Makefile targets delegate to `act` (nektos/act) to execute the `lint.yml` and `test.yml` workflows locally. Since `act` is not installed on GitHub Actions runners, both checks fail with:

```
ERROR: act is not installed.
make: *** [Makefile:149: _require-act] Error 1
```

This causes `passed=false` → `all_passed=false` → Review/PR jobs are skipped → no PR is created. This was observed in [run #21847584942](https://github.com/jwbron/egg/actions/runs/21847584942).

**Desired outcome**: The SDLC work loop should successfully run lint and test checks on GitHub Actions without depending on `act`.

## Current Behavior

### Makefile Targets (Makefile:148-169)

The Makefile is designed to provide local/CI parity by using `act` to run the actual workflow files:

```makefile
_require-act:
    @if ! command -v act >/dev/null 2>&1; then \
        echo "ERROR: act is not installed."; \
        ...
        exit 1; \
    fi

lint: _require-act
    act -j lint

test: _require-act
    act -j unit
```

This design is intentional for local development—developers run `make lint` and get the exact same checks as CI. However, it creates a circular dependency when called from within GitHub Actions.

### SDLC Work Loop Check Jobs (sdlc-work-loop.yml:445-519)

The check jobs defensively call `make lint`/`make test`:

```yaml
check-lint:
  steps:
    - name: Run lint checks
      run: |
        if make -n lint >/dev/null 2>&1; then
          if make lint; then
            echo "passed=true" >> "$GITHUB_OUTPUT"
          else
            echo "passed=false" >> "$GITHUB_OUTPUT"
            echo "fixable=true" >> "$GITHUB_OUTPUT"
          fi
        else
          echo "::notice::No 'make lint' target found, skipping"
          echo "passed=true" >> "$GITHUB_OUTPUT"
        fi
```

The `make -n lint` check passes (the target exists), but `make lint` fails because `act` isn't installed.

### Standalone Lint/Test Workflows

The actual lint and test logic lives in standalone workflows:

- **lint.yml** (207 lines): 6 parallel jobs (python, shell, yaml, docker, actions, custom-checks)
- **test.yml** (53 lines): 2 parallel jobs (unit, security)

These workflows are triggered on `push` and `pull_request` events but have no `workflow_call` trigger—they cannot currently be invoked as reusable workflows.

## Constraints

### Technical Constraints
- **GitHub Actions limitation**: Reusable workflows must be in the same repository (or a public repository) and support `workflow_call` trigger
- **Job outputs**: The SDLC work loop needs `passed` and `fixable` outputs from checks to control downstream jobs (fixer, aggregate-checks)
- **Parallel job aggregation**: `lint.yml` has 6 parallel jobs; aggregating their results requires a final job or composite output

### Design Constraints
- **Single source of truth**: Lint/test commands should be defined in one place, not duplicated
- **Local/CI parity**: Developers running `make lint` should get the same checks as CI
- **Existing patterns**: The codebase already has reusable workflow patterns (`reusable-autofix.yml`, `reusable-review.yml`)

### Operational Constraints
- **Backward compatibility**: Standalone `lint.yml`/`test.yml` must continue to work for push/PR triggers
- **Minimal changes**: Prefer adding `workflow_call` to existing workflows over creating new ones

## Options Considered

### Option A: Add `workflow_call` Trigger to lint.yml and test.yml

**Approach**: Modify `lint.yml` and `test.yml` to support both event triggers and `workflow_call`. The SDLC check jobs would invoke them as reusable workflows instead of calling `make lint`.

```yaml
# lint.yml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_call:
    outputs:
      passed:
        description: "Whether all lint checks passed"
        value: ${{ jobs.aggregate.outputs.passed }}
```

The SDLC work loop would call:
```yaml
check-lint:
  uses: ./.github/workflows/lint.yml
  secrets: inherit
```

A new aggregation job in `lint.yml` would collect results from all parallel jobs and output a single `passed` boolean.

**Pros**:
- True single source of truth—no command duplication
- Reuses existing, tested workflow logic
- Follows established patterns from `reusable-autofix.yml`
- Fixes the root cause (`act` dependency)

**Cons**:
- Requires modifying `lint.yml` and `test.yml` to add aggregation job
- Reusable workflow calls cannot pass dynamic `uses:` refs (minor limitation)
- Slight increase in complexity of lint/test workflows

### Option B: Inline Lint/Test Commands in SDLC Work Loop

**Approach**: Replace `make lint`/`make test` calls with inline commands that directly run the linters and tests, duplicating the logic from `lint.yml`/`test.yml`.

```yaml
check-lint:
  steps:
    - name: Run ruff
      run: ruff check . && ruff format --check .
    - name: Run mypy
      run: mypy gateway shared sandbox
    # ... repeat for all linters
```

**Pros**:
- Simple to implement
- No changes to existing workflows
- Full control over what runs in SDLC context

**Cons**:
- Duplicates lint/test logic across two locations
- Divergence risk—changes to `lint.yml` must be manually synced
- Violates DRY principle
- Would miss linters added to `lint.yml` in the future

### Option C: Create Wrapper Scripts for Direct Execution

**Approach**: Create shell scripts (`scripts/run-lint.sh`, `scripts/run-test.sh`) that contain the actual commands. Both Makefile targets and workflows call these scripts.

```bash
# scripts/run-lint.sh
#!/bin/bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
# ... other linters
```

Makefile changes:
```makefile
lint:
    ./scripts/run-lint.sh
```

SDLC work loop:
```yaml
check-lint:
  run: ./scripts/run-lint.sh
```

**Pros**:
- Single source of truth (in scripts)
- No `act` dependency
- Works both locally and in GHA

**Cons**:
- Parallel job execution lost—all linters run sequentially
- Requires refactoring lint.yml to call the script instead of inline commands
- Shell scripts harder to maintain than YAML workflow definitions
- Lose GitHub Actions' native job parallelization benefits

### Option D: Install `act` on GHA Runners

**Approach**: Add a step to install `act` before running `make lint`/`make test`.

```yaml
check-lint:
  steps:
    - name: Install act
      run: curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | bash
    - name: Run lint
      run: make lint
```

**Pros**:
- Minimal changes
- Preserves existing Makefile design

**Cons**:
- Running workflows inside workflows is fragile and slow
- `act` requires Docker, adding complexity
- Nested execution makes debugging harder
- Circular dependency (GHA → act → GHA workflows)
- Significant runtime overhead

## Recommended Approach

**Option A: Add `workflow_call` Trigger to lint.yml and test.yml**

This approach is recommended because it:

1. **Follows the repository's established pattern**: `reusable-autofix.yml` and `reusable-review.yml` already demonstrate how to create reusable workflows with inputs, outputs, and secrets inheritance.

2. **Maintains single source of truth**: The lint and test logic remains in one place. Changes to linting rules automatically apply to both standalone CI runs and SDLC checks.

3. **Fixes the root cause**: Eliminates the `act` dependency entirely for GHA execution.

4. **Aligns with issue #430's goals**: Issue #430 established the SDLC work loop pattern; this issue extends it by reusing existing workflows for checks, as the issue description explicitly suggests.

### Implementation Outline

1. **Modify `lint.yml`**:
   - Add `workflow_call` trigger with `outputs.passed`
   - Add final `aggregate` job that collects results from all parallel jobs
   - Output `passed: true` only if all jobs succeeded

2. **Modify `test.yml`**:
   - Add `workflow_call` trigger with `outputs.passed`
   - Add final `aggregate` job that collects results from `unit` and `security` jobs

3. **Update `sdlc-work-loop.yml`**:
   - Replace inline `check-lint` job with `uses: ./.github/workflows/lint.yml`
   - Replace inline `check-test` job with `uses: ./.github/workflows/test.yml`
   - Map workflow outputs to job outputs for `aggregate-checks` consumption
   - Handle `fixable` output (if a check fails, set `fixable: true` in the calling job)

4. **Preserve Makefile behavior**:
   - No changes needed; `make lint` continues to use `act` for local dev
   - Developers still get local/CI parity via `act`

## Open Questions

**Output Format for Fixable Checks:**

The current SDLC checks set `fixable=true` when lint/test fails, triggering the autofix workflow. However, reusable workflows only support simple outputs—they can't distinguish between "failed and fixable" vs "failed and not fixable" (e.g., security scan failures).

Should we:
1. Assume all lint failures are fixable, all test failures are not fixable (current implicit behavior)
2. Add explicit `fixable` output to each workflow
3. Determine fixability in the calling job based on which workflow failed

This can be decided during implementation and doesn't block the approach.

---

*Authored-by: egg*
