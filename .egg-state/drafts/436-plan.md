# Plan: Unify SDLC phases into single reusable work loop

> Issue: #436 | Phase: plan

## Summary

This plan implements a unified work loop for all SDLC pipeline phases (refine, plan, implement) by creating a parameterized reusable workflow (`sdlc-work-loop.yml`) that handles the generate/review/respond cycle with configurable prompts, check DAGs, and human review mechanisms. The main `sdlc-pipeline.yml` becomes a thin orchestrator that calls the work loop with phase-specific configuration. This approach directly addresses the code duplication (~2,600 lines to ~1,200 lines) while preserving all existing behavior.

Based on approved decisions from the refine phase:
- **Check scripts location**: `.github/scripts/` (collocated with workflows)
- **Review unification**: All review (including implement phase) unified into work loop
- **Check DAG behavior**: Merge conflict fixer first → lint/test in parallel → check fixer last → review

## Implementation Phases

### Phase 1: Contract Schema Extension

**Goal**: Extend the contract schema to support phase configuration and check DAG definitions, providing the data model for the unified work loop.

**Tasks**:
- [TASK-1-1] Add `PhaseConfig` and `CheckDefinition` models to contract schema — Acceptance: New Pydantic models exist with validation, unit tests pass
- [TASK-1-2] Add `phase_config` field to Contract model — Acceptance: Contract can serialize/deserialize with phase config, existing contracts remain valid
- [TASK-1-3] Update JSON schema file to match Pydantic models — Acceptance: Schema validates sample contracts with phase configs
- [TASK-1-4] Add default phase configurations as constants — Acceptance: Default configs exist for refine, plan, implement phases

**Dependencies**: None

**Exit criteria**: Contract schema supports phase configs with checks, existing tests pass, new model tests pass

### Phase 2: Check Scripts Infrastructure

**Goal**: Create the check script infrastructure and implement the initial set of checks as executable scripts.

**Tasks**:
- [TASK-2-1] Create check runner framework script (`run-check.sh`) — Acceptance: Script executes arbitrary check by name, handles exit codes, supports retry
- [TASK-2-2] Implement merge conflict check script — Acceptance: Script detects merge conflicts, returns structured JSON result
- [TASK-2-3] Implement draft validation check script — Acceptance: Script validates draft exists and has expected sections for refine/plan
- [TASK-2-4] Implement plan YAML extraction check script — Acceptance: Script extracts YAML tasks from plan, validates structure
- [TASK-2-5] Implement lint check wrapper script — Acceptance: Script runs linters, captures failures, returns JSON result
- [TASK-2-6] Implement test check wrapper script — Acceptance: Script runs tests, captures failures, returns JSON result
- [TASK-2-7] Implement check fixer dispatcher script — Acceptance: Script runs appropriate fixers based on failed checks

**Dependencies**: Phase 1 (needs CheckDefinition model for result schema)

**Exit criteria**: All check scripts exist, are executable, produce consistent JSON output format

### Phase 3: Unified Work Loop Workflow

**Goal**: Create the `sdlc-work-loop.yml` reusable workflow that implements the generate/review/respond loop with configurable phases.

**Tasks**:
- [TASK-3-1] Create work loop workflow skeleton with inputs schema — Acceptance: Workflow accepts all required inputs (phase, issue_number, prompts, checks, etc.)
- [TASK-3-2] Implement `work` job with parameterized prompt building — Acceptance: Job builds prompt from trusted main, runs agent, handles output
- [TASK-3-3] Implement `run-checks` job with DAG execution — Acceptance: Job runs checks in DAG order (merge-fix → parallel lint/test → fixer)
- [TASK-3-4] Implement `review` job with unified review logic — Acceptance: Job runs reviewer agent, parses verdict, handles all phases
- [TASK-3-5] Implement `respond` job for feedback handling — Acceptance: Job updates contract, triggers re-dispatch or human gate
- [TASK-3-6] Implement `human-gate` job for issue checkbox approval — Acceptance: Job posts draft to issue with approval checkbox
- [TASK-3-7] Implement `human-gate-pr` job for PR-based approval — Acceptance: Job creates/updates PR, waits for human review
- [TASK-3-8] Add circuit breaker logic throughout work loop — Acceptance: Loop respects max cycles, escalates appropriately

**Dependencies**: Phase 1 (contract schema), Phase 2 (check scripts)

**Exit criteria**: Work loop workflow is complete and can be called with different phase configs

### Phase 4: Refactor Main Pipeline

**Goal**: Refactor `sdlc-pipeline.yml` to use the unified work loop for all phases, removing duplicated code.

