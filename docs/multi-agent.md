# Multi-Agent Orchestration

Multi-agent orchestration enables parallel execution of specialized agents
during the implement and plan phases of the SDLC pipeline.

## Overview

When enabled, each phase runs agents in dependency-ordered waves rather
than using a single CODER agent. This allows specialized work (testing,
documentation, architecture analysis) to happen in parallel where
dependencies allow.

## Agent Roles

### Implement Phase

| Wave | Agents | Description |
|------|--------|-------------|
| 1 | CODER | Implements code changes based on plan tasks |
| 2 | TESTER, DOCUMENTER | Tests and docs run in parallel after code is written |
| 3 | INTEGRATOR | Validates all changes work together |

### Plan Phase

| Wave | Agents | Description |
|------|--------|-------------|
| 1 | ARCHITECT | Designs system architecture |
| 2 | TASK_PLANNER, RISK_ANALYST | Task breakdown and risk assessment in parallel |

### Reviewer Roles (Unified Orchestrator)

Reviewers execute as the final wave after all worker agents complete:

- **REVIEWER_UNIFIED**: Overall quality and correctness review
- **REVIEWER_CODE**: Security, correctness, and robustness review
- **REVIEWER_CONTRACT**: Contract compliance verification
- **REVIEWER_AGENT_DESIGN**: Agent-mode design principles review

## Configuration

### Pipeline Config

In the pipeline configuration (orchestrator):

```python
PipelineConfig(
    multi_agent=True,           # Enable multi-agent (default: False)
    max_parallel_agents=10,     # Max agents per wave (default: 10)
)
```

### Contract Config

In the SDLC contract:

```json
{
  "multi_agent_config": {
    "enabled": true,
    "max_retries": 2,
    "parallel_execution": true,
    "max_parallel_agents": 10,
    "roles_enabled": ["coder", "tester", "documenter", "integrator"],
    "phase_overrides": {
      "plan": false
    }
  }
}
```

### Per-Phase Overrides

Use `phase_overrides` to enable/disable multi-agent per phase:

```json
{
  "phase_overrides": {
    "implement": true,
    "plan": false
  }
}
```

## Environment Variables

Each agent container receives:

| Variable | Description |
|----------|-------------|
| `EGG_AGENT_ROLE` | Agent role (e.g., `coder`, `tester`, `architect`) |
| `EGG_HANDOFF_DATA` | JSON with outputs from dependency agents |
| `EGG_WAVE_NUMBER` | Current wave number (1-indexed) |

## Handoff Data

Agents pass data to downstream agents via handoff files stored in
`.egg-state/agent-outputs/{role}-output.json`. The orchestrator collects
outputs from dependency agents and injects them as `EGG_HANDOFF_DATA`.

## Conflict Resolution

When parallel agents modify the same files, the orchestrator uses a
merge-attempt strategy:

1. Each agent in a parallel wave works on the shared worktree
2. If agents modify different files, changes coexist naturally
3. If agents modify the same files, git merge is attempted
4. On merge conflict, the wave fails with a list of conflicting files

## Retry Logic

- Agents retry up to `max_retries` (default 2) on transient failures
- Conflict failures (merge conflicts) are not retried
- Container spawn failures are not retried

## Revision Cycles

When reviewers produce a `needs_revision` verdict:

1. The orchestrator resets worker agents to PENDING
2. A new wave sequence starts with review feedback
3. This repeats up to `max_review_cycles` (default 3)
4. After max cycles, the phase advances (circuit breaker)

## Backward Compatibility

- Without `multi_agent: true`, phases use the existing single-agent behavior
- All schema changes are additive with sensible defaults
- Old contracts work without any multi-agent fields
