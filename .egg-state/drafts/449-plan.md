# Plan: SDLC Unification 2/4: Unified Work Loop Workflow

> Issue: #449 | Phase: plan

## Summary

This plan creates the unified `sdlc-work-loop.yml` reusable workflow that consolidates the work/review/respond cycle pattern currently duplicated across refine, plan, and implement phases. The workflow accepts phase configuration as inputs and uses parameterized jobs to handle all phases through a single code path. This is implemented alongside the existing pipeline (no removals) to enable parallel testing.

The approach follows the human input from issue #449's analysis: checks run as parallel GitHub jobs for better observability, the new unified review prompt builder is created fresh (existing builders remain until migration), and the circuit breaker escalates to human when checks can't pass.

## Implementation Phases

### Phase 1: Core Workflow Skeleton

**Goal**: Create the work loop workflow file with complete input schema and job structure (without job implementations).

**Tasks**:
- [TASK-1-1] Create `.github/workflows/sdlc-work-loop.yml` with full input schema — Acceptance: YAML is valid, includes all documented inputs (phase, issue_number, work_prompt_script, review_prompt_script, checks, output_type, human_review_mechanism, max_review_cycles, branch_name)
- [TASK-1-2] Add workflow-level configuration (permissions, concurrency) — Acceptance: Permissions match sdlc-pipeline.yml; concurrency prevents parallel runs for same issue
- [TASK-1-3] Add job skeletons for all jobs (work, run-checks, review, respond, human-gate, human-gate-pr) — Acceptance: All 6 jobs defined with `needs` dependencies and conditional `if` clauses

**Dependencies**: None

**Exit criteria**: Workflow file passes `yamllint` and `actionlint` validation; can be viewed in GitHub Actions UI

### Phase 2: Implement the `work` Job

**Goal**: Implement the parameterized work job that builds prompts and runs the egg action.

**Tasks**:
- [TASK-2-1] Implement trusted checkout and prompt building step — Acceptance: Checks out main branch, runs work_prompt_script input with phase context
- [TASK-2-2] Implement issue branch checkout and git identity configuration — Acceptance: Checks out branch from inputs.branch_name, configures bot identity
- [TASK-2-3] Implement egg action invocation with phase-aware environment — Acceptance: Runs egg action with EGG_PIPELINE_PHASE, EGG_AGENT_ROLE set; timeout configurable
- [TASK-2-4] Implement output artifact capture (draft file path or code changes) — Acceptance: Outputs draft_file for refine/plan phases; no output for implement phase

**Dependencies**: Phase 1

**Exit criteria**: Work job can be invoked manually with test inputs for each phase and produces expected outputs

### Phase 3: Implement the `run-checks` Job with DAG Execution

**Goal**: Implement check execution with dependency-aware parallelism using GitHub Actions job matrix.

**Tasks**:
- [TASK-3-1] Create check DAG execution model with parallel jobs — Acceptance: Implement phase-specific check jobs that run in DAG order (merge-fix first, lint/test parallel, fixer last)
- [TASK-3-2] Implement check-merge-conflict job (implement phase only) — Acceptance: Runs merge conflict check; outputs success/failure status
- [TASK-3-3] Implement check-lint job with lint/test parallelism — Acceptance: Runs after merge-conflict; parallel with check-test
- [TASK-3-4] Implement check-test job with lint/test parallelism — Acceptance: Runs after merge-conflict; parallel with check-lint
- [TASK-3-5] Implement check-fixer job that runs after lint/test — Acceptance: Runs only if lint or test failed and check is fixable; invokes autofix workflow
- [TASK-3-6] Implement check aggregation logic — Acceptance: Collects all check results; outputs overall pass/fail and individual statuses

**Dependencies**: Phase 2

**Exit criteria**: Check DAG executes correctly for implement phase; skips checks appropriately for refine/plan phases

### Phase 4: Implement the `review` Job

**Goal**: Implement unified review logic that works across all phases.

