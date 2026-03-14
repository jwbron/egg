# Plan: Integrate specialized reviewers into work loop review cycle

> Issue: #464 | Phase: plan

## Summary

This plan implements parallel multi-reviewer integration in the work loop, as selected in the refine phase (Option C+E). The approach runs multiple specialized reviewers concurrently during the review step, adapts reviewer scripts to work without PR context for refine/plan phases, and aggregates verdicts to drive the work/review/respond cycle. Based on human feedback: review timeout increases to 30 minutes, reviewers are configurable via input list, and reviewer failures escalate to human.

## Implementation Phases

### Phase 1: Adapt Specialized Review Scripts for Non-PR Context

**Goal**: Modify the agent mode design and contract verification review scripts to work in refine/plan phases where no PR exists, using `git diff origin/main..HEAD` instead of `gh pr diff`.

**Tasks**:
- [TASK-1-1] Create `build-agent-mode-design-review-prompt-workloop.sh` — A work-loop-compatible version of the agent mode design reviewer that uses `EGG_PIPELINE_PHASE` and `EGG_ISSUE_NUMBER` instead of `PR_NUMBER`, and writes verdict to JSON file instead of posting via `gh pr review`. Acceptance: Script runs without `PR_NUMBER`, outputs to `.egg-state/reviews/{ISSUE}-{PHASE}-agent-design.json`
- [TASK-1-2] Create `build-contract-verification-prompt-workloop.sh` — A work-loop-compatible version of the contract verification reviewer that uses the same environment variables as the work loop. Acceptance: Script runs for implement phase, outputs to `.egg-state/reviews/{ISSUE}-{PHASE}-contract.json`
- [TASK-1-3] Create `build-code-review-prompt-workloop.sh` — A work-loop-compatible version of the code reviewer that outputs JSON verdict instead of posting to PR. Acceptance: Script runs for implement phase, outputs to `.egg-state/reviews/{ISSUE}-{PHASE}-code.json`

**Dependencies**: None

**Exit criteria**: All three new scripts can be invoked with work loop environment variables and produce correctly-formatted JSON verdict files.

### Phase 2: Add Parallel Reviewer Jobs to Work Loop

**Goal**: Refactor `sdlc-work-loop.yml` to run multiple reviewer jobs in parallel, each using its own prompt script and outputting to a distinct JSON file.

**Tasks**:
- [TASK-2-1] Add `reviewers` input to work loop — Accept a JSON array of reviewer names to run (e.g., `["unified", "agent-design"]`). Default varies by phase: `["unified", "agent-design"]` for refine/plan, `["unified", "agent-design", "code", "contract"]` for implement. Acceptance: Input is validated and parsed correctly, default values work
- [TASK-2-2] Create reviewer job matrix — Convert the single `review` job into a matrix job that spawns one job per reviewer in the input list. Each job runs its corresponding prompt script and produces output to `.egg-state/reviews/{ISSUE}-{PHASE}-{REVIEWER}.json`. Acceptance: Multiple reviewer jobs run in parallel, each writes to correct file
- [TASK-2-3] Add review timeout configuration — Update `review_timeout` input default from 15 to 30 minutes to accommodate multiple reviewers. Acceptance: Timeout is 30 minutes by default

**Dependencies**: Phase 1 (scripts must exist)

**Exit criteria**: Running the work loop spawns parallel reviewer jobs, each producing its own verdict file.

### Phase 3: Implement Verdict Aggregation

**Goal**: Create an aggregation job that collects verdicts from all reviewers, combines feedback, and outputs a single aggregate verdict.

**Tasks**:
- [TASK-3-1] Create `aggregate-reviews` job — A job that runs after all reviewer jobs complete, reads all verdict JSON files, and outputs an aggregated result. Logic: any `needs_revision` → aggregate `needs_revision`; all `approved` → aggregate `approved`; any failure → escalate to human. Acceptance: Job correctly aggregates verdicts from all reviewer files
- [TASK-3-2] Implement failure escalation — If any reviewer job fails (timeout, crash, missing verdict file), escalate to human instead of continuing. Post escalation comment with details of which reviewer failed. Acceptance: Reviewer failures trigger human escalation
- [TASK-3-3] Combine feedback from multiple reviewers — When aggregate verdict is `needs_revision`, concatenate feedback from all reviewers with headers indicating source (e.g., "## Unified Reviewer\n...\n## Agent Design Reviewer\n..."). Acceptance: Combined feedback is readable and properly attributed

