# Plan: Audit test suite for CI coverage and consistency

> Issue: #632 | Phase: plan | Revision: 2

## Revision Notes

This is a revision addressing feedback from the unified and plan reviewers. Changes from v1:

1. **CRITICAL — Coverage threshold (R1)**: Removed `--cov=orchestrator` from all acceptance criteria. Orchestrator tests run in CI but orchestrator module is excluded from coverage measurement. Follow-up tracked separately.
2. **MEDIUM — Test interaction failure (R2)**: Added TASK-1-3 to diagnose and fix `test_cli.py::test_serve_runs_as_non_root` logging interaction, verified against the combined pytest command before Phase 2.
3. **MEDIUM — Docker package dependency (R4)**: Added TASK-1-2 to add `docker` to pyproject.toml dev extras so the 2 docker-dependent test files collect in CI.
4. **LOW — Pre-existing failures (R3)**: Added baseline context about 2 pre-existing `gateway/tests/test_session_manager.py` failures to Phase 1 and to TASK-4-3 documentation scope.
5. **Minor — Line numbers removed**: Removed fragile line-number references to test.yml.
6. **Minor — `--tb=short` preserved**: TASK-3-1 explicitly preserves `--tb=short` from pytest.ini when consolidating into pyproject.toml.

## Summary

The test suite has a significant coverage gap: 20 orchestrator test files (~8800 lines) are completely excluded from CI. Additionally, pytest configuration is duplicated across `pytest.ini` and `pyproject.toml`, creating confusion about which is authoritative. This plan adds orchestrator tests to CI, fixes known test interaction issues, consolidates pytest configuration, and ensures `make test` runs all unit tests.

**Key constraint**: Adding `--cov=orchestrator` drops combined coverage from ~80% to ~71%, which fails the `--cov-fail-under=80` gate. The orchestrator module has 10,321 uncovered lines. Therefore, orchestrator tests are added to CI execution **without** orchestrator coverage measurement. Coverage for orchestrator is tracked as separate follow-up work.

**Baseline context**: Two pre-existing failures exist in `gateway/tests/test_session_manager.py::TestSessionEndCheckpointCapture` (`test_capture_and_cleanup_handles_import_error`, `test_capture_and_cleanup_handles_capture_failure`) that fail in combined pytest sessions even without orchestrator tests. These are caused by the same `egg_logging` state interaction pattern and are out of scope for this PR but are documented.

## Implementation Phases

### Phase 1: Verify orchestrator tests and fix interactions

**Goal**: Confirm orchestrator tests pass standalone, handle the docker dependency, fix the `test_cli.py` logging interaction, and verify the combined test run works.

**Baseline**: Note the 2 pre-existing failures in `gateway/tests/test_session_manager.py::TestSessionEndCheckpointCapture` — these exist today without orchestrator tests and are out of scope.

- **[TASK-1-1]** Run orchestrator tests standalone and capture baseline
  - **Command**: `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v`
  - **Acceptance**: All collectable orchestrator tests pass. 2 collection errors from `test_container_spawner.py` and `test_docker_client.py` (docker package missing) are identified.

- **[TASK-1-2]** Add `docker` to pyproject.toml dev extras
  - The `docker` package is already a runtime dependency of the orchestrator (in `orchestrator/requirements.txt`) but is missing from pyproject.toml dev extras. Without it, `test_container_spawner.py` and `test_docker_client.py` fail to collect because `orchestrator/docker_client.py` imports `docker` at module level.
  - **Files**: `pyproject.toml`
  - **Acceptance**: After `uv sync --extra dev`, running `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v --collect-only` shows 0 collection errors. All 20 test files collect successfully.

