# Analysis: Unify SDLC phases into single reusable work loop

> Issue: #436 | Phase: refine

## Problem Statement

The current SDLC pipeline (`sdlc-pipeline.yml`) implements separate job definitions for each phase (refine, plan, implement) with significant code duplication. The file is ~2,600 lines with repeated patterns across phases:

- Each phase has a work job (`refine`, `plan`, `implement`)
- Each phase has a review job (`refine-review`, `plan-review`)
- Each phase has a re-dispatch job (`refine-redispatch`, `plan-redispatch`)

The issue proposes a unified work loop that all phases share:
1. Human gives input
2. Agent does work
3. Agent reviews work
4. Agent responds to review
5. Cycle repeats until work is complete
6. Agent collects work and presents to human

This pattern is fundamentally the same across refine, plan, and implement — only the agents, context, contract configuration, and intermediate checks differ.

## Current Behavior

### Workflow Structure (~2,600 lines in `sdlc-pipeline.yml`)

The pipeline currently consists of:

1. **Input Resolution** (`resolve-inputs`): Resolves workflow parameters from various trigger sources

2. **Initialization** (`init`): Creates contract, branch, applies labels, posts status

3. **Phase-specific job triads** (repeated 3x with minor variations):
   - `refine` → `refine-review` → `refine-redispatch`
   - `plan` → `plan-review` → `plan-redispatch`
   - `implement` → `wait-for-checks` → `finalize-pr` / `checks-failed`

4. **Supporting workflows**:
   - `sdlc-hitl.yml`: Handles HITL decision resolution, phase approval, and feedback submission
   - `reusable-review.yml`: Generic PR review workflow (already parameterized)
   - `reusable-autofix.yml`: Generic autofix workflow (already parameterized)

### Code Duplication Analysis

Each phase work job follows the same pattern:
```
1. Generate bot token
2. Checkout main (trusted)
3. Build phase-specific prompt (build-sdlc-prompt.sh)
4. Checkout issue branch
5. Run egg agent
6. Validate output
7. Post status comments
```

Each phase review job follows the same pattern:
```
1. Generate bot token
2. Checkout main (trusted)
3. Save trusted scripts
4. Checkout issue branch
5. Get review cycle from contract
6. Check circuit breaker
7. Build review prompt (build-{phase}-review-prompt.sh)
8. Run reviewer agent
9. Parse verdict
10. Update contract with review state
11. Post final output to issue (if approved)
```

Each re-dispatch job follows the same pattern:
```
1. Generate bot token
2. Checkout issue branch
3. Check circuit breaker
4. Post escalation comment (if circuit breaker open)
5. Re-dispatch workflow (if circuit breaker closed)
```

### Contract Management

The contract schema (`shared/egg_contracts/models.py`) already supports:
- Phase-specific review cycles (`refine_review_cycles`, `plan_review_cycles`)
- Phase-specific feedback (`refine_review_feedback`, `plan_review_feedback`)
- Circuit breaker with configurable thresholds
- Task-level and phase-level cycle tracking
- Audit logging for all state changes

### Existing Reusable Patterns

`reusable-review.yml` demonstrates the parameterization approach:
- Accepts inputs: `pr_number`, `bot_name`, `prompt_script`, `timeout`, `agent_role`
- Uses trusted checkout pattern for prompt building
- Handles CI check waiting
- Integrates with contract via environment variables

## Constraints

### Technical Constraints

1. **GitHub Actions limitations**:
   - Cannot dynamically select `uses:` for actions (must hardcode)
   - Reusable workflows have limited context propagation
   - `workflow_call` triggers have different input handling than `workflow_dispatch`

2. **Security model**:
   - Prompt scripts must run from trusted `main` checkout
   - Untrusted PR code cannot access secrets during prompt building
   - Gateway sidecar enforces branch ownership

3. **Contract integrity**:
   - Contract updates must be atomic with conflict-resistant push
   - Audit log must capture all state transitions
   - Circuit breaker must prevent runaway loops

### Business Constraints

1. **Backward compatibility**: Not required per issue description — "don't worry about saving work we've done so far or backwards compatibility"

2. **Completeness**: Must be fully functional end-to-end before creating PR (per issue instructions)

