# Analysis: Integrate specialized reviewers into work loop review cycle

> Issue: #464 | Phase: refine

## Problem Statement

The unified work loop (`sdlc-work-loop.yml`) uses a single internal reviewer (`build-unified-review-prompt.sh`) during its review step. Three specialized PR reviewers (code reviewer, agent mode design reviewer, contract verification reviewer) only trigger via `on: pull_request` events — running *after* the work loop completes and a PR is created.

**Current state:** Specialized reviewer feedback only surfaces after the PR is created, outside the iterative work/review/respond cycle.

**Desired outcome:** Specialized reviewers run as part of the work loop's review step, enabling their feedback to drive revision cycles *before* human review.

## Current Behavior

### Work Loop Review Architecture

The work loop (`sdlc-work-loop.yml:602-777`) has a single review job:

1. **Build review prompt** (line 696): Runs `build-unified-review-prompt.sh` from trusted main branch
2. **Run reviewer agent** (lines 705-721): Invokes egg action with `EGG_AGENT_ROLE: reviewer`
3. **Parse verdict** (lines 723-776): Reads JSON verdict from `.egg-state/reviews/{ISSUE}-{PHASE}-review.json`
4. **Route** (respond job): If `needs_revision`, redispatch work loop; if `approved`, proceed to human gate

**Key limitation:** The `review_prompt_script` input (line 45-49) accepts a single script path — no mechanism exists for running multiple reviewers.

### Specialized Reviewers

Three PR-triggered workflows exist:

| Reviewer | Workflow | Prompt Script | PR Dependency |
|----------|----------|---------------|---------------|
| Code reviewer | `on-pull-request.yml` | `build-review-prompt.sh` | Uses `PR_NUMBER`, `gh pr diff`, posts via `gh pr review` |
| Agent mode design | `on-pull-request-agent-mode-design.yml` | `build-agent-mode-design-review-prompt.sh` | Uses `PR_NUMBER`, `gh pr diff`, posts via `gh pr review` |
| Contract verification | `on-pull-request-contract-verify.yml` | `build-contract-verification-prompt.sh` | Uses `PR_NUMBER`, `egg-contract` CLI, posts via `gh pr review` |

**All three require `PR_NUMBER`** — they cannot run in refine/plan phases where no PR exists.

### Verdict Output Mechanisms

| Context | Verdict Format | Output Location |
|---------|----------------|-----------------|
| Work loop unified reviewer | JSON `{verdict, summary, feedback}` | `.egg-state/reviews/{ISSUE}-{PHASE}-review.json` |
| PR reviewers | HTML marker `<!-- egg-automated-review bot=X commit=Y verdict=Z -->` | PR review body (via `gh pr review`) |

## Constraints

### Technical Constraints

1. **PR dependency**: Two of three specialized reviewers (`build-review-prompt.sh`, `build-agent-mode-design-review-prompt.sh`) use `gh pr diff` and `gh pr review` — these require a PR to exist
2. **Environment variables**: Specialized scripts expect `PR_NUMBER`, work loop uses `EGG_ISSUE_NUMBER` + `EGG_PIPELINE_PHASE`
3. **Verdict aggregation**: Work loop expects a single verdict; multiple reviewers need aggregation logic
4. **Parallel vs sequential**: Running reviewers sequentially is simpler but slower; parallel execution requires job coordination
5. **Contract verification role**: `build-contract-verification-prompt.sh` expects `EGG_AGENT_ROLE: reviewer` for CLI access — already compatible with work loop

### Scope Constraints

1. **Implement phase only**: Code reviewer and contract verifier are only meaningful when code exists (implement phase)
2. **Agent mode design**: Applies to refine/plan phases (reviewing analysis/plan artifacts) *and* implement phase (reviewing code changes)
3. **Re-review support**: All reviewers support `LAST_REVIEW_COMMIT` for incremental review — this maps to work loop's `PRIOR_FEEDBACK` mechanism

### Compatibility Constraints

1. **Trusted main checkout**: Prompt scripts must run from trusted main branch (security requirement in work loop)
2. **Existing PR workflows**: PR-triggered workflows should continue to function for manual dispatch and post-merge validation
3. **Circuit breaker**: Aggregate verdict must feed into existing circuit breaker logic (any `needs_revision` should count as revision needed)

## Options Considered

### Option A: Unified Multi-Reviewer Prompt Script

**Approach**: Create a new `build-multi-review-prompt.sh` that generates a single prompt containing review criteria from all applicable reviewers. One agent runs with combined instructions.

**Pros**:
- Minimal workflow changes — single reviewer agent as before
- No job coordination complexity
- Single verdict output, no aggregation needed
- Lower API costs (one agent invocation)

**Cons**:
- Extremely long prompts may reduce review quality
- Conflicting review criteria could confuse the agent
- Loses specialization — one agent doing three jobs may do each less well
- Harder to attribute feedback to specific review types
- All-or-nothing: can't selectively enable/disable reviewers

### Option B: Sequential Multi-Reviewer Jobs

**Approach**: Add multiple review jobs (`review-unified`, `review-agent-design`, `review-code`, `review-contract`) that run sequentially, each with its own prompt script. Add an aggregation job that combines verdicts.

**Pros**:
- Clear separation of concerns
- Each reviewer can specialize and excel at its domain
- Easy to enable/disable specific reviewers per phase
- Straightforward verdict aggregation (any `needs_revision` → aggregate `needs_revision`)

**Cons**:
- Sequential execution is slow (3-4x review time)
- Workflow complexity increases significantly
- More job coordination and state passing
- Higher API costs (multiple agent invocations)