**Tasks**:
- [TASK-4-1] Create `action/build-unified-review-prompt.sh` script — Acceptance: Accepts EGG_PIPELINE_PHASE env var; generates phase-appropriate review criteria; supports re-review with prior feedback
- [TASK-4-2] Implement review cycle tracking from contract — Acceptance: Reads {phase}_review_cycles from contract; increments on each review
- [TASK-4-3] Implement circuit breaker check before review — Acceptance: Skips review if circuit breaker is open
- [TASK-4-4] Implement reviewer agent invocation — Acceptance: Runs egg action with reviewer role; uses opus model
- [TASK-4-5] Implement verdict parsing from review file — Acceptance: Reads .egg-state/reviews/{issue}-{phase}-review.json; outputs verdict (approved/needs_revision) and feedback

**Dependencies**: Phase 3

**Exit criteria**: Review job produces valid verdict for each phase; re-review includes prior feedback

### Phase 5: Implement the `respond` Job

**Goal**: Implement feedback handling and re-dispatch logic.

**Tasks**:
- [TASK-5-1] Implement verdict routing logic — Acceptance: If approved, continue to human gate; if needs_revision, prepare for redispatch
- [TASK-5-2] Implement contract update with review feedback — Acceptance: Updates {phase}_review_cycles and {phase}_review_feedback in contract; commits and pushes with conflict-resistant retry
- [TASK-5-3] Implement circuit breaker trigger logic — Acceptance: Opens circuit breaker if review_cycles exceeds max_review_cycles; posts escalation comment
- [TASK-5-4] Implement pipeline redispatch for revisions — Acceptance: Triggers workflow_dispatch with same phase to restart work loop with feedback

**Dependencies**: Phase 4

**Exit criteria**: Respond job correctly routes to human gate on approval; correctly redispatches on revision needed

### Phase 6: Implement the `human-gate` Job (Issue Checkbox)

**Goal**: Implement human approval mechanism for refine and plan phases via issue checkbox.

**Tasks**:
- [TASK-6-1] Implement draft-to-issue posting — Acceptance: Reads draft from .egg-state/drafts/{issue}-{type}.md; posts to issue with approval checkbox
- [TASK-6-2] Implement phase approval comment format — Acceptance: Uses `<!-- egg-phase-approval -->` marker; includes checkbox for approval
- [TASK-6-3] Implement sdlc:awaiting-approval label application — Acceptance: Adds label after posting; removes previous phase labels
- [TASK-6-4] Implement contract phase advancement setup — Acceptance: Does NOT advance phase (HITL workflow does that); just prepares state for human decision

**Dependencies**: Phase 5

**Exit criteria**: Human gate posts correctly formatted approval comment; HITL workflow can detect and process checkbox changes

### Phase 7: Implement the `human-gate-pr` Job (PR Review)

**Goal**: Implement human approval mechanism for implement phase via PR review.

**Tasks**:
- [TASK-7-1] Implement PR ready-for-review transition — Acceptance: Calls `gh pr ready` on draft PR; updates PR title (removes [SDLC] prefix)
- [TASK-7-2] Implement reviewer assignment — Acceptance: Adds workflow_owner as reviewer if available
- [TASK-7-3] Implement contract phase update to 'pr' — Acceptance: Updates current_phase in contract; commits with conflict-resistant retry
- [TASK-7-4] Implement completion comment on issue — Acceptance: Posts status comment linking to PR

**Dependencies**: Phase 6

**Exit criteria**: Human gate PR correctly transitions PR to ready state; assigns reviewer; updates contract

### Phase 8: Circuit Breaker and Error Handling

**Goal**: Add circuit breaker logic with configurable thresholds and proper error handling.

**Tasks**:
- [TASK-8-1] Add max_total_cycles input with default threshold — Acceptance: Input defaults to 10; read from contract.circuit_breaker.max_total_cycles
- [TASK-8-2] Implement total cycle tracking across phases — Acceptance: Increments circuit_breaker.total_cycles on each work loop iteration
- [TASK-8-3] Implement circuit breaker open logic — Acceptance: Sets circuit_breaker.status to "open" when thresholds exceeded; adds audit log entry
- [TASK-8-4] Implement escalation comment posting — Acceptance: Posts comment with escalation reason; adds sdlc:awaiting-approval label

