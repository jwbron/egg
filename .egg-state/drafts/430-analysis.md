# Analysis: Set Up a Single Reusable Workflow for All SDLC Workflows

> Issue: #430 | Phase: refine

## Problem Statement

The current SDLC pipeline (`sdlc-pipeline.yml`) implements separate job definitions for each phase (refine, plan, implement) with significant code duplication. Each phase follows the same fundamental loop pattern:

1. Agent does work (producer)
2. Agent reviews work (reviewer)
3. Agent responds to review (redispatch if needed)
4. Cycles until work is complete or circuit breaker triggers
5. Agent posts work for human approval

Despite this shared pattern, the current implementation has:
- **~2,600 lines** in `sdlc-pipeline.yml` with substantial repetition
- Separate jobs for each phase: `refine`, `refine-review`, `refine-redispatch`, `plan`, `plan-review`, `plan-redispatch`, `implement`, etc.
- Phase-specific logic scattered across multiple locations
- Inconsistent handling of review cycles and feedback across phases
- No support for arbitrary DAG-based intermediate checks (e.g., linting, testing, auto-fixers) between work and review

The issue requests a **unified workflow loop** that can be parameterized for different phases, with support for:
- Different agents and context per phase
- Arbitrary DAG of intermediate checks between work and review
- Consistent review cycle handling with both automated agents and async human reviewers
- Same exit requirements across all phases (consensus or max cycles reached)

## Current Behavior

### Phase Structure (As Implemented)

Each phase currently follows this pattern with minor variations:

```
Producer Job (refine/plan/implement)
     ↓
Reviewer Job (refine-review/plan-review/wait-for-checks)
     ↓
[If needs revision] → Redispatch Job → Re-run Producer
     ↓
[If approved] → Post to Issue/PR → Await Human Approval
     ↓
Human Approves → Advance to Next Phase (via sdlc-hitl.yml)
```

**Key Files:**
- `.github/workflows/sdlc-pipeline.yml:1234-1739` — refine phase jobs
- `.github/workflows/sdlc-pipeline.yml:1743-1867` — refine-redispatch job
- `.github/workflows/sdlc-pipeline.yml:1871-2475` — plan phase jobs
- `.github/workflows/sdlc-pipeline.yml:418-782` — implement phase

### Current Pain Points

1. **Code Duplication**: The refine and plan phases are nearly identical in structure (producer → reviewer → redispatch), yet each is implemented as separate jobs with duplicated logic for:
   - Checkout and bot token generation
   - Circuit breaker checks
   - Contract state updates
   - Comment posting and minimizing

2. **No Intermediate Check DAG**: The implement phase runs checks only after the agent completes work. There's no mechanism to run arbitrary checks (linters, tests, auto-fixers) between the "work" step and the "review" step.

3. **Inconsistent Review Handling**:
   - Refine/Plan use auto-review agents writing to JSON files
   - Implement uses PR-based review with `reusable-review.yml`
   - Human review uses checkbox-based approval on issue comments (refine/plan) or PR review (implement)

4. **Tight Coupling**: Phase-specific logic is embedded in job definitions rather than being parameterized, making it difficult to:
   - Add new phases
   - Modify review behavior consistently
   - Test workflow logic in isolation

## Constraints

### Technical Constraints
- **GitHub Actions limitations**: Reusable workflows can call other reusable workflows but with restrictions on nesting depth and input passing
- **Job-level conditions**: Cannot use `workflow_call` inputs directly in job `if:` conditions (requires `resolve-inputs` workaround)
- **Concurrency control**: Must prevent concurrent runs for the same issue
- **Trusted script isolation**: Trusted action scripts must be checked out from `main` before running on feature branches

### Security Constraints
- **Role-based field ownership**: Implementer agents cannot mark their own work complete
- **Gateway enforcement**: Phase permissions must be enforced at infrastructure level
- **Audit trail**: All contract mutations must be logged

### Operational Constraints
- **Backward compatibility**: Existing contracts and branches must remain functional during migration
- **Incremental rollout**: Should be possible to migrate one phase at a time
- **Observability**: Clear logging and status comments for debugging

## Options Considered

### Option A: Parameterized Reusable Workflow with Phase Configuration

**Approach**: Create a single reusable workflow (`sdlc-work-loop.yml`) that accepts phase configuration as inputs. Each phase invocation passes different:
- Agent prompt builder script
- Reviewer prompt builder script
- Contract field mappings (e.g., `refine_review_cycles` vs `plan_review_cycles`)
- Allowed intermediate checks (DAG definition)
- Output artifact paths

