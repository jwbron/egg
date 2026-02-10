# Plan: Run self-improvement workflow after every workflow completes

> Issue: #289 | Phase: plan

## Summary

Implement event-triggered self-improvement analysis that runs on workflow failures while avoiding redundant processing. The approach combines error-heuristic filtering (Option C) with per-workflow concurrency groups and issue-based deduplication (Option D) as directed by the human reviewer. Analysis will only process a single failed run per workflow until the corresponding self-improvement issue is closed, preventing continuous reprocessing of persistently failing workflows.

## Implementation Phases

### Phase 1: Add workflow_run trigger with per-workflow concurrency

**Goal**: Enable self-improvement to trigger on workflow completions with proper concurrency isolation.

**Tasks**:
- [TASK-1-1] Add `workflow_run` trigger to `self-improvement.yml` for egg workflows — Acceptance: Workflow triggers on completion of on-mention, on-pull-request, on-check-failure, and self-improvement workflows
- [TASK-1-2] Add gate job to filter to failure conclusions only — Acceptance: Job only proceeds when `github.event.workflow_run.conclusion == 'failure'`
- [TASK-1-3] Implement per-workflow concurrency groups — Acceptance: Concurrent failures of the same workflow are queued (not cancelled), different workflows can run in parallel

**Dependencies**: None

**Exit criteria**: Self-improvement triggers on workflow failures with per-workflow isolation; nightly schedule continues to work unchanged.

### Phase 2: Implement single-run analysis mode

**Goal**: Support analyzing a specific run ID instead of a time window, for focused per-failure analysis.

**Tasks**:
- [TASK-2-1] Add `--run-id` parameter to `collect.py` CLI — Acceptance: CLI accepts `--run-id <id>` flag that overrides `--since-hours` behavior
- [TASK-2-2] Add `fetch_single_run` method to `GHALogCollector` — Acceptance: Method fetches and returns a single run by ID, returning empty list if run doesn't match egg workflows
- [TASK-2-3] Update `collect.py` main logic to use single-run mode when `--run-id` provided — Acceptance: When `--run-id` is provided, only that run is analyzed (no time window filtering)
- [TASK-2-4] Add `run_id` input parameter to `self-improvement.yml` workflow_dispatch and workflow_call — Acceptance: Workflow can be invoked with specific run ID
- [TASK-2-5] Pass `github.event.workflow_run.id` to collect script when triggered by workflow_run — Acceptance: Event-triggered runs analyze only the failed run that triggered them

**Dependencies**: Phase 1

**Exit criteria**: Event-triggered analysis processes only the single failed run; nightly analysis continues to use time window.

### Phase 3: Implement issue-based deduplication

**Goal**: Prevent continuous reprocessing by tracking which workflows have open self-improvement issues.

**Tasks**:
- [TASK-3-1] Create `check_open_issues.py` utility to query for open self-improvement issues by workflow — Acceptance: Script returns whether an open issue exists for a given workflow name using `gh issue list --label self-improvement`
- [TASK-3-2] Add pre-check step to self-improvement workflow gate job — Acceptance: Workflow skips if an open `self-improvement` issue already exists that mentions the failed workflow name
- [TASK-3-3] Update self-improvement issue template to include workflow name in a machine-parseable format — Acceptance: Issues created include `workflow: <name>` in body for reliable detection

**Dependencies**: Phase 2

**Exit criteria**: Repeated failures of the same workflow do not spawn duplicate analysis runs while an issue is open.

### Phase 4: Add error-heuristic pre-filtering

**Goal**: Further reduce unnecessary egg invocations by pre-filtering logs for actionable error patterns.

**Tasks**:
- [TASK-4-1] Define error pattern configuration in `config.py` — Acceptance: `ERROR_PATTERNS` list contains regex patterns for actionable errors (ToolError, GatewayTimeout, APIError, CRITICAL, Traceback, etc.)
- [TASK-4-2] Add `has_actionable_errors` function to `collect.py` — Acceptance: Function returns True if log content matches any error pattern
- [TASK-4-3] Add `--require-actionable-errors` flag to `collect.py` — Acceptance: When flag is set, runs without actionable error patterns are excluded from analysis
- [TASK-4-4] Update workflow_run-triggered collect step to use `--require-actionable-errors` — Acceptance: Event-triggered analysis only proceeds if logs contain actionable error patterns; nightly analysis does not use this flag (comprehensive scan)

