# Analysis: Support parallel phase-level dispatch for implement phase

> Issue: #732 | Phase: refine

## Problem Statement

The SDLC pipeline currently supports two dispatch modes: low-complexity short-circuit (Tier 1, via PR #734) and mid-complexity sequential multi-agent waves (Tier 2, the default). There is no support for high-complexity tasks where multiple independent implementation phases could run in parallel, each with its own coder-tester-review cycle.

Large features decompose into multiple plan phases with a dependency graph, but today all phases execute in a single implement pass with one coder processing all tasks sequentially. This means:

- **Serial bottleneck**: A 4-phase feature runs all tasks through a single coder, even when phases are independent.
- **Late integration failures**: Issues between phases are only discovered after all work is complete.
- **Unbounded reviewer scope**: Reviewers see the full feature diff rather than phase-scoped changes.
- **No early abort**: If the first phase reveals a design flaw, the pipeline still burns tokens on later phases before discovering it.

The desired outcome is a Tier 3 dispatch mode where independent plan phases run as parallel implement cycles (coder → tester → agentic review), dependent phases run sequentially, and an integrator merges and validates the combined result before human review.

## Current Behavior

### Complexity assessment (Tier 1 / Tier 2)

The refine agent assesses complexity and optionally signals `short_circuit: true` via a YAML metadata block at the end of the analysis document. The orchestrator detects this in `_check_short_circuit_signal()` (`orchestrator/routes/pipelines.py:1128-1160`) and skips the plan phase.

Currently, complexity is binary: either `low` (short-circuit) or not (full pipeline). The `complexity` field in the metadata is informational only — the orchestrator checks `short_circuit: true/false`, not the complexity value. There is no `high` tier that triggers different dispatch behavior.

### Multi-agent orchestration (Tier 2)

The implement phase runs agents in wave-based execution:

- **Wave 1**: CODER (no dependencies)
- **Wave 2**: TESTER + DOCUMENTER (parallel, both depend on CODER)
- **Wave 3**: INTEGRATOR (depends on CODER + TESTER)

This is orchestrated by `MultiAgentExecutor.execute_all_waves()` (`orchestrator/multi_agent.py`), which iterates: get next wave → spawn agents → wait → repeat.

Key architectural facts:

1. **Dependency graph is role-based, not phase-based**: `DependencyGraph` nodes are `AgentRole` enum values. Waves group roles that can run in parallel. There is no concept of "CODER for Phase 1" vs "CODER for Phase 2" — there is only one CODER execution slot (`shared/egg_contracts/dependency_graph.py`).

2. **Execution state is role-keyed**: `OrchestrationState.executions` is `dict[AgentRole, AgentExecutionModel]`. Running the same role twice would overwrite state (`shared/egg_contracts/orchestration.py`).

3. **All agents work on a single branch**: Each pipeline has one worktree and one branch (`egg/issue-NNN`). No sub-branch isolation exists.

4. **Plan phases are parsed but not dispatched independently**: `plan_parser.py` extracts `ParsedPhase` objects including a `dependencies` field, but this field is **not preserved in the contract schema** — the contract `Phase` model has no `dependencies` or `exit_criteria` field (`contract.schema.json:206-267`).

5. **Gateway branch rules**: The gateway validates branch ownership via `egg-` or `egg/` prefix or open PR association (`gateway/policy.py`). No per-phase branch concept exists.

6. **Integrator is read-only**: The current integrator can only write to `.egg-state/agent-outputs/`. It cannot modify source, tests, or docs.

### Plan phase structure

The plan template (`docs/templates/plan.md`) produces phases with tasks:

```yaml
phases:
  - id: 1
    name: "Core Library"
    tasks:
      - id: TASK-1-1
        description: "..."
        files: [...]
  - id: 2
    name: "Integration"
    dependencies: "phase-1"
    tasks: [...]
```

The YAML `dependencies` field is parsed by `plan_parser.py` into `ParsedPhase.dependencies` but is **discarded** when tasks are populated into the contract.

## Constraints

### Technical constraints

- **Execution state keying**: The `AgentExecutionModel` is keyed by `AgentRole`. Running multiple CODER instances requires a composite key (`phase_id + role`) throughout the orchestration stack: `OrchestrationState`, `DependencyGraph`, `Orchestrator`, `MultiAgentExecutor`, and `PipelineDispatcher`.
- **Contract schema migration**: Adding `dependencies` to the `Phase` model and `phase_id` to `AgentExecutionModel` requires a schema change with backward compatibility for existing pipelines.
- **Gateway branch policy**: Sub-branch support (`egg/<feature>/phase-N`) requires gateway policy changes to allow pushes to nested branches owned by the same pipeline.
- **Worktree management**: Each parallel phase needs its own working directory to avoid file conflicts. The gateway currently manages worktrees — adding per-phase worktrees introduces lifecycle complexity.
- **Agent prompt isolation**: Each phase's coder must receive only its phase's tasks and file boundaries, not the full plan.
- **Handoff data scoping**: `collect_handoff_data()` currently reads all agent outputs. In Tier 3, handoffs must be scoped to the current phase.

### Operational constraints

- **Token cost**: Tier 3 is ~2-2.5x more expensive than Tier 2 (15 agents vs 6 for a 3-phase feature). The tier selection must be deliberate.
- **Complexity**: This is the most significant architectural change to the orchestration system since multi-agent support was added.

### Dependencies

- **PR #734 (short-circuit)**: Already merged. Tier 3 builds on the same complexity assessment mechanism.
- **Plan parser dependency field**: Already parsed but not stored — needs contract schema extension.
- **Gateway sidecar**: Must be updated to support sub-branches and per-phase worktrees.

## Options Considered

### Option A: Phase-level orchestration with sub-branches

**Approach**: The orchestrator treats each plan phase as an independent implement cycle. Independent phases run in parallel, each with its own coder → tester → agentic review loop on a sub-branch (`egg/<feature>/phase-N`). After all phases complete, an integrator merges sub-branches, runs the full test suite, and fixes integration issues.

This is the approach described in the issue.

**Pros**:
- True parallelism with branch-level isolation prevents merge conflicts during implementation
- Each agentic review covers a bounded diff (one phase), improving review quality
- Early abort: if Phase 1's review fails, later phases can be stopped
- Natural fit with the plan's existing phase decomposition
- Integrator has clear responsibility: merge + validate + fix

**Cons**:
- Requires sub-branch support in the gateway (new branch naming convention and ownership rules)
- Requires per-phase worktree management (new lifecycle in gateway sidecar)
- Integrator needs write access (privilege escalation from current read-only)
- Composite key (`phase_id + role`) is a pervasive change across the entire orchestration stack
- Merge conflicts between sub-branches are possible if `files_affected` boundaries are imprecise
- Highest implementation complexity of all options

### Option B: Sequential phase cycling on a single branch

**Approach**: Instead of parallel execution, the orchestrator runs implement cycles sequentially — one per plan phase — on the same branch. Each cycle runs coder → tester → agentic review for that phase's tasks. No sub-branches or gateway changes needed.

**Pros**:
- No gateway, worktree, or branch policy changes needed
- No merge conflicts between phases (sequential execution)
- Reuses existing single-branch model
- Simpler composite key: still need `(phase_id, role)` tracking but no concurrent state management
- Simpler integrator role: validates at the end rather than merging branches

**Cons**:
- No parallelism — loses the key benefit for independent phases
- Still requires composite execution tracking (`phase_id + role`)
- Higher total latency for multi-phase tasks (serial execution)
- Still need prompt isolation per phase
- Does not address the "unbounded reviewer scope" problem as effectively (reviews are per-phase but execution is serial)

### Option C: Hybrid — sequential phases with optional parallelism

**Approach**: Default to sequential phase cycling (Option B) but allow parallel execution of independent phases when explicitly opted in via pipeline configuration. Parallel phases use sub-branches; sequential phases share the main branch. The parallelism infrastructure is built but gated behind a feature flag.

**Pros**:
- Incremental delivery: ship sequential cycling first, add parallelism later
- Reduces risk by separating the orchestration changes (phase cycling) from the infrastructure changes (sub-branches, gateway)
- Feature flag allows gradual rollout and easy rollback
- Sequential cycling alone provides per-phase agentic review and early abort
- Parallel dispatch can be validated independently once the foundation is in place

**Cons**:
- Two code paths to maintain (sequential + parallel)
- Delayed delivery of full Tier 3 parallelism
- Sequential-first may be seen as incomplete
- Still requires the same composite key changes as Option A

### Option D: Task-level parallelism (alternative decomposition)

**Approach**: Instead of phase-level dispatch, parallelize at the task level. Each independent task gets its own coder agent, working on a dedicated sub-branch. No phase-level cycling; the dependency graph operates on `(task_id, role)` tuples.

**Pros**:
- Finer-grained parallelism (task-level vs phase-level)
- No need for the plan to define phase dependencies — task dependencies suffice
- Simpler per-unit scope (one task = one coder)

**Cons**:
- **Rejected in the issue** for good reasons: the plan's phase decomposition is already the right abstraction
- Reviewer scope becomes fragmented (reviewing 10 single-task diffs is worse than 3 phase diffs)
- More concurrent agents = more token cost with less coherent review
- Task-level isolation is harder to enforce (tasks within a phase often share files)
- `files_affected` overlap between tasks would cause frequent merge conflicts

## Recommended Approach

**Option C: Hybrid — sequential phases with optional parallelism.**

Rationale:

1. **Incremental delivery reduces risk.** The orchestration changes (phase cycling, composite execution tracking, per-phase agentic review) are the architectural foundation. Sub-branch parallelism is an optimization on top. Delivering them separately allows each to be validated independently.

2. **Sequential phase cycling provides most of the value.** Per-phase agentic review, early abort, bounded reviewer scope, and prompt isolation all work with sequential cycling. Parallelism primarily saves wall-clock time.

3. **Gateway and worktree changes are independently scoped.** Sub-branch support in the gateway, per-phase worktree management, and the integrator's merge role are infrastructure concerns that can be built and tested in isolation.

4. **The issue's acceptance criteria are fully met.** All 9 acceptance criteria can be satisfied: Tier 3 distinguishes from Tier 1/2, implement cycles run per phase, each cycle has coder → tester → agentic review with retry, integrator merges and fixes, and execution tracking is `(phase_id, role)` scoped. The only difference is that parallelism is opt-in rather than default.

The implementation would proceed in two logical stages:
- **Stage 1**: Three-tier complexity assessment, sequential phase cycling, composite execution tracking, per-phase agentic review, integrator with write access
- **Stage 2**: Sub-branch isolation, gateway support for nested branches, parallel phase dispatch, per-phase worktrees

## Open Questions

### Q1: Integrator write access scope

The issue specifies the integrator should gain write access to source, tests, and docs for merging sub-branches and fixing integration issues. This is a significant privilege escalation from the current read-only role.

Should the integrator's write access be:
- **(a)** Unrestricted within `src/`, `tests/`, `docs/` (full write, same as coder + tester + documenter combined)
- **(b)** Scoped to files modified by the phase coders (only files in `changed_files` from handoff data)
- **(c)** Unrestricted, but with a separate agentic review of the integrator's changes before human review

### Q2: Phase dependency preservation

The plan parser already extracts `ParsedPhase.dependencies` from the YAML plan, but discards it when populating the contract. To enable phase-level dispatch, this field must be preserved.

Should phase dependencies be:
- **(a)** Stored in the contract `Phase` model as a `dependencies: list[str]` field (schema migration)
- **(b)** Stored in a separate `phase_graph` field on the contract (avoids modifying the Phase model)
- **(c)** Computed dynamically from `files_affected` overlaps between phases (no explicit declaration needed)

### Q3: Tier selection authority

Currently the refine agent decides complexity unilaterally (the human can override during HITL review). For Tier 3, which involves ~2.5x token cost:

Should Tier 3 selection:
- **(a)** Follow the same model as Tier 1: refine agent signals, human can override during HITL
- **(b)** Always require explicit human approval before Tier 3 dispatch begins
- **(c)** Be auto-selected based on the number of plan phases (e.g., >= 3 independent phases → Tier 3)

### Q4: Sequential-first vs parallel-first delivery

The recommended approach (Option C) proposes sequential phase cycling first, parallelism second. Does this sequencing align with priorities, or should full Tier 3 parallelism be delivered in a single pass?

---

*Authored-by: egg*
