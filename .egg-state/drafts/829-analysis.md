# Analysis: Full DAG visualization for Tier 3 parallel phase execution

> Issue: #829 | Phase: refine

## Problem Statement

The DAG visualizer renders a flat linear sequence of top-level pipeline phases (Refine → Plan → Implement → PR). For Tier 3 (high-complexity) pipelines, the Implement phase dispatches multiple plan sub-phases — some running in parallel — but this internal structure is invisible in the visualization.

The current workaround groups agents by `plan_phase_id` within the Implement box, but has two problems:
1. **Non-deterministic ordering** — parallel sub-phases appear in lock-acquisition order, so display order varies across runs.
2. **No branching structure** — there is no visual fan-out/fan-in, so parallel sub-phases cannot be shown side-by-side.

The desired outcome is a DAG that expands the Implement phase into individual sub-phase boxes arranged by dependency wave, with fan-out/fan-in connectors between sequential and parallel sections.

## Current Behavior

### Visualizer architecture

The DAG visualizer (`orchestrator/dag_visualizer.py`) renders a linear sequence of 4 phase boxes connected by vertical arrows. Key functions:

- `render_pipeline_dag()` (line 362): Iterates `PHASE_ORDER` (Refine, Plan, Implement, PR) and renders one box per phase via `_render_phase_box()`.
- `_render_phase_box()` (line 222): Renders a single Unicode/ASCII box with status, agents grouped by wave, and duration. For Tier 3, agents with a `plan_phase_id` are bucketed by sub-phase (lines 283–300) and rendered as labeled groups inside the single Implement box.
- `_render_arrow()` (line 354): Renders a simple vertical connector between two boxes.
- `_compute_wave_order()` (line 150): Groups agents by execution wave using the agent-role dependency graph.

Current Tier 3 output looks like:
```
>>> ╔════════════════════════════╗
    │ ▶ Implement               │
    │   running                 │
    │   phase-1:                │
    │   ✓ coder  ✓ tester       │
    │   phase-2:                │
    │   ▶ coder  ○ tester       │
    ╚════════════════════════════╝
```

### Data model

- **Pipeline model** (`orchestrator/models.py`): `Pipeline.phases` is a `dict[str, PhaseExecution]`. Each `PhaseExecution` holds a flat list of `AgentExecution` objects. The `AgentExecution.plan_phase_id` field (line 138) tags which plan sub-phase each agent belongs to. The Pipeline model does **not** store the plan phase dependency graph.

- **Contract model** (`shared/egg_contracts/models.py`): `Contract.phases` (line 425) is a `list[Phase]`, where each `Phase` has an `id`, `name`, `status`, and `dependencies: list[str]` (line 154). This contains the full dependency structure.

- **PhaseDependencyGraph** (`shared/egg_contracts/dependency_graph.py`, line 379): Builds a DAG from `Phase` objects and computes `PhaseWave` objects (groups of independent phase IDs that can execute concurrently).

### Orchestration

`_run_tier3_implement()` in `orchestrator/routes/pipelines.py` (line 2696) loads the contract, builds a `PhaseDependencyGraph`, computes waves, and dispatches sub-phases either sequentially or in parallel via `ThreadPoolExecutor` (line 3236).

### Consumers

`render_pipeline_dag()` is consumed by:
- SSE status streams (`sse.py`, `unified_sse.py`)
- Status reporter (`status_reporter.py`)
- Pipeline API routes (`routes/pipelines.py`)
- `generate_status_report()` API response builder

## Constraints

- **Data availability**: The visualizer receives only a `Pipeline` object, not the contract. The plan phase dependency graph (needed for fan-out/fan-in layout) is in the contract, not the pipeline state. Either the Pipeline model must be extended or the visualizer must load the contract independently.
- **Multiple consumers**: The `render_pipeline_dag()` function is used by SSE streams, the status API, and the status reporter. Changes to its signature or return format affect all consumers.
- **Box width**: Side-by-side rendering of parallel sub-phases requires horizontal space. For wide phase names or many parallel phases, lines could exceed typical terminal widths (80–120 chars).
- **ASCII compatibility**: The visualizer supports both Unicode and ASCII-only rendering modes. Any new connector/layout logic must work in both modes.
- **Non-Tier-3 pipelines**: The change must be backward-compatible — Tier 1 (low) and Tier 2 (mid) pipelines should render exactly as they do today.
- **Test coverage**: The existing test file (`orchestrator/tests/test_dag_visualizer.py`, 1313 lines) has no tests for Tier 3 sub-phase rendering. New tests are needed.

## Options Considered

### Option A: Expand Implement phase into sub-phase boxes in-line

