# Analysis: Remove per-phase/task breakdown delegation from implement phase

> Issue: #1165 | Phase: refine

## Problem Statement

The implement phase has accumulated multiple conditional execution paths: Tier 3 phase-level dispatch, multi-agent wave execution, single-agent fallback, short-circuit mode, and complexity tiers. The decision tree in `orchestrator/routes/pipelines.py` (~line 6468) selects between these paths based on `coordinator_enabled`, `multi_agent`, `complexity_tier`, and `concurrent_execution` flags.

This complexity is not justified by the results. The branching logic is hard to reason about, test, and maintain. Different execution paths produce inconsistent behavior.

The desired outcome: collapse all execution paths so that **every phase always runs concurrent BRC execution** — no conditional dispatch, no fallbacks. Also remove the integrator role, short-circuit mode, complexity tiers, and all associated plumbing.

## Current Behavior

The implement phase dispatch works as follows (pipelines.py:6468-6883):

1. **Coordinator mode** (lines 6493-6678): Spawns `CoordinatorExecutor` — **already removed by PR #1169** (issue #1164). Some artifacts may linger (e.g., `coordinator_executor.py` still exists on disk, `sandbox/.claude/rules/coordinator.md` still present).
2. **Concurrent BRC** (lines 6680-6723): Calls `_run_concurrent_phase()` — spawns all agents simultaneously with consensus polling. **This is the keeper.**
3. **Tier 3 phase-level dispatch** (lines 6725-6766): Calls `_run_tier3_implement()` (lines 3667-4385) — for HIGH complexity, iterates plan phases in dependency order, spawning CODER → TESTER → DOCUMENTER → CHECKER → REVIEWER_CODE per phase, with an integrator pass at the end.
4. **Multi-agent wave execution** (lines 6768-6813): Calls `_run_multi_agent_phase()` (lines 4386-4579) — wave-based execution using `MultiAgentExecutor`.
5. **Single-agent fallback** (lines 6815-6882): Spawns a lone CODER agent.

Supporting infrastructure:
- **Short-circuit detection** (`_check_short_circuit_signal`, lines 1663-1695): Reads `short_circuit: true` from refine draft YAML to skip the plan phase.
- **Complexity tier detection** (`_check_high_complexity_signal`, lines 1698-1753): Reads `complexity_tier` and `parallel_phases` from refine draft YAML.
- **PhaseDependencyGraph** (`shared/egg_contracts/dependency_graph.py:382-539`): Models plan phase dependencies for Tier 3 wave computation.
- **PipelineDispatcher** (`orchestrator/dispatch.py`): Bridges orchestrator with egg_contracts dispatch logic for wave coordination. Used by `signals.py` for completion/progress recording.
- **MultiAgentExecutor** (`orchestrator/multi_agent.py`): Wave-based parallel agent management. Also hosts `is_concurrent_execution()` (lines 653-673).

## Constraints

- **Dependency on #1164**: PR #1169 (Remove coordinator agent) is merged, but some coordinator artifacts remain on disk (e.g., `coordinator_executor.py`, `sandbox/.claude/rules/coordinator.md`). This issue should clean up any stragglers or assume #1169 handled them.
- **No backward compatibility**: The issue explicitly states old `.egg-state/pipelines/*.json` files with removed fields will fail validation intentionally. Clean break.
- **signals.py refactoring required first**: `orchestrator/routes/signals.py` imports `create_dispatcher` and `map_agent_role_to_contract_role` from `dispatch.py` (lines 339-345, 489-494). These must be inlined using direct `egg_contracts.Orchestrator` calls before `dispatch.py` can be deleted.
- **`is_concurrent_execution()` relocation**: Must move from `multi_agent.py` to `concurrent_executor.py` before deleting `multi_agent.py`.
- **`max_parallel_agents` replacement**: Reviewer spawning (~line 7096) uses `min(len(reviewer_roles), max_parallel_agents)` — after removal, use `len(reviewer_roles)` directly as `max_workers`.
- **Open PR #1171**: "docs: add integrator to concurrent implement phase roles" is currently open. This PR adds integrator references that #1165 will remove. It should either be closed/superseded or merged before this work begins.
- **Scope**: ~5000-6000 lines removed across ~25-30 files. 9 test files deleted entirely, 6 test files updated.

