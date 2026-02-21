# Plan: Scope agent prompts to role-relevant context only

> Issue: #835 | Phase: plan | Pipeline: issue-835

## Summary

This PR replaces the one-size-fits-all `pipeline.prompt` embedding in agent prompts
with role-appropriate context summaries. Two new helpers — `_summarize_issue()` and
`_build_role_context()` — are introduced in `orchestrator/routes/pipelines.py`.
`_build_agent_prompt()` is refactored to use these helpers for tester, documenter, and
integrator roles while preserving the full issue body for architect, task_planner, and
risk_analyst. Additionally, `_build_phase_scoped_prompt()` is updated to embed only
the plan overview and current phase detail instead of the full plan verbatim.

The approach follows Option A from the architecture analysis: a centralized
`_build_role_context()` dispatch helper. This keeps the change manageable (one
production file, one test file), provides clean testability, and aligns with the
issue's proposed design.

## Implementation Phases

### Phase 1: Core helper functions and _build_agent_prompt refactor

**Goal**: Introduce `_summarize_issue()` and `_build_role_context()` helpers, refactor
`_build_agent_prompt()` to use role-scoped context instead of the raw issue body, and
update all call sites to thread the Pipeline object through.

**Tasks**:

- [TASK-1-1] Implement `_summarize_issue(title, body)` helper that returns a 1-2
  sentence summary from the issue title and first paragraph of the body (truncated at
  ~200 chars at a sentence boundary). Falls back to title-only if body is empty/None.
  — Acceptance: Function returns title + first paragraph for normal issues; handles
  None/empty inputs gracefully; output ≤ 250 chars.

- [TASK-1-2] Implement `_build_role_context(role, pipeline, phase_obj, handoff_summary)`
  helper that dispatches by role. For architect/task_planner/risk_analyst: returns
  `pipeline.prompt` verbatim (full issue body). For tester: returns summary + task
  descriptions with acceptance criteria + files_affected + "For more context" pointers.
  For documenter: returns summary + implementation summary from handoff + files changed
  + pointers. For integrator: returns summary + structured phase summary + test results
  from handoff + pointers. For all others: returns summary + pointers.
  — Acceptance: Each role path returns role-appropriate markdown; scoped roles omit
  the full issue body; analysis roles include the full issue body; all scoped roles
  include "For more context" section with `gh issue view` and file path pointers.

- [TASK-1-3] Refactor `_build_agent_prompt()` (lines 1942-2240) to accept an optional
  `pipeline: Pipeline | None = None` and `phase_obj = None` parameter. Replace the
  `if prompt:` block (lines 2014-2018) with a call to `_build_role_context()` when
  `pipeline` is provided, falling back to the existing `prompt` embedding for backward
  compatibility.
  — Acceptance: When `pipeline` is provided, the "## Task Description" section
  contains role-scoped context from `_build_role_context()` instead of the raw issue
  body. When only `prompt` is provided (no pipeline), behavior is unchanged.

- [TASK-1-4] Update all four call sites to pass the Pipeline object: (1) tester in
  `_run_tier3_implement` (line 2555), (2) documenter in `_run_tier3_implement`
  (line 2631), (3) integrator in `_run_tier3_implement` (line 2961), (4) the role
  loop in `_run_multi_agent_phase` (line 3108). For Tier 3 tester/documenter, also
  pass the current `phase_obj` so `_build_role_context()` can include phase-specific
  task details.
  — Acceptance: All four call sites pass `pipeline=pipeline` and where applicable
  `phase_obj=phase_obj`. No call site passes `prompt=pipeline.prompt` when `pipeline`
  is available.

