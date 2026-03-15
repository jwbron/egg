# Analysis: Audit Tests

> Issue: #632 | Phase: refine

## Problem Statement

Tests should all run reliably in CI checks and via `make test`. The issue asks us to:
1. Ensure all tests are being run in CI checks
2. Ensure tests run consistently
3. Ensure we're not ignoring or skipping tests we shouldn't be
4. All tests should run when `make test` is run

## Current Behavior

### Test Inventory

The codebase has **~4,195 test functions** across **157 test files** in four major test suites:

| Suite | Directory | Test Files | ~Tests | CI Workflow |
|-------|-----------|-----------|--------|-------------|
| Unit (core) | `tests/` | 83 | 2,311 | `test.yml` (job: `unit`) |
| Gateway | `gateway/tests/` | 25 | 1,055 | `test.yml` (job: `unit`) |
| Orchestrator | `orchestrator/tests/` | 21 | 511 | **NONE** |
| Integration | `integration_tests/` | 28 | 318 | `test-integration.yml` / `test-e2e.yml` |

Additionally, `test-action.yml` runs shell-based config generation and prompt builder tests for the GitHub Action.

### How `make test` Works

`make test` runs `act -j unit`, which invokes the `unit` job from `.github/workflows/test.yml`. That job runs:

```
PYTHONPATH=shared:gateway .venv/bin/pytest tests/ gateway/tests/ -v \
  --cov=gateway --cov=shared --cov=sandbox \
  --cov-report=term-missing --cov-fail-under=80
```

### How CI Checks Work (on PRs)

Here is what currently runs on every PR:

| Workflow | Trigger | What It Does |
|----------|---------|--------------|
| `on-pull-request.yml` | `pull_request` | AI code review (not tests) |
| `on-pull-request-agent-mode-design.yml` | `pull_request` (path-filtered) | AI design review (not tests) |
| `on-pull-request-contract-verify.yml` | `pull_request` | Contract verification (not tests) |
| `test-action.yml` | `pull_request` (path-filtered to `action/**`) | Action config tests only |

**What does NOT run on PRs:**

| Workflow | Trigger | What It Contains |
|----------|---------|-----------------|
| `lint.yml` | `workflow_call` / `workflow_dispatch` only | Python linting, shell checks, YAML, Docker, custom checks |
| `test.yml` | `workflow_call` / `workflow_dispatch` only | Unit tests + security scan |
| `test-integration.yml` | `workflow_call` / `workflow_dispatch` only | Integration tests |
| `test-e2e.yml` | `workflow_dispatch` / weekly cron only | E2E tests |

This is confirmed by recent CI history: out of the last 50 workflow runs, there are **zero** runs of "Lint" or "Test" workflows. No workflow calls `lint.yml` or `test.yml` on PR events.

## Issues Found

### Issue 1: Lint and Test Workflows Never Run on PRs (Critical)

`lint.yml` and `test.yml` are defined as `workflow_call` workflows but **nothing calls them on PR events**. They can only be invoked manually via `workflow_dispatch` or by another workflow calling them — but no workflow does so.

The `on-check-failure.yml` autofix workflow listens for completions of workflows named "Lint" and "Test", but since those workflows never trigger on PRs, the autofix workflow never activates either.

**Impact**: Code can be merged without any lint or test checks passing.

### Issue 2: Orchestrator Tests Not Run Anywhere in CI (Critical)

There are 21 test files with ~511 tests in `orchestrator/tests/`, but:
- `test.yml` only runs `pytest tests/ gateway/tests/` — `orchestrator/tests/` is excluded
- No other CI workflow includes orchestrator tests
- `make test` (which delegates to `act -j unit`) also excludes them
- The `--cov` flags only cover `gateway`, `shared`, and `sandbox` — not `orchestrator`

### Issue 3: Duplicate Pytest Configuration (Minor)

Both `pytest.ini` and `pyproject.toml` define pytest settings. Per pytest precedence rules, `pytest.ini` wins when both exist. This causes a silent conflict:

- `pytest.ini` `addopts`: `-v --tb=short` (no coverage)
- `pyproject.toml` `addopts`: `-v --cov=gateway --cov=shared --cov=sandbox --cov-report=term-missing` (with coverage)

Running bare `pytest` locally uses `pytest.ini` settings and skips coverage. The CI workflow works around this by passing `--cov` flags explicitly on the command line, but the `pyproject.toml` coverage config is dead code.

### Issue 4: Test Skipping Patterns (No Issues Found)