```yaml
# sdlc-work-loop.yml
on:
  workflow_call:
    inputs:
      phase_name:
        type: string
        required: true
      producer_prompt_script:
        type: string
        required: true
      reviewer_prompt_script:
        type: string
        required: true
      intermediate_checks:
        type: string  # JSON array of check definitions
        required: false
      max_cycles:
        type: number
        default: 3
```

The main `sdlc-pipeline.yml` becomes a thin orchestrator calling the work loop with phase-specific parameters.

**Pros**:
- Maximum code reuse — one implementation for all phases
- Easy to add new phases by adding configuration
- Consistent behavior guaranteed across phases
- Clear separation between orchestration (main workflow) and execution (work loop)
- Intermediate check DAG can be defined per-phase

**Cons**:
- GitHub Actions limitations on reusable workflow inputs may require complex serialization
- Debugging is harder with abstraction layers
- All phases must conform to the same interface (may limit flexibility)

### Option B: Composite Actions with Phase-Specific Wrappers

**Approach**: Extract common logic into composite actions, keeping phase-specific jobs in `sdlc-pipeline.yml` but dramatically reducing duplication.

```
composite-actions/
├── run-producer/
│   └── action.yml       # Runs producer agent
├── run-reviewer/
│   └── action.yml       # Runs reviewer agent
├── handle-review-result/
│   └── action.yml       # Updates contract, posts comments
└── run-intermediate-checks/
    └── action.yml       # Runs DAG of checks
```

Each phase job becomes a thin wrapper calling these actions with phase-specific inputs.

**Pros**:
- More flexibility — phases can diverge when needed
- Easier debugging — logic is in composite actions, not reusable workflows
- Incremental migration — can extract actions one at a time
- No complex serialization of inputs

**Cons**:
- Still some duplication in job definitions (checkout, token generation)
- Composite actions have limited control flow capabilities
- Doesn't solve the fundamental structural issue of repeated job patterns

### Option C: State Machine with Single Loop Job

**Approach**: Replace the multi-job structure with a single "loop runner" job that uses a state machine to drive phase transitions. Contract state determines next action.

```yaml
jobs:
  loop:
    runs-on: ubuntu-latest
    steps:
      - name: Load state
      - name: Determine action based on state
      - name: Execute action (producer/reviewer/check)
      - name: Update state
      - name: Re-dispatch if not terminal
```

State machine transitions:
```
INIT → PRODUCER → INTERMEDIATE_CHECKS → REVIEWER →
  [if approved] → HUMAN_REVIEW → NEXT_PHASE
  [if rejected] → PRODUCER (cycle)
  [if max_cycles] → ESCALATION → HUMAN_REVIEW
```

**Pros**:
- Maximum simplicity — single job handles all phases
- State machine is explicit and testable
- Easy to add intermediate check nodes to the DAG
- Natural support for async human review (workflow pauses, resumes on event)

**Cons**:
- Major architectural change — harder to migrate incrementally
- Long-running jobs may hit timeout limits (current implement phase is 6 hours)
- GitHub Actions billing per-minute may increase costs for waiting jobs
- Loses parallelism benefits of separate jobs

### Option D: Hybrid — Reusable Workflow with State-Driven Control

**Approach**: Combine Option A and C. Create a reusable workflow that implements the work loop, but use contract state to drive behavior. The workflow reads phase configuration from a new `phases` section in the contract schema.

```json
// Contract schema addition
{
  "phase_config": {
    "refine": {
      "producer_prompt": "build-refine-prompt.sh",
      "reviewer_prompt": "build-refine-review-prompt.sh",
      "intermediate_checks": [],
      "max_cycles": 3
    },
    "plan": { ... },
    "implement": {
      "producer_prompt": "build-implement-prompt.sh",
      "reviewer_prompt": null,  // Uses PR review
      "intermediate_checks": ["lint", "test", "autofix"],
      "max_cycles": 5
    }
  }
}
```

The reusable workflow reads configuration from the contract and executes the appropriate loop.

**Pros**:
- Configuration is versioned with the contract (auditable)
- Supports per-issue customization if needed
- Clean separation of workflow logic and phase configuration
- Intermediate checks are first-class citizens

**Cons**:
- Contract schema becomes more complex
- Configuration changes require contract migration
- May be over-engineered for current needs

## Recommended Approach

**Option A: Parameterized Reusable Workflow with Phase Configuration**

This option provides the best balance of:
1. **Code reduction** — Eliminates ~60% of duplicate workflow code
2. **Consistency** — All phases use the same loop implementation
3. **Extensibility** — New phases can be added with configuration alone
4. **Intermediate checks** — DAG support is naturally parameterized per-phase
5. **Incremental migration** — Can migrate one phase at a time while keeping old jobs

### High-Level Implementation Plan