### Option C: Parallel Multi-Reviewer Jobs with Aggregation

**Approach**: Run multiple review jobs in parallel (using job matrices or explicit parallel jobs), each producing a JSON verdict. Add an aggregation job that waits for all and combines results.

**Pros**:
- Fast — all reviewers run concurrently
- Each reviewer specializes
- Selective enabling per phase
- Clear separation of feedback by reviewer type

**Cons**:
- Complex job coordination (parallel jobs with shared needs)
- Git push conflicts if multiple reviewers commit to same branch
- Aggregation logic must handle partial failures gracefully
- Higher API costs (multiple parallel invocations)
- Workflow becomes significantly more complex

### Option D: Reviewer Dispatch via Composite Action

**Approach**: Create a composite action (`run-reviewer`) that accepts a list of reviewer scripts. The action internally loops through reviewers, runs each, collects verdicts, and outputs an aggregated result. The work loop calls this single action.

**Pros**:
- Work loop stays simple (single review step calling composite action)
- Reviewer logic encapsulated in reusable action
- Can run sequentially or with internal parallelization
- Easy to test and iterate on reviewer dispatch logic
- Clean interface: input `reviewers: ["unified", "agent-design"]`, output `verdict: approved|needs_revision`

**Cons**:
- New action to maintain
- Still sequential unless composite action spawns parallel processes
- Action timeout must accommodate all reviewers
- Debugging spread across action and workflow

### Option E: Adapt Specialized Scripts for Non-PR Context

**Approach**: Modify the three specialized prompt scripts to work without `PR_NUMBER`. For refine/plan phases, use `git diff origin/main..HEAD` instead of `gh pr diff`. For implement phase, continue using PR context. Add phase-aware logic to each script.

Combined with Option B or C, this enables specialized reviewers in all phases.

**Pros**:
- Enables agent mode design review for refine/plan phases
- Scripts become phase-agnostic
- Maximizes review coverage across all phases

**Cons**:
- Each script needs modification
- Must handle missing PR context gracefully
- Verdict output mechanism differs (JSON file vs PR review post)
- Testing complexity increases

## Recommended Approach

**Option C (Parallel Multi-Reviewer Jobs with Aggregation)** combined with **Option E (Adapt Scripts for Non-PR Context)**.

### Rationale

1. **Speed is critical**: The work loop is already multi-step. Sequential reviewers would make iteration painfully slow. Parallel execution keeps review time constant regardless of reviewer count.

2. **Specialization improves quality**: A dedicated agent mode design reviewer applying focused criteria will catch design issues better than a unified prompt trying to cover everything.

3. **Phase-appropriate review**: By adapting scripts for non-PR contexts:
   - **Refine phase**: Agent mode design reviewer validates analysis follows design principles
   - **Plan phase**: Agent mode design reviewer validates plan follows design principles
   - **Implement phase**: All three reviewers (code, agent mode design, contract) run in parallel

4. **Clean aggregation semantics**: Any `needs_revision` from any reviewer triggers redispatch. Feedback from all reviewers is concatenated for the implementer.

5. **Git conflict mitigation**: Each reviewer writes to a distinct file (`.egg-state/reviews/{ISSUE}-{PHASE}-{REVIEWER}.json`). Aggregation job reads all files — no commit conflicts.

### High-Level Implementation

```
review-unified:     writes → {ISSUE}-{PHASE}-unified.json
review-agent-design: writes → {ISSUE}-{PHASE}-agent-design.json
review-code:        writes → {ISSUE}-{PHASE}-code.json (implement only)
review-contract:    writes → {ISSUE}-{PHASE}-contract.json (implement only)
    ↓ (all complete)
aggregate-reviews:
    reads all JSON files
    outputs: verdict=approved|needs_revision
    outputs: feedback="Unified:\n...\nAgent Design:\n...\nCode:\n..."
    ↓
respond:
    uses aggregated verdict for routing
```

### Phase Matrix

| Phase | Reviewers to Run |
|-------|------------------|
| refine | unified, agent-design |
| plan | unified, agent-design |
| implement | unified, agent-design, code, contract |

## Open Questions

### Multiple-Choice Decision Required

The following decision requires human input before implementation:

---

**HITL Decision: reviewer-orchestration**

Which reviewer orchestration approach should we implement?

- [ ] **Option C+E: Parallel jobs with script adaptation** — Parallel reviewer jobs, adapt scripts for non-PR context. Fastest, most comprehensive, but highest complexity.
- [ ] **Option B+E: Sequential jobs with script adaptation** — Sequential reviewer jobs, adapt scripts for non-PR context. Simpler but slower.
- [ ] **Option D: Composite action** — Encapsulate reviewer dispatch in a reusable action. Moderate complexity, good encapsulation.
- [ ] **Option A: Unified multi-reviewer prompt** — Single prompt with all review criteria. Simplest but may reduce review quality.
- [ ] Other (explain in reply)

---

### Open-Ended Questions

1. **Review timeout allocation**: With multiple parallel reviewers, should we increase the overall review timeout, or should each reviewer have its own timeout? Current default is 15 minutes for a single reviewer.

2. **Selective reviewer configuration**: Should the work loop accept a list of reviewers to run (e.g., `reviewers: ["unified", "agent-design"]`), or should it use a fixed phase-based matrix as proposed?

3. **Failure handling**: If one reviewer fails (crashes, times out) but others succeed, should the aggregate verdict:
   - Treat the failed reviewer as "approved" (lenient)
   - Treat the failed reviewer as "needs_revision" (strict)
   - Skip aggregation and escalate to human (conservative)

---

*Authored-by: egg*
