# Plan: Namespace .egg-state files per-pipeline to prevent merge conflicts

> Issue: #871 | Phase: plan

## Summary

Add issue/pipeline identifier prefix to all globally-named files in
`.egg-state/agent-outputs/` and `.egg-state/checks/`, matching the convention
already used by `contracts/`, `drafts/`, and `reviews/`. This eliminates merge
conflicts when concurrent pipelines merge to main (e.g., PR #869 had 4 such
conflicts).

## Approach

The implementation follows Option A from the architect analysis: prefix
filenames with the issue/pipeline identifier, matching the `{identifier}-`
convention established by `contracts/`, `drafts/`, and `reviews/`.

### Scope

All agent output files are namespaced, not just the 4 listed in the issue.
The `load_agent_output()` / `save_agent_output()` functions use the same
global pattern for ALL agent roles, so fixing only the 3 handoff files leaves
`coder-output.json`, `tester-output.json`, `documenter-output.json`, and
`task_planner-output.json` vulnerable to the same conflict. Since we modify
the shared functions, all roles benefit at zero additional cost.

Files affected:
- `agent-outputs/`: `{role}-output.json` -> `{identifier}-{role}-output.json`
- `checks/`: `implement-results.json` -> `{identifier}-implement-results.json`

### Path construction changes

Two categories of code construct these paths:

1. **Programmatic paths** — `load_agent_output()`, `save_agent_output()`,
   `collect_handoff_data()` in `shared/egg_contracts/orchestrator.py`, plus
   `_synthesize_plan_draft_from_outputs()` and `_read_tester_gaps()` in
   `pipelines.py`. These get an `identifier` parameter added.

2. **Prompt-embedded paths** — Hardcoded strings in prompt builders for
   architect, integrator, risk_analyst, checker, and check-and-fix agents
   in `pipelines.py`. These are replaced with dynamically constructed paths
   using the issue number or pipeline ID already available as parameters.

### Backward compatibility

Readers use a fallback strategy: try the new prefixed path first, fall back
to the old global path. This ensures in-flight pipelines that already wrote
to old paths continue to work. Writers always write to the new prefixed path.
The fallback can be removed after all in-flight pipelines complete.

### What does NOT change

- **Gateway phase filter** (`gateway/phase_filter.py`) — uses directory-level
  wildcards (`.egg-state/agent-outputs/*`), so file renames are transparent.
- **Gateway agent restrictions** (`gateway/agent_restrictions.py`) — same
  directory-level patterns.
- **Readonly mount enforcement** (`shared/egg_container/__init__.py`) —
  `agent-outputs/` and `checks/` are not in `_IMPLEMENT_READONLY_DIRS`.
- **Phase permissions config** (`.egg/phase-permissions.json`) — directory-level.

## Phases

### Phase 1: Core path construction (shared library)

Update the shared utility functions that build agent output file paths. This
is the foundation all other changes depend on.

The `load_agent_output()` and `save_agent_output()` functions in
`shared/egg_contracts/orchestrator.py` get an optional `identifier` parameter.
When provided, paths use `{identifier}-{role.value}-output.json`. When `None`
(default), the old `{role.value}-output.json` path is used for backward
compatibility. `load_agent_output()` additionally falls back to the old path
when the new path doesn't exist.

`collect_handoff_data()` similarly gets an `identifier` parameter that it
passes through to `load_agent_output()`.

### Phase 2: Prompt builder and reader updates (orchestrator)

Update all prompt builders and reader functions in `orchestrator/routes/pipelines.py`:

**Prompt builders** (write paths embedded in agent instructions):
- `_build_agent_prompt()` for architect, integrator, risk_analyst — replace
  hardcoded `{role}-output.json` with `{identifier}-{role}-output.json`
- `_build_checker_prompt()` — replace `implement-results.json` with
  `{identifier}-implement-results.json`
- `_build_check_and_fix_prompt()` — same change
- `_build_autofix_prompt()` — same change for any implement-results.json refs

**Reader functions**:
- `_synthesize_plan_draft_from_outputs()` — use prefixed filenames with
  fallback to old names
- `_read_tester_gaps()` — use prefixed path with fallback; update both
  callers (~line 3139 and ~line 5173) to pass the identifier