3. **Human review gates**:
   - Refine/plan phases use issue comment checkboxes
   - Implement phase uses PR review
   - These mechanisms should remain separate (approved decision from #430)

### Dependencies

1. **Gateway sidecar**: Enforces git operations, holds tokens
2. **Contract schema**: Must evolve to support check DAGs
3. **Prompt builders**: Phase-specific prompts currently in separate scripts

## Options Considered

### Option A: Single Reusable Workflow (Recommended)

**Approach**: Create `sdlc-work-loop.yml` that accepts phase configuration as inputs. The main `sdlc-pipeline.yml` becomes a thin orchestrator that calls the reusable workflow with different parameters.

```yaml
# sdlc-work-loop.yml (reusable workflow)
on:
  workflow_call:
    inputs:
      phase: { type: string }  # refine, plan, implement
      issue_number: { type: number }
      work_prompt_script: { type: string }
      review_prompt_script: { type: string }
      checks: { type: string }  # JSON array of check definitions
      output_type: { type: string }  # draft, pr
```

The unified loop handles:
1. Work execution (agent does work)
2. Intermediate checks (configurable DAG)
3. Review (agent reviews work)
4. Response (agent addresses feedback)
5. Circuit breaker (escalation after N cycles)
6. Human review gate (issue checkbox or PR review)

**Pros**:
- Single source of truth for the work loop
- Easy to add new phases in the future
- Reduced maintenance burden (~2,600 lines → ~1,000 lines estimated)
- Consistent behavior across phases
- Check DAGs are configurable per-phase

**Cons**:
- Complex input schema for reusable workflow
- Some phase-specific edge cases may require conditionals
- Debugging may be harder with abstraction layer

### Option B: Composite Actions

**Approach**: Create composite actions for shared steps, keeping jobs in the main workflow but reducing duplication.

```yaml
# .github/actions/sdlc-work-step/action.yml
# .github/actions/sdlc-review-step/action.yml
# .github/actions/sdlc-redispatch-step/action.yml
```

**Pros**:
- Incremental refactoring possible
- Actions are simpler than reusable workflows
- Easier to test in isolation

**Cons**:
- Composite actions cannot use `secrets` context directly
- Limited to single job — cannot orchestrate job dependencies
- Would still have significant duplication at job level

### Option C: External Orchestrator

**Approach**: Move loop logic into a Python/TypeScript orchestrator that makes API calls to trigger workflow runs.

**Pros**:
- Full programming language control
- Could implement arbitrary DAGs
- Easier testing

**Cons**:
- Introduces new infrastructure component
- Loses GitHub Actions native features (logs, UI, concurrency)
- More complex deployment and maintenance
- Over-engineered for the problem

## Recommended Approach

**Option A: Single Reusable Workflow** is recommended because:

1. **Directly addresses the issue goal**: "Let's put together a workflow covering the above loop. We pass in different agents, context, and contract management to the loop to set the work phase."

2. **Aligns with approved decision from #430**: "Parameterized Reusable Workflow" was already selected.

3. **Builds on existing patterns**: `reusable-review.yml` and `reusable-autofix.yml` already demonstrate this approach works.

4. **Enables check DAGs**: The issue specifically requests "any arbitrary DAG of checks" between work and review steps.

5. **Supports human review unification**: The same loop structure can handle both issue-based and PR-based human review gates.

## Implementation Architecture

### Unified Work Loop Structure

```
┌─────────────────────────────────────────────────────────┐
│                   sdlc-work-loop.yml                    │
├─────────────────────────────────────────────────────────┤
│  Inputs:                                                │
│  - phase (refine | plan | implement)                    │
│  - issue_number                                         │
│  - work_prompt_script                                   │
│  - review_prompt_script                                 │
│  - checks (JSON: [{name, script, required}...])         │
│  - output_type (draft | pr)                             │
│  - human_review_mechanism (issue_checkbox | pr_review)  │
├─────────────────────────────────────────────────────────┤
│  Jobs:                                                  │
│  1. work: Run work agent with phase-specific prompt     │
│  2. checks: Run intermediate checks (DAG execution)     │
│  3. review: Run review agent                            │
│  4. respond: Handle review feedback (redispatch or exit)│
│  5. human-gate: Post for human review                   │
└─────────────────────────────────────────────────────────┘
```

### Contract Schema Extension for Check DAGs

```python
class CheckDefinition(BaseModel):
    """Definition of an intermediate check in the work loop."""
    id: str = Field(..., pattern=r"^check-[a-z0-9-]+$")
    name: str
    script: str  # Path to check script
    required: bool = True
    retry_on_fail: bool = False
    max_retries: int = 3

class PhaseConfig(BaseModel):
    """Configuration for a pipeline phase."""
    checks: list[CheckDefinition] = []
    max_review_cycles: int = 3
    human_review_mechanism: str  # "issue_checkbox" | "pr_review"
```

### Phase Configurations

**Refine Phase**:
- Work prompt: `build-sdlc-prompt.sh` with `EGG_PIPELINE_PHASE=refine`
- Review prompt: `build-refine-review-prompt.sh`
- Checks: None (just validation that draft exists)
- Output: Draft to `.egg-state/drafts/{issue}-analysis.md`
- Human review: Issue checkbox

**Plan Phase**:
- Work prompt: `build-sdlc-prompt.sh` with `EGG_PIPELINE_PHASE=plan`
- Review prompt: `build-plan-review-prompt.sh`
- Checks: Plan validation, YAML task extraction
- Output: Draft to `.egg-state/drafts/{issue}-plan.md`
- Human review: Issue checkbox

**Implement Phase**:
- Work prompt: `build-sdlc-prompt.sh` with `EGG_PIPELINE_PHASE=implement`
- Review prompt: `build-review-prompt.sh`
- Checks: Lint, test, autofix, merge conflict check
- Output: PR creation
- Human review: PR review (via existing `reusable-review.yml`)

### Human Review Unification

The human review step uses the **same workflow structure** with different configuration:

```yaml
human-gate:
  if: inputs.human_review_mechanism == 'issue_checkbox'
  steps:
    - Post analysis/plan to issue with approval checkbox
    - Wait for sdlc-hitl.yml to handle checkbox toggle

human-gate-pr:
  if: inputs.human_review_mechanism == 'pr_review'
  steps:
    - Create/update PR
    - Wait for human PR review approval
```

This is still "async" from the workflow's perspective — the workflow exits after posting, and human action triggers a new workflow run via `sdlc-hitl.yml` or PR review webhook.

## Open Questions

### Decision: Check Script Location

Where should intermediate check scripts live?

```bash
egg-contract add-decision --question "Where should intermediate check scripts live?" \
  --options "In .github/scripts/ (collocated with workflows)" \
  "In action/ (collocated with prompt builders)" \
  "In a new checks/ directory" --format markdown
```

<!-- egg-hitl-decision id=decision-1 -->
- [ ] In `.github/scripts/` (collocated with workflows)
- [ ] In `action/` (collocated with prompt builders)
- [ ] In a new `checks/` directory
- [ ] Other (explain in reply)

### Decision: Review Agent Reuse

Should the implement phase reuse `reusable-review.yml` or have its own review step in the unified loop?

```bash
egg-contract add-decision --question "Should implement phase review be unified into the work loop?" \
  --options "Yes - unify all review into work loop" \
  "No - keep reusable-review.yml for PR reviews" \
  "Hybrid - work loop handles internal review, reusable-review.yml handles PR review" --format markdown
```

<!-- egg-hitl-decision id=decision-2 -->
- [ ] Yes — unify all review into work loop
- [ ] No — keep `reusable-review.yml` for PR reviews
- [ ] Hybrid — work loop handles internal review, `reusable-review.yml` handles PR review
- [ ] Other (explain in reply)

### Open-Ended Question

The issue mentions "any arbitrary DAG of checks" between work and review steps. To design this properly:

1. What specific checks are anticipated beyond the current lint/test/autofix?
2. Are there ordering constraints between checks (e.g., lint must pass before test)?
3. Should check failures block review, or should review see the failures?

## Risk Assessment

### High Risk: Incomplete Implementation

The previous attempt (#430) produced an incomplete implementation. Mitigations:
- Detailed task breakdown in plan phase with precise acceptance criteria
- All tasks must be implemented before PR creation
- End-to-end testing before marking complete

### Medium Risk: Regression in Existing Behavior

Mitigations:
- Create comprehensive test cases covering current behavior
- Run parallel testing against existing pipeline
- Document all behavioral changes

### Low Risk: Performance Degradation

Reusable workflow calls add ~10s overhead per call. Mitigations:
- Minimize workflow_call nesting
- Use job-level parallelism where possible

## Summary

The recommended approach is to create a **single reusable workflow** (`sdlc-work-loop.yml`) that encapsulates the generate/review/respond loop with configurable:
- Phase-specific prompts
- Intermediate check DAGs
- Human review mechanisms
- Circuit breaker thresholds

The main `sdlc-pipeline.yml` becomes a thin orchestrator (~200 lines) that:
1. Resolves inputs
2. Initializes contract
3. Calls `sdlc-work-loop.yml` with phase-specific configuration
4. Handles phase transitions via `sdlc-hitl.yml`

This approach directly implements the issue requirements while building on existing patterns and approved decisions.

---

*Authored-by: egg*