**Approach**: When the pipeline is Tier 3 with plan sub-phases, replace the single Implement box with a sequence of sub-phase boxes arranged by wave. Each wave gets its own row. Waves with multiple phases render boxes side-by-side. Fan-out/fan-in connectors (branching lines) connect sequential waves to parallel waves.

The visualizer would need access to the plan phase dependency graph. This could be achieved by:
1. Adding an optional `plan_phases` field to the `Pipeline` model (populated during Tier 3 dispatch), or
2. Having `render_pipeline_dag()` accept an optional dependency structure parameter.

Each sub-phase box would be a smaller version of a phase box, showing the agent sequence (coder → tester → documenter → checker → reviewer_code) and phase status.

**Pros**:
- Directly addresses the issue's desired visualization
- Preserves the existing linear flow for non-Tier-3 pipelines
- Each sub-phase box reuses `_render_phase_box()` logic with minimal adaptation
- Fan-out/fan-in connectors make parallelism immediately visible

**Cons**:
- Side-by-side box rendering is the most complex part — requires careful width calculation, padding, and line-by-line concatenation of boxes with different heights
- Fan-out/fan-in ASCII connector rendering is non-trivial (branching lines, merge points)
- Requires either extending the Pipeline model or passing extra data through all callers

### Option B: Nested indented sub-phases within Implement box (enhanced current approach)

**Approach**: Keep the single Implement box but improve the internal rendering. Sort sub-phases by dependency-wave order (not insertion order) and add visual wave markers (e.g., wave separators, indentation levels, or parallel indicators like `║`). Add the dependency graph to fix non-deterministic ordering.

```
>>> ╔══════════════════════════════════╗
    │ ▶ Implement                      │
    │   running                        │
    │   ── wave 1 ──                   │
    │   phase-1: ✓ coder  ✓ tester     │
    │   ── wave 2 (parallel) ──        │
    │   phase-2: ▶ coder  ○ tester     │
    │   phase-3: ▶ coder  ○ tester     │
    │   ── integrator ──               │
    │   ○ reviewer_contract             │
    ╚══════════════════════════════════╝
```

**Pros**:
- Much simpler to implement — no side-by-side rendering or fan-out/fan-in connectors needed
- Fixes the non-deterministic ordering problem
- Fits within the existing box model without layout changes
- Lower risk of breaking existing consumers

**Cons**:
- Does not show true branching/parallel structure visually
- Does not match the issue's desired visualization (side-by-side boxes)
- Still crams everything into one box, which grows large with many phases

### Option C: Hybrid — sub-phase boxes stacked vertically with parallel indicators

**Approach**: Render each sub-phase as its own box in the DAG (not side-by-side), but add visual indicators for parallelism. Parallel phases would be rendered in sequence with a "parallel group" bracket or marker.

```
    ╔════════╗
    │  Plan  │
    ╚════════╝
        │
    ┌───┤ parallel
    │   │
    │ ╔═══════════════════╗
    │ │ phase-1: Auth     │
    │ │  ✓ coder ✓ tester │
    │ ╚═══════════════════╝
    │   │
    │ ╔═══════════════════╗
    │ │ phase-2: API      │
    │ │  ▶ coder ○ tester │
    │ ╚═══════════════════╝
    │   │
    └───┘
        │
    ╔═════════════════════╗
    │ integrator          │
    ╚═════════════════════╝
```

**Pros**:
- Avoids the complexity of side-by-side box rendering
- Still shows each sub-phase as an independent entity
- Parallel grouping brackets indicate concurrent execution
- Works better with narrow terminals

**Cons**:
- Doesn't truly show parallel layout (still vertical)
- The bracket notation is less intuitive than side-by-side boxes
- Partially addresses the issue but doesn't fully deliver the desired visualization

## Recommended Approach

**Option A: Expand Implement phase into sub-phase boxes in-line** is recommended.

This is the only option that fully addresses the issue's requirements. The fan-out/fan-in visualization with side-by-side boxes directly shows the parallel execution structure and is the standard way to represent DAG branching in ASCII.

The key technical challenge — side-by-side box rendering — is a well-understood problem (line-by-line concatenation with height normalization). The dependency graph data is already available in the contract and `PhaseDependencyGraph` can compute waves.

For data access, the cleanest approach is to add an optional `plan_phase_waves` field to the `Pipeline` model (a lightweight serialization of phase wave structure) populated during Tier 3 dispatch. This avoids having the visualizer load the contract itself, keeping the rendering function pure.

The non-deterministic ordering fix is a natural side-effect: waves are computed from the dependency graph via topological sort, which produces a deterministic order.

## Open Questions

No blocking questions — the approach is well-scoped and the data model is clear. The plan phase will handle implementation details such as exact connector rendering and box width constraints.

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: mid
```
