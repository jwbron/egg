# Plan: Set Up a Single Reusable Workflow for All SDLC Workflows

> Issue: #430 | Phase: plan

## Summary

This plan implements a unified reusable workflow (`sdlc-work-loop.yml`) that consolidates the refine, plan, and implement phases into a single parameterized loop. The approach follows Option A from the approved analysis: each phase invocation passes different agents, context, and contract configuration to the same underlying loop. This eliminates ~60% of duplicated workflow code while enabling arbitrary intermediate check DAGs between work and review steps.

## Architecture Overview

The unified loop implements a state-driven work cycle:

```
Producer Agent → [Intermediate Checks DAG] → Reviewer Agent → [Decision]
                                                               ↓
                                        [Approved] → Post to Issue/PR → Human Review
                                        [Rejected] → Re-dispatch (if under max cycles)
                                        [Escalated] → HITL Decision
```

Key design decisions from the approved analysis:
- **Intermediate checks**: Defined via contract schema extension (allows per-issue customization and audit trail)
- **Human review**: Separate mechanisms preserved (issue comments for refine/plan, PR review for implement)
- **Migration**: Big-bang approach (all phases converted simultaneously)

## Implementation Phases

### Phase 1: Contract Schema Extension

**Goal**: Extend the contract schema to support phase-agnostic work loop configuration and intermediate check definitions.

**Tasks**:
- [TASK-1-1] Add `phase_config` section to contract schema — Acceptance: Schema validates phase configuration for all three phases (refine, plan, implement) with prompt scripts, reviewer config, max cycles, and intermediate checks
- [TASK-1-2] Add `intermediate_checks` definition to contract schema — Acceptance: Schema supports check definitions with name, command, auto-fix capability, and dependency ordering
- [TASK-1-3] Update Pydantic models in `shared/egg_contracts/models.py` — Acceptance: Models match updated schema, existing unit tests pass, new models have validation tests
- [TASK-1-4] Create schema migration script for existing contracts — Acceptance: Script adds default phase_config to contracts without breaking existing fields, test with sample contracts

**Dependencies**: None (foundational)

**Exit criteria**: Updated schema validates against test contracts, models pass type checking, migration script successfully updates test contracts.

### Phase 2: Core Work Loop Workflow

**Goal**: Create the reusable workflow that implements the producer → check → reviewer → decision loop.

**Tasks**:
- [TASK-2-1] Create `sdlc-work-loop.yml` reusable workflow skeleton — Acceptance: Workflow accepts phase name, issue number, branch name, and phase configuration as inputs; compiles without errors
- [TASK-2-2] Implement producer agent job with configurable prompt script — Acceptance: Job runs correct prompt builder script based on phase config, handles agent execution, captures outputs
- [TASK-2-3] Implement intermediate checks DAG executor job — Acceptance: Job parses check definitions from contract, executes checks in dependency order, captures results, supports auto-fix retry
- [TASK-2-4] Implement reviewer agent job with configurable prompt script — Acceptance: Job runs reviewer agent (or skips for PR-based review), parses verdict, updates contract
- [TASK-2-5] Implement decision routing job — Acceptance: Job correctly routes to re-dispatch (if under max cycles), escalation (if over), or human review (if approved)
- [TASK-2-6] Implement re-dispatch mechanism — Acceptance: Re-dispatch triggers new workflow run with incremented cycle count, feedback passed to next iteration

**Dependencies**: Phase 1 (contract schema)

**Exit criteria**: Work loop can execute a complete refine cycle (producer → reviewer → approval) in isolation.

### Phase 3: Refine Phase Migration

**Goal**: Migrate the refine phase from `sdlc-pipeline.yml` to use the new work loop.

**Tasks**:
- [TASK-3-1] Create refine phase configuration in default contract template — Acceptance: Default contract includes refine phase config with correct prompt scripts and max cycles (3)
- [TASK-3-2] Update `sdlc-pipeline.yml` refine job to call work loop — Acceptance: Refine job replaced with single call to `sdlc-work-loop.yml` with refine configuration
- [TASK-3-3] Remove deprecated refine-review and refine-redispatch jobs — Acceptance: Jobs removed, no orphan references in workflow file
- [TASK-3-4] Update `build-refine-review-prompt.sh` for work loop compatibility — Acceptance: Script works when called from work loop with standardized environment variables

**Dependencies**: Phase 2 (work loop)

**Exit criteria**: Refine phase works identically to before using new work loop, integration tests pass.

### Phase 4: Plan Phase Migration

**Goal**: Migrate the plan phase to use the unified work loop.

