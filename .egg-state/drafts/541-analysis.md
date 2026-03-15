# Analysis: Improve Visibility into Local Orchestration

> Issue: #541 | Phase: refine

## Problem Statement

The orchestrator currently executes pipelines with multiple phases and multi-agent waves, but provides limited visibility into execution progress. When a human initiates a workflow from a collaborator egg instance, they have no real-time view of:

1. The overall DAG structure being executed
2. Which phases have completed, are running, or are pending
3. The multi-agent waves within the implement phase
4. Review cycles per phase
5. Current execution status in a visual format

The desired outcome is an ASCII visualization of the DAG that:
- Shows the overall workflow structure
- Displays the number of review cycles per phase
- Reports agent execution status within phases
- Provides real-time updates to the collaborator instance

## Current Behavior

### Pipeline Structure (`orchestrator/models.py:181-213`)

The pipeline model tracks:
- `current_phase`: One of REFINE, PLAN, IMPLEMENT, PR
- `phases`: Dict of PhaseExecution objects with status, review_cycles, containers, agents
- `status`: Overall pipeline status (pending, running, awaiting_human, complete, failed, cancelled)

### Phase Execution (`orchestrator/models.py:142-159`)

Each phase tracks:
- `status`: Phase-level status
- `review_cycles`: Count of internal review iterations
- `containers`: List of spawned containers
- `agents`: List of AgentExecution objects (for implement phase)

### Multi-Agent Execution (`orchestrator/multi_agent.py:45-76`)

The `AgentWave` class tracks parallel agent execution:
- `wave_number`: Sequential wave identifier
- `agents`: List of AgentRole in this wave
- `containers`: Mapping of role to ContainerInfo
- `results`: Mapping of role to AgentExecution

### Dependency Graph (`shared/egg_contracts/dependency_graph.py:50-111`)

The DAG is defined by agent dependencies:
```
Wave 1: CODER (no dependencies)
Wave 2: TESTER, DOCUMENTER (depend on CODER, can run in parallel)
Wave 3: INTEGRATOR (depends on CODER and TESTER)
```

### Existing Status Reporting

1. **REST API** (`orchestrator/routes/pipelines.py:489-546`): `/api/v1/pipelines/{id}/status` returns JSON status
2. **Event System** (`orchestrator/events.py`): Pub/sub EventBus with history
3. **Metrics** (`orchestrator/metrics.py`): Prometheus-style counters and gauges

### Collaborator Communication

Currently, the collaborator must poll the status endpoint. There is no push-based notification or streaming mechanism for real-time updates.

## Constraints

### Technical Constraints
- ASCII visualization must be terminal-compatible (no Unicode box-drawing characters on some terminals)
- Real-time updates require either polling, SSE, or WebSocket support
- The orchestrator runs as a Flask service; adding WebSocket requires additional dependencies
- Must work across the gateway network topology (collaborator -> orchestrator)

### Dependencies
- `shared/egg_contracts/dependency_graph.py`: Defines the agent dependency DAG
- `orchestrator/events.py`: Provides event pub/sub that could be extended
- `orchestrator/routes/pipelines.py`: Status endpoint that collaborators query

### Scope
- Phase 1: Generate ASCII visualization
- Phase 2: Report to collaborator (real-time or polling)

## Options Considered

### Option A: ASCII Renderer with Polling

**Approach**: Create a dedicated ASCII renderer module that generates DAG visualizations from pipeline state. The collaborator polls the status endpoint which includes the rendered ASCII in the response.

**Implementation**:
1. New module `orchestrator/dag_visualizer.py`:
   - `render_pipeline_dag(pipeline) -> str`: Renders the overall phase DAG
   - `render_agent_dag(phase_execution) -> str`: Renders multi-agent waves
   - `render_combined(pipeline) -> str`: Full visualization with both
2. Extend `/api/v1/pipelines/{id}/status` to include `dag_visualization` field
3. Collaborator polls at configurable interval (e.g., 5-10 seconds)

**ASCII Format Example**:
```
Pipeline: issue-541 [RUNNING]
============================================================

Phase DAG:
  [x] REFINE (2 cycles) ----+
                            |
                            v
  [x] PLAN (1 cycle) -------+
                            |
                            v
  [>] IMPLEMENT ------------+
      |                     |
      |  Wave 1:            |
      |    [x] CODER        |
      |                     |
      |  Wave 2:            |
      |    [>] TESTER       |
      |    [>] DOCUMENTER   |
      |                     |
      |  Wave 3:            |
      |    [ ] INTEGRATOR   |
      |                     |
                            v
  [ ] PR -------------------+

Legend: [x] complete  [>] running  [ ] pending  [!] failed
```

**Pros**:
- Simple implementation with no new dependencies
- Works with existing REST infrastructure
- Easy to test and debug
- Stateless - can regenerate from current state at any time