**Dependencies**: Phases 5, 6, 7

**Exit criteria**: Circuit breaker triggers correctly; escalation is visible in issue

## Test Strategy

- **YAML validation**: All workflow files pass `yamllint` and `actionlint`
- **Manual trigger testing**: Workflow can be triggered via `workflow_dispatch` with test inputs for each phase
- **Isolated job testing**: Each job can complete successfully when invoked in isolation with mock inputs
- **Integration testing**: End-to-end test on a dedicated test issue (not a real issue) to verify full work loop
- **Parallel testing**: Existing `sdlc-pipeline.yml` continues to work unchanged; new workflow tested on separate issues

## Rollback Plan

1. **No removal of existing code**: The existing `sdlc-pipeline.yml` is not modified. If the new workflow fails, simply don't use it.
2. **Manual disable**: Add `if: false` to the workflow's jobs to disable without deleting.
3. **Delete workflow file**: If the new workflow causes issues, delete `.github/workflows/sdlc-work-loop.yml`.
4. **Branch revert**: If commits cause problems, `git revert` the commits that added the workflow.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Check DAG complexity | Medium | Medium | Start with simple sequential checks; add parallelism incrementally |
| Workflow nesting depth limit (4 levels) | Low | High | Keep all jobs in single workflow; avoid calling other reusable workflows from jobs |
| Contract race conditions | Medium | Medium | Use existing push-contract-update.sh with conflict-resistant retry |
| Prompt builder compatibility | Low | Medium | New unified builder created fresh; existing builders untouched |
| HITL workflow integration | Medium | Medium | Test checkbox detection with existing sdlc-hitl.yml before full integration |

## Migration Notes