**Dependencies**: Phase 3

**Exit criteria**: Low-signal failures (e.g., infrastructure timeouts without actionable content) do not trigger egg analysis.

### Phase 5: Prevent infinite recursion for self-improvement failures

**Goal**: Ensure self-improvement failures are analyzed without creating infinite loops.

**Tasks**:
- [TASK-5-1] Add recursion detection based on triggering workflow — Acceptance: When self-improvement fails and triggers itself, it follows the same deduplication logic (one open issue per workflow)
- [TASK-5-2] Add special handling to limit self-improvement recursion depth — Acceptance: Self-improvement failures during event-triggered runs are queued for next nightly analysis instead of immediate re-trigger
- [TASK-5-3] Add workflow_run conclusion check for self-improvement workflow name — Acceptance: When triggered workflow is self-improvement.yml, apply more conservative skip logic

**Dependencies**: Phase 3 (uses issue-based deduplication)

**Exit criteria**: Self-improvement failures are analyzed but cannot create infinite trigger loops.

## Test Strategy

- **Unit tests**:
  - Test `--run-id` parameter parsing in `collect.py` (`test_self_improvement.py`)
  - Test `fetch_single_run` method in `GHALogCollector` with mock subprocess calls
  - Test `has_actionable_errors` function with various log patterns
  - Test `check_open_issues` utility with mock `gh issue list` output

- **Integration tests**:
  - Test end-to-end collect script with `--run-id` flag using mock API responses
  - Verify partitioning is skipped in single-run mode

- **Manual testing**:
  - Trigger workflow_dispatch with explicit run_id to verify single-run analysis
  - Verify nightly schedule still analyzes full 24-hour window
  - Create a test failure, verify self-improvement triggers and creates issue
  - Create second failure of same workflow, verify deduplication prevents re-analysis
  - Close the issue, verify next failure triggers analysis again

## Rollback Plan

1. **Immediate rollback**: Revert the `workflow_run` trigger addition in `self-improvement.yml`:
   ```bash
   git checkout origin/main -- .github/workflows/self-improvement.yml
   git commit -m "Rollback: Remove workflow_run trigger from self-improvement"
   git push origin HEAD
   ```

2. **Partial rollback** (keep single-run mode, disable auto-trigger):
   - Comment out the `workflow_run` trigger block
   - Keep `--run-id` functionality for manual use

3. **Code changes are additive**: The `--since-hours` path remains functional; nightly analysis is unaffected if event-triggered path has issues.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Infinite trigger loop from self-improvement failures | Medium | High | Issue-based deduplication + special handling for self-improvement workflow (Phase 5) |
| High API/compute cost from frequent failures | Medium | Medium | Per-workflow concurrency, issue deduplication, error-heuristic filtering |
| Missing new error types due to heuristic filtering | Low | Low | Keep nightly comprehensive analysis; periodically review and update error patterns |
| Race condition in issue deduplication check | Low | Low | Use atomic gh CLI operations; accept occasional duplicate as acceptable |
| Workflow name changes break deduplication | Low | Medium | Use workflow file path (stable) rather than display name |

## Migration Notes

- **No database migrations required**
- **Configuration changes**: New `ERROR_PATTERNS` config in `config.py`
- **Workflow changes**: `self-improvement.yml` gains new trigger and inputs (backwards compatible)
- **No breaking changes**: Existing nightly schedule and workflow_dispatch behavior unchanged

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Trigger self-improvement on workflow failures"
  description: |
    Run the self-improvement workflow when egg workflows fail, with smart deduplication
    to avoid reprocessing the same failure. Adds per-workflow concurrency groups,
    single-run analysis mode, and issue-based deduplication to prevent continuous
    reprocessing until fixes are applied.

    Closes #289