- **[TASK-1-3]** Fix `test_cli.py` logging interaction for combined runs
  - `test_serve_runs_as_non_root` fails in combined pytest sessions (`tests/ gateway/tests/ orchestrator/tests/`) because `orchestrator/cli.py` calls `logger.info()` with keyword arguments (`host=`, `port=`, `debug=`) that only work with `EggLogger`, not standard `logging.Logger`. In combined sessions, logger state is affected by `shared/egg_logging` monkey-patching `logging.Logger._log`.
  - **Fix**: Mock `cli.logger` in the test to isolate it from global logging state. This avoids production code changes.
  - **Files**: `orchestrator/tests/test_cli.py`
  - **Acceptance**: Combined pytest command passes: `PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v` — `test_serve_runs_as_non_root` does not fail.

- **[TASK-1-4]** Verify full combined suite passes
  - Run the full combined command after TASK-1-2 and TASK-1-3 to catch any other interaction failures.
  - **Command**: `PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v`
  - **Acceptance**: Command exits 0 (excluding the 2 pre-existing `test_session_manager.py` failures which are baseline).

**Dependencies**: None — this is foundational.

**Exit criteria**: All orchestrator tests pass in both standalone and combined modes. Docker-dependent tests collect successfully. No new test interaction failures introduced.

### Phase 2: Add orchestrator tests to CI (without orchestrator coverage)

**Goal**: Include `orchestrator/tests/` in the CI unit test job and add orchestrator to the Bandit security scan. **Do NOT add `--cov=orchestrator`**.

- **[TASK-2-1]** Update `test.yml` to run orchestrator tests
  - Add `orchestrator/tests/` to the pytest command.
  - Add `orchestrator` to `PYTHONPATH` (change from `shared:gateway` to `shared:gateway:orchestrator`).
  - **CRITICAL**: Do NOT add `--cov=orchestrator`. The existing `--cov` flags remain: `--cov=gateway --cov=shared --cov=sandbox`. The `--cov-fail-under=80` threshold is unchanged.
  - **Files**: `.github/workflows/test.yml`
  - **Acceptance**: The unit job pytest command becomes:
    ```
    PYTHONPATH=shared:gateway:orchestrator .venv/bin/pytest tests/ gateway/tests/ orchestrator/tests/ -v \
      --cov=gateway --cov=shared --cov=sandbox \
      --cov-report=term-missing \
      --cov-fail-under=80
    ```
    Note: NO `--cov=orchestrator`.

- **[TASK-2-2]** Add orchestrator to Bandit security scan
  - Add `orchestrator` to the bandit scan targets.
  - **Files**: `.github/workflows/test.yml`
  - **Acceptance**: Bandit command becomes: `.venv/bin/bandit -r gateway shared sandbox orchestrator -ll -c pyproject.toml`

**Dependencies**: Phase 1 (orchestrator tests must pass in combined mode before adding to CI).

**Exit criteria**: CI workflow includes orchestrator in test execution and security scanning. Coverage measurement is unchanged (still only `gateway`, `shared`, `sandbox`). The `--cov-fail-under=80` threshold still passes.

### Phase 3: Consolidate pytest configuration

**Goal**: Single pytest config in `pyproject.toml`. Delete `pytest.ini`.

Both `pytest.ini` and `pyproject.toml` define test configuration. `pytest.ini` takes precedence when both exist (pytest warns: "ignoring pytest config in pyproject.toml!"). CI bypasses both by passing explicit arguments. This creates confusion — consolidate to `pyproject.toml`.

- **[TASK-3-1]** Migrate pytest.ini settings into pyproject.toml
  - Move all settings from `pytest.ini` to `pyproject.toml [tool.pytest.ini_options]`:
    1. `markers`: `integration`, `functional`, `e2e`, `security`, `agent_flaky` (currently only in pytest.ini)
    2. `filterwarnings`: `ignore::DeprecationWarning` (currently only in pytest.ini)
    3. `python_files`, `python_classes`, `python_functions` patterns (currently only in pytest.ini)
    4. `testpaths`: Update to `["tests", "gateway/tests", "orchestrator/tests"]`
    5. `addopts`: Merge to include `--tb=short` (from pytest.ini) AND existing `--cov` flags. Result: `-v --tb=short --cov=gateway --cov=shared --cov=sandbox --cov-report=term-missing`
  - **Note**: Do NOT add `--cov=orchestrator` to addopts. Do NOT add `--cov-fail-under` to addopts (keep it CI-only to avoid blocking local development).
  - **Files**: `pyproject.toml`
  - **Acceptance**: `pyproject.toml [tool.pytest.ini_options]` contains all settings from `pytest.ini` including markers, filterwarnings, `--tb=short`, and updated testpaths. No settings are lost.

