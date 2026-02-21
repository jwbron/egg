# Plan: Full DAG visualization for Tier 3 parallel phase execution

> Issue: #829 | Phase: plan

## Summary

Add side-by-side sub-phase box rendering with fan-out/fan-in connectors to the
DAG visualizer for Tier 3 pipelines. This replaces the current flat grouping
inside a single Implement box with individual sub-phase boxes arranged by
dependency wave, making parallel execution structure visible.

## Approach

The implementation follows Option A from the architect analysis: expand the
Implement phase into individual sub-phase boxes arranged by dependency wave.

### Data flow

The visualizer currently receives only a `Pipeline` object, which has no
knowledge of plan phase dependencies. The cleanest solution is to add an
optional `plan_phase_waves` field to the `Pipeline` model, populated during
Tier 3 dispatch in `_run_tier3_implement()`. This keeps the rendering function
pure (no contract loading) and avoids changing the function signature.

The field stores a lightweight representation of the wave structure:
`list[list[str]]` — a list of waves, each wave being a list of phase IDs.
This is computed from `PhaseDependencyGraph.compute_waves()` and serialized
as part of pipeline state.

An optional `plan_phase_names` field (a `dict[str, str]` mapping phase ID to
human-readable name) is also added so sub-phase boxes can display names like
"Auth" instead of raw IDs like "phase-1".

### Rendering strategy

When `render_pipeline_dag()` encounters the Implement phase on a Tier 3
pipeline (detected by the presence of `plan_phase_waves`), it replaces the
single Implement box with:

1. A header arrow from the Plan box
2. For each wave:
   - If the wave has a single phase: render a standard sub-phase box (centered)
   - If the wave has multiple phases: render boxes **side-by-side** with
     fan-out connectors above and fan-in connectors below
3. A footer arrow to the PR box

**Side-by-side rendering** works by rendering each sub-phase box independently,
then concatenating them line-by-line with horizontal padding. Height
normalization pads shorter boxes with blank lines.

**Fan-out connector** (above a parallel wave):
```
      │
   ┌──┴──┐
   │     │
```

**Fan-in connector** (below a parallel wave):
```
   │     │
   └──┬──┘
      │
```

For ASCII mode, `┌──┴──┐` becomes `+--+--+`, etc.

### Sub-phase box content

Each sub-phase box is a smaller version of a phase box, showing:
- Phase name (from `plan_phase_names`) or phase ID as fallback
- Status (derived from the aggregate status of agents with that `plan_phase_id`)
- Agent sequence within the sub-phase (coder, tester, etc.) with status symbols

Top-level agents (those without `plan_phase_id`, like `reviewer_contract` and
`integrator`) are rendered in a separate box after all sub-phase waves, before
the PR phase.

### Width management

Terminal width constraint (80-120 chars) is handled by:
- Sub-phase boxes use a compact width (minimum ~20 chars)
- Maximum 4 boxes side-by-side before wrapping to a second row
- Phase names are truncated if they exceed the box width

### Backward compatibility

- Tier 1 and Tier 2 pipelines have no `plan_phase_waves` field, so they
  render exactly as today — the code path is guarded by `if pipeline.plan_phase_waves`
- All consumers (`render_pipeline_dag` callers) pass only a `Pipeline` object,
  so no signature changes are needed
- Both Unicode and ASCII modes are supported for all new rendering

## Phases

### Phase 1: Extend Pipeline model and populate wave data

Add `plan_phase_waves` and `plan_phase_names` fields to the `Pipeline` model.
Populate them in `_run_tier3_implement()` after computing the phase dependency
graph. This provides the data foundation for all subsequent visualization work.

### Phase 2: Implement sub-phase box and side-by-side rendering

Add new rendering functions to `dag_visualizer.py`:
- `_render_subphase_box()` — renders a compact box for a single sub-phase
- `_render_side_by_side()` — concatenates multiple boxes horizontally
- `_render_fan_out()` / `_render_fan_in()` — renders branching connectors
- `_render_tier3_implement()` — orchestrates the full expanded Implement section

