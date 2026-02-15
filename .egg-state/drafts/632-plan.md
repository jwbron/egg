# Plan: Audit test suite for CI coverage and consistency

> Issue: #632 | Phase: plan

## Summary

The test suite has a significant coverage gap: 20 orchestrator test files (~8800 lines) are completely excluded from CI. Additionally, pytest configuration is duplicated across `pytest.ini` and `pyproject.toml`, and coverage measurement excludes the orchestrator module. This plan addresses all findings from the architecture analysis by adding orchestrator tests to CI, consolidating pytest configuration to a single source of truth, and ensuring `make test` runs all unit tests.

The approach is conservative: verify orchestrator tests pass first, then update CI configuration. Config consolidation is a separate phase to isolate risk. The changes are limited to CI config, test config, and the Makefile — no production code is modified.

## Implementation Phases

### Phase 1: Verify and fix orchestrator tests

**Goal**: Confirm all 20 orchestrator test files pass when run standalone, and fix any failures.

The orchestrator tests have never been run in CI, so they may have drifted. Before adding them to the CI pipeline, we must verify they pass and fix anything broken.

- [TASK-1-1] Run orchestrator tests locally and capture results
  - **Command**: `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v`
  - **Acceptance**: All orchestrator tests pass, or failures are identified and documented

- [TASK-1-2] Fix any failing orchestrator tests
  - **Files**: `orchestrator/tests/*.py` (as needed)
  - **Acceptance**: `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v` exits 0 with all tests passing

**Dependencies**: None — this is foundational.

**Exit criteria**: All 20 orchestrator test files pass when run with `PYTHONPATH=orchestrator:shared`.

### Phase 2: Add orchestrator tests to CI and update coverage

**Goal**: Include `orchestrator/tests/` in the CI unit test job and add orchestrator to coverage tracking.

- [TASK-2-1] Update `.github/workflows/test.yml` to include orchestrator tests
  - **File**: `.github/workflows/test.yml`
  - Add `orchestrator/tests/` to the pytest command (line 31)
  - Add `orchestrator` to the PYTHONPATH (currently `shared:gateway`, change to `shared:gateway:orchestrator`)
  - Add `--cov=orchestrator` to coverage flags
  - **Acceptance**: The `unit` job pytest command becomes:
    ```
    PYTHONPATH=shared:gateway:orchestrator .venv/bin/pytest tests/ gateway/tests/ orchestrator/tests/ -v \
      --cov=gateway --cov=shared --cov=sandbox --cov=orchestrator \
      --cov-report=term-missing \
      --cov-fail-under=80
    ```

- [TASK-2-2] Add Bandit security scan for orchestrator module
  - **File**: `.github/workflows/test.yml`
  - Add `orchestrator` to the bandit scan targets (line 54, currently `gateway shared sandbox`)
  - **Acceptance**: Bandit command becomes: `.venv/bin/bandit -r gateway shared sandbox orchestrator -ll -c pyproject.toml`

**Dependencies**: Phase 1 (orchestrator tests must pass before adding to CI).

**Exit criteria**: CI workflow includes orchestrator in test execution, coverage tracking, and security scanning.

### Phase 3: Consolidate pytest configuration

**Goal**: Remove duplicate pytest config by keeping `pyproject.toml` as the single source of truth and deleting `pytest.ini`.

Both `pytest.ini` and `pyproject.toml` define `testpaths` and `addopts`. `pytest.ini` takes precedence when both exist, but CI overrides both by passing directories explicitly. This creates confusion about which config is authoritative.

- [TASK-3-1] Migrate pytest.ini settings into pyproject.toml
  - **Files**: `pyproject.toml`, `pytest.ini`
  - Move marker definitions from `pytest.ini` to `pyproject.toml` `[tool.pytest.ini_options]`
  - Move `filterwarnings` setting to `pyproject.toml`
  - Update `testpaths` in pyproject.toml to include all unit test directories: `["tests", "gateway/tests", "orchestrator/tests"]`
  - Update `addopts` in pyproject.toml to include orchestrator coverage: `-v --tb=short --cov=gateway --cov=shared --cov=sandbox --cov=orchestrator --cov-report=term-missing`
  - **Acceptance**: `pyproject.toml [tool.pytest.ini_options]` contains all settings from `pytest.ini` plus orchestrator paths/coverage