## Options Considered

### Option A: Single large PR

**Approach**: Make all removals in one PR following the migration order in the issue.

**Pros**:
- Atomic change — no intermediate broken states
- Easier to review as a coherent whole
- Matches the "clean break" philosophy

**Cons**:
- Very large PR (~5000-6000 lines) is harder to review
- Higher risk of merge conflicts if other work lands concurrently
- Rollback is all-or-nothing

### Option B: Ordered sequence of smaller PRs

**Approach**: Split into 3-4 PRs following the dependency order: (1) signals.py refactor + relocate `is_concurrent_execution`, (2) remove execution paths + decision tree collapse, (3) remove integrator/short-circuit/complexity-tier/multi-agent-config, (4) docs + test cleanup.

**Pros**:
- Each PR is reviewable in isolation
- Lower risk per merge
- Can validate incrementally (tests pass at each step)

**Cons**:
- Intermediate states may have dead code temporarily
- More coordination overhead
- Slower to complete

### Option C: Two PRs — prep + removal

**Approach**: (1) Prep PR: relocate `is_concurrent_execution`, refactor `signals.py` to inline dispatcher calls. (2) Main PR: all removals, collapse decision tree, delete files, update docs.

**Pros**:
- Prep PR is small and safe — just refactoring, no behavior change
- Main PR is the actual removal — large but straightforward deletions
- Two review cycles, not four

**Cons**:
- Main PR is still very large

## Recommended Approach

**Option C: Two PRs** — this balances reviewability with execution speed. The prep PR unblocks the main removal by eliminating the `dispatch.py` and `multi_agent.py` dependencies. The main PR is then mostly deletions and simplifications, which are easy to review even at scale.

The migration order from the issue body is well-structured and should be followed as-is.

## Open Questions

All questions below are registered in the contract as decisions or feedback items.

### Decision 1: PR splitting strategy

> How should this be split into PRs?

- [ ] **Option A**: Single large PR (~5000-6000 lines, atomic change)
- [ ] **Option B**: 3-4 smaller PRs following dependency order
- [ ] **Option C**: Two PRs — prep refactor + main removal (recommended)
- [ ] Other (explain in reply)

### Decision 2: Coordinator artifact cleanup

> Should we clean up coordinator artifacts left over from PR #1169?

PR #1169 merged but `coordinator_executor.py` and `sandbox/.claude/rules/coordinator.md` still exist on disk.

- [ ] Yes, include coordinator cleanup in this issue
- [ ] No, coordinator cleanup is out of scope (track separately)
- [ ] Other (explain in reply)

### Decision 3: Open PR #1171

> What should happen to open PR #1171 (docs: add integrator to concurrent implement phase roles)?

This PR adds integrator documentation that #1165 will remove.

- [ ] Close #1171 — integrator is being removed anyway
- [ ] Merge #1171 first, then remove in this issue
- [ ] Ignore — this issue will supersede it
- [ ] Other (explain in reply)

### Decision 4: concurrent_execution config flags

> After removal, should `concurrent_execution` and `concurrent_phases` config flags also be removed (since BRC is now the only mode)?

The issue's "Keep" list says to keep these flags. But if BRC is always-on, they become dead config.

- [ ] Yes, remove them too — they are now always-on
- [ ] No, keep them for now — useful as kill switches
- [ ] Other (explain in reply)

### Feedback Requested

1. **In-flight pipelines**: Are there any in-flight pipelines using Tier 3 or multi-agent wave execution that need to complete before this lands?

2. **Stale state handling**: The issue says no backward compatibility for old pipeline state. Should we add a one-time migration script to clean up old `.egg-state/pipelines/*.json` files, or is intentional failure on stale state acceptable?

---

*Authored-by: egg*

# metadata
complexity_tier: high
parallel_phases: true
