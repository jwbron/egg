# Analysis: Update Orchestration DAG Visualization Context (#596)

## Problem Statement

The DAG visualization in the pipeline watch tool shows unhelpful aggregate
counts for containers and agents without distinguishing their roles or
statuses. From the issue example:

```
│ ▶ Refine                    │
│   running                   │
│   2 container(s), 2 agent(s)│
│   [10m50s]                  │
```

Three specific problems:

1. **Container count is noise.** "2 containers" adds no value alongside
   "2 agents" — containers are implementation details of agent execution.
   Users care about agents, not containers.

2. **No role visibility.** The user cannot see *which* agents are running
   (e.g., coder vs. reviewer). The issue notes "it was actually only one
   agent running, and it was a review agent."

3. **No status distinction.** All agents are counted equally regardless of
   whether they are running, completed, or failed. The issue notes "The
   coder agent had just completed" — but the visualization gave no indication
   of this.

## Current Architecture

### Data Available

The `AgentExecution` model (`orchestrator/models.py:108`) tracks per-agent:
- `role: AgentRole` — coder, reviewer, checker, tester, documenter, integrator
- `status: AgentExecutionStatus` — pending, running, complete, failed
- `started_at`, `completed_at` — timestamps
- `container_id` — link to sandbox container
- `commit`, `error` — output data

The `PhaseExecution` model has both `containers: list[ContainerInfo]` and
`agents: list[AgentExecution]`, so the visualizer already has full access
to per-agent role and status information.

### Current Rendering

In `dag_visualizer.py:117-123`, the phase box info line is:

```python
if containers_count > 0 or agents_count > 0:
    info_parts = []
    if containers_count > 0:
        info_parts.append(f"{containers_count} container(s)")
    if agents_count > 0:
        info_parts.append(f"{agents_count} agent(s)")
    info_line = "   " + ", ".join(info_parts)
```

This simply counts totals with no breakdown. The `_render_phase_box` function
receives only integer counts (`containers_count`, `agents_count`), not the
actual agent/container objects — so it cannot currently render per-agent
details.

### Detailed View Already Exists

`render_phase_detail()` (line 249) already renders per-agent role and status
with symbols, and per-container role details. However, this detailed view is
only used in the phase detail API — it is never shown in the main DAG
visualization.

### Downstream Consumers

The DAG visualization string is consumed by:
1. **SSE stream** (`sse.py:213`) — embedded in event payloads
2. **CLI watch tool** (`sandbox/bin/egg-pipeline-watch`) — renders DAG in terminal
3. **Status report API** (`dag_visualizer.py:416`) — returned in JSON responses
4. **Status reporter** (`status_reporter.py`) — dispatches to handlers

All consumers receive the pre-rendered string, so changes to `_render_phase_box`
and `render_pipeline_dag` propagate everywhere automatically.

## Constraints

- Box width is dynamic (auto-calculated from content) — adding longer lines
  will widen the box, which is acceptable
- ASCII mode must be supported (`use_ascii=True`)
- The compact status line (`render_compact_status`) is a separate function and
  may or may not need updating (it already omits agent info)
- Existing tests in `test_dag_visualizer.py` assert on "2 container(s)" — these
  will need to be updated

## Implementation Approaches

### Approach A: Per-Agent Role+Status Lines (Recommended)

Replace the single aggregate count line with per-agent lines showing role and
status symbol. Drop container count entirely.

**Before:**
```
╔═════════════════════════════╗
│ ▶ Refine                    │
│   running                   │
│   2 container(s), 2 agent(s)│
│   [10m50s]                  │
╚═════════════════════════════╝
```

**After:**
```
╔═════════════════════════════╗
│ ▶ Refine                    │
│   running                   │
│   ✓ coder  ▶ reviewer       │
│   [10m50s]                  │
╚═════════════════════════════╝
```

Each agent is shown as `{status_symbol} {role}`. Multiple agents are
space-separated on one line. If there are many agents (4+), they wrap to
additional lines.

**Pros:**
- Directly addresses all three complaints
- Minimal vertical space — fits on one line for typical 2-agent phases
- Uses existing status symbols for visual consistency
- Clean and scannable

**Cons:**
- Slightly wider boxes when agent role names are long
- Need to handle edge case of many agents (unlikely in practice — max is
  typically 2-3 per phase)

**Changes required:**
- `_render_phase_box()`: Accept `agents: list[AgentExecution]` instead of
  `containers_count`/`agents_count` integers. Render per-agent role+status.
- `render_pipeline_dag()`: Pass full `phase_exec.agents` list to the box
  renderer.
- Tests: Update assertions from "N container(s)" to role-based assertions.

### Approach B: Summary with Counts by Status

Keep a summary line but break down by status instead of by type.

**After:**
```
╔════════════════════════════════╗
│ ▶ Refine                       │
│   running                      │
│   1 running, 1 complete agent  │
│   [10m50s]                     │
╚════════════════════════════════╝
```

**Pros:**
- Less visual change from current format
- Still removes unhelpful container count

**Cons:**
- Doesn't show which roles are running/complete
- Still aggregates — misses the "reviewer is running, coder completed" insight

### Approach C: Multi-Line Agent Detail Block

Show each agent on its own line within the box.

**After:**
```
╔═══════════════════════════════╗
│ ▶ Refine                      │
│   running                     │
│   ✓ coder       [3m20s]      │
│   ▶ reviewer    [1m05s]      │
│   [10m50s]                    │
╚═══════════════════════════════╝
```

**Pros:**
- Maximum detail including per-agent duration
- Very clear which agent is doing what

**Cons:**
- Increases box height significantly for multi-agent phases
- Makes the overall DAG much taller
- Per-agent duration may be excessive for a summary view

## Recommendation

**Approach A** is recommended. It solves all three stated problems with minimal
visual disruption. The inline `{symbol} {role}` format is compact, scannable,
and leverages the existing status symbol vocabulary that users already
understand from the phase-level status.

Key implementation details:
- Pass `agents: list[AgentExecution]` to `_render_phase_box()` instead of
  integer counts
- Remove `containers_count` parameter entirely — container info stays
  available in the detailed phase view (`render_phase_detail`) for users who
  need it
- Render agents sorted by role enum order (coder first, then reviewer, etc.)
  for consistency
- For phases with no agents (e.g., pending phases), show nothing (same as
  current behavior when counts are 0)
- Handle ASCII mode by using ASCII status symbols
- Keep `render_phase_detail()` unchanged — it already shows full container and
  agent details for the drill-down use case

## Files to Modify

| File | Change |
|------|--------|
| `orchestrator/dag_visualizer.py` | Update `_render_phase_box` signature and rendering; update `render_pipeline_dag` to pass agent list; update `generate_status_report` to include role-level breakdown |
| `orchestrator/tests/test_dag_visualizer.py` | Update assertions for new agent display format |

No changes needed in `sse.py`, `status_reporter.py`, `models.py`, or
`egg-pipeline-watch` — they all consume the pre-rendered DAG string.

## Testing Strategy

1. Update existing test `test_phase_with_containers` to verify role+status
   rendering
2. Add test for multi-agent display with mixed statuses (e.g., coder complete,
   reviewer running)
3. Add test for phases with no agents (pending phases)
4. Add test for ASCII mode agent rendering
5. Verify `render_phase_detail` behavior is unchanged
6. Run full test suite: `PYTHONPATH=orchestrator:shared pytest orchestrator/tests/test_dag_visualizer.py -v`