- [TASK-3-2] Delete pytest.ini
  - **File**: `pytest.ini` (delete)
  - **Acceptance**: `pytest.ini` no longer exists; running `pytest` with no arguments uses pyproject.toml config and discovers tests across all three directories

- [TASK-3-3] Add mypy override for orchestrator tests
  - **File**: `pyproject.toml`
  - Add `[[tool.mypy.overrides]]` for `orchestrator.tests.*` matching the pattern used for `gateway.tests.*`
  - **Acceptance**: mypy config includes an override for `orchestrator.tests.*` with `disallow_untyped_defs = false` and `ignore_missing_imports = true`

**Dependencies**: Phase 2 (CI already includes orchestrator tests; this phase consolidates the local config to match).

**Exit criteria**: Single pytest config in `pyproject.toml`; `pytest.ini` deleted; bare `pytest` discovers all unit tests.

### Phase 4: Update Makefile and verify end-to-end

**Goal**: Ensure `make test` runs all unit tests (including orchestrator) and document the test structure.

- [TASK-4-1] Verify `make test` includes orchestrator tests
  - `make test` delegates to `act -j unit`, which runs the `test.yml` unit job. Since Phase 2 updated that job, `make test` automatically includes orchestrator tests. Verify this works.
  - **Acceptance**: `make test` (or the equivalent direct pytest command) executes tests from `tests/`, `gateway/tests/`, and `orchestrator/tests/`

- [TASK-4-2] Add PYTHONPATH to Makefile integration test targets if needed
  - **File**: `Makefile`
  - Check if integration test targets need `orchestrator` on PYTHONPATH. Currently they use `PYTHONPATH=shared`. If integration tests import orchestrator modules, add it.
  - **Acceptance**: All Makefile test targets work correctly

- [TASK-4-3] Document skipped tests in PR description
  - Document the 6 intentionally-skipped tests:
    - 5 tests in `integration_tests/local_pipeline/test_signals.py` — skip with "Signals API not implemented" (feature gap placeholder)
    - 1 test in `integration_tests/local_pipeline/test_hitl_edge_cases.py` — skip with "Decision timeout feature not supported" (feature gap placeholder)
  - These are legitimate conditional skips, not bugs. All other ~20 `pytest.skip()` calls are environment-conditional guards (Docker, API tokens, network) which are correct patterns.
  - **Acceptance**: PR description documents skipped tests and explains they are intentional

**Dependencies**: Phases 2 and 3 (CI and config must be updated first).

**Exit criteria**: `make test` runs all unit tests; skipped tests are documented; all tests pass.

## Test Strategy

**Pre-implementation baseline**: Run existing tests to confirm current state before any changes.

**Phase 1 verification**:
```bash
PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v
```

**Phase 2 verification**: Run the full CI command locally:
```bash
PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v \
  --cov=gateway --cov=shared --cov=sandbox --cov=orchestrator \
  --cov-report=term-missing --cov-fail-under=80
```

**Phase 3 verification**: After deleting `pytest.ini`, run bare `pytest` to confirm pyproject.toml is picked up:
```bash
pytest -v  # Should discover tests in all three directories
```

**Phase 4 verification**: Full end-to-end with `make test` or equivalent direct invocation.

**Coverage requirement**: The 80% threshold must still be met after adding orchestrator to coverage measurement. If orchestrator coverage is below 80%, this could cause CI to fail — check before committing.

## Rollback Plan

Changes are limited to configuration files. Rollback is straightforward:
1. Revert `.github/workflows/test.yml` to remove orchestrator from pytest command and coverage flags
2. Restore `pytest.ini` from git history
3. Revert `pyproject.toml` changes
4. Revert any Makefile changes