**Dependencies**: Phase 2 (reviewer matrix must exist)

**Exit criteria**: Aggregate verdict drives the respond job, failed reviewers escalate to human, feedback is properly combined.

### Phase 4: Update Respond Job for Aggregated Verdicts

**Goal**: Modify the respond job to consume the aggregated verdict and update the contract with multi-reviewer state.

**Tasks**:
- [TASK-4-1] Update respond job inputs — Change respond job to read from aggregate outputs instead of single review job. Acceptance: Respond job uses aggregated verdict and combined feedback
- [TASK-4-2] Update contract with per-reviewer state — Store review cycle count and feedback per-reviewer in the contract (e.g., `refine_agent_design_review_cycles`, `refine_unified_review_cycles`). Acceptance: Contract JSON has per-reviewer tracking fields
- [TASK-4-3] Update circuit breaker logic — Circuit breaker should count aggregate cycles, not individual reviewer cycles. If aggregate verdict is `needs_revision` for max cycles, open circuit breaker. Acceptance: Circuit breaker opens after max aggregate review cycles

**Dependencies**: Phase 3 (aggregation must exist)

**Exit criteria**: Work loop correctly iterates based on aggregate verdict, contract tracks per-reviewer state.

### Phase 5: Integration Testing and Documentation

**Goal**: Validate the complete multi-reviewer flow and document the new architecture.

**Tasks**:
- [TASK-5-1] Add integration test for parallel reviewers — Test that runs work loop with multiple reviewers and verifies correct verdict aggregation. Acceptance: Test passes with parallel reviewers producing mixed verdicts
- [TASK-5-2] Add integration test for reviewer failure escalation — Test that verifies reviewer timeout/failure triggers human escalation. Acceptance: Test passes with simulated reviewer failure
- [TASK-5-3] Update workflow documentation — Document the new `reviewers` input, verdict aggregation logic, and failure escalation behavior. Acceptance: Documentation is clear and accurate

**Dependencies**: Phase 4

**Exit criteria**: All integration tests pass, documentation is complete.

## Test Strategy

- **Unit tests**:
  - Test each new prompt script in isolation with mock environment variables
  - Test verdict aggregation logic with various combinations (all approved, one needs_revision, one failed)
  - Test combined feedback formatting

- **Integration tests**:
  - Test full work loop cycle with multiple reviewers
  - Test failure escalation when one reviewer times out
  - Test circuit breaker behavior with aggregate verdicts

- **Manual testing**:
  1. Run work loop in refine phase, verify unified + agent-design reviewers run in parallel
  2. Run work loop in implement phase, verify all four reviewers run
  3. Simulate reviewer failure (e.g., bad prompt script), verify human escalation
  4. Verify combined feedback is readable when multiple reviewers request revision

## Rollback Plan

1. The `reviewers` input has sensible defaults matching current behavior (unified-only for backward compatibility)
2. If parallel reviewers cause issues, set `reviewers: '["unified"]'` to revert to single-reviewer behavior
3. Original prompt scripts (`build-unified-review-prompt.sh`, etc.) are preserved; new work-loop-compatible scripts are separate files
4. Contract changes are additive (new fields), existing fields remain unchanged