1. **Create `sdlc-work-loop.yml`** — The core reusable workflow implementing:
   - Producer agent invocation
   - Intermediate check DAG execution
   - Reviewer agent invocation (or PR review coordination)
   - Review result handling (approve/reject/escalate)
   - Human review checkpoint posting

2. **Define phase configuration schema** — JSON structure for each phase specifying:
   - Prompt builder scripts
   - Intermediate checks (as workflow job references or script paths)
   - Review cycle limits
   - Contract field mappings
   - Human review mechanism (issue comment vs PR)

3. **Refactor `sdlc-pipeline.yml`** — Replace phase jobs with calls to work loop:
   ```yaml
   refine-loop:
     uses: ./.github/workflows/sdlc-work-loop.yml
     with:
       phase: refine
       # ... phase configuration
   ```

4. **Implement intermediate check DAG** — Support for running arbitrary checks between producer and reviewer:
   - Linters (run, capture failures)
   - Tests (run, capture failures)
   - Auto-fixers (run, commit fixes, retry producer if fixed)
   - Custom checks (defined per-repository)

5. **Unify human review handling** — Create consistent async review pattern:
   - Post work artifact with review request
   - Pause workflow (workflow completes, resumes on event)
   - Resume via `sdlc-hitl.yml` when human approves/provides feedback
   - Same mechanism for both issue comments and PR reviews

### Open Questions

**Intermediate Check DAG Definition:**

```
egg-contract add-decision --question "How should intermediate checks (linters, tests, auto-fixers) be defined?" \
  --options \
  "Workflow matrix" \
  "JSON configuration file" \
  "Contract schema extension" \
  --format markdown
```

<!-- egg-hitl-decision id=decision-1 -->
**How should intermediate checks (linters, tests, auto-fixers) be defined?**

- [ ] **Workflow matrix** — Define checks as a job matrix in the reusable workflow, with inputs specifying which checks to run. Simple but less flexible.
- [ ] **JSON configuration file** — Create `.egg/check-config.json` defining check commands and dependencies. More flexible, repository-customizable.
- [ ] **Contract schema extension** — Add check definitions to the contract schema. Fully auditable but increases contract complexity.
- [ ] Other (explain in reply)

---

**Human Review Unification:**

The current system uses different mechanisms for human review:
- **Refine/Plan**: Checkbox on issue comment, handled by `sdlc-hitl.yml`
- **Implement/PR**: PR review via GitHub's native review system

```
egg-contract add-decision --question "Should human review be unified across all phases?" \
  --options \
  "Unified (issue comments for all)" \
  "Unified (PR-based for all)" \
  "Keep separate mechanisms" \
  --format markdown
```

<!-- egg-hitl-decision id=decision-2 -->
**Should human review be unified across all phases?**

- [ ] **Unified (issue comments for all)** — All phases use checkbox-based approval on issue comments. Simpler but loses PR review features for code changes.
- [ ] **Unified (PR-based for all)** — Create PRs for all phases, use PR reviews for approval. Richer review features but heavier for non-code phases.
- [ ] **Keep separate mechanisms** — Refine/Plan use issue comments, Implement uses PR review. Current behavior, but documents must handle both patterns.
- [ ] Other (explain in reply)

---

**Migration Strategy:**

```
egg-contract add-decision --question "How should we migrate existing pipelines to the new workflow?" \
  --options \
  "Big-bang migration" \
  "Phase-by-phase migration" \
  "Feature flag with gradual rollout" \
  --format markdown
```

<!-- egg-hitl-decision id=decision-3 -->
**How should we migrate existing pipelines to the new workflow?**

- [ ] **Big-bang migration** — Replace all phase implementations at once. Faster but higher risk, requires comprehensive testing.
- [ ] **Phase-by-phase migration** — Migrate refine first, then plan, then implement. Lower risk, allows learning from each migration.
- [ ] **Feature flag with gradual rollout** — Add input flag to choose old vs new workflow. Safest but most complex, doubles maintenance temporarily.
- [ ] Other (explain in reply)

---

## Implementation Phases (Preliminary)

If Option A is approved, implementation would proceed in phases:

1. **Phase 1: Core Work Loop** (~1 week)
   - Create `sdlc-work-loop.yml` with producer/reviewer cycle
   - Migrate `refine` phase as proof of concept
   - Validate contract state handling

2. **Phase 2: Intermediate Checks** (~1 week)
   - Add DAG execution capability to work loop
   - Define check configuration schema
   - Implement standard checks (lint, test)

3. **Phase 3: Full Migration** (~1 week)
   - Migrate `plan` phase
   - Migrate `implement` phase (most complex due to PR creation)
   - Update documentation

4. **Phase 4: Cleanup** (~3 days)
   - Remove deprecated job definitions
   - Update ADR documentation
   - Performance testing

---

*Authored-by: egg*