phases:
  - id: 1
    name: Workflow trigger and concurrency
    goal: Enable self-improvement to trigger on workflow completions with proper concurrency isolation
    tasks:
      - id: TASK-1-1
        description: Add workflow_run trigger to self-improvement.yml for egg workflows
        acceptance: Workflow triggers on completion of on-mention, on-pull-request, on-check-failure, and self-improvement workflows
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-1-2
        description: Add gate job to filter to failure conclusions only
        acceptance: Job only proceeds when github.event.workflow_run.conclusion == 'failure'
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-1-3
        description: Implement per-workflow concurrency groups
        acceptance: Concurrent failures of the same workflow are queued, different workflows can run in parallel
        files:
          - .github/workflows/self-improvement.yml
  - id: 2
    name: Single-run analysis mode
    goal: Support analyzing a specific run ID instead of a time window
    tasks:
      - id: TASK-2-1
        description: Add --run-id parameter to collect.py CLI
        acceptance: CLI accepts --run-id flag that overrides --since-hours behavior
        files:
          - sandbox/egg_lib/self_improvement/collect.py
      - id: TASK-2-2
        description: Add fetch_single_run method to GHALogCollector
        acceptance: Method fetches and returns a single run by ID
        files:
          - sandbox/egg_lib/self_improvement/collectors/gha.py
      - id: TASK-2-3
        description: Update collect.py main logic to use single-run mode when --run-id provided
        acceptance: When --run-id is provided, only that run is analyzed
        files:
          - sandbox/egg_lib/self_improvement/collect.py
      - id: TASK-2-4
        description: Add run_id input parameter to self-improvement.yml
        acceptance: Workflow can be invoked with specific run ID
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-2-5
        description: Pass github.event.workflow_run.id to collect script when triggered by workflow_run
        acceptance: Event-triggered runs analyze only the failed run that triggered them
        files:
          - .github/workflows/self-improvement.yml
  - id: 3
    name: Issue-based deduplication
    goal: Prevent continuous reprocessing by tracking open self-improvement issues
    tasks:
      - id: TASK-3-1
        description: Create check_open_issues.py utility to query for open issues by workflow
        acceptance: Script returns whether an open issue exists for a given workflow name
        files:
          - sandbox/egg_lib/self_improvement/check_open_issues.py
      - id: TASK-3-2
        description: Add pre-check step to self-improvement workflow gate job
        acceptance: Workflow skips if an open self-improvement issue exists for the workflow
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-3-3
        description: Update self-improvement issue template to include workflow name
        acceptance: Issues include workflow name in machine-parseable format
        files:
          - .github/workflows/self-improvement.yml
  - id: 4
    name: Error-heuristic pre-filtering
    goal: Reduce unnecessary egg invocations by pre-filtering for actionable errors
    tasks:
      - id: TASK-4-1
        description: Define error pattern configuration in config.py
        acceptance: ERROR_PATTERNS list contains regex patterns for actionable errors
        files:
          - sandbox/egg_lib/self_improvement/config.py
      - id: TASK-4-2
        description: Add has_actionable_errors function to collect.py
        acceptance: Function returns True if log content matches any error pattern
        files:
          - sandbox/egg_lib/self_improvement/collect.py
      - id: TASK-4-3
        description: Add --require-actionable-errors flag to collect.py
        acceptance: Runs without actionable error patterns are excluded when flag is set
        files:
          - sandbox/egg_lib/self_improvement/collect.py
      - id: TASK-4-4
        description: Update workflow_run-triggered collect step to use --require-actionable-errors
        acceptance: Event-triggered analysis only proceeds if logs contain actionable errors
        files:
          - .github/workflows/self-improvement.yml
  - id: 5
    name: Self-improvement recursion prevention
    goal: Ensure self-improvement failures are analyzed without infinite loops
    tasks:
      - id: TASK-5-1
        description: Add recursion detection based on triggering workflow
        acceptance: Self-improvement failures follow same deduplication logic
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-5-2
        description: Add special handling to limit self-improvement recursion depth
        acceptance: Self-improvement failures during event-triggered runs are queued for nightly
        files:
          - .github/workflows/self-improvement.yml
      - id: TASK-5-3
        description: Add workflow_run conclusion check for self-improvement workflow name
        acceptance: More conservative skip logic when triggered workflow is self-improvement.yml
        files:
          - .github/workflows/self-improvement.yml
```

---

*Authored-by: egg*
