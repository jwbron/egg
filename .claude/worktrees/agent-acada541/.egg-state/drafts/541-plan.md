# Plan: Improve Visibility into Local Orchestration

> Issue: #541 | Phase: plan

## Summary

This plan implements ASCII DAG visualization with real-time SSE streaming for the orchestrator. Per the human's decisions: we'll use Unicode box-drawing characters with ASCII fallback, implement SSE from the start (no polling), and auto-display the visualization in the collaborator egg session. The implementation adds a `dag_visualizer.py` module for rendering, an SSE streaming endpoint, and client-side display integration in the sandbox statusbar.

## Implementation Phases

### Phase 1: DAG Visualization Renderer

**Goal**: Create the core ASCII rendering module that generates DAG visualizations from pipeline state.

**Tasks**:
- [TASK-1-1] Create `orchestrator/dag_visualizer.py` module with character set abstraction — Acceptance: Module exists with `CharacterSet` class supporting Unicode and ASCII modes
- [TASK-1-2] Implement phase DAG renderer — Acceptance: `render_phase_dag(pipeline)` returns formatted string showing REFINE→PLAN→IMPLEMENT→PR with status indicators
- [TASK-1-3] Implement agent wave renderer — Acceptance: `render_agent_waves(phase_execution, execution_plan)` returns formatted string showing waves with parallel agents and dependencies
- [TASK-1-4] Implement combined renderer with legend — Acceptance: `render_pipeline_visualization(pipeline)` returns complete visualization with phase DAG, agent waves (if implement phase), and legend
- [TASK-1-5] Add terminal capability detection — Acceptance: Auto-detect Unicode support with `TERM`/`LANG` environment checks, fallback to ASCII
- [TASK-1-6] Write unit tests for visualization module — Acceptance: Tests cover all renderers, both character sets, various pipeline states (pending, running, complete, failed)

**Dependencies**: None

**Exit criteria**: All visualization functions work correctly with both Unicode and ASCII modes, tests pass

### Phase 2: SSE Streaming Infrastructure

**Goal**: Add Server-Sent Events streaming endpoint for real-time pipeline updates.

**Tasks**:
- [TASK-2-1] Create `orchestrator/sse.py` module with SSE response helpers — Acceptance: `sse_response()` generator and `format_sse_event()` functions work correctly
- [TASK-2-2] Add `/api/v1/pipelines/<id>/stream` SSE endpoint — Acceptance: Endpoint returns `text/event-stream` content type, connects to EventBus
- [TASK-2-3] Implement EventBus subscription for SSE stream — Acceptance: SSE endpoint receives pipeline events via wildcard subscription
- [TASK-2-4] Emit visualization events on state changes — Acceptance: Events include `dag_visualization` field with rendered ASCII
- [TASK-2-5] Add connection lifecycle management — Acceptance: Handles client disconnect, connection timeout, heartbeat keepalive
- [TASK-2-6] Write integration tests for SSE endpoint — Acceptance: Tests verify event streaming, reconnection, and visualization updates

**Dependencies**: Phase 1

**Exit criteria**: SSE endpoint streams real-time visualization updates on pipeline state changes

### Phase 3: Event Emission Points

**Goal**: Ensure all relevant state changes emit events that trigger visualization updates.

**Tasks**:
- [TASK-3-1] Add events in `_run_pipeline()` for phase transitions — Acceptance: `PHASE_STARTED`, `PHASE_COMPLETED` events include visualization payload
- [TASK-3-2] Add events in `MultiAgentExecutor` for wave/agent updates — Acceptance: `AGENT_STARTED`, `AGENT_COMPLETED` events include updated visualization
- [TASK-3-3] Add events for review cycle increments — Acceptance: Review cycle count changes trigger visualization update event
- [TASK-3-4] Add events for HITL decision state changes — Acceptance: `DECISION_CREATED`, `DECISION_RESOLVED` trigger visualization update

**Dependencies**: Phases 1 and 2

**Exit criteria**: All state changes that affect visualization emit events with updated ASCII rendering

### Phase 4: Collaborator Client Integration

**Goal**: Display the visualization automatically in the egg session that triggered the workflow.

