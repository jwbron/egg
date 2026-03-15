# Analysis: SDLC Unification 2/4: Unified Work Loop Workflow

> Issue: #449 | Phase: refine

## Problem Statement

The current SDLC pipeline (`sdlc-pipeline.yml`, ~103KB) implements separate job definitions for each phase (refine, plan, implement) with significant code duplication. Each phase follows the same fundamental loop:

1. Human provides input
2. Agent does work
3. Agent reviews work
4. Agent responds to review
5. Steps 2-4 cycle until work is complete
6. Agent presents work to human

Currently this pattern is implemented as separate jobs: `refine`, `refine-review`, `refine-redispatch`, `plan`, `plan-review`, `plan-redispatch`, `implement`, etc. This results in:

- **Code duplication**: Similar job structures repeated for each phase
- **Maintenance burden**: Changes to the work loop pattern require modifications in multiple places
- **Inconsistency risk**: Different phases may diverge in behavior over time
- **Limited extensibility**: Adding new phases requires duplicating the entire job structure

The desired outcome is a **single reusable workflow** (`sdlc-work-loop.yml`) that accepts phase configuration as inputs, with each phase invocation passing different agents, context, checks, and contract configuration to the same underlying loop.

## Current Behavior

### Pipeline Structure (sdlc-pipeline.yml)

The existing pipeline is organized as follows:

1. **resolve-inputs**: Normalizes inputs across trigger types (label/dispatch/call)
2. **init**: Creates contract and branch, posts status comment
3. **Phase-specific jobs**:
   - `refine` → `refine-review` → `refine-post-analysis` (or `refine-redispatch`)
   - `plan` → `plan-review` → `plan-post-plan` (or `plan-redispatch`)
   - `implement` → creates PR → PR review workflow

### Prompt Building System (action/)

Each phase has a dedicated prompt builder:

| Script | Phase | Purpose |
|--------|-------|---------|
| `build-sdlc-prompt.sh` | All | Phase-specific prompts via `EGG_PIPELINE_PHASE` |
| `build-refine-review-prompt.sh` | Refine | Review analysis quality |
| `build-plan-review-prompt.sh` | Plan | Review plan quality |

The main prompt builder (`build-sdlc-prompt.sh`) already supports phase parameterization via the `EGG_PIPELINE_PHASE` environment variable.

### Contract System (.egg-state/contracts/)