Modify `render_pipeline_dag()` to call `_render_tier3_implement()` when
`plan_phase_waves` is present on the pipeline.

### Phase 3: Tests

Add comprehensive tests for all new rendering logic:
- Sub-phase box rendering (single phase, status variants)
- Side-by-side rendering (2 phases, 3+ phases, height normalization)
- Fan-out/fan-in connectors (Unicode and ASCII)
- Full DAG rendering with Tier 3 pipeline data
- Backward compatibility (Tier 1/2 pipelines unchanged)
- Edge cases: single-wave Tier 3, many parallel phases, long phase names

## Files Modified

| File | Change |
|------|--------|
| `orchestrator/models.py` | Add `plan_phase_waves` and `plan_phase_names` fields to `Pipeline` |
| `orchestrator/routes/pipelines.py` | Populate wave data in `_run_tier3_implement()` |
| `orchestrator/dag_visualizer.py` | Add sub-phase box rendering, side-by-side layout, fan-out/fan-in connectors |
| `orchestrator/tests/test_dag_visualizer.py` | Add Tier 3 DAG visualization tests |

## Test Strategy

Tests are added in Phase 3 but the test-first principle applies: each rendering
function should be tested in isolation (unit tests) and then as part of the
full DAG (integration tests).

**Unit tests:**
- `_render_subphase_box()` with various statuses and agent combinations
- `_render_side_by_side()` with boxes of different heights
- `_render_fan_out()` / `_render_fan_in()` in both Unicode and ASCII modes
- Sub-phase status derivation from agent statuses

**Integration tests:**
- Full `render_pipeline_dag()` with a Tier 3 pipeline (2 waves: 1 sequential + 2 parallel)
- Full DAG with top-level agents (integrator, reviewer_contract) after sub-phases
- ASCII mode full DAG
- Tier 1/2 pipeline regression (output unchanged)

**Edge case tests:**
- Tier 3 with single wave (no fan-out/fan-in needed)
- Tier 3 with 4+ parallel phases (width wrapping)
- Empty `plan_phase_waves` (should fall back to current rendering)
- Missing `plan_phase_names` (should use phase IDs)

## Risks

- **Side-by-side rendering complexity**: The line-by-line concatenation with
  height normalization is the most complex part. Mitigated by implementing as
  a standalone utility function with thorough unit tests.
- **Terminal width overflow**: Many parallel phases could exceed terminal width.
  Mitigated by capping side-by-side rendering at 4 boxes and wrapping.
- **Persisted pipeline state migration**: Adding new optional fields to Pipeline
  is backward-compatible (Pydantic defaults to `None`). No migration needed.

---