- **[TASK-3-2]** Delete pytest.ini
  - Remove `pytest.ini`. Verify bare `pytest` uses `pyproject.toml` config and discovers tests across all three directories.
  - **Files**: `pytest.ini` (delete)
  - **Acceptance**: `pytest.ini` no longer exists. Running `pytest` with no arguments discovers tests in `tests/`, `gateway/tests/`, and `orchestrator/tests/`.

- **[TASK-3-3]** Add mypy override for `orchestrator.tests.*`
  - Add `[[tool.mypy.overrides]]` for `orchestrator.tests.*` matching the pattern used for `gateway.tests.*`: `disallow_untyped_defs = false`, `ignore_missing_imports = true`.
  - **Files**: `pyproject.toml`
  - **Acceptance**: mypy config includes `orchestrator.tests.*` override with relaxed typing rules.

**Dependencies**: Phase 2 (CI already includes orchestrator tests; this phase consolidates local config to match).

**Exit criteria**: Single pytest config in `pyproject.toml`. `pytest.ini` deleted. Bare `pytest` discovers all unit tests.

### Phase 4: Verify end-to-end and document

**Goal**: Confirm `make test` runs all unit tests. Document skipped tests and pre-existing failures.

- **[TASK-4-1]** Verify `make test` includes orchestrator tests
  - `make test` delegates to `act -j unit` which runs `test.yml`. Since Phase 2 updated that job, `make test` automatically includes orchestrator tests. Verify this works.
  - **Acceptance**: `make test` (or the equivalent direct pytest command) executes tests from `tests/`, `gateway/tests/`, and `orchestrator/tests/`.

- **[TASK-4-2]** Check Makefile integration test PYTHONPATH
  - Check if integration test targets need `orchestrator` on PYTHONPATH. Currently they use `PYTHONPATH=shared`.
  - **Files**: `Makefile`
  - **Acceptance**: All Makefile test targets work correctly.

- **[TASK-4-3]** Document skipped tests and pre-existing failures in PR
  - Document in PR description:
    1. **Intentionally-skipped tests**: 5 tests in `integration_tests/local_pipeline/test_signals.py` skip with "Signals API not implemented" and 1 test in `integration_tests/local_pipeline/test_hitl_edge_cases.py` skips with "Decision timeout feature not supported" — these are feature-gap placeholders.
    2. **Environment-conditional skips**: ~20 `pytest.skip()` calls guard on Docker, API tokens, and network availability — these are correct patterns.
    3. **Pre-existing failures**: 2 tests in `gateway/tests/test_session_manager.py::TestSessionEndCheckpointCapture` (`test_capture_and_cleanup_handles_import_error`, `test_capture_and_cleanup_handles_capture_failure`) fail in combined pytest sessions even without orchestrator tests due to `egg_logging` state interaction. Out of scope for this PR.
    4. **Orchestrator coverage**: `--cov=orchestrator` is intentionally not included because it drops combined coverage to ~71% (below the 80% threshold). Tracked as follow-up.
  - **Acceptance**: PR description documents all skipped tests, pre-existing failures, and the coverage strategy.

**Dependencies**: Phases 2 and 3 (CI and config must be updated first).

**Exit criteria**: `make test` runs all unit tests. All test behaviors are documented.

## Test Strategy

**Pre-implementation baseline**: Run existing tests to confirm current state before any changes:
```bash
PYTHONPATH=shared:gateway pytest tests/ gateway/tests/ -v
```

**Phase 1 standalone verification**:
```bash
PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v
```

**Phase 1 combined verification** (after fixing interactions):
```bash
PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v
```