All test skips are appropriate and dynamic:
- 26 `pytest.skip()` calls, all checking runtime prerequisites (Docker availability, env vars, container startup)
- 2 `pytest.xfail()` calls in security fuzz tests (expected: edge cases under investigation)
- No `@pytest.mark.skip` or `@pytest.mark.skipif` decorators
- No commented-out tests or `collect_ignore` patterns
- No tests permanently disabled

## Constraints

- `lint.yml` and `test.yml` are designed as reusable `workflow_call` workflows. Any fix must preserve this pattern (they should remain callable by other workflows).
- The `on-check-failure.yml` autofix workflow depends on workflows named "Lint" and "Test" completing — adding PR triggers must not break this.
- Integration and E2E tests require Docker and/or API keys, so they can't run in all environments. Only unit tests and lint should be mandatory on PRs.
- The `make test` command uses `act` (a local GitHub Actions runner), so changes to the workflow affect both CI and local behavior.

## Options Considered

### Option A: Add `pull_request` Trigger to `lint.yml` and `test.yml`

**Approach**: Add `on: pull_request` triggers directly to `lint.yml` and `test.yml`, alongside their existing `workflow_call` triggers. Add `orchestrator/tests/` to the test command. Consolidate pytest config into `pyproject.toml`.

**Pros**:
- Simplest change — minimal new files
- `workflow_call` + `pull_request` triggers are supported by GitHub Actions
- The `on-check-failure.yml` workflow already listens for "Lint" and "Test" completions, so autofix will work automatically

**Cons**:
- Mixing `workflow_call` and direct triggers in the same file can be confusing
- If another workflow ever calls them via `workflow_call` on PRs, they'd run twice

### Option B: Create a New Orchestrating `ci.yml` Workflow

**Approach**: Create a new `ci.yml` workflow triggered on `pull_request` that calls `lint.yml` and `test.yml` via `workflow_call`. Add `orchestrator/tests/` to the test command. Consolidate pytest config.

**Pros**:
- Clean separation of concerns — reusable workflows stay reusable
- Single place to manage what runs on PRs
- Easy to add future checks (e.g., integration tests) to the same orchestrator
- `on-check-failure.yml` may need updating (it listens for workflow names "Lint" and "Test", not "CI")

**Cons**:
- Adds a new workflow file
- The autofix workflow (`on-check-failure.yml`) listens for workflow_run events from "Lint" and "Test" — with an orchestrating workflow, the individual workflow names may not emit separate `workflow_run` events (GitHub Actions treats called workflows as jobs within the caller, so the event name would be "CI" not "Lint"/"Test")
- Requires verifying `on-check-failure.yml` compatibility

## Recommended Approach

**Option A** is recommended. Adding `pull_request` triggers directly to `lint.yml` and `test.yml` is the simplest path that:

1. Ensures lint and tests run on every PR
2. Preserves compatibility with `on-check-failure.yml` autofix (which listens for "Lint" and "Test" workflow completions)
3. Preserves `workflow_call` for potential future reuse
4. Requires no changes to other workflows

The full scope of changes:

1. **`test.yml`**: Add `on: pull_request` trigger. Add `orchestrator/tests/` to the pytest command. Add `--cov=orchestrator` to coverage flags.
2. **`lint.yml`**: Add `on: pull_request` trigger.
3. **`pytest.ini`**: Remove this file (consolidate into `pyproject.toml`).
4. **`pyproject.toml`**: Update `[tool.pytest.ini_options]` to include all settings currently in `pytest.ini` (markers, filterwarnings, etc.). Update `testpaths` to include `orchestrator/tests/` if running bare `pytest` should cover them too.
5. **`Makefile`**: Update the `make test` target to also run orchestrator tests (this happens automatically if `test.yml` is updated, since `make test` runs `act -j unit`).

## Open Questions

1. **Should orchestrator tests be included in the `make test` / CI unit test run?** The orchestrator has 511 tests that appear to be unit tests (no Docker required). Including them in the standard unit test run seems correct, but confirming this avoids surprises if any have hidden infrastructure dependencies.

2. **Should integration tests (`test-integration.yml`) also be triggered on PRs?** Currently they only run via `workflow_call` or manual dispatch. They require Docker (available on GitHub Actions runners) but add CI time. The issue says "all tests should run" — does this include integration tests, or just unit tests?

3. **Is there a branch protection rule requiring specific status checks?** If branch protection requires checks named "Unit Tests" or "Lint / Python", adding the PR triggers will automatically satisfy those. If no branch protection exists, we should recommend adding it.

---

*Authored-by: egg*