**Tasks**:
- [TASK-4-1] Refactor `init` job to set up phase configs in contract — Acceptance: Init job writes phase config to contract based on starting phase
- [TASK-4-2] Replace `refine` job triad with work loop call — Acceptance: Refine phase uses sdlc-work-loop.yml, behavior unchanged
- [TASK-4-3] Replace `plan` job triad with work loop call — Acceptance: Plan phase uses sdlc-work-loop.yml, behavior unchanged
- [TASK-4-4] Replace `implement` job group with work loop call — Acceptance: Implement phase uses sdlc-work-loop.yml, behavior unchanged
- [TASK-4-5] Update job dependencies and conditionals — Acceptance: Pipeline flows correctly between phases
- [TASK-4-6] Remove orphaned jobs (old phase-specific jobs) — Acceptance: No dead code remains, workflow is ~200 lines of orchestration

**Dependencies**: Phase 3 (work loop must be complete)

**Exit criteria**: Main pipeline uses work loop for all phases, line count reduced significantly

### Phase 5: HITL Integration

**Goal**: Ensure the HITL decision handler integrates cleanly with the unified work loop.

**Tasks**:
- [TASK-5-1] Update HITL handler for unified phase approval marker — Acceptance: Handler recognizes approval checkboxes from all phases
- [TASK-5-2] Verify phase transition logic works with new contract structure — Acceptance: Approving refine→plan→implement transitions work
- [TASK-5-3] Test escalation flow through unified work loop — Acceptance: Circuit breaker escalation posts correctly, checkbox works

**Dependencies**: Phase 4 (pipeline must use work loop)

**Exit criteria**: HITL decisions trigger correct transitions for all phases

### Phase 6: Prompt Builder Consolidation

**Goal**: Consolidate the review prompt builders into a unified structure that the work loop can call.

**Tasks**:
- [TASK-6-1] Create unified review prompt builder script — Acceptance: Single script handles all phase reviews via environment variables
- [TASK-6-2] Update work loop to use unified review prompt builder — Acceptance: Work loop calls single script, passes phase context
- [TASK-6-3] Deprecate phase-specific review prompt scripts — Acceptance: Old scripts remain as wrappers calling unified script (for backwards compat during transition)

**Dependencies**: Phase 3 (work loop exists)

**Exit criteria**: Single review prompt builder handles all phases

### Phase 7: Testing and Validation

**Goal**: Comprehensive testing of the unified pipeline to ensure no regressions.

**Tasks**:
- [TASK-7-1] Create workflow validation test for work loop inputs — Acceptance: Test validates all input combinations are accepted
- [TASK-7-2] Test refine phase end-to-end on test issue — Acceptance: Refine completes successfully with review cycle
- [TASK-7-3] Test plan phase end-to-end on test issue — Acceptance: Plan completes successfully with review cycle
- [TASK-7-4] Test implement phase end-to-end on test issue — Acceptance: Implement creates PR, runs checks, gets reviewed
- [TASK-7-5] Test circuit breaker escalation scenario — Acceptance: Escalation triggers after max cycles
- [TASK-7-6] Test human override of escalated phase — Acceptance: Human can approve despite failed reviews

**Dependencies**: Phase 5 (full pipeline must be integrated)

**Exit criteria**: All phases work end-to-end, escalation works, no regressions

### Phase 8: Documentation and Cleanup

**Goal**: Update documentation and clean up any remaining artifacts.

**Tasks**:
- [TASK-8-1] Update SDLC pipeline guide in docs — Acceptance: Guide reflects new architecture, includes check DAG config
- [TASK-8-2] Update ADR with architectural changes — Acceptance: ADR documents unified work loop decision
- [TASK-8-3] Remove deprecated code and comments — Acceptance: No TODO comments referencing old structure

**Dependencies**: Phase 7 (testing complete)

**Exit criteria**: Documentation is current, codebase is clean

## Test Strategy

- **Unit tests**:
  - Pydantic model validation tests for PhaseConfig, CheckDefinition
  - Contract serialization/deserialization with new fields
  - Check script output format tests

- **Integration tests**:
  - Work loop workflow syntax validation (`act` or manual validation)
  - Check DAG execution order verification
  - Contract state transitions through work loop

- **Manual testing**:
  - Run full pipeline on a test issue through all phases
  - Verify HITL approval triggers correct transitions
  - Test escalation by forcing review failures
  - Verify existing `reusable-review.yml` callers still work

## Rollback Plan

Since this is a big-bang migration with no backwards compatibility requirement:

1. **Git-based rollback**: Revert the merge commit if issues are found in production
   ```bash
   git revert -m 1 <merge-commit>
   ```

2. **Parallel testing**: Before merging, can run both old and new pipelines on different issues to compare behavior

3. **Feature flag option**: If needed, can add an input to `sdlc-pipeline.yml` to choose legacy vs unified mode during transition

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Incomplete implementation (repeat of #430) | Medium | High | Detailed task breakdown, all tasks must be done before PR, comprehensive testing |
| Regression in existing behavior | Medium | High | End-to-end testing of all phases, comparison with current output |
| Performance degradation from workflow_call | Low | Low | Minimize nesting, use job parallelism where possible |
| Complex debugging due to abstraction | Medium | Medium | Comprehensive logging, clear error messages, maintain audit trail |
| HITL handler incompatibility | Low | Medium | Test HITL integration early in Phase 5 |

## Migration Notes

- **No database migrations**: All state is in contract JSON files on branches
- **No config changes required**: Consuming workflows that call `reusable-review.yml` unaffected
- **Breaking change for contract schema**: Contracts created before this change won't have `phase_config`, but this is fine since we're adding fields with defaults

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Unify SDLC phases into single reusable work loop"
  description: |
    Refactors the SDLC pipeline to use a single parameterized reusable workflow
    (`sdlc-work-loop.yml`) for all phases (refine, plan, implement). This reduces
    code duplication from ~2,600 lines to ~1,200 lines while preserving all existing
    behavior and enabling configurable check DAGs between work and review steps.

    Closes #436
phases:
  - id: 1
    name: Contract Schema Extension
    goal: Extend contract schema to support phase configuration and check DAG definitions
    tasks:
      - id: TASK-1-1
        description: Add PhaseConfig and CheckDefinition models to contract schema
        acceptance: New Pydantic models exist with validation, unit tests pass
        files:
          - shared/egg_contracts/models.py
          - shared/egg_contracts/tests/test_models.py
      - id: TASK-1-2
        description: Add phase_config field to Contract model
        acceptance: Contract can serialize/deserialize with phase config, existing contracts remain valid
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Update JSON schema file to match Pydantic models
        acceptance: Schema validates sample contracts with phase configs
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-4
        description: Add default phase configurations as constants
        acceptance: Default configs exist for refine, plan, implement phases
        files:
          - shared/egg_contracts/phase_defaults.py

  - id: 2
    name: Check Scripts Infrastructure
    goal: Create check script infrastructure and implement initial checks
    tasks:
      - id: TASK-2-1
        description: Create check runner framework script (run-check.sh)
        acceptance: Script executes arbitrary check by name, handles exit codes, supports retry
        files:
          - .github/scripts/run-check.sh
      - id: TASK-2-2
        description: Implement merge conflict check script
        acceptance: Script detects merge conflicts, returns structured JSON result
        files:
          - .github/scripts/checks/merge-conflict-check.sh
      - id: TASK-2-3
        description: Implement draft validation check script
        acceptance: Script validates draft exists and has expected sections
        files:
          - .github/scripts/checks/draft-validation-check.sh
      - id: TASK-2-4
        description: Implement plan YAML extraction check script
        acceptance: Script extracts YAML tasks from plan, validates structure
        files:
          - .github/scripts/checks/plan-yaml-check.sh
      - id: TASK-2-5
        description: Implement lint check wrapper script
        acceptance: Script runs linters, captures failures, returns JSON result
        files:
          - .github/scripts/checks/lint-check.sh
      - id: TASK-2-6
        description: Implement test check wrapper script
        acceptance: Script runs tests, captures failures, returns JSON result
        files:
          - .github/scripts/checks/test-check.sh
      - id: TASK-2-7
        description: Implement check fixer dispatcher script
        acceptance: Script runs appropriate fixers based on failed checks
        files:
          - .github/scripts/checks/check-fixer.sh

  - id: 3
    name: Unified Work Loop Workflow
    goal: Create sdlc-work-loop.yml reusable workflow implementing generate/review/respond loop
    tasks:
      - id: TASK-3-1
        description: Create work loop workflow skeleton with inputs schema
        acceptance: Workflow accepts all required inputs (phase, issue_number, prompts, checks, etc.)
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-2
        description: Implement work job with parameterized prompt building
        acceptance: Job builds prompt from trusted main, runs agent, handles output
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-3
        description: Implement run-checks job with DAG execution
        acceptance: Job runs checks in DAG order (merge-fix → parallel lint/test → fixer)
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-4
        description: Implement review job with unified review logic
        acceptance: Job runs reviewer agent, parses verdict, handles all phases
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-5
        description: Implement respond job for feedback handling
        acceptance: Job updates contract, triggers re-dispatch or human gate
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-6
        description: Implement human-gate job for issue checkbox approval
        acceptance: Job posts draft to issue with approval checkbox
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-7
        description: Implement human-gate-pr job for PR-based approval
        acceptance: Job creates/updates PR, waits for human review
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-8
        description: Add circuit breaker logic throughout work loop
        acceptance: Loop respects max cycles, escalates appropriately
        files:
          - .github/workflows/sdlc-work-loop.yml

  - id: 4
    name: Refactor Main Pipeline
    goal: Refactor sdlc-pipeline.yml to use unified work loop for all phases
    tasks:
      - id: TASK-4-1
        description: Refactor init job to set up phase configs in contract
        acceptance: Init job writes phase config to contract based on starting phase
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-2
        description: Replace refine job triad with work loop call
        acceptance: Refine phase uses sdlc-work-loop.yml, behavior unchanged
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-3
        description: Replace plan job triad with work loop call
        acceptance: Plan phase uses sdlc-work-loop.yml, behavior unchanged
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-4
        description: Replace implement job group with work loop call
        acceptance: Implement phase uses sdlc-work-loop.yml, behavior unchanged
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-5
        description: Update job dependencies and conditionals
        acceptance: Pipeline flows correctly between phases
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-6
        description: Remove orphaned jobs (old phase-specific jobs)
        acceptance: No dead code remains, workflow is ~200 lines of orchestration
        files:
          - .github/workflows/sdlc-pipeline.yml

  - id: 5
    name: HITL Integration
    goal: Ensure HITL decision handler integrates with unified work loop
    tasks:
      - id: TASK-5-1
        description: Update HITL handler for unified phase approval marker
        acceptance: Handler recognizes approval checkboxes from all phases
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-5-2
        description: Verify phase transition logic works with new contract structure
        acceptance: Approving refine→plan→implement transitions work
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-5-3
        description: Test escalation flow through unified work loop
        acceptance: Circuit breaker escalation posts correctly, checkbox works
        files:
          - .github/workflows/sdlc-hitl.yml

  - id: 6
    name: Prompt Builder Consolidation
    goal: Consolidate review prompt builders into unified structure
    tasks:
      - id: TASK-6-1
        description: Create unified review prompt builder script
        acceptance: Single script handles all phase reviews via environment variables
        files:
          - action/build-unified-review-prompt.sh
      - id: TASK-6-2
        description: Update work loop to use unified review prompt builder
        acceptance: Work loop calls single script, passes phase context
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-6-3
        description: Deprecate phase-specific review prompt scripts
        acceptance: Old scripts remain as wrappers calling unified script
        files:
          - action/build-refine-review-prompt.sh
          - action/build-plan-review-prompt.sh

  - id: 7
    name: Testing and Validation
    goal: Comprehensive testing of unified pipeline
    tasks:
      - id: TASK-7-1
        description: Create workflow validation test for work loop inputs
        acceptance: Test validates all input combinations are accepted
        files:
          - .github/workflows/test-work-loop.yml
      - id: TASK-7-2
        description: Test refine phase end-to-end on test issue
        acceptance: Refine completes successfully with review cycle
        files: []
      - id: TASK-7-3
        description: Test plan phase end-to-end on test issue
        acceptance: Plan completes successfully with review cycle
        files: []
      - id: TASK-7-4
        description: Test implement phase end-to-end on test issue
        acceptance: Implement creates PR, runs checks, gets reviewed
        files: []
      - id: TASK-7-5
        description: Test circuit breaker escalation scenario
        acceptance: Escalation triggers after max cycles
        files: []
      - id: TASK-7-6
        description: Test human override of escalated phase
        acceptance: Human can approve despite failed reviews
        files: []

  - id: 8
    name: Documentation and Cleanup
    goal: Update documentation and clean up artifacts
    tasks:
      - id: TASK-8-1
        description: Update SDLC pipeline guide in docs
        acceptance: Guide reflects new architecture, includes check DAG config
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-8-2
        description: Update ADR with architectural changes
        acceptance: ADR documents unified work loop decision
        files:
          - docs/adr/implemented/ADR-SDLC-Pipeline.md
      - id: TASK-8-3
        description: Remove deprecated code and comments
        acceptance: No TODO comments referencing old structure
        files:
          - .github/workflows/sdlc-pipeline.yml
```

---

*Authored-by: egg*
