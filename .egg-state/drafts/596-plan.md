# Plan: Update Orchestration DAG Visualization to Include More Context

> Issue: #596 | Phase: plan

## Summary

The DAG visualization shows unhelpful aggregate counts ("2 container(s), 2 agent(s)") with no role or status breakdown. This plan replaces that line with per-agent role and status information so users can see at a glance which agents are running, completed, or failed — directly addressing the three problems identified in the issue and analysis.

The approach follows Approach A from the analysis: inline `{status_symbol} {role}` entries on one line, dropping container counts entirely from the DAG box. The detailed phase view (`render_phase_detail`) already shows full container and agent drill-down and remains unchanged.

## Implementation Phases

### Phase 1: Update `_render_phase_box` to Accept and Render Agent Details

**Goal**: Replace integer count parameters with a list of `AgentExecution` objects and render per-agent role+status inline.

**Tasks**:

- [TASK-1-1] Change `_render_phase_box` signature — Replace `containers_count: int = 0` and `agents_count: int = 0` parameters with `agents: list[AgentExecution] = None` (default to empty list via `None`/`or []` pattern). Import `AgentExecution` and `AgentRole` at the top of `dag_visualizer.py`.
  - **File**: `orchestrator/dag_visualizer.py:18-24` (imports), `:90-99` (signature)
  - **Acceptance**: Function accepts `agents` parameter; old `containers_count`/`agents_count` params removed; no callers use old params.

- [TASK-1-2] Add agent status symbol mapping helper — Create a helper function `_get_agent_status_symbol(status: AgentExecutionStatus, use_ascii: bool) -> str` that maps `AgentExecutionStatus` to display symbols, reusing the same symbol vocabulary as `render_phase_detail` (lines 310-318). This avoids duplicating the mapping logic inline.
  - **File**: `orchestrator/dag_visualizer.py` (new function near line 67)
  - **Acceptance**: Helper maps COMPLETE→✓, RUNNING→▶, FAILED→✗, PENDING→○ (and ASCII equivalents). Used by both `_render_phase_box` and `render_phase_detail`.

- [TASK-1-3] Replace aggregate count rendering with per-agent role+status line — In `_render_phase_box` (lines 116-123), replace the `info_parts` logic. When agents are present, render them as space-separated `{symbol} {role}` entries sorted by `AgentRole` enum order. If there are 4+ agents, wrap to multiple lines (2-3 per line). When no agents exist, render nothing (same as current behavior when counts are 0).
  - **File**: `orchestrator/dag_visualizer.py:116-123`
  - **Acceptance**: A phase with a completed coder and running reviewer renders as `✓ coder  ▶ reviewer`. A phase with no agents shows no info line. Width calculation accounts for the new content.

**Exit criteria**: `_render_phase_box` renders per-agent info; old count-based rendering is removed.

### Phase 2: Update `render_pipeline_dag` to Pass Agent List

**Goal**: Wire the full agent list from `PhaseExecution` through to the box renderer.

**Tasks**:

- [TASK-2-1] Pass `agents` list instead of counts — In `render_pipeline_dag` (lines 210-239), replace `containers_count` and `agents_count` variables with `agents = phase_exec.agents if phase_exec else []`. Pass `agents=agents` to `_render_phase_box`.
  - **File**: `orchestrator/dag_visualizer.py:214-239`
  - **Acceptance**: `render_pipeline_dag` no longer references `containers_count` or `agents_count`. It passes the actual `AgentExecution` list to `_render_phase_box`.

**Exit criteria**: The full pipeline DAG renders correctly with per-agent details.

### Phase 3: Update `generate_status_report` Agent Data

**Goal**: Expose per-agent role and status in the API report, replacing the flat count.

**Tasks**:

- [TASK-3-1] Replace agent count with agent detail list in status report — In `generate_status_report` (lines 437-441), change the `"agents"` field from a count (`len(...)`) to a list of dicts with `role` and `status` fields. This gives API consumers the same visibility that the DAG visualization now provides.
  - **File**: `orchestrator/dag_visualizer.py:437-441`
  - **Acceptance**: `report["phases"]["implement"]["agents"]` returns `[{"role": "coder", "status": "complete"}, {"role": "reviewer", "status": "running"}]` instead of `2`. Existing `containers` count field stays as-is (containers are still useful metadata in the API even though we dropped them from the visual).

**Exit criteria**: Status report API includes per-agent role+status data.

### Phase 4: Refactor `render_phase_detail` to Use Shared Helper

**Goal**: Eliminate duplicated agent status mapping logic.

**Tasks**:

- [TASK-4-1] Refactor `render_phase_detail` to use `_get_agent_status_symbol` — Replace the inline `PipelineStatus` mapping block (lines 310-318) with a call to the new `_get_agent_status_symbol` helper from TASK-1-2. This removes ~8 lines of duplicated mapping logic.
  - **File**: `orchestrator/dag_visualizer.py:310-318`
  - **Acceptance**: `render_phase_detail` produces identical output to before. The inline status mapping is replaced with a single function call. All existing `render_phase_detail` tests pass unchanged.