The contract schema (from #448) now includes:

- `PhaseConfig`: Configuration for checks, max_review_cycles, human_review_mechanism
- `CheckDefinition`: Individual check specification (id, name, script, required, retry_on_fail)
- `CheckResult`: Result of running a check (status, message, fixable)
- Default configurations in `shared/egg_contracts/phase_defaults.py`

### Check Infrastructure (from #448)

Phase defaults define checks per phase:

- **Refine**: `check-draft-validation`
- **Plan**: `check-plan-yaml`
- **Implement**: `check-merge-conflict` → `check-lint` + `check-test` (parallel) → `check-fixer`

## Constraints

### Technical Constraints

1. **GitHub Actions limitations**:
   - Reusable workflows can only be nested 4 levels deep
   - Job-level `if` conditions cannot reference `inputs` directly (requires resolve-inputs job)
   - Concurrency groups must be unique per issue to prevent parallel runs

2. **Security model**:
   - Prompt builders must run from trusted `main` checkout (not issue branch)
   - Check scripts should run from trusted sources
   - Contract updates need conflict-resistant retry logic

3. **Existing integrations**:
   - External repos call `sdlc-pipeline.yml` as a reusable workflow
   - HITL workflow (`sdlc-hitl.yml`) handles checkbox decisions separately
   - Must preserve existing input schema for backward compatibility

### Dependency Constraints

1. **Prerequisite**: Issue #448 (Contract Schema Extension & Check Scripts) - **CLOSED**
   - `PhaseConfig` and `CheckDefinition` models are available
   - `phase_defaults.py` provides default configurations

2. **Parallel testing requirement**: This issue creates the new workflow **without removing old jobs**, allowing parallel testing alongside the existing pipeline.

### Human Review Mechanisms (approved in #436)

- **Refine/Plan**: Issue checkbox (`ISSUE_CHECKBOX`)
- **Implement**: PR review (`PR_REVIEW`)

These mechanisms remain separate as per the approved decision in the parent issue.

## Options Considered

### Option A: Single Reusable Workflow with Job Composition

**Approach**: Create `sdlc-work-loop.yml` as a reusable workflow with jobs that compose based on inputs. Each job conditionally executes based on the phase configuration.

```yaml
on:
  workflow_call:
    inputs:
      phase: { type: string }  # refine, plan, implement
      issue_number: { type: number }
      work_prompt_script: { type: string }
      review_prompt_script: { type: string }  # unified or fallback
      checks: { type: string }  # JSON array
      output_type: { type: string }  # draft, pr
      human_review_mechanism: { type: string }  # issue_checkbox, pr_review

jobs:
  work:
    # Agent does work
  run-checks:
    # DAG of checks (merge-fix → lint/test parallel → fixer)
  review:
    # Unified review logic
  respond:
    # Handle feedback, potentially re-dispatch
  human-gate:
    # Issue checkbox approval (refine/plan)
  human-gate-pr:
    # PR-based approval (implement)
```

**Pros**:
- Single source of truth for work loop logic
- Phase behavior driven by inputs
- Easy to add new phases
- Consistent check DAG execution

**Cons**:
- Complex conditional logic in workflow
- All jobs exist in one file (larger file)
- May require careful handling of job dependencies

### Option B: Workflow per Phase Calling Shared Jobs

**Approach**: Keep phase-specific entry workflows but extract common jobs into reusable workflows.

```
sdlc-refine.yml    → calls sdlc-work-job.yml, sdlc-review-job.yml
sdlc-plan.yml      → calls sdlc-work-job.yml, sdlc-review-job.yml
sdlc-implement.yml → calls sdlc-work-job.yml, sdlc-check-job.yml, sdlc-review-job.yml
```

**Pros**:
- Phase-specific workflows are smaller
- Easier to understand per-phase flow
- Can customize per-phase without affecting others

**Cons**:
- Still has some duplication in phase workflows
- More files to maintain
- Nested workflow call depth concerns (4 level limit)
- Doesn't fully achieve the "single work loop" goal

### Option C: Composite Actions Instead of Reusable Workflows

**Approach**: Use composite actions for shared logic, keeping the main workflow structure.

**Pros**:
- Composite actions have no nesting limit
- Can be more granular

**Cons**:
- Composite actions can't define jobs
- Would still need workflow-level job orchestration
- Less visibility in GitHub Actions UI

## Recommended Approach

**Option A: Single Reusable Workflow with Job Composition**

This approach best achieves the goal stated in the parent issue (#436): "a single reusable workflow that accepts phase configuration as inputs." It provides:

1. **Single source of truth**: All work loop logic in one file
2. **Parameterized behavior**: Phase differences driven by inputs
3. **DAG check execution**: Clean implementation of check ordering
4. **Human gate separation**: Distinct jobs for issue checkbox vs PR review

### Work Loop Workflow Structure

The workflow should implement these jobs:

1. **`work`**: Run the agent with parameterized prompt
   - Build prompt using `work_prompt_script` input
   - Run egg action with phase context
   - Output artifacts (draft file or code changes)

2. **`run-checks`**: Execute check DAG
   - Parse `checks` JSON input
   - Run checks in specified order (respecting dependencies)
   - For implement: merge-fix → lint/test parallel → fixer
   - Aggregate results for review

3. **`review`**: Unified review with parameterized criteria
   - Build review prompt (unified builder replaces phase-specific builders)
   - Run reviewer agent
   - Parse verdict (approved/needs_revision)
   - Update contract with review state

4. **`respond`**: Handle review feedback
   - If approved → continue to human gate
   - If needs_revision → re-dispatch work with feedback
   - Track cycle count for circuit breaker

5. **`human-gate`**: Issue checkbox approval (refine/plan)
   - Post analysis/plan to issue with approval checkbox
   - Wait for HITL decision (handled by `sdlc-hitl.yml`)

6. **`human-gate-pr`**: PR-based approval (implement)
   - Create/update PR
   - Mark ready for review
   - Wait for PR approval

### Unified Review Prompt Builder

Create `action/build-unified-review-prompt.sh` that accepts the phase as input and generates appropriate review criteria:

```bash
# Environment variables:
#   EGG_PIPELINE_PHASE — refine, plan, implement
#   EGG_ISSUE_NUMBER   — Issue number
#   REVIEW_CYCLE       — Current cycle
#   PRIOR_FEEDBACK     — Previous feedback (for re-reviews)
```

This consolidates `build-refine-review-prompt.sh` and `build-plan-review-prompt.sh` into a single parameterized script.

### Circuit Breaker Logic

The workflow should implement circuit breaker with configurable thresholds:

```yaml
inputs:
  max_review_cycles: { type: number, default: 3 }
  max_total_cycles: { type: number, default: 10 }
```

When thresholds are exceeded:
1. Open circuit breaker in contract
2. Post escalation comment
3. Add `sdlc:awaiting-approval` label
4. Stop automated processing

## Implementation Considerations

### Input Schema

The work loop should accept:

| Input | Type | Description |
|-------|------|-------------|
| `phase` | string | Pipeline phase (refine/plan/implement) |
| `issue_number` | number | GitHub issue number |
| `work_prompt_script` | string | Script to build work prompt |
| `review_prompt_script` | string | Script to build review prompt (unified) |
| `checks` | string | JSON array of CheckDefinition objects |
| `output_type` | string | `draft` or `pr` |
| `human_review_mechanism` | string | `issue_checkbox` or `pr_review` |
| `max_review_cycles` | number | Cycles before escalation (default: 3) |
| `branch_name` | string | Issue branch name |

### Check DAG Execution

For implement phase checks, the DAG is:

```
merge-conflict-check
       |
       v
 ┌─────┴─────┐
 |           |
lint       test
 |           |
 └─────┬─────┘
       |
       v
   check-fixer
```

This can be implemented with job dependencies:

```yaml
jobs:
  check-merge-conflict:
    # ...
  check-lint:
    needs: check-merge-conflict
  check-test:
    needs: check-merge-conflict
  check-fixer:
    needs: [check-lint, check-test]
```

However, since checks are passed as JSON input, a more flexible approach uses a check runner that respects dependencies within a single job:

```yaml
jobs:
  run-checks:
    steps:
      - name: Run check DAG
        run: |
          python .github/scripts/run-check-dag.py \
            --checks '${{ inputs.checks }}'
```

### Migration Safety

This issue explicitly does NOT modify existing jobs:

> "Create the new `sdlc-work-loop.yml` reusable workflow without removing old jobs yet. This allows parallel testing alongside the existing pipeline."

The migration to the new workflow (replacing old jobs) happens in issue #450.

## Open Questions

The following questions may require human input:

1. **Check execution model**: Should checks run as separate GitHub jobs (better parallelism, more visibility) or as steps within a single job (simpler dependencies, less overhead)?
   - Recommendation: Single job with DAG runner for flexibility, but this could be revisited based on observability needs.

2. **Error handling for unavoidable check failures**: Per the parent issue, "if there are unavoidable failures, work should still be presented to human." What constitutes "unavoidable" vs "needs fixing"?
   - Recommendation: Checks marked `required: false` always pass to review; `required: true` failures block unless all retries exhausted, then escalate.

3. **Unified review prompt builder naming**: Should the new unified builder replace the existing phase-specific builders, or coexist during migration?
   - Recommendation: Create new `build-unified-review-prompt.sh`; existing builders remain until #450 migration.

---

*Authored-by: egg*