**Tasks**:
- [TASK-4-1] Create plan phase configuration in default contract template — Acceptance: Default contract includes plan phase config with correct prompt scripts, max cycles (3), and contract population step
- [TASK-4-2] Update `sdlc-pipeline.yml` plan job to call work loop — Acceptance: Plan job replaced with single call to `sdlc-work-loop.yml` with plan configuration
- [TASK-4-3] Remove deprecated plan-review and plan-redispatch jobs — Acceptance: Jobs removed, no orphan references
- [TASK-4-4] Update `build-plan-review-prompt.sh` for work loop compatibility — Acceptance: Script works when called from work loop
- [TASK-4-5] Integrate contract population step into work loop — Acceptance: Work loop calls `populate-contract-tasks.py` after plan producer completes

**Dependencies**: Phase 3 (refine migration validates approach)

**Exit criteria**: Plan phase works identically to before using new work loop, integration tests pass.

### Phase 5: Implement Phase Migration

**Goal**: Migrate the implement phase to use the unified work loop, including intermediate checks.

**Tasks**:
- [TASK-5-1] Create implement phase configuration with intermediate checks — Acceptance: Config includes lint, test, and autofix checks with proper dependency ordering
- [TASK-5-2] Update `sdlc-pipeline.yml` implement job to call work loop — Acceptance: Implement job uses work loop with PR-based review configuration
- [TASK-5-3] Integrate existing autofix workflow as intermediate check — Acceptance: `reusable-autofix.yml` called as intermediate check, auto-fix commits trigger producer re-run
- [TASK-5-4] Update PR creation logic for work loop — Acceptance: Work loop creates draft PR after producer completes, supports check-failure handling
- [TASK-5-5] Remove deprecated implement-specific jobs (wait-for-checks, finalize-pr) — Acceptance: Jobs consolidated into work loop, no orphan references
- [TASK-5-6] Update `reusable-review.yml` integration — Acceptance: Review workflow integrated as reviewer step in work loop

**Dependencies**: Phase 4 (plan migration)

**Exit criteria**: Implement phase works with intermediate checks, PR creation and finalization work correctly.

### Phase 6: Human Review Unification

**Goal**: Standardize the async human review pattern across all phases.

**Tasks**:
- [TASK-6-1] Create unified human review checkpoint job in work loop — Acceptance: Job posts work artifact with phase-appropriate approval mechanism (issue checkbox or PR ready)
- [TASK-6-2] Update `sdlc-hitl.yml` for work loop compatibility — Acceptance: HITL handler recognizes checkpoints from work loop, resumes correctly
- [TASK-6-3] Implement feedback loop for human revisions — Acceptance: Human feedback captured in contract, work loop re-invoked with feedback context
- [TASK-6-4] Add phase transition handling to work loop — Acceptance: Work loop advances contract phase after human approval, triggers next phase

**Dependencies**: Phase 5 (all phases migrated)

**Exit criteria**: Human can provide feedback on any phase, feedback incorporated into re-run, phase transitions work correctly.

### Phase 7: Cleanup and Documentation

**Goal**: Remove deprecated code, update documentation, and finalize the migration.

**Tasks**:
- [TASK-7-1] Remove all deprecated job definitions from `sdlc-pipeline.yml` — Acceptance: Workflow file reduced by ~60%, no dead code
- [TASK-7-2] Update `sdlc-pipeline.yml` to be thin orchestrator — Acceptance: Main workflow only handles init and calls work loop for each phase
- [TASK-7-3] Update ADR documentation for new architecture — Acceptance: ADR describes work loop design, phase configuration, intermediate checks
- [TASK-7-4] Update integration tests for new workflow structure — Acceptance: All integration tests pass, coverage maintained
- [TASK-7-5] Add work loop unit tests — Acceptance: Key decision points (cycle limits, check ordering, verdict parsing) have test coverage

**Dependencies**: Phase 6 (human review)

**Exit criteria**: All tests pass, documentation updated, code review ready.

## Test Strategy

- **Unit tests**:
  - Contract schema validation tests for new phase_config and intermediate_checks fields
  - Pydantic model tests for PhaseConfig, IntermediateCheck, CheckResult classes
  - Plan parser tests for extracting check definitions from YAML
  - Decision routing logic tests (approved/rejected/escalated paths)

- **Integration tests**:
  - End-to-end refine cycle: issue labeled → analysis produced → reviewed → posted
  - End-to-end plan cycle: approval → plan produced → reviewed → posted
  - End-to-end implement cycle: approval → code implemented → checks run → PR created → reviewed
  - Re-dispatch cycle: review rejection → feedback passed → producer re-run
  - Escalation cycle: max cycles exceeded → HITL comment posted
  - Human feedback loop: feedback provided → incorporated → work updated