**Exit criteria**: No duplicated agent-status-to-symbol mapping exists in the codebase.

### Phase 5: Update Tests

**Goal**: Update existing tests and add new coverage for the changed behavior.

**Tasks**:

- [TASK-5-1] Update `test_phase_with_containers` — Rename to `test_phase_with_agents` (or similar). Replace the fixture to include `AgentExecution` objects instead of only `ContainerInfo` objects. Assert that agent roles and status symbols appear in the DAG output. Remove assertion on `"2 container(s)"`.
  - **File**: `orchestrator/tests/test_dag_visualizer.py:168-193`
  - **Acceptance**: Test creates a phase with 2 agents (e.g., coder=COMPLETE, reviewer=RUNNING), renders the DAG, and asserts `"coder"` and `"reviewer"` appear in the output with correct symbols. Old `"container(s)"` assertion is gone.

- [TASK-5-2] Add test for mixed agent statuses in DAG — New test that verifies a phase with agents in different states (COMPLETE, RUNNING, FAILED, PENDING) renders each with the correct symbol.
  - **File**: `orchestrator/tests/test_dag_visualizer.py` (new test in `TestRenderPipelineDag`)
  - **Acceptance**: Test creates agents with each status, renders DAG, and asserts correct symbol appears before each role name.

- [TASK-5-3] Add test for phase with no agents — Verify that a phase with an empty agents list (or `None`) renders no info line (no blank line or placeholder).
  - **File**: `orchestrator/tests/test_dag_visualizer.py` (new test in `TestRenderPipelineDag`)
  - **Acceptance**: Rendered box for a pending phase has no agent info line.

- [TASK-5-4] Add test for ASCII mode agent rendering — Verify that agent symbols use ASCII equivalents when `use_ascii=True`.
  - **File**: `orchestrator/tests/test_dag_visualizer.py` (new test in `TestRenderPipelineDag`)
  - **Acceptance**: With `use_ascii=True`, completed agent shows `+` symbol, running shows `>`, etc.

- [TASK-5-5] Update `test_phases_contains_all_phases` for new agent data format — Update assertion from checking `"agents" in phase_data` (integer) to verifying it's a list of dicts with `role` and `status` keys.
  - **File**: `orchestrator/tests/test_dag_visualizer.py:382-393`
  - **Acceptance**: Test verifies `report["phases"][phase]["agents"]` is a list. When agents exist, each entry has `"role"` and `"status"` keys.

- [TASK-5-6] Add test for `_get_agent_status_symbol` helper — Unit test for the new helper function covering all `AgentExecutionStatus` values in both Unicode and ASCII modes.
  - **File**: `orchestrator/tests/test_dag_visualizer.py` (new test class or in `TestStatusSymbol`)
  - **Acceptance**: All 4 statuses map to expected symbols in both modes.

**Exit criteria**: All tests pass. `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/test_dag_visualizer.py -v` exits 0.

## Test Strategy

- **Unit tests**: All changes are covered by unit tests in `test_dag_visualizer.py`
- **Existing tests**: `render_phase_detail` tests (lines 295-354) must continue to pass unchanged — this validates that the detail view was not regressed
- **Integration**: The DAG string is consumed by SSE, CLI watch, and API — all receive the pre-rendered string, so changes propagate automatically. No integration test changes needed.
- **Manual verification**: Render a sample pipeline with mixed agent states and visually inspect the box output for correctness and alignment
- **Test command**: `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/test_dag_visualizer.py -v`

## Rollback Plan

1. All changes are confined to `orchestrator/dag_visualizer.py` and its test file — no model changes, no API contract changes beyond the `agents` field format
2. The `agents` field in `generate_status_report` changes from `int` to `list[dict]` — any consumers parsing this as an integer will need updating. Check SSE/CLI consumers before merging. If this is a breaking change, keep as `int` and add a separate `agent_details` field instead
3. If the new rendering causes width issues in narrow terminals, the box auto-sizes to content width, so this is self-correcting. Worst case: add a `max_agents_per_line` parameter

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `generate_status_report` `agents` field type change breaks consumers | Low | Check all consumers of the status report API; if needed, keep count + add separate detail field |
| Box width grows too wide with many agents | Very Low | Max typical agents per phase is 2-3; add line wrapping at 4+ |
| Compact status (`render_compact_status`) expectations change | None | Compact status already omits agent info — no changes needed |

## Files Modified

| File | Changes |
|------|---------|
| `orchestrator/dag_visualizer.py` | New `_get_agent_status_symbol` helper; `_render_phase_box` signature + rendering; `render_pipeline_dag` passes agent list; `render_phase_detail` uses shared helper; `generate_status_report` agents field |
| `orchestrator/tests/test_dag_visualizer.py` | Update `test_phase_with_containers`; update `test_phases_contains_all_phases`; add 4 new tests |