**Callers**: Update all callers of `load_agent_output`, `save_agent_output`,
and `collect_handoff_data` in `pipelines.py` to pass the identifier from their
pipeline context (`issue_number` for issue mode, `pipeline_id` for local mode).

### Phase 3: Tests

Update existing tests and add new ones:
- `orchestrator/tests/test_pipeline_prompts.py` — update assertions that check
  for `implement-results.json` in prompt content; update `TestReadTesterGaps`
  tests to use prefixed paths
- `orchestrator/tests/test_health_check_tester_coverage.py` — update mock file
  creation to use prefixed filenames
- `orchestrator/tests/test_health_check_tier1_advanced.py` — same
- `integration_tests/local_pipeline/mock-sandbox/phase-runner.sh` — update
  `RESULTS_FILE` to use prefixed filename from pipeline context variables
- Add new tests for backward-compat fallback in `load_agent_output()`:
  new path preferred, falls back to old, returns empty dict when neither exists

### Phase 4: Documentation and agent mode commands

Update all documentation and sandbox agent mode commands that reference old
globally-named filenames:
- `docs/guides/sdlc-pipeline.md` — update `{role}-output.json` convention refs
- `docs/guides/agent-development.md` — update agent output file naming tree
- `docs/architecture/orchestrator.md` — update `implement-results.json` ref
- `sandbox/.claude/commands/coder-mode.md` — update coder-output.json path
- `sandbox/.claude/commands/tester-mode.md` — update coder/tester output paths
- `sandbox/.claude/commands/integrator-mode.md` — update all output file refs
- `sandbox/.claude/commands/documenter-mode.md` — update coder/documenter refs
- `orchestrator/health_checks/tier1/phase_output.py` — update docstring (line 10)

## Test Strategy

**Existing test updates**: All tests referencing old filenames must be updated
to expect prefixed filenames. Key test files:
- `test_pipeline_prompts.py` — prompt content assertions, tester gap tests
- `test_health_check_tester_coverage.py` — mock file creation
- `test_health_check_tier1_advanced.py` — mock file creation
- `phase-runner.sh` integration test

**New tests**: Add tests for the backward-compat fallback behavior in
`load_agent_output()`:
1. When prefixed path exists, it is used
2. When only old global path exists, fallback reads it
3. When neither exists, returns empty dict
4. When both exist, prefixed path takes priority

**Gateway tests**: `tests/gateway/test_agent_restrictions.py` and
`gateway/tests/test_phase_filter.py` use directory-level wildcard patterns,
not specific filenames. These tests pass with any filename within the
`agent-outputs/` directory, so no changes are needed.

## Risks

- **In-flight pipeline breakage**: Mitigated by backward-compat fallback in
  all readers. Old globally-named files continue to be found.
- **Identifier not available in some code paths**: The `identifier` parameter
  defaults to `None`, preserving old behavior. All pipeline-context code paths
  have access to `issue_number` or `pipeline_id`.
- **Agent mode commands reference old paths**: These are documentation that
  agents use as reference. Pipeline-spawned agents receive the correct paths
  from `_build_agent_prompt()`, so the mode commands are secondary. Updating
  them in Phase 4 eliminates the discrepancy.

## Files Modified

| File | Phase | Change |
|------|-------|--------|
| `shared/egg_contracts/orchestrator.py` | 1 | Add `identifier` param to `load_agent_output`, `save_agent_output`, `collect_handoff_data`; add fallback logic |
| `orchestrator/routes/pipelines.py` | 2 | Update prompt builders, reader functions, and all callers to use prefixed paths |
| `orchestrator/tests/test_pipeline_prompts.py` | 3 | Update prompt assertions and tester gap tests |
| `orchestrator/tests/test_health_check_tester_coverage.py` | 3 | Update mock file creation |
| `orchestrator/tests/test_health_check_tier1_advanced.py` | 3 | Update mock file creation |
| `integration_tests/local_pipeline/mock-sandbox/phase-runner.sh` | 3 | Update `RESULTS_FILE` to prefixed path |
| `docs/guides/sdlc-pipeline.md` | 4 | Update naming convention references |
| `docs/guides/agent-development.md` | 4 | Update agent output file naming tree |
| `docs/architecture/orchestrator.md` | 4 | Update `implement-results.json` reference |
| `sandbox/.claude/commands/coder-mode.md` | 4 | Update output file path |
| `sandbox/.claude/commands/tester-mode.md` | 4 | Update output file paths |
| `sandbox/.claude/commands/integrator-mode.md` | 4 | Update output file paths |
| `sandbox/.claude/commands/documenter-mode.md` | 4 | Update output file paths |
| `orchestrator/health_checks/tier1/phase_output.py` | 4 | Update docstring |