- **Manual testing**:
  - Create test issue with `sdlc:refine` label
  - Verify complete refine → plan → implement → PR flow
  - Test human feedback on each phase
  - Test escalation path by forcing review rejections
  - Verify intermediate checks run in correct order for implement phase

## Rollback Plan

If issues are discovered after deployment:

1. **Immediate revert**: The old workflow is preserved in git history. Run:
   ```bash
   git revert <merge-commit-sha>
   git push origin main
   ```

2. **Partial revert**: If only one phase has issues, the work loop supports per-phase fallback:
   - Set `use_legacy_workflow: true` in phase config
   - Work loop will call legacy job definitions (preserved in separate file during migration)

3. **Contract recovery**: Contracts use additive schema changes only. Old contracts work with new schema (missing fields get defaults). If rollback needed:
   - Contracts remain valid
   - New fields are ignored by old workflow

4. **In-progress issues**: Active SDLC issues during migration:
   - Complete current phase before migration
   - Or: Cancel and re-label with `sdlc:refine` after migration

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| GitHub Actions reusable workflow input limits | Low | Medium | Test all input combinations during development; use JSON serialization for complex configs |
| Concurrent contract updates cause conflicts | Medium | Low | Existing push-contract-update.sh handles this; no additional risk |
| Work loop timeout for long implement phases | Low | High | Preserve 6-hour timeout; add checkpoint logic from current implement job |
| Breaking change to HITL marker format | Low | Medium | Keep marker format identical; add version field for future changes |
| Intermediate check ordering causes deadlocks | Low | Medium | Validate check DAG at workflow start; fail fast on cycles |
| Performance regression from additional abstraction | Low | Low | Measure job durations before/after; optimize if >10% slower |

## Migration Notes

**Schema changes**:
- New fields added to contract schema: `phase_config`, `intermediate_checks` (in phase definition)
- All new fields have defaults; existing contracts remain valid
- No database migrations required (contracts stored in git)

**Configuration changes**:
- Default contract template updated with phase configurations
- Existing contracts upgraded on first write (additive only)

**Breaking changes**: None for end users. Internal workflow structure changes:
- Phase jobs now call `sdlc-work-loop.yml` instead of inline steps
- Review jobs consolidated into work loop
- Re-dispatch uses workflow_dispatch instead of job chaining

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
    Consolidates refine, plan, and implement phases into a single parameterized
    reusable workflow (sdlc-work-loop.yml). Adds support for intermediate check
    DAGs between work and review steps. Extends contract schema for phase
    configuration. Eliminates ~60% of workflow code duplication.

    Closes #430