**Phase 2 verification** (CI command, run locally):
```bash
PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v \
  --cov=gateway --cov=shared --cov=sandbox \
  --cov-report=term-missing --cov-fail-under=80
```

**Phase 3 verification**: After deleting `pytest.ini`, run bare `pytest`:
```bash
pytest -v  # Should discover tests in all three directories
```

**Phase 4 verification**: Full end-to-end with `make test` or equivalent.

## Follow-up Work (out of scope for this PR)

1. **Add orchestrator to coverage measurement**: Once orchestrator module has sufficient test coverage (near 80%), add `--cov=orchestrator` to both CI (`test.yml`) and local (`pyproject.toml` addopts). This requires writing significant additional tests — the module currently has 10,321 uncovered lines. Track as a separate issue.

2. **Fix pre-existing gateway `test_session_manager` failures**: The 2 `TestSessionEndCheckpointCapture` failures are likely caused by the same `egg_logging` state interaction pattern as the orchestrator `test_cli.py` issue. Fix separately.

## Rollback Plan

All changes are confined to:
- CI config: `.github/workflows/test.yml`
- Test config: `pytest.ini` (deleted), `pyproject.toml` (modified)
- One test file fix: `orchestrator/tests/test_cli.py`
- Dev dependency: `docker` added to `pyproject.toml` dev extras

Rollback is a simple `git revert` of the PR. No production code changes, no database migrations, no infrastructure changes.

## Risk Assessment

| Risk | Severity | Likelihood | Status | Mitigation |
|------|----------|------------|--------|------------|
| R1: Coverage threshold broken by `--cov=orchestrator` | HIGH | CERTAIN | MITIGATED | Do NOT add `--cov=orchestrator`. Track separately. |
| R2: `test_cli.py` logging interaction in combined runs | MEDIUM | HIGH | ADDRESSED | TASK-1-3 fixes before combining in CI. |
| R3: Pre-existing `test_session_manager` failures | MEDIUM | HIGH | DOCUMENTED | Out of scope. Documented in TASK-4-3. |
| R4: Docker package collection errors | LOW | CERTAIN | ADDRESSED | TASK-1-2 adds docker to dev extras. |
| R5: Dual pytest config confusion | LOW | LOW | ADDRESSED | Phase 3 consolidates to pyproject.toml. |
| R6: PYTHONPATH missing orchestrator | LOW | CERTAIN | ADDRESSED | TASK-2-1 adds orchestrator to PYTHONPATH. |

## Technical Decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| TD1 | Do NOT add `--cov=orchestrator` | Risk analyst verified coverage drops to ~71%. Adding orchestrator tests without coverage measurement catches regressions without breaking CI. |
| TD2 | Mock `cli.logger` in `test_cli.py` | Safest fix for logging interaction — no production code changes, isolates test from global state. |
| TD3 | Add `docker>=7.0.0` to dev extras | Already a runtime dependency in `orchestrator/requirements.txt`. Ensures docker-dependent test files collect in CI. |
| TD4 | Consolidate to pyproject.toml, preserve `--tb=short` | Modern standard. Must preserve `--tb=short` from pytest.ini that is missing from current pyproject.toml addopts. |
| TD5 | Interpret "make test" as unit-only | Integration/E2E tests require Docker and API tokens. Consistent with existing infrastructure design. |

*Authored-by: egg*

