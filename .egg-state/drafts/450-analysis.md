# Analysis: SDLC Unification 3/4: Pipeline Migration

> Issue: #450 | Phase: refine

## Problem Statement

The current `sdlc-pipeline.yml` is ~2,600 lines of code with significant duplication across the refine, plan, and implement phases. Each phase follows the same pattern:
1. Execute work (generate analysis/plan/code)
2. Run automated review
3. Handle review verdict (approve → human gate, or reject → redispatch)
4. Escalate via circuit breaker if max cycles exceeded

This repetition creates maintenance burden and inconsistency risk. The unified work loop (`sdlc-work-loop.yml`, ~1,500 lines, merged via PR #457) already implements this pattern as a reusable workflow. This issue migrates the main pipeline to call the work loop, reducing `sdlc-pipeline.yml` to ~300 lines of pure orchestration.

## Current Behavior

### sdlc-pipeline.yml Structure (2,568 lines)

The pipeline currently has these jobs:

| Job | Lines | Purpose |
|-----|-------|---------|
| `resolve-inputs` | 83-120 | Normalize inputs from triggers |
| `init` | 124-395 | Setup branch, create contract, close stale PRs |
| `refine` | 1236-1359 | Run egg agent for issue analysis |
| `refine-review` | 1363-1651 | Automated review of analysis |
| `refine-redispatch` | 1729-1852 | Retry or escalate refine phase |
| `plan` | 1857-2075 | Run egg agent for implementation plan |
| `plan-review` | 2080-2440 | Automated review of plan |
| `plan-redispatch` | 2445-2539 | Retry or escalate plan phase |
| `implement` | 400-769 | Run egg agent for code implementation |
| `wait-for-checks` | 773-922 | Poll for CI completion |
| `finalize-pr` | 926-1147 | Mark PR ready for human review |
| `checks-failed` | 1151-1231 | Handle CI failures |

### sdlc-work-loop.yml Structure (1,494 lines)

The unified work loop (merged in PR #457) provides:

| Job | Purpose |
|-----|---------|
| `resolve-inputs` | Validate and resolve phase configuration |
| `work` | Execute phase work (refine/plan/implement) |
| `check-merge-conflict` | Detect merge conflicts (implement only) |
| `check-lint` | Run lint checks (implement only) |
| `check-test` | Run test checks (implement only) |
| `check-fixer` | Auto-fix failures (implement only) |
| `aggregate-checks` | Collect check results |
| `review` | Automated review with circuit breaker |
| `respond` | Route based on verdict, update contract |
| `human-gate` | Post to issue with approval checkbox |
| `human-gate-pr` | Mark PR ready for review |

Key inputs accepted by the work loop:
- `phase`: refine, plan, or implement
- `issue_number`, `branch_name`: Issue context
- `work_prompt_script`, `review_prompt_script`: Phase-specific prompt builders
- `output_type`: "draft" (refine/plan) or "pr" (implement)
- `human_review_mechanism`: "issue_checkbox" or "pr_review"
- `max_review_cycles`, `max_total_cycles`: Circuit breaker thresholds
- `work_timeout`, `review_timeout`: Execution timeouts
- `pr_number`: Existing PR for implement phase

### Phase Transitions via sdlc-hitl.yml

Human approval triggers phase transitions:
1. Human checks approval checkbox on issue comment
2. `sdlc-hitl.yml` detects the `issue_comment` edit event
3. Parses decision from `<!-- egg-hitl-decision -->` marker
4. Updates contract `current_phase` field
5. Re-triggers `sdlc-pipeline.yml` to continue with next phase

## Constraints

### Technical Constraints

1. **Backwards compatibility**: The work loop is already merged. Pipeline migration must work with existing contract schema and HITL mechanisms.

2. **Concurrency control**: The work loop uses `concurrency: sdlc-work-loop-${{ inputs.issue_number }}`. The pipeline must not create conflicting concurrency groups.

3. **Secret propagation**: The work loop requires secrets (`BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`, `BOT_APP_INSTALLATION_ID`, `ANTHROPIC_OAUTH_TOKEN`). Pipeline must pass these correctly.

4. **Trusted script isolation**: Scripts must always be sourced from `main` branch checkout, not the issue branch.

5. **Contract state consistency**: Multiple concurrent writes to the contract file must be handled via idempotent JQ transformations with retry logic.

### Business Constraints

1. **Phase transitions must work**: Human approval on refine must trigger plan, approval on plan must trigger implement.

2. **Existing HITL checkboxes**: The current checkbox format must continue to work.

3. **Gradual rollout**: Should not break in-flight issues during deployment.

### Dependencies

1. **PR #448 (Schema Extension)**: Must be merged first - provides `phase_configs` in contract schema (already available).

2. **PR #457 (Work Loop)**: Already merged - provides `sdlc-work-loop.yml`.

## Options Considered

### Option A: Direct Work Loop Calls

**Approach**: Replace each phase's jobs with a single `workflow_call` to `sdlc-work-loop.yml`.

```yaml
refine:
  needs: [resolve-inputs, init]
  if: needs.init.outputs.current_phase == 'refine'
  uses: ./.github/workflows/sdlc-work-loop.yml
  with:
    phase: refine
    issue_number: ${{ needs.init.outputs.issue_number }}
    branch_name: ${{ needs.init.outputs.branch_name }}
    output_type: draft
    human_review_mechanism: issue_checkbox
  secrets: inherit
```

**Pros**:
- Simple, direct approach
- Minimal new code
- Clear job dependency graph
- Work loop handles all complexity internally

**Cons**:
- GitHub Actions limitation: `workflow_call` jobs cannot have `if` conditions based on `needs` outputs when using reusable workflows (the `if` is evaluated before `needs` are resolved)
- Requires workaround for conditional execution

### Option B: Conditional Dispatch via Wrapper Jobs

**Approach**: Create thin wrapper jobs that conditionally dispatch to the work loop.

```yaml
refine-dispatch:
  needs: [resolve-inputs, init]
  if: needs.init.outputs.current_phase == 'refine'
  runs-on: ubuntu-latest
  steps:
    - name: Dispatch work loop
      uses: actions/github-script@v7
      with:
        script: |
          await github.rest.actions.createWorkflowDispatch({
            owner: context.repo.owner,
            repo: context.repo.repo,
            workflow_id: 'sdlc-work-loop.yml',
            ref: 'main',
            inputs: {
              phase: 'refine',
              issue_number: '${{ needs.init.outputs.issue_number }}',
              branch_name: '${{ needs.init.outputs.branch_name }}'
            }
          });
```

**Pros**:
- Works around GitHub Actions conditional limitations
- Each phase can have its own conditional logic
- Pipeline can continue after dispatch (fire-and-forget)

**Cons**:
- Loses job dependency tracking (dispatch is async)
- Cannot wait for work loop completion
- More complex error handling
- Harder to debug (two separate workflow runs)

### Option C: Hybrid with Phase Router Job

**Approach**: Keep a single entry point that routes to phases, using `workflow_call` with a router pattern.

```yaml
phase-router:
  needs: [resolve-inputs, init]
  runs-on: ubuntu-latest
  outputs:
    run_refine: ${{ steps.route.outputs.run_refine }}
    run_plan: ${{ steps.route.outputs.run_plan }}
    run_implement: ${{ steps.route.outputs.run_implement }}
  steps:
    - id: route
      run: |
        PHASE="${{ needs.init.outputs.current_phase }}"
        echo "run_refine=$([[ $PHASE == 'refine' ]] && echo true || echo false)" >> "$GITHUB_OUTPUT"
        echo "run_plan=$([[ $PHASE == 'plan' ]] && echo true || echo false)" >> "$GITHUB_OUTPUT"
        echo "run_implement=$([[ $PHASE == 'implement' ]] && echo true || echo false)" >> "$GITHUB_OUTPUT"

refine:
  needs: [resolve-inputs, init, phase-router]
  if: needs.phase-router.outputs.run_refine == 'true'
  uses: ./.github/workflows/sdlc-work-loop.yml
  with:
    phase: refine
    ...
```

**Pros**:
- Works with GitHub Actions conditional model
- Maintains job dependency tracking
- Clear routing logic in one place
- Can pass `secrets: inherit`

**Cons**:
- Extra job adds complexity
- Three workflow_call jobs defined even though only one runs
- Slightly harder to understand flow

### Option D: Unified Phase Call with Dynamic Inputs

**Approach**: Single work loop call that receives phase from init job.

```yaml
work-loop:
  needs: [resolve-inputs, init]
  if: needs.init.outputs.current_phase != 'pr'
  uses: ./.github/workflows/sdlc-work-loop.yml
  with:
    phase: ${{ needs.init.outputs.current_phase }}
    issue_number: ${{ needs.init.outputs.issue_number }}
    branch_name: ${{ needs.init.outputs.branch_name }}
    output_type: ${{ needs.init.outputs.current_phase == 'implement' && 'pr' || 'draft' }}
    human_review_mechanism: ${{ needs.init.outputs.current_phase == 'implement' && 'pr_review' || 'issue_checkbox' }}
  secrets: inherit
```

**Pros**:
- Simplest possible pipeline structure
- Single job handles all phases
- Phase-specific configuration handled via expressions
- Easiest to maintain

**Cons**:
- Less explicit about what each phase does
- Debugging requires understanding dynamic inputs
- Work loop must handle all phase variations internally (already does)

## Recommended Approach

**Option D: Unified Phase Call with Dynamic Inputs** is recommended.

### Rationale

1. **Simplicity**: The work loop already handles phase-specific logic internally. The pipeline should be a thin orchestration layer.

2. **The work loop is already built for this**: `sdlc-work-loop.yml` accepts `phase` as input and uses it to:
   - Set `output_type` default to "pr" for implement, "draft" otherwise
   - Set `human_review_mechanism` default to "pr_review" for implement, "issue_checkbox" otherwise
   - Run appropriate checks only for implement phase

3. **Minimal code**: The pipeline becomes:
   - `resolve-inputs`: ~40 lines (unchanged)
   - `init`: ~270 lines (unchanged, but refactored for phase config)
   - `work-loop`: ~20 lines (single reusable workflow call)
   - Total: ~330 lines (vs current ~2,600)

4. **Phase transitions still work**: After human approval, `sdlc-hitl.yml` updates `current_phase` in contract and re-triggers the pipeline. The `init` job reads `current_phase` from contract and the work loop runs the next phase.

### Implementation Plan

#### Phase 1: Update `init` job

1. Read `current_phase` from existing contract (if exists)
2. Set up `phase_configs` in contract for each phase:
   ```json
   "phase_configs": {
     "refine": {"max_review_cycles": 3, "human_review_mechanism": "ISSUE_CHECKBOX"},
     "plan": {"max_review_cycles": 3, "human_review_mechanism": "ISSUE_CHECKBOX"},
     "implement": {"max_review_cycles": 3, "human_review_mechanism": "PR_REVIEW"}
   }
   ```
3. Output `current_phase` for downstream jobs

#### Phase 2: Replace phase jobs with single work loop call

1. Remove jobs: `refine`, `refine-review`, `refine-redispatch`, `plan`, `plan-review`, `plan-redispatch`, `implement`, `wait-for-checks`, `finalize-pr`, `checks-failed`

2. Add single job:
   ```yaml
   work-loop:
     needs: [resolve-inputs, init]
     if: needs.init.outputs.current_phase != 'pr'
     uses: ./.github/workflows/sdlc-work-loop.yml
     with:
       phase: ${{ needs.init.outputs.current_phase }}
       issue_number: ${{ needs.init.outputs.issue_number }}
       branch_name: ${{ needs.init.outputs.branch_name }}
       workflow_owner: ${{ needs.init.outputs.workflow_owner }}
     secrets: inherit
   ```

#### Phase 3: Verify HITL integration

1. Confirm `sdlc-hitl.yml` updates `current_phase` correctly
2. Confirm pipeline re-trigger works for phase transitions
3. Test circuit breaker escalation flow

### Jobs After Migration

| Job | Purpose | Lines (est.) |
|-----|---------|--------------|
| `resolve-inputs` | Normalize trigger inputs | ~40 |
| `init` | Setup branch, contract, phase config | ~270 |
| `work-loop` | Call unified work loop | ~20 |
| **Total** | | **~330** |

This achieves the exit criteria of reducing from ~2,600 lines to ~300 lines.

## Open Questions

### Question 1: Starting Phase Override

The current `starting_phase` input allows manual override of which phase to run. Should this:

A) Override `current_phase` from contract (force restart from specific phase)
B) Only apply when contract doesn't exist (initial run only)
C) Be removed entirely (always trust contract state)

**Current behavior**: `starting_phase` is used when contract is created, but contract's `current_phase` is authoritative after that.

**Recommendation**: Keep option B (apply only for initial run). This maintains backwards compatibility while respecting contract state.

### Question 2: PR Phase Handling

The current pipeline has no explicit "pr" phase job - it's handled by `finalize-pr` at the end of implement. The work loop handles this via `human-gate-pr`.

Should the pipeline:

A) Keep `if: current_phase != 'pr'` (work loop not called when in PR phase)
B) Have the work loop handle PR phase as a no-op
C) Add explicit PR phase handling (e.g., for PR updates/amendments)

**Recommendation**: Option A. Once in PR phase, human review happens via GitHub's native PR review system. No further work loop runs needed unless feedback requires re-implementation (which would transition back to implement phase).

### Question 3: Migration Testing Strategy

How should we test the migration?

A) Create a test issue specifically for migration validation
B) Run both old and new pipelines in parallel on a test issue
C) Deploy directly and monitor for failures

**Recommendation**: Option A. Create issue #451 or similar as a dedicated test case that exercises all three phases and circuit breaker escalation.

---

*Authored-by: egg*