**Tasks**:
- [TASK-4-1] Create `sandbox/egg_lib/sse_client.py` module — Acceptance: Async SSE client can connect to orchestrator stream endpoint
- [TASK-4-2] Extend `sandbox/statusbar.py` with multi-line visualization support — Acceptance: StatusBar can render multi-line ASCII blocks without flickering
- [TASK-4-3] Add visualization display to workflow trigger flow — Acceptance: When collaborator triggers workflow, SSE connection established automatically
- [TASK-4-4] Implement terminal clear/redraw for real-time updates — Acceptance: Visualization updates in-place without scrolling
- [TASK-4-5] Add graceful degradation for SSE connection failures — Acceptance: If SSE fails, fall back to periodic status display with warning
- [TASK-4-6] Write end-to-end tests for collaborator display — Acceptance: Tests verify visualization appears and updates in simulated terminal

**Dependencies**: Phases 2 and 3

**Exit criteria**: Collaborator egg session displays real-time DAG visualization automatically

### Phase 5: Documentation and Polish

**Goal**: Document the feature and handle edge cases.

**Tasks**:
- [TASK-5-1] Add configuration for visualization preferences — Acceptance: `PipelineConfig` has `visualization_charset` (auto/unicode/ascii) option
- [TASK-5-2] Handle terminal resize gracefully — Acceptance: Visualization adapts to terminal width changes
- [TASK-5-3] Update orchestrator API documentation — Acceptance: SSE endpoint documented with examples
- [TASK-5-4] Add troubleshooting section to docs — Acceptance: Common issues (terminal compatibility, network) documented

**Dependencies**: Phase 4

**Exit criteria**: Feature is fully documented, handles edge cases gracefully

## Test Strategy

- **Unit tests**:
  - `dag_visualizer.py`: Test all render functions with various pipeline states, both character sets
  - `sse.py`: Test SSE formatting, event serialization
  - `sse_client.py`: Test connection handling, event parsing

- **Integration tests**:
  - SSE endpoint with mock EventBus: Verify event streaming
  - Pipeline execution with SSE: Verify visualization updates on phase transitions
  - End-to-end: Trigger workflow, verify collaborator receives visualization

- **Manual testing**:
  1. Trigger a workflow from collaborator session
  2. Verify visualization appears automatically
  3. Watch as phases progress and agents execute
  4. Verify status indicators update in real-time
  5. Test with different terminals (urxvt, xterm, tmux)
  6. Test with `LANG=C` to verify ASCII fallback

## Rollback Plan

1. **Feature flag**: Add `ENABLE_DAG_VISUALIZATION=true` environment variable
2. **If SSE causes issues**: The SSE endpoint is additive; existing polling status endpoint unchanged
3. **If visualization breaks terminals**: Collaborator can set `visualization_charset=ascii` in config
4. **Full rollback**: Revert the commit; no database migrations or breaking API changes