**Cons**:
- Polling introduces latency (seconds, not real-time)
- Increased API load from frequent polling
- Collaborator must implement polling logic

### Option B: Server-Sent Events (SSE) Stream

**Approach**: Add an SSE endpoint that streams pipeline state changes to connected collaborators. Visualization is generated server-side and pushed on each state change.

**Implementation**:
1. New module `orchestrator/dag_visualizer.py` (same as Option A)
2. New route `GET /api/v1/pipelines/{id}/stream`:
   - Returns `text/event-stream` content type
   - Subscribes to EventBus for pipeline events
   - Sends ASCII visualization on each state change
3. Collaborator connects once and receives updates

**Pros**:
- True real-time updates (sub-second latency)
- Efficient - no wasted polling requests
- Uses HTTP/1.1 (no WebSocket complexity)
- Flask supports SSE natively via generators

**Cons**:
- Requires long-lived connections (resource management)
- May have issues with some proxies/load balancers
- Collaborator must handle connection lifecycle
- More complex error handling for disconnections

### Option C: File-Based Visualization with Watcher

**Approach**: The orchestrator writes ASCII visualization to a file in `.egg-state/`. The collaborator (if on same filesystem) watches the file for changes.

**Implementation**:
1. New module `orchestrator/dag_visualizer.py`
2. Hook into EventBus to regenerate visualization on state changes
3. Write to `.egg-state/pipeline-status/{id}.txt`
4. Collaborator uses filesystem watcher or reads periodically

**Pros**:
- Very simple server-side implementation
- No network protocol changes
- Visualization persists and can be inspected manually
- Works well for local development

**Cons**:
- Only works when collaborator has filesystem access to the repo
- Doesn't work for remote orchestrator deployments
- File I/O on every state change
- Requires file watching infrastructure on collaborator

### Option D: Hybrid (REST + Optional SSE)

**Approach**: Implement Option A (polling) as baseline, with Option B (SSE) as an enhancement. Collaborators can choose their preferred method.

**Implementation**:
1. Implement `dag_visualizer.py` (shared by both approaches)
2. Extend status endpoint to include visualization
3. Add SSE endpoint for real-time streaming
4. Collaborator chooses based on capability/preference

**Pros**:
- Maximum flexibility for different deployment scenarios
- Graceful degradation if SSE isn't available
- Incremental implementation possible

**Cons**:
- More code to maintain
- Potential for inconsistencies between approaches
- Higher testing burden

## Recommended Approach

**Option D (Hybrid)** with **Option A as Phase 1** and **Option B as Phase 2**.

**Rationale**:

1. **Start with polling (Option A)**:
   - Lowest implementation risk
   - Immediate value with minimal changes
   - Establishes the visualization format and data model
   - Can be implemented and tested quickly

2. **Add SSE later (Option B)**:
   - Once the visualization is proven, add real-time streaming
   - SSE is well-supported in Flask and browsers
   - Provides the "real-time" experience requested in the issue

3. **Why not Option C**:
   - The collaborator may not have filesystem access (remote orchestrator)
   - File-based approach is less general-purpose

**Implementation Priority**:
1. `dag_visualizer.py` - ASCII rendering logic (shared)
2. Extend status endpoint with `dag_visualization` field
3. Collaborator-side display logic (if needed)
4. (Future) SSE streaming endpoint

## Open Questions

```
egg-contract add-decision --question "Which visualization style should we use?" \
  --options "Box-drawing (Unicode)" "ASCII-only (compatible)" "Both with fallback" --format markdown
```

**Decision: Visualization Style**

The ASCII art can use different character sets:

- [ ] **Box-drawing (Unicode)**: `┌─┬─┐ │ └─┴─┘` - cleaner look, may not render on all terminals
- [ ] **ASCII-only (compatible)**: `+--+ | +--+` - works everywhere, slightly less elegant
- [ ] **Both with fallback**: Auto-detect terminal capability, fallback to ASCII
- [ ] Other (explain in reply)

---

```
egg-contract add-decision --question "Should real-time updates be in initial scope?" \
  --options "Polling only (simpler)" "SSE from start (real-time)" "Polling first, SSE later" --format markdown
```

**Decision: Real-time Updates Scope**

- [ ] **Polling only (simpler)**: Collaborator polls every N seconds, defer SSE to future work
- [ ] **SSE from start (real-time)**: Implement streaming immediately for best UX
- [ ] **Polling first, SSE later**: Ship polling quickly, add SSE as enhancement
- [ ] Other (explain in reply)

---

**Open-ended questions for human input**:

1. What is the expected polling interval if we go with the polling approach? (5s, 10s, configurable?)
2. Should the visualization be shown automatically in the collaborator's terminal, or only on request (e.g., a command to display it)?
3. Are there specific terminal environments we need to support that have character encoding limitations?

---

*Authored-by: egg*