- **No breaking changes**: This issue creates new files only; no existing files are modified or deleted
- **External callers unaffected**: Repos calling `sdlc-pipeline.yml` continue to work
- **Manual testing required**: The new workflow should be tested on a dedicated test issue before use in production
- **Migration in #450**: The actual switch from old pipeline to new workflow happens in issue #450 (part 3 of 4)

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add unified work loop workflow for SDLC phases"
  description: |
    Creates the new `sdlc-work-loop.yml` reusable workflow that consolidates
    the work/review/respond cycle pattern across refine, plan, and implement
    phases. This is part 2 of 4 for SDLC unification (#436).

    The workflow is added alongside the existing pipeline for parallel testing.
    No existing code is modified or removed.

    Fixes #449
phases:
  - id: 1
    name: Core Workflow Skeleton
    goal: Create the work loop workflow file with complete input schema and job structure
    tasks:
      - id: TASK-1-1
        description: Create sdlc-work-loop.yml with full input schema
        acceptance: YAML is valid, includes all documented inputs
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-1-2
        description: Add workflow-level configuration (permissions, concurrency)
        acceptance: Permissions match sdlc-pipeline.yml; concurrency prevents parallel runs
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-1-3
        description: Add job skeletons for all jobs
        acceptance: All 6 jobs defined with needs dependencies and conditional if clauses
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 2
    name: Implement the work Job
    goal: Implement the parameterized work job that builds prompts and runs the egg action
    tasks:
      - id: TASK-2-1
        description: Implement trusted checkout and prompt building step
        acceptance: Checks out main branch, runs work_prompt_script input with phase context
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-2
        description: Implement issue branch checkout and git identity configuration
        acceptance: Checks out branch from inputs.branch_name, configures bot identity
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-3
        description: Implement egg action invocation with phase-aware environment
        acceptance: Runs egg action with EGG_PIPELINE_PHASE and EGG_AGENT_ROLE set
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-4
        description: Implement output artifact capture
        acceptance: Outputs draft_file for refine/plan phases
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 3
    name: Implement the run-checks Job with DAG Execution
    goal: Implement check execution with dependency-aware parallelism
    tasks:
      - id: TASK-3-1
        description: Create check DAG execution model with parallel jobs
        acceptance: Phase-specific check jobs run in DAG order
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-2
        description: Implement check-merge-conflict job
        acceptance: Runs merge conflict check; outputs success/failure status
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-3
        description: Implement check-lint job with parallelism
        acceptance: Runs after merge-conflict; parallel with check-test
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-4
        description: Implement check-test job with parallelism
        acceptance: Runs after merge-conflict; parallel with check-lint
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-5
        description: Implement check-fixer job
        acceptance: Runs only if lint or test failed and check is fixable
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-6
        description: Implement check aggregation logic
        acceptance: Collects all check results; outputs overall pass/fail
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 4
    name: Implement the review Job
    goal: Implement unified review logic that works across all phases
    tasks:
      - id: TASK-4-1
        description: Create build-unified-review-prompt.sh script
        acceptance: Accepts EGG_PIPELINE_PHASE; generates phase-appropriate review criteria
        files:
          - action/build-unified-review-prompt.sh
      - id: TASK-4-2
        description: Implement review cycle tracking from contract
        acceptance: Reads and increments review cycles from contract
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-3
        description: Implement circuit breaker check before review
        acceptance: Skips review if circuit breaker is open
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-4
        description: Implement reviewer agent invocation
        acceptance: Runs egg action with reviewer role; uses opus model
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-5
        description: Implement verdict parsing from review file
        acceptance: Reads review JSON; outputs verdict and feedback
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 5
    name: Implement the respond Job
    goal: Implement feedback handling and re-dispatch logic
    tasks:
      - id: TASK-5-1
        description: Implement verdict routing logic
        acceptance: Routes to human gate on approval; prepares redispatch on revision
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-2
        description: Implement contract update with review feedback
        acceptance: Updates review cycles and feedback in contract with conflict-resistant retry
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-3
        description: Implement circuit breaker trigger logic
        acceptance: Opens circuit breaker if review_cycles exceeds max
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-5-4
        description: Implement pipeline redispatch for revisions
        acceptance: Triggers workflow_dispatch with same phase to restart work loop
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 6
    name: Implement the human-gate Job
    goal: Implement human approval via issue checkbox for refine and plan phases
    tasks:
      - id: TASK-6-1
        description: Implement draft-to-issue posting
        acceptance: Reads draft file; posts to issue with approval checkbox
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-6-2
        description: Implement phase approval comment format
        acceptance: Uses egg-phase-approval marker; includes checkbox
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-6-3
        description: Implement sdlc:awaiting-approval label application
        acceptance: Adds label after posting
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-6-4
        description: Implement contract phase advancement setup
        acceptance: Prepares state for human decision; does not advance phase
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 7
    name: Implement the human-gate-pr Job
    goal: Implement human approval via PR review for implement phase
    tasks:
      - id: TASK-7-1
        description: Implement PR ready-for-review transition
        acceptance: Calls gh pr ready; updates PR title
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-7-2
        description: Implement reviewer assignment
        acceptance: Adds workflow_owner as reviewer if available
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-7-3
        description: Implement contract phase update to pr
        acceptance: Updates current_phase with conflict-resistant retry
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-7-4
        description: Implement completion comment on issue
        acceptance: Posts status comment linking to PR
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 8
    name: Circuit Breaker and Error Handling
    goal: Add circuit breaker logic with configurable thresholds
    tasks:
      - id: TASK-8-1
        description: Add max_total_cycles input with default threshold
        acceptance: Input defaults to 10; read from contract
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-8-2
        description: Implement total cycle tracking across phases
        acceptance: Increments circuit_breaker.total_cycles on each iteration
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-8-3
        description: Implement circuit breaker open logic
        acceptance: Sets status to open when thresholds exceeded; adds audit log
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-8-4
        description: Implement escalation comment posting
        acceptance: Posts escalation comment; adds awaiting-approval label
        files:
          - .github/workflows/sdlc-work-loop.yml
```

---

*Authored-by: egg*