```yaml
# yaml-tasks
pr:
  title: "Audit test suite: add orchestrator tests to CI, consolidate config"
  description: |
    Adds the 20 orchestrator test files (~8800 lines) to CI that were previously
    excluded. Fixes the test_cli.py logging interaction that breaks combined runs.
    Adds docker to dev extras for test collection. Consolidates duplicate pytest
    configuration from pytest.ini and pyproject.toml into a single source of truth.
    Orchestrator tests run in CI but --cov=orchestrator is intentionally omitted
    to avoid breaking the 80% coverage threshold (orchestrator coverage tracked
    as follow-up). Documents pre-existing test failures and intentional skips.

    Closes #632
phases:
  - id: 1
    name: Verify orchestrator tests and fix interactions
    goal: Confirm orchestrator tests pass standalone and combined, fix docker dependency and test_cli.py logging interaction
    tasks:
      - id: TASK-1-1
        description: Run orchestrator tests standalone and capture baseline results
        acceptance: All collectable orchestrator tests pass; 2 docker-dependent collection errors identified
        files:
          - orchestrator/tests/
      - id: TASK-1-2
        description: Add docker>=7.0.0 to pyproject.toml dev extras so docker-dependent test files collect
        acceptance: After uv sync --extra dev, PYTHONPATH=orchestrator:shared pytest orchestrator/tests/ -v --collect-only shows 0 collection errors
        files:
          - pyproject.toml
      - id: TASK-1-3
        description: Fix test_cli.py::test_serve_runs_as_non_root logging interaction by mocking cli.logger in the test
        acceptance: "Combined command passes: PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v (test_serve_runs_as_non_root does not fail)"
        files:
          - orchestrator/tests/test_cli.py
      - id: TASK-1-4
        description: Run full combined suite and fix any remaining interaction failures
        acceptance: "PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v exits 0 (excluding 2 pre-existing test_session_manager failures)"
        files:
          - orchestrator/tests/
  - id: 2
    name: Add orchestrator tests to CI (without orchestrator coverage)
    goal: Include orchestrator/tests/ in CI unit test job and Bandit scan WITHOUT adding --cov=orchestrator
    tasks:
      - id: TASK-2-1
        description: "Update test.yml: add orchestrator/tests/ to pytest command, add orchestrator to PYTHONPATH, do NOT add --cov=orchestrator"
        acceptance: "CI pytest command is: PYTHONPATH=shared:gateway:orchestrator pytest tests/ gateway/tests/ orchestrator/tests/ -v --cov=gateway --cov=shared --cov=sandbox --cov-report=term-missing --cov-fail-under=80 (no --cov=orchestrator)"
        files:
          - .github/workflows/test.yml
      - id: TASK-2-2
        description: Add orchestrator to Bandit security scan targets in test.yml
        acceptance: "Bandit command becomes: .venv/bin/bandit -r gateway shared sandbox orchestrator -ll -c pyproject.toml"
        files:
          - .github/workflows/test.yml
  - id: 3
    name: Consolidate pytest configuration
    goal: Single pytest config in pyproject.toml; delete pytest.ini; preserve --tb=short
    tasks:
      - id: TASK-3-1
        description: "Migrate all pytest.ini settings into pyproject.toml: markers, filterwarnings, python_files/classes/functions, updated testpaths, merged addopts with --tb=short preserved (no --cov=orchestrator, no --cov-fail-under)"
        acceptance: "pyproject.toml [tool.pytest.ini_options] contains markers, filterwarnings, --tb=short, testpaths=[tests, gateway/tests, orchestrator/tests], and existing --cov flags"
        files:
          - pyproject.toml
      - id: TASK-3-2
        description: Delete pytest.ini
        acceptance: pytest.ini no longer exists; bare pytest uses pyproject.toml and discovers tests in all three directories
        files:
          - pytest.ini
      - id: TASK-3-3
        description: Add mypy override for orchestrator.tests.* matching gateway.tests.* pattern (disallow_untyped_defs=false, ignore_missing_imports=true)
        acceptance: mypy config includes orchestrator.tests.* override with relaxed typing rules
        files:
          - pyproject.toml
  - id: 4
    name: Verify end-to-end and document
    goal: Confirm make test runs all unit tests; document skipped tests, pre-existing failures, and coverage strategy
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
        description: "Document in PR: 6 intentionally-skipped tests, ~20 environment-conditional skips, 2 pre-existing test_session_manager failures (out of scope), and --cov=orchestrator omission rationale"
        acceptance: PR description documents all skipped tests, pre-existing failures, and coverage strategy
        files: []
```