- [TASK-1-5] Add unit tests for `_summarize_issue()` and `_build_role_context()` in
  `test_pipeline_prompts.py`. Test cases: (a) `_summarize_issue` with short body, long
  body, None body, empty body, markdown-heavy body. (b) `_build_role_context` for each
  role: verify tester context contains task descriptions and acceptance criteria but not
  full issue body; verify architect context contains full issue body; verify all scoped
  roles contain "For more context" pointers; verify integrator context contains phase
  summary structure. Follow existing test patterns (string presence/absence assertions,
  mock Pipeline objects).
  — Acceptance: All new tests pass. Tests verify presence of expected sections and
  absence of full issue body for scoped roles.

**Dependencies**: None

**Exit criteria**: `_build_agent_prompt()` uses `_build_role_context()` for all
non-coder, non-reviewer roles. Tester/documenter/integrator prompts contain focused
context. Architect/task_planner/risk_analyst prompts still contain the full issue body.
All new and existing tests pass.

### Phase 2: Phase-scoped plan filtering

**Goal**: Update `_build_phase_scoped_prompt()` to embed only the plan overview and
current phase detail instead of the full plan verbatim. Other phases appear as one-line
summaries for orientation.

**Tasks**:

- [TASK-2-1] Implement plan filtering logic in `_build_phase_scoped_prompt()` (lines
  2293-2304). Replace the full plan embedding with: (a) the plan's overview/summary
  section (everything before the first `### Phase` heading), (b) the current phase's
  full detail section, (c) other phases as one-line summaries ("Phase N: [name] —
  [goal]"), (d) a pointer: "For full plan: `cat .egg-state/drafts/<plan-file>`". Parse
  the plan by heading level (## for top-level sections, ### for phases). If parsing
  fails (unexpected format), fall back to embedding the full plan.
  — Acceptance: A Tier 3 coder prompt for phase 2 contains the plan overview and
  phase 2's full detail, but only one-line summaries for phases 1 and 3. Includes a
  pointer to the full plan file. Falls back gracefully on malformed plans.

- [TASK-2-2] Add unit tests for plan filtering in `test_pipeline_prompts.py`. Test
  cases: (a) standard plan with 3 phases — verify only current phase detail is
  included, others are summarized. (b) plan with non-standard headings — verify
  fallback to full plan. (c) plan with single phase — verify no summary lines for
  other phases. Follow existing `TestBuildPhasePromptPlanEmbedding` patterns.
  — Acceptance: All new tests pass. Tests use string presence/absence to verify
  correct filtering behavior.

- [TASK-2-3] Run full test suite and verify no regressions. Fix any test failures
  caused by the prompt content changes (existing tests that assert specific prompt
  content may need updating).
  — Acceptance: `pytest orchestrator/tests/test_pipeline_prompts.py` passes. No
  pre-existing test failures introduced.

**Dependencies**: Phase 1

**Exit criteria**: Phase-scoped coder prompts contain plan overview + current phase
detail only. All tests pass.

## Test Strategy

- **Unit tests**: New `TestSummarizeIssue` class tests the summary extraction helper
  with edge cases (short/long/empty/None bodies, markdown-heavy content).
  New `TestBuildRoleContext` class tests each role path of `_build_role_context()` with
  mock Pipeline objects, verifying correct content inclusion/exclusion per role.
  New `TestPhaseScopedPlanFiltering` class tests `_build_phase_scoped_prompt()` plan
  filtering with multi-phase plans, single-phase plans, and malformed plans.
- **Integration tests**: Run existing `test_pipeline_prompts.py` test suite end-to-end
  to verify no regressions in checker, autofix, phase prompt, revision mode, shared
  criteria, or contract rendering tests.
- **Manual testing**: Not required — changes are fully testable via unit tests since
  all affected functions are pure prompt-building functions with deterministic output.

## Rollback Plan

All changes are in two files (`orchestrator/routes/pipelines.py` and
`orchestrator/tests/test_pipeline_prompts.py`). Revert the PR to restore the original
`pipeline.prompt` embedding behavior. No database migrations, config changes, or
schema changes involved.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Issue body parsing fragility in `_summarize_issue()` | Low | Low | Simple approach: title + first paragraph. Falls back to title-only on empty/malformed input. |
| Plan heading structure varies between drafts | Low | Medium | Plans follow standardized template. Parse by heading level. Fall back to full plan embedding on failure. |
| Agent behavior regression — scoped agents lack context | Low | Medium | Every scoped prompt includes "For more context" pointers. Summary provides orientation. Agents can self-serve via CLI. |
| Existing tests break due to prompt content changes | Medium | Low | Tests use string presence/absence. Update assertions to match new prompt structure. Run full suite in Phase 2 TASK-2-3. |

## Migration Notes

No database migrations, config changes, or breaking changes. The only observable
difference is prompt content — agents receive focused context instead of the full
issue body. The `_build_agent_prompt()` function signature gains two optional
parameters (`pipeline`, `phase_obj`) with backward-compatible defaults.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Scope agent prompts to role-relevant context"
  description: |
    Replace the one-size-fits-all pipeline.prompt embedding in agent prompts
    with role-appropriate context summaries. Introduces _summarize_issue() and
    _build_role_context() helpers, refactors _build_agent_prompt() to use
    role-scoped context for tester/documenter/integrator while preserving full
    issue body for analysis roles, and filters plan embedding in
    _build_phase_scoped_prompt() to show only the overview and current phase
    detail. Closes #835.
phases:
  - id: 1
    name: Core helper functions and _build_agent_prompt refactor
    goal: Introduce role-context helpers and refactor prompt building to use role-scoped context instead of raw issue body
    tasks:
      - id: TASK-1-1
        description: Implement _summarize_issue(title, body) helper that returns a 1-2 sentence summary from the issue title and first paragraph
        acceptance: Function returns title + first paragraph for normal issues; handles None/empty inputs; output ≤ 250 chars
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-2
        description: Implement _build_role_context(role, pipeline, phase_obj, handoff_summary) helper with role-specific context dispatch
        acceptance: Scoped roles (tester, documenter, integrator) get focused context without full issue body; analysis roles get full body; all scoped roles include "For more context" pointers
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-3
        description: Refactor _build_agent_prompt() to accept optional pipeline and phase_obj parameters; replace if-prompt block with _build_role_context() call
        acceptance: When pipeline is provided, Task Description contains role-scoped context; when only prompt is provided, behavior is unchanged
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-4
        description: Update all four _build_agent_prompt() call sites to pass pipeline object and phase_obj where applicable
        acceptance: All call sites pass pipeline=pipeline; Tier 3 tester/documenter also pass phase_obj; no call site uses prompt=pipeline.prompt when pipeline is available
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-5
        description: Add unit tests for _summarize_issue() and _build_role_context() covering all role paths and edge cases
        acceptance: Tests verify presence of expected sections and absence of full issue body for scoped roles; all tests pass
        files:
          - orchestrator/tests/test_pipeline_prompts.py
  - id: 2
    name: Phase-scoped plan filtering
    goal: Filter plan embedding in _build_phase_scoped_prompt() to overview + current phase detail only
    dependencies:
      - phase-1
    tasks:
      - id: TASK-2-1
        description: Implement plan filtering in _build_phase_scoped_prompt() to embed overview + current phase detail + one-line summaries of other phases
        acceptance: Tier 3 coder prompt for phase N contains plan overview and phase N detail but only summaries for other phases; includes pointer to full plan; falls back on malformed plans
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-2
        description: Add unit tests for plan filtering with multi-phase plans, single-phase plans, and malformed plans
        acceptance: Tests verify correct filtering behavior using string presence/absence; all tests pass
        files:
          - orchestrator/tests/test_pipeline_prompts.py
      - id: TASK-2-3
        description: Run full prompt test suite and fix any regressions from prompt content changes
        acceptance: pytest orchestrator/tests/test_pipeline_prompts.py passes with no failures
        files:
          - orchestrator/tests/test_pipeline_prompts.py
```

---

*Authored-by: egg*