No production code is modified, so rollback carries zero risk of runtime regressions.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Orchestrator tests fail when first run | Medium | Medium | Phase 1 dedicates time to fixing failures before CI integration |
| Coverage drops below 80% threshold when orchestrator is added | Medium | High | Check orchestrator coverage before enabling `--cov-fail-under`; may need to temporarily exclude orchestrator from threshold or add tests |
| Deleting pytest.ini breaks local dev workflow | Low | Medium | Verify `pytest` picks up pyproject.toml correctly before deleting; easy to revert |
| Orchestrator tests have dependencies not in dev extras | Low | Low | Run locally first; add any missing deps to pyproject.toml |

## Open Questions

1. **Coverage threshold with orchestrator**: If orchestrator module coverage is below 80%, adding `--cov=orchestrator` could cause CI to fail. The implementer should check orchestrator coverage independently and decide whether to include it in `--cov-fail-under` or add it without the threshold initially.

2. **`make test` scope**: The issue says "All tests should run when `make test` is run." The architect recommended interpreting this as "all unit tests" since integration/E2E tests require Docker and API keys. This plan follows that interpretation. If the intent is broader, a `make test-all` target could be added.

*Authored-by: egg*

```yaml
# yaml-tasks
pr:
  title: "Audit test suite: add orchestrator tests to CI, consolidate config"
  description: |
    Adds the 20 orchestrator test files (~8800 lines) to CI that were previously
    excluded. Consolidates duplicate pytest configuration from pytest.ini and
    pyproject.toml into a single source of truth (pyproject.toml). Updates
    coverage tracking to include the orchestrator module. Ensures make test
    runs all unit tests consistently.

    Closes #632
phases:
  - id: 1
    name: Verify and fix orchestrator tests
    goal: Confirm all orchestrator test files pass standalone and fix any failures
    tasks:
      - id: TASK-1-1
        description: Run orchestrator tests locally with PYTHONPATH=orchestrator:shared and capture results
        acceptance: All orchestrator tests pass or failures are identified
        files:
          - orchestrator/tests/
      - id: TASK-1-2
        description: Fix any failing orchestrator tests
        acceptance: PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v exits 0
        files:
          - orchestrator/tests/
  - id: 2
    name: Add orchestrator tests to CI
    goal: Include orchestrator/tests/ in CI unit test job and coverage tracking
    tasks:
      - id: TASK-2-1
        description: Update test.yml to run orchestrator tests with correct PYTHONPATH and coverage flags
        acceptance: CI pytest command includes orchestrator/tests/ with --cov=orchestrator and PYTHONPATH includes orchestrator
        files:
          - .github/workflows/test.yml
      - id: TASK-2-2
        description: Add orchestrator to Bandit security scan targets
        acceptance: Bandit command scans gateway shared sandbox orchestrator
        files:
          - .github/workflows/test.yml
  - id: 3
    name: Consolidate pytest configuration
    goal: Single pytest config source of truth in pyproject.toml; delete pytest.ini
    tasks:
      - id: TASK-3-1
        description: Migrate all pytest.ini settings (markers, filterwarnings, testpaths, addopts) into pyproject.toml
        acceptance: pyproject.toml [tool.pytest.ini_options] contains all settings including orchestrator paths and coverage
        files:
          - pyproject.toml
      - id: TASK-3-2
        description: Delete pytest.ini
        acceptance: pytest.ini no longer exists; bare pytest uses pyproject.toml and discovers all unit tests
        files:
          - pytest.ini
      - id: TASK-3-3
        description: Add mypy override for orchestrator.tests.* matching gateway.tests.* pattern
        acceptance: mypy config includes orchestrator.tests.* override with relaxed typing rules
        files:
          - pyproject.toml
  - id: 4
    name: Verify end-to-end and document
    goal: Confirm make test runs all unit tests and document skipped tests
    tasks:
      - id: TASK-4-1
        description: Verify make test (or equivalent direct pytest) executes tests from all three directories
        acceptance: Test run includes tests/, gateway/tests/, and orchestrator/tests/
        files:
          - Makefile
      - id: TASK-4-2
        description: Check if Makefile integration test targets need orchestrator on PYTHONPATH
        acceptance: All Makefile test targets work correctly
        files:
          - Makefile
      - id: TASK-4-3
        description: Document the 6 intentionally-skipped tests (5 Signals API + 1 HITL timeout) in PR description
        acceptance: PR description explains skipped tests are intentional feature-gap placeholders
        files: []
```