```yaml
# yaml-tasks
pr:
  title: "Add DAG visualization for Tier 3 parallel phases"
  description: |
    Expands the DAG visualizer to render Tier 3 parallel sub-phases as
    individual boxes arranged by dependency wave, with fan-out/fan-in
    connectors showing parallel execution structure. Fixes non-deterministic
    ordering of parallel phases by using topological sort from the dependency
    graph. Tier 1 and Tier 2 pipelines continue to render as before.
phases:
  - id: 1
    name: Extend Pipeline model and populate wave data
    goal: Provide the wave structure data needed by the visualizer
    tasks:
      - id: TASK-1-1
        description: Add optional plan_phase_waves (list[list[str]] | None) and plan_phase_names (dict[str, str] | None) fields to the Pipeline model in orchestrator/models.py
        acceptance: Pipeline model accepts and serializes the new fields; existing pipelines without these fields deserialize without error (defaults to None)
        files:
          - orchestrator/models.py
      - id: TASK-1-2
        description: Populate plan_phase_waves and plan_phase_names in _run_tier3_implement() after computing the PhaseDependencyGraph, before entering the phase dispatch loop
        acceptance: After Tier 3 dispatch starts, pipeline.plan_phase_waves contains the wave structure from PhaseDependencyGraph.compute_waves() and plan_phase_names maps phase IDs to names from the contract
        files:
          - orchestrator/routes/pipelines.py
  - id: 2
    name: Implement sub-phase box and side-by-side rendering
    goal: Render Tier 3 sub-phases as individual boxes with fan-out/fan-in connectors
    tasks:
      - id: TASK-2-1
        description: Add _render_subphase_box() function that renders a compact box for a single sub-phase, showing phase name, aggregate status, and agent sequence with status symbols
        acceptance: Function returns a list[str] of box lines; works in both Unicode and ASCII modes; derives sub-phase status from agent statuses
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-2-2
        description: Add _render_side_by_side() function that takes multiple box line-lists, normalizes heights by padding shorter boxes, and concatenates them horizontally with spacing
        acceptance: Function correctly merges boxes of different heights; output lines are consistently wide; handles 1 to 4+ boxes
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-2-3
        description: Add _render_fan_out() and _render_fan_in() functions that render branching connectors between sequential and parallel waves, supporting both Unicode and ASCII
        acceptance: Fan-out produces a top-to-branches visual; fan-in produces a branches-to-bottom visual; connector width scales with the number and width of parallel boxes
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-2-4
        description: Add _render_tier3_implement() orchestrator function that assembles the full expanded Implement section using wave data, sub-phase boxes, side-by-side rendering, and connectors; renders top-level agents (integrator, reviewer_contract) in a separate box after sub-phases
        acceptance: Returns list[str] lines for the complete Tier 3 Implement section; handles sequential waves (single box), parallel waves (side-by-side boxes with connectors), and top-level agents
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-2-5
        description: Modify render_pipeline_dag() to detect Tier 3 pipelines (plan_phase_waves is not None) and call _render_tier3_implement() instead of _render_phase_box() for the Implement phase
        acceptance: Tier 3 pipelines render expanded sub-phase DAG; Tier 1/2 pipelines render unchanged; no change to function signature
        files:
          - orchestrator/dag_visualizer.py
  - id: 3
    name: Add tests for Tier 3 DAG visualization
    goal: Comprehensive test coverage for all new rendering logic
    tasks:
      - id: TASK-3-1
        description: Add unit tests for _render_subphase_box() covering various agent statuses, phase names, Unicode and ASCII modes
        acceptance: Tests pass and cover completed/running/pending/failed sub-phase states
        files:
          - orchestrator/tests/test_dag_visualizer.py
      - id: TASK-3-2
        description: Add unit tests for _render_side_by_side() covering different box heights, 2-box and 3+-box scenarios
        acceptance: Tests verify correct horizontal concatenation and height normalization
        files:
          - orchestrator/tests/test_dag_visualizer.py
      - id: TASK-3-3
        description: Add unit tests for _render_fan_out() and _render_fan_in() in Unicode and ASCII modes with varying widths
        acceptance: Tests verify correct connector rendering for 2 and 3+ branches
        files:
          - orchestrator/tests/test_dag_visualizer.py
      - id: TASK-3-4
        description: Add integration tests for render_pipeline_dag() with Tier 3 pipeline data (sequential + parallel waves, top-level agents, ASCII mode)
        acceptance: Full DAG output matches expected structure; sub-phase boxes appear in wave order; fan-out/fan-in connectors present for parallel waves
        files:
          - orchestrator/tests/test_dag_visualizer.py
      - id: TASK-3-5
        description: Add backward-compatibility regression tests verifying Tier 1/2 pipelines render identically to current output
        acceptance: Tests create pipelines without plan_phase_waves and verify output matches baseline
        files:
          - orchestrator/tests/test_dag_visualizer.py
      - id: TASK-3-6
        description: Add edge case tests for single-wave Tier 3, empty plan_phase_waves, missing plan_phase_names, and 4+ parallel phases
        acceptance: All edge cases handled gracefully without errors
        files:
          - orchestrator/tests/test_dag_visualizer.py
```

---

*Authored-by: egg*