---

```yaml
# yaml-tasks
pr:
  title: "Namespace .egg-state files per-pipeline to prevent merge conflicts"
  description: |
    Add issue/pipeline identifier prefix to all globally-named files in
    .egg-state/agent-outputs/ and .egg-state/checks/, matching the convention
    already used by contracts/, drafts/, and reviews/. This eliminates merge
    conflicts when concurrent pipelines merge to main. Includes backward-compat
    fallback so in-flight pipelines continue working.
phases:
  - id: 1
    name: Core path construction
    goal: Update shared utility functions to support identifier-prefixed agent output paths
    tasks:
      - id: TASK-1-1
        description: Add optional identifier parameter (int | str | None) to load_agent_output() in shared/egg_contracts/orchestrator.py. When identifier is provided, construct path as {identifier}-{role.value}-output.json. Add fallback — if prefixed path does not exist, try old {role.value}-output.json path. When identifier is None, use old path directly (backward compat).
        acceptance: load_agent_output(repo, role, identifier=871) reads from 871-{role}-output.json, falling back to {role}-output.json. load_agent_output(repo, role) reads from {role}-output.json (unchanged behavior).
        files:
          - shared/egg_contracts/orchestrator.py
      - id: TASK-1-2
        description: Add optional identifier parameter to save_agent_output() in shared/egg_contracts/orchestrator.py. When identifier is provided, write to {identifier}-{role.value}-output.json. When None, write to {role.value}-output.json (backward compat).
        acceptance: save_agent_output(repo, role, data, identifier=871) writes to 871-{role}-output.json. save_agent_output(repo, role, data) writes to {role}-output.json.
        files:
          - shared/egg_contracts/orchestrator.py
      - id: TASK-1-3
        description: Add optional identifier parameter to collect_handoff_data() in shared/egg_contracts/orchestrator.py and pass it through to load_agent_output() for each dependency.
        acceptance: collect_handoff_data(repo, role, identifier=871) reads dependency outputs from prefixed paths with fallback.
        files:
          - shared/egg_contracts/orchestrator.py
  - id: 2
    name: Prompt builder and reader updates
    goal: Update all prompt builders and orchestrator reader functions to use identifier-prefixed paths
    tasks:
      - id: TASK-2-1
        description: Update _build_agent_prompt() in orchestrator/routes/pipelines.py for architect, integrator, and risk_analyst roles. Replace hardcoded output filenames (e.g., "architect-output.json") with dynamically constructed {identifier}-prefixed paths using the issue_number/pipeline_id already available in the prompt builder context. Affects lines ~2509, ~2524, ~2590.
        acceptance: Prompts contain e.g. "871-architect-output.json" for issue 871. Verified by running test_pipeline_prompts.py.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: Update _build_checker_prompt(), _build_check_and_fix_prompt(), and _build_autofix_prompt() to replace hardcoded "implement-results.json" with {identifier}-implement-results.json. Add identifier parameter if not already available from pipeline context. Affects lines ~4063, ~4275, and related autofix prompt code.
        acceptance: Checker and autofix prompts contain e.g. "871-implement-results.json".
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Update _synthesize_plan_draft_from_outputs() (~line 4335) to construct prefixed filenames in the agent_files list. Derive identifier from the existing pipeline_mode/issue_number parameters. Fall back to old filenames if prefixed files don't exist.
        acceptance: Function reads from {identifier}-architect-output.json first, falls back to architect-output.json. Same for risk_analyst.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-4
        description: Update _read_tester_gaps() (~line 1619) to accept identifier parameter and construct prefixed path with fallback. Update both callers (~line 3139 and ~line 5173) to pass identifier from pipeline context.
        acceptance: Function reads from {identifier}-tester-output.json first, falls back to tester-output.json.
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-5
        description: Update all remaining callers of load_agent_output(), save_agent_output(), and collect_handoff_data() in orchestrator/routes/pipelines.py to pass the identifier from pipeline context.
        acceptance: No calls to these functions without identifier in pipeline code paths. grep confirms all invocations pass identifier.
        files:
          - orchestrator/routes/pipelines.py
  - id: 3
    name: Tests
    goal: Update all existing tests and add backward-compat fallback tests
    tasks:
      - id: TASK-3-1
        description: Update test_pipeline_prompts.py — change all assertions that check for "implement-results.json" in prompt content to expect prefixed filenames. Update TestReadTesterGaps tests to create/expect prefixed filenames.
        acceptance: All tests in test_pipeline_prompts.py pass with prefixed filenames.
        files:
          - orchestrator/tests/test_pipeline_prompts.py
      - id: TASK-3-2
        description: Update test_health_check_tester_coverage.py and test_health_check_tier1_advanced.py — update mock file creation to use prefixed filenames where these tests create/reference architect-output.json or similar files.
        acceptance: Health check tests pass with prefixed filenames.
        files:
          - orchestrator/tests/test_health_check_tester_coverage.py
          - orchestrator/tests/test_health_check_tier1_advanced.py
      - id: TASK-3-3
        description: Update integration_tests/local_pipeline/mock-sandbox/phase-runner.sh — update RESULTS_FILE assignment (~line 143) to use prefixed filename derived from pipeline context environment variables (EGG_ISSUE_NUMBER or EGG_PIPELINE_ID).
        acceptance: Integration test writes to {identifier}-implement-results.json. Local pipeline integration test passes.
        files:
          - integration_tests/local_pipeline/mock-sandbox/phase-runner.sh
      - id: TASK-3-4
        description: Add tests for backward-compat fallback in load_agent_output(). Test cases — (1) prefixed path exists and is returned, (2) only old global path exists and fallback reads it, (3) neither exists and empty dict returned, (4) both exist and prefixed path takes priority.
        acceptance: All 4 fallback test cases pass. Tests placed alongside existing orchestrator contract tests.
        files:
          - shared/egg_contracts/tests/test_orchestrator.py
  - id: 4
    name: Documentation and agent mode commands
    goal: Update all documentation and sandbox commands referencing old globally-named filenames
    tasks:
      - id: TASK-4-1
        description: Update docs/guides/sdlc-pipeline.md — change references to {role}-output.json naming convention (lines ~362, ~717) to document {identifier}-{role}-output.json.
        acceptance: Documentation reflects new naming convention with examples.
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-4-2
        description: Update docs/guides/agent-development.md — update the agent output file naming tree (lines ~215-218) to show {identifier}-prefixed filenames.
        acceptance: File tree example shows prefixed filenames.
        files:
          - docs/guides/agent-development.md
      - id: TASK-4-3
        description: Update docs/architecture/orchestrator.md — update implement-results.json reference (line ~121) to show prefixed filename.
        acceptance: Architecture doc reflects new naming.
        files:
          - docs/architecture/orchestrator.md
      - id: TASK-4-4
        description: Update sandbox/.claude/commands/ agent mode files (coder-mode.md, tester-mode.md, integrator-mode.md, documenter-mode.md) — replace all hardcoded {role}-output.json paths with a note that paths are prefixed with the issue/pipeline identifier.
        acceptance: All agent mode command files reference {identifier}-prefixed paths.
        files:
          - sandbox/.claude/commands/coder-mode.md
          - sandbox/.claude/commands/tester-mode.md
          - sandbox/.claude/commands/integrator-mode.md
          - sandbox/.claude/commands/documenter-mode.md
      - id: TASK-4-5
        description: Update docstring in orchestrator/health_checks/tier1/phase_output.py (line ~10) to reflect new naming convention.
        acceptance: Docstring mentions {identifier}-prefixed filenames.
        files:
          - orchestrator/health_checks/tier1/phase_output.py
```

---

*Authored-by: egg*