```bash
# To rollback to single reviewer:
# In workflow dispatch, set reviewers='["unified"]'

# Or revert the workflow changes:
git revert <commit-sha>
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Parallel jobs cause race conditions on git push | Medium | Medium | Each reviewer writes to distinct file; aggregation job commits all files together |
| Reviewer timeout causes workflow to hang | Low | High | 30-minute timeout per reviewer; failure escalation to human |
| Aggregated feedback is too long for issue comment | Low | Medium | Truncate combined feedback if over 65000 chars with "see workflow logs" link |
| Matrix job complexity makes debugging harder | Medium | Low | Clear job naming (`review-unified`, `review-agent-design`), detailed logging |
| Backward compatibility broken for callers | Low | High | Default reviewers match current behavior; new inputs are optional |

## Migration Notes

**No breaking changes for existing callers.** The work loop continues to work with default inputs. New functionality is opt-in via the `reviewers` input.

**Contract schema changes**:
- New optional fields: `{phase}_unified_review_cycles`, `{phase}_agent_design_review_cycles`, etc.
- Existing `{phase}_review_cycles` field remains and represents aggregate cycles

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Integrate specialized reviewers into work loop"
  description: |
    Adds parallel multi-reviewer support to the SDLC work loop. Specialized reviewers
    (agent mode design, code, contract) now run alongside the unified reviewer during
    the review step, with verdict aggregation and failure escalation.

    Closes #464
phases:
  - id: 1
    name: Adapt Review Scripts
    goal: Modify specialized review scripts to work without PR context
    tasks:
      - id: TASK-1-1
        description: Create work-loop-compatible agent mode design review script
        acceptance: Script runs without PR_NUMBER, outputs JSON verdict to .egg-state/reviews/{ISSUE}-{PHASE}-agent-design.json
        files:
          - action/build-agent-mode-design-review-prompt-workloop.sh
      - id: TASK-1-2
        description: Create work-loop-compatible contract verification review script
        acceptance: Script runs for implement phase, outputs JSON verdict to .egg-state/reviews/{ISSUE}-{PHASE}-contract.json
        files:
          - action/build-contract-verification-prompt-workloop.sh
      - id: TASK-1-3
        description: Create work-loop-compatible code review script
        acceptance: Script runs for implement phase, outputs JSON verdict to .egg-state/reviews/{ISSUE}-{PHASE}-code.json
        files:
          - action/build-code-review-prompt-workloop.sh
  - id: 2
    name: Add Parallel Reviewer Jobs
    goal: Refactor work loop to run multiple reviewer jobs in parallel
    tasks:
      - id: TASK-2-1
        description: Add reviewers input to work loop with phase-based defaults
        acceptance: Input is validated and parsed correctly, default values work per phase
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-2
        description: Convert review job to matrix for parallel execution
        acceptance: Multiple reviewer jobs run in parallel, each writes to correct file
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-2-3
        description: Update review timeout default to 30 minutes
        acceptance: Timeout is 30 minutes by default
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 3
    name: Implement Verdict Aggregation
    goal: Create aggregation job that combines verdicts from all reviewers
    tasks:
      - id: TASK-3-1
        description: Create aggregate-reviews job to combine verdicts
        acceptance: Job correctly aggregates verdicts from all reviewer files
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-2
        description: Implement failure escalation for reviewer failures
        acceptance: Reviewer failures trigger human escalation with details
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-3
        description: Combine feedback from multiple reviewers with headers
        acceptance: Combined feedback is readable and properly attributed
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 4
    name: Update Respond Job
    goal: Modify respond job for aggregated verdicts
    tasks:
      - id: TASK-4-1
        description: Update respond job to read from aggregate outputs
        acceptance: Respond job uses aggregated verdict and combined feedback
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-2
        description: Update contract with per-reviewer state tracking
        acceptance: Contract JSON has per-reviewer tracking fields
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-4-3
        description: Update circuit breaker for aggregate cycles
        acceptance: Circuit breaker opens after max aggregate review cycles
        files:
          - .github/workflows/sdlc-work-loop.yml
  - id: 5
    name: Integration Testing and Documentation
    goal: Validate multi-reviewer flow and document architecture
    tasks:
      - id: TASK-5-1
        description: Add integration test for parallel reviewers
        acceptance: Test passes with parallel reviewers producing mixed verdicts
        files:
          - tests/integration/test_multi_reviewer.py
      - id: TASK-5-2
        description: Add integration test for reviewer failure escalation
        acceptance: Test passes with simulated reviewer failure
        files:
          - tests/integration/test_multi_reviewer.py
      - id: TASK-5-3
        description: Update workflow documentation
        acceptance: Documentation is clear and accurate
        files:
          - docs/workflows/sdlc-work-loop.md
```

---

*Authored-by: egg*