Rollback commands:
```bash
# Disable feature
export ENABLE_DAG_VISUALIZATION=false

# Or revert commit
git revert <commit-sha>
git push origin egg/issue-541
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Unicode rendering issues on some terminals | Medium | Low | Provide ASCII fallback with auto-detection |
| SSE connections not closing properly | Medium | Medium | Implement heartbeat timeout, connection tracking |
| Flask SSE performance under load | Low | Medium | SSE is per-pipeline; typical load is 1-2 concurrent viewers |
| Event bus subscription memory leak | Low | High | Use weak references, cleanup on disconnect |
| Terminal flickering during updates | Medium | Low | Use ANSI cursor positioning, minimize redraws |

## Migration Notes

- **No database migrations required** — All state is in existing Pipeline model
- **No breaking API changes** — SSE endpoint is additive
- **No config changes required** — Feature works with defaults
- **Backwards compatible** — Existing clients can ignore visualization events

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add ASCII DAG visualization with SSE streaming"
  description: |
    Implements real-time ASCII visualization of pipeline execution DAG.
    Shows phase progression, multi-agent waves, review cycles, and status
    with Unicode box-drawing (ASCII fallback). Streams updates via SSE
    to the collaborator session that triggered the workflow.

    Fixes #541.
phases:
  - id: 1
    name: DAG Visualization Renderer
    goal: Create the core ASCII rendering module for DAG visualizations
    tasks:
      - id: TASK-1-1
        description: Create dag_visualizer module with character set abstraction
        acceptance: Module exists with CharacterSet class supporting Unicode and ASCII modes
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-1-2
        description: Implement phase DAG renderer
        acceptance: render_phase_dag returns formatted string with REFINE→PLAN→IMPLEMENT→PR and status
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-1-3
        description: Implement agent wave renderer
        acceptance: render_agent_waves returns formatted string showing waves with parallel agents
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-1-4
        description: Implement combined renderer with legend
        acceptance: render_pipeline_visualization returns complete visualization
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-1-5
        description: Add terminal capability detection
        acceptance: Auto-detect Unicode support with TERM/LANG checks and ASCII fallback
        files:
          - orchestrator/dag_visualizer.py
      - id: TASK-1-6
        description: Write unit tests for visualization module
        acceptance: Tests cover all renderers, both character sets, various pipeline states
        files:
          - orchestrator/tests/test_dag_visualizer.py
  - id: 2
    name: SSE Streaming Infrastructure
    goal: Add Server-Sent Events streaming endpoint for real-time updates
    tasks:
      - id: TASK-2-1
        description: Create sse module with SSE response helpers
        acceptance: sse_response generator and format_sse_event functions work correctly
        files:
          - orchestrator/sse.py
      - id: TASK-2-2
        description: Add SSE stream endpoint to pipelines routes
        acceptance: Endpoint returns text/event-stream, connects to EventBus
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-2-3
        description: Implement EventBus subscription for SSE stream
        acceptance: SSE endpoint receives pipeline events via wildcard subscription
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/sse.py
      - id: TASK-2-4
        description: Emit visualization events on state changes
        acceptance: Events include dag_visualization field with rendered ASCII
        files:
          - orchestrator/sse.py
      - id: TASK-2-5
        description: Add connection lifecycle management
        acceptance: Handles client disconnect, timeout, heartbeat keepalive
        files:
          - orchestrator/sse.py
      - id: TASK-2-6
        description: Write integration tests for SSE endpoint
        acceptance: Tests verify event streaming, reconnection, visualization updates
        files:
          - orchestrator/tests/test_sse.py
  - id: 3
    name: Event Emission Points
    goal: Ensure state changes emit events that trigger visualization updates
    tasks:
      - id: TASK-3-1
        description: Add events in _run_pipeline for phase transitions
        acceptance: PHASE_STARTED, PHASE_COMPLETED events include visualization payload
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-2
        description: Add events in MultiAgentExecutor for wave/agent updates
        acceptance: AGENT_STARTED, AGENT_COMPLETED events include updated visualization
        files:
          - orchestrator/multi_agent.py
      - id: TASK-3-3
        description: Add events for review cycle increments
        acceptance: Review cycle count changes trigger visualization update event
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-3-4
        description: Add events for HITL decision state changes
        acceptance: DECISION_CREATED, DECISION_RESOLVED trigger visualization update
        files:
          - orchestrator/routes/decisions.py
  - id: 4
    name: Collaborator Client Integration
    goal: Display visualization automatically in the egg session
    tasks:
      - id: TASK-4-1
        description: Create SSE client module for sandbox
        acceptance: Async SSE client can connect to orchestrator stream endpoint
        files:
          - sandbox/egg_lib/sse_client.py
      - id: TASK-4-2
        description: Extend statusbar with multi-line visualization support
        acceptance: StatusBar can render multi-line ASCII blocks without flickering
        files:
          - sandbox/statusbar.py
      - id: TASK-4-3
        description: Add visualization display to workflow trigger flow
        acceptance: SSE connection established automatically when workflow triggered
        files:
          - sandbox/egg_lib/orchestrator.py
      - id: TASK-4-4
        description: Implement terminal clear/redraw for real-time updates
        acceptance: Visualization updates in-place without scrolling
        files:
          - sandbox/statusbar.py
      - id: TASK-4-5
        description: Add graceful degradation for SSE connection failures
        acceptance: Fall back to periodic status display with warning if SSE fails
        files:
          - sandbox/egg_lib/sse_client.py
      - id: TASK-4-6
        description: Write end-to-end tests for collaborator display
        acceptance: Tests verify visualization appears and updates in simulated terminal
        files:
          - sandbox/tests/test_visualization.py
  - id: 5
    name: Documentation and Polish
    goal: Document the feature and handle edge cases
    tasks:
      - id: TASK-5-1
        description: Add configuration for visualization preferences
        acceptance: PipelineConfig has visualization_charset option (auto/unicode/ascii)
        files:
          - orchestrator/models.py
      - id: TASK-5-2
        description: Handle terminal resize gracefully
        acceptance: Visualization adapts to terminal width changes
        files:
          - sandbox/statusbar.py
      - id: TASK-5-3
        description: Update orchestrator API documentation
        acceptance: SSE endpoint documented with examples
        files:
          - docs/orchestrator-api.md
      - id: TASK-5-4
        description: Add troubleshooting section to docs
        acceptance: Common issues documented (terminal compatibility, network)
        files:
          - docs/orchestrator-api.md
```

---

*Authored-by: egg*