phases:
  - id: 1
    name: Contract Schema Extension
    goal: Extend contract schema to support phase configuration and intermediate checks
    tasks:
      - id: TASK-1-1
        description: Add phase_config section to contract schema
        acceptance: Schema validates phase configuration for all three phases with prompt scripts, reviewer config, max cycles, and intermediate checks
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-2
        description: Add intermediate_checks definition to contract schema
        acceptance: Schema supports check definitions with name, command, auto-fix capability, and dependency ordering
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-1-3
        description: Update Pydantic models for new schema fields
        acceptance: Models match updated schema, existing unit tests pass, new models have validation tests
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-4
        description: Create schema migration script for existing contracts
        acceptance: Script adds default phase_config to contracts without breaking existing fields
        files:
          - scripts/migrate-contracts.py
  - id: 2
    name: Core Work Loop Workflow
    goal: Create reusable workflow implementing producer-check-reviewer-decision loop
    tasks:
      - id: TASK-2-1
        description: Create sdlc-work-loop.yml reusable workflow skeleton
        acceptance: Workflow accepts phase name, issue number, branch name, and phase configuration as inputs
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-2
        description: Implement producer agent job with configurable prompt script
        acceptance: Job runs correct prompt builder script based on phase config, handles agent execution
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-3
        description: Implement intermediate checks DAG executor job
        acceptance: Job executes checks in dependency order, captures results, supports auto-fix retry
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-4
        description: Implement reviewer agent job with configurable prompt script
        acceptance: Job runs reviewer agent or skips for PR-based review, parses verdict
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-5
        description: Implement decision routing job
        acceptance: Job correctly routes to re-dispatch, escalation, or human review based on verdict
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-6
        description: Implement re-dispatch mechanism
        acceptance: Re-dispatch triggers new workflow run with incremented cycle count and feedback
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 3
    name: Refine Phase Migration
    goal: Migrate refine phase to use the new work loop
    tasks:
      - id: TASK-3-1
        description: Create refine phase configuration in default contract template
        acceptance: Default contract includes refine phase config with correct prompt scripts and max cycles
        files:
          - .egg/templates/contract.json
      - id: TASK-3-2
        description: Update sdlc-pipeline.yml refine job to call work loop
        acceptance: Refine job replaced with single call to sdlc-work-loop.yml
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-3
        description: Remove deprecated refine-review and refine-redispatch jobs
        acceptance: Jobs removed, no orphan references in workflow file
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-4
        description: Update build-refine-review-prompt.sh for work loop compatibility
        acceptance: Script works when called from work loop with standardized environment variables
        files:
          - action/build-refine-review-prompt.sh
  - id: 4
    name: Plan Phase Migration
    goal: Migrate plan phase to use the unified work loop
    tasks:
      - id: TASK-4-1
        description: Create plan phase configuration in default contract template
        acceptance: Default contract includes plan phase config with contract population step
        files:
          - .egg/templates/contract.json
      - id: TASK-4-2
        description: Update sdlc-pipeline.yml plan job to call work loop
        acceptance: Plan job replaced with single call to sdlc-work-loop.yml
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-3
        description: Remove deprecated plan-review and plan-redispatch jobs
        acceptance: Jobs removed, no orphan references
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-4-4
        description: Update build-plan-review-prompt.sh for work loop compatibility
        acceptance: Script works when called from work loop
        files:
          - action/build-plan-review-prompt.sh
      - id: TASK-4-5
        description: Integrate contract population step into work loop
        acceptance: Work loop calls populate-contract-tasks.py after plan producer completes
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 5
    name: Implement Phase Migration
    goal: Migrate implement phase to use work loop with intermediate checks
    tasks:
      - id: TASK-5-1
        description: Create implement phase configuration with intermediate checks
        acceptance: Config includes lint, test, and autofix checks with dependency ordering
        files:
          - .egg/templates/contract.json
      - id: TASK-5-2
        description: Update sdlc-pipeline.yml implement job to call work loop
        acceptance: Implement job uses work loop with PR-based review configuration
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-3
        description: Integrate existing autofix workflow as intermediate check
        acceptance: reusable-autofix.yml called as intermediate check, auto-fix commits trigger producer re-run
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-4
        description: Update PR creation logic for work loop
        acceptance: Work loop creates draft PR after producer completes
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-5
        description: Remove deprecated implement-specific jobs
        acceptance: wait-for-checks and finalize-pr jobs consolidated into work loop
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-6
        description: Update reusable-review.yml integration
        acceptance: Review workflow integrated as reviewer step in work loop
        files:
          - .github/workflows/sdlc-work-loop.yml
          - .github/workflows/reusable-review.yml
  - id: 6
    name: Human Review Unification
    goal: Standardize async human review pattern across all phases
    tasks:
      - id: TASK-6-1
        description: Create unified human review checkpoint job in work loop
        acceptance: Job posts work artifact with phase-appropriate approval mechanism
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-6-2
        description: Update sdlc-hitl.yml for work loop compatibility
        acceptance: HITL handler recognizes checkpoints from work loop, resumes correctly
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-6-3
        description: Implement feedback loop for human revisions
        acceptance: Human feedback captured in contract, work loop re-invoked with feedback
        files:
          - .github/workflows/sdlc-work-loop.yml
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-6-4
        description: Add phase transition handling to work loop
        acceptance: Work loop advances contract phase after human approval
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 7
    name: Cleanup and Documentation
    goal: Remove deprecated code and update documentation
    tasks:
      - id: TASK-7-1
        description: Remove all deprecated job definitions from sdlc-pipeline.yml
        acceptance: Workflow file reduced by ~60%, no dead code
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-7-2
        description: Update sdlc-pipeline.yml to be thin orchestrator
        acceptance: Main workflow only handles init and calls work loop for each phase
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-7-3
        description: Update ADR documentation for new architecture
        acceptance: ADR describes work loop design, phase configuration, intermediate checks
        files:
          - docs/adrs/adr-XXX-unified-work-loop.md
      - id: TASK-7-4
        description: Update integration tests for new workflow structure
        acceptance: All integration tests pass, coverage maintained
        files:
          - integration_tests/sdlc/test_work_loop.py
      - id: TASK-7-5
        description: Add work loop unit tests
        acceptance: Key decision points have test coverage
        files:
          - tests/workflows/test_work_loop_logic.py
```

---

*Authored-by: egg*
