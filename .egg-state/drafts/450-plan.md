# Plan: SDLC Unification 3/4: Pipeline Migration

> Issue: #450 | Phase: plan

## Summary

This plan migrates `sdlc-pipeline.yml` from ~2,600 lines to ~330 lines by replacing the duplicated phase-specific jobs with a single call to the unified `sdlc-work-loop.yml` workflow (created in PR #457). The approach follows Option D from the analysis: a unified phase call with dynamic inputs. The `init` job is refactored to handle phase configuration and support the `starting_phase` override for stepping back to prior phases (per human decision). All old phase jobs are removed, and the pipeline becomes a thin orchestration layer.

Key aspects:
- **Single work loop call**: One job calls `sdlc-work-loop.yml` with phase-specific configuration derived from `current_phase`
- **Starting phase override**: Users can pass `starting_phase` to restart from any earlier phase (clears subsequent work)
- **HITL compatibility**: Phase transitions continue via `sdlc-hitl.yml` with existing checkbox/PR review mechanisms
- **PR phase handling**: Work loop not called when `current_phase == 'pr'` (native GitHub PR review takes over)

## Implementation Phases

### Phase 1: Refactor `init` Job for Phase Override Support

**Goal**: Update the `init` job to support `starting_phase` override that allows users to step back to prior phases, clearing subsequent work.

**Tasks**:
- [TASK-1-1] Add phase validation logic for `starting_phase` override — Acceptance: Validates that `starting_phase` is not ahead of contract's `current_phase` (e.g., can go refine→refine or implement→plan but not refine→implement)
- [TASK-1-2] Implement phase reset logic when stepping back — Acceptance: When `starting_phase` < `current_phase`, deletes draft/review files for phases being reset, updates contract `current_phase`
- [TASK-1-3] Add `phase_configs` initialization in contract creation — Acceptance: New contracts include `phase_configs` with default values for all three phases (max_review_cycles, human_review_mechanism)
- [TASK-1-4] Output additional job outputs for work loop — Acceptance: Job outputs include `pr_number` (if PR exists), `workflow_owner`

**Dependencies**: None

**Exit criteria**: `init` job correctly handles phase override requests and outputs all required values for work loop

### Phase 2: Add Single Work Loop Call

**Goal**: Replace all phase-specific jobs with a single call to `sdlc-work-loop.yml`.

**Tasks**:
- [TASK-2-1] Add `work-loop` job that calls `sdlc-work-loop.yml` — Acceptance: Job uses `needs: [resolve-inputs, init]`, passes all required inputs (phase, issue_number, branch_name, workflow_owner, pr_number)
- [TASK-2-2] Add conditional skip for PR phase — Acceptance: Job has `if: needs.init.outputs.current_phase != 'pr'` to skip work loop when in PR review phase
- [TASK-2-3] Pass secrets to work loop — Acceptance: Uses `secrets: inherit` to pass all required secrets (BOT_APP_ID, BOT_APP_PRIVATE_KEY, BOT_APP_INSTALLATION_ID, ANTHROPIC_OAUTH_TOKEN)
- [TASK-2-4] Configure phase-aware inputs — Acceptance: Inputs like `output_type` and `human_review_mechanism` are computed from `current_phase` using GitHub expressions

**Dependencies**: Phase 1

**Exit criteria**: Work loop job correctly calls `sdlc-work-loop.yml` with phase-appropriate configuration

### Phase 3: Remove Old Phase Jobs

**Goal**: Delete all phase-specific jobs that are now handled by the work loop.

**Tasks**:
- [TASK-3-1] Remove `refine` job (lines ~1236-1359) — Acceptance: Job deleted; no references remain
- [TASK-3-2] Remove `refine-review` job (lines ~1363-1725) — Acceptance: Job deleted; no references remain
- [TASK-3-3] Remove `refine-redispatch` job (lines ~1729-1852) — Acceptance: Job deleted; no references remain
- [TASK-3-4] Remove `plan` job (lines ~1857-2075) — Acceptance: Job deleted; no references remain
- [TASK-3-5] Remove `plan-review` job (lines ~2080-2440) — Acceptance: Job deleted; no references remain
- [TASK-3-6] Remove `plan-redispatch` job (lines ~2445-2568) — Acceptance: Job deleted; no references remain
- [TASK-3-7] Remove `implement` job (lines ~400-769) — Acceptance: Job deleted; no references remain
- [TASK-3-8] Remove `wait-for-checks` job (lines ~773-922) — Acceptance: Job deleted; no references remain
- [TASK-3-9] Remove `finalize-pr` job (lines ~926-1147) — Acceptance: Job deleted; no references remain
- [TASK-3-10] Remove `checks-failed` job (lines ~1151-1231) — Acceptance: Job deleted; no references remain

**Dependencies**: Phase 2

**Exit criteria**: Only `resolve-inputs`, `init`, and `work-loop` jobs remain in `sdlc-pipeline.yml`

### Phase 4: Update HITL Integration

**Goal**: Verify and update `sdlc-hitl.yml` to work correctly with the unified pipeline structure.

**Tasks**:
- [TASK-4-1] Verify phase transition triggers work with new pipeline — Acceptance: HITL workflow's `gh workflow run sdlc-pipeline.yml --field starting_phase="${NEXT_PHASE}"` triggers correct phase in work loop
- [TASK-4-2] Verify circuit breaker escalation flow — Acceptance: When work loop opens circuit breaker, HITL workflow correctly detects and handles escalation
- [TASK-4-3] Update any hardcoded job references in HITL workflow — Acceptance: No references to removed jobs (refine, plan, implement, etc.) remain in sdlc-hitl.yml
- [TASK-4-4] Test phase approval checkbox detection — Acceptance: Existing `<!-- egg-phase-approval -->` markers continue to trigger phase transitions

**Dependencies**: Phase 3

**Exit criteria**: HITL workflow correctly handles all phase transitions with the new pipeline structure

### Phase 5: Validate End-to-End Flow

**Goal**: Ensure the complete pipeline works for all phases and transitions.

**Tasks**:
- [TASK-5-1] Test refine phase end-to-end — Acceptance: Pipeline runs refine via work loop, posts analysis with approval checkbox
- [TASK-5-2] Test refine → plan transition — Acceptance: Checking approval box triggers plan phase in work loop
- [TASK-5-3] Test plan phase end-to-end — Acceptance: Pipeline runs plan via work loop, posts plan with approval checkbox
- [TASK-5-4] Test plan → implement transition — Acceptance: Checking approval box triggers implement phase in work loop
- [TASK-5-5] Test implement phase end-to-end — Acceptance: Pipeline runs implement via work loop, creates PR, marks ready for review
- [TASK-5-6] Test phase step-back functionality — Acceptance: Running with `starting_phase=refine` from implement phase clears plan/implement work and restarts

**Dependencies**: Phase 4

**Exit criteria**: All phase transitions work correctly; circuit breaker escalation works; step-back functionality works

## Test Strategy

- **YAML validation**: Run `yamllint` and `actionlint` on modified workflow files
- **Unit testing**: Not applicable (workflow file changes only)
- **Integration testing**: Create test issue #XYZ to run full pipeline end-to-end
  1. Label issue with `sdlc:refine` to trigger pipeline
  2. Verify refine phase runs and posts analysis
  3. Approve refine to trigger plan phase
  4. Verify plan phase runs and posts plan
  5. Approve plan to trigger implement phase
  6. Verify implement creates PR
- **Phase step-back testing**: After reaching implement, run with `starting_phase=refine` and verify reset
- **Circuit breaker testing**: Manually trigger review rejection loop to verify escalation

## Rollback Plan

The migration can be rolled back at any stage:

1. **Immediate rollback**: `git revert <commit-sha>` to restore original `sdlc-pipeline.yml`
2. **Partial rollback**: If only HITL integration fails, revert HITL changes while keeping pipeline changes
3. **Work loop fallback**: If work loop has issues, the original `sdlc-work-loop.yml` can be reverted in a separate PR

Specific commands:
```bash
# Revert entire PR
git revert --no-edit <merge-commit-sha>

# Or revert specific commit
git revert --no-edit <commit-sha>
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| In-flight issues break during migration | Medium | Medium | Deploy during low-activity period; test on dedicated issue first |
| HITL phase transitions fail | Low | High | Verify HITL workflow before removing old jobs; keep old workflow as reference |
| Phase step-back clears too much work | Low | Medium | Implement confirmation in UI (future); document behavior clearly |
| Secrets not inherited correctly | Low | High | Test secrets inheritance on first work loop call; verify early in pipeline |
| Concurrency conflicts between pipeline and work loop | Low | Medium | Work loop uses separate concurrency group; pipeline cancellation is disabled |

## Migration Notes

**Breaking changes**: None for external callers. The `starting_phase` input gains new behavior (allows stepping back) but is backward-compatible.

**Deployment considerations**:
- In-flight issues will continue with old behavior until next pipeline trigger
- New pipeline behavior takes effect on next run
- No database or config migrations required

**Documentation updates needed**:
- Update `docs/sdlc-pipeline.md` with new architecture
- Document phase step-back feature
- Update troubleshooting guide for new job structure

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Migrate sdlc-pipeline.yml to use unified work loop"
  description: |
    Part 3 of 4 for SDLC Unification. Migrates the main pipeline from ~2,600
    lines to ~330 lines by replacing duplicated phase jobs with a single call
    to sdlc-work-loop.yml. Adds phase step-back functionality per human input.

    Fixes #450
phases:
  - id: 1
    name: Refactor init Job for Phase Override Support
    goal: Update init job to support starting_phase override for stepping back to prior phases
    tasks:
      - id: TASK-1-1
        description: Add phase validation logic for starting_phase override
        acceptance: Validates starting_phase is not ahead of current_phase
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-2
        description: Implement phase reset logic when stepping back
        acceptance: Deletes draft/review files for reset phases, updates contract
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-3
        description: Add phase_configs initialization in contract creation
        acceptance: New contracts include phase_configs with defaults for all phases
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-1-4
        description: Output additional job outputs for work loop
        acceptance: Job outputs include pr_number and workflow_owner
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 2
    name: Add Single Work Loop Call
    goal: Replace all phase-specific jobs with a single call to sdlc-work-loop.yml
    tasks:
      - id: TASK-2-1
        description: Add work-loop job that calls sdlc-work-loop.yml
        acceptance: Job uses needs dependencies, passes all required inputs
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-2
        description: Add conditional skip for PR phase
        acceptance: Job has if condition to skip when current_phase is pr
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-3
        description: Pass secrets to work loop
        acceptance: Uses secrets inherit to pass all required secrets
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-4
        description: Configure phase-aware inputs
        acceptance: Inputs computed from current_phase using GitHub expressions
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 3
    name: Remove Old Phase Jobs
    goal: Delete all phase-specific jobs now handled by the work loop
    tasks:
      - id: TASK-3-1
        description: Remove refine job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-2
        description: Remove refine-review job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-3
        description: Remove refine-redispatch job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-4
        description: Remove plan job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-5
        description: Remove plan-review job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-6
        description: Remove plan-redispatch job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-7
        description: Remove implement job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-8
        description: Remove wait-for-checks job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-9
        description: Remove finalize-pr job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-3-10
        description: Remove checks-failed job
        acceptance: Job deleted; no references remain
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 4
    name: Update HITL Integration
    goal: Verify and update sdlc-hitl.yml to work with unified pipeline
    tasks:
      - id: TASK-4-1
        description: Verify phase transition triggers work with new pipeline
        acceptance: HITL workflow triggers correct phase in work loop
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-4-2
        description: Verify circuit breaker escalation flow
        acceptance: Work loop circuit breaker escalation handled correctly
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-4-3
        description: Update any hardcoded job references in HITL workflow
        acceptance: No references to removed jobs remain
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-4-4
        description: Test phase approval checkbox detection
        acceptance: Existing egg-phase-approval markers trigger transitions
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 5
    name: Validate End-to-End Flow
    goal: Ensure complete pipeline works for all phases and transitions
    tasks:
      - id: TASK-5-1
        description: Test refine phase end-to-end
        acceptance: Pipeline runs refine via work loop, posts analysis
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-2
        description: Test refine to plan transition
        acceptance: Approval checkbox triggers plan phase
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-3
        description: Test plan phase end-to-end
        acceptance: Pipeline runs plan via work loop, posts plan
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-4
        description: Test plan to implement transition
        acceptance: Approval checkbox triggers implement phase
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-5
        description: Test implement phase end-to-end
        acceptance: Pipeline creates PR and marks ready for review
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-5-6
        description: Test phase step-back functionality
        acceptance: Running with starting_phase clears subsequent work
        files:
          - .github/workflows/sdlc-pipeline.yml
```

---

*Authored-by: egg*
