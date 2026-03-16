# Tier 3 Dispatch: Phase-Level Parallel Execution

Tier 3 is the high-complexity execution path for tasks that decompose into multiple largely independent work phases. Instead of a single implement cycle, each plan phase runs its own implement cycle (Coder → Tester → Documenter → Code Reviewer), and independent phases execute in parallel.

## What Tier 3 Is

The pipeline has three complexity tiers:

| Tier | `complexity_tier` | Description |
|------|-------------------|-------------|
| Tier 1 | `low` | Short-circuit: refine signals `short_circuit: true`, skips plan, jumps to implement |
| Tier 2 | `mid` | Standard wave-based: coder → tester + documenter → reviewers |
| Tier 3 | `high` | Phase-level dispatch: each plan phase runs its own implement cycle |

Tier 3 is activated when the plan document defines multiple phases with explicit dependency relationships, and the task complexity is assessed as `high`.

## How `complexity_tier` Is Set and Propagated

The `complexity_tier` field on the `Pipeline` model (`orchestrator/models.py`) defaults to `mid`. It is set during the plan phase when the plan document is parsed:

1. The plan document's YAML appendix defines phases and their dependencies
2. The plan parser (`shared/egg_contracts/plan_parser.py`) extracts phases and inter-phase dependencies
3. If the parsed plan contains multiple phases with non-trivial dependency structure, the orchestrator sets `complexity_tier = "high"` on the pipeline
4. The `complexity_tier` is stored in pipeline state and passed to the gateway when spawning containers via the `EGG_COMPLEXITY_TIER` environment variable

The gateway's `get_agent_pattern()` function reads this tier to determine which file access rules apply per role.

## Plan Phase Dependency Syntax

Phases declare their dependencies in the YAML appendix of the plan document. The format is in a ` ```yaml ` code fence marked with `# yaml-tasks`:

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Core Data Models
    goal: Define base data model classes
    dependencies: []
    tasks:
      - id: TASK-1-1
        description: Create base model classes
        acceptance: Models pass schema validation
        files:
          - shared/models.py

  - id: 2
    name: API Layer
    goal: Implement REST API endpoints
    dependencies: ["phase-1"]
    tasks:
      - id: TASK-2-1
        description: Implement CRUD endpoints
        acceptance: All endpoints return correct HTTP status codes
        files:
          - api/routes.py

  - id: 3
    name: Tests
    goal: Write integration tests
    dependencies: ["phase-1"]
    tasks:
      - id: TASK-3-1
        description: Integration tests for core models
        acceptance: All tests pass
        files:
          - tests/test_models.py

  - id: 4
    name: Documentation
    goal: Update API docs
    dependencies: ["phase-2", "phase-3"]
    tasks:
      - id: TASK-4-1
        description: Update API reference docs
        acceptance: Docs reflect current API
        files:
          - docs/api.md
```

### Dependency Format

Dependencies are specified as a list of phase ID strings. The parser normalizes several formats:

| Input format | Normalized to |
|-------------|---------------|
| `"phase-1"` | `"phase-1"` |
| `"Phase 1"` | `"phase-1"` |
| `"1"` (short numeric string) | `"phase-1"` |
| `phase-1, phase-2` (comma-separated string) | `["phase-1", "phase-2"]` |

Phases without a `dependencies` field (or with `dependencies: []`) are treated as having no dependencies.

## DAG Construction and Wave Ordering

The `PhaseDependencyGraph` (in `shared/egg_contracts/dependency_graph.py`) constructs a directed acyclic graph from the parsed phases and computes execution waves using topological sort.

### Algorithm

1. Build graph: each phase becomes a node; each dependency becomes a directed edge
2. Detect cycles: DFS-based cycle detection; a cyclic dependency configuration raises `ValueError`
3. Topological sort: Kahn's algorithm with deterministic tie-breaking (sorted by phase ID)
4. Wave assignment: each phase is assigned to wave `max(dependency_wave) + 1`

### Example

Given the phases above:
```
Phase 1: no deps  → wave 1
Phase 2: deps [1] → wave 2
Phase 3: deps [1] → wave 2  (can run in parallel with Phase 2)
Phase 4: deps [2, 3] → wave 3
```

Wave structure:
```
Wave 1: [phase-1]
Wave 2: [phase-2, phase-3]   -- parallel
Wave 3: [phase-4]
```

The resulting `plan_phase_waves` field on the `Pipeline` model stores this as `[["phase-1"], ["phase-2", "phase-3"], ["phase-4"]]` for DAG visualization.

## Parallel Phase Execution Semantics

Each plan phase in a wave runs its own implement cycle in a sub-worktree branched from the pipeline worktree. The branch is named `egg/<feature>/phase-N` (managed by `gateway/worktree_manager.py:create_phase_worktree()`).

**Per-phase implement cycle agents:**
- Coder
- Tester
- Documenter
- Code Reviewer

These agents run in the same dependency-ordered waves as Tier 2, but scoped to the phase's tasks and files.

**Prompt context scoping**: Phase-scoped coders receive the plan overview and their specific phase's tasks, not the full multi-phase plan. This focuses each agent on its bounded work scope and reduces context overhead.

**Parallel phases**: Phases within the same wave execute concurrently. The `enable_parallel_phases` config flag (default: `true`) controls whether this parallelism is used. When disabled, phases in a wave run sequentially.

**Phase isolation**: Each phase cycle commits to its own sub-branch. Agents in one phase cannot see in-progress uncommitted work from another phase (they see the committed baseline).

## Cleanup

After all phase cycles complete, `cleanup_phase_worktrees()` in `gateway/worktree_manager.py` removes the per-phase sub-worktrees. The pipeline worktree itself remains until the pipeline is deleted.

## DAG Visualization

The `GET /api/v1/pipelines/{id}/visualization` endpoint renders Tier 3 pipelines with expanded sub-phase boxes under the Implement phase, with fan-out/fan-in connectors for parallel phases. This reads from `Pipeline.plan_phase_waves` and `Pipeline.plan_phase_names`, which are populated at Tier 3 implement start.

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — Full pipeline operation including Tier 3 detail
- [Agent Roles Reference](../reference/agent-roles.md) — All role permissions
- [Orchestrator Architecture](../architecture/orchestrator.md) — Per-phase worktrees and state management
