# SDLC Pipeline Architecture

The SDLC (Software Development Lifecycle) Pipeline provides structurally enforced agent checkpoints and verification gates for autonomous software development.

> **Note:** The GitHub Actions-based SDLC workflow described historically has been superseded by the local distributed orchestrator (`orchestrator/` package). The architectural principles, contract system, role-based access control, and HITL mechanisms remain valid — only the execution layer changed.

For operational details, CLI commands, and triggering instructions, see the [SDLC Pipeline Guide](../guides/sdlc-pipeline.md).

**The core guarantee**: An agent cannot bypass verification gates or self-approve its own work. All state transitions are enforced structurally through role-based mutations and gateway policy enforcement.

**Key properties:**
- **Phased execution**: Work progresses through defined phases (refine → plan → implement → pr)
- **Role-based control**: Implementer, Reviewer, and Human roles have distinct permissions
- **Human-in-the-loop**: Critical transitions pause for human approval
- **Audit trail**: All mutations are logged for accountability

## Motivation

Autonomous agents operating on codebases require oversight. Behavioral controls (instructions) are insufficient because:
1. **Prompt injection risk**: Agents may be tricked into bypassing instructions
2. **Model drift**: Agent behavior may vary across runs
3. **Infinite loops**: Without human oversight, agents may cycle indefinitely

This architecture implements **structural enforcement**: the agent physically cannot perform certain operations without appropriate role authorization, regardless of its instructions.

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Agent self-approves work | Role-based mutations prevent implementer from marking tasks complete |
| Agent skips review phase | Phase transitions require reviewer or human role |
| Agent loops indefinitely | PR-based reviews provide human visibility at every cycle |
| Agent modifies own permissions | Role comes from workflow context, not agent environment |
| Changes lack accountability | Audit log tracks all mutations with role and actor |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            SDLC Pipeline                                │
│                                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐  │
│  │   Refine    │───▶│    Plan     │───▶│  Implement  │───▶│    PR    │  │
│  │  (Human)    │    │  (Human)    │    │ (Reviewer)  │    │ (Human)  │  │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘  │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Contract State                               │    │
│  │  .egg-state/contracts/{identifier}.json                         │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
└──────────────────────────────│──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Gateway Sidecar                                  │
│  Contract API → Role Enforcement → Phase Filter                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### Pipeline Phases

| Phase | Purpose | Exit Requires |
|-------|---------|---------------|
| **refine** | Problem analysis and requirements gathering | Human approval |
| **plan** | Implementation planning with task breakdown | Human approval |
| **implement** | Task execution and code changes | All checks pass (CI + PR review) |
| **pr** | Pull request creation and merge | Human merge |

### Role Permissions

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| **Implementer** | Task commit, notes, files_affected | Task status, phase status, current_phase |
| **Reviewer** | Task status, phase status, current_phase | Task commit, notes, decision resolution |
| **Human** | All fields | — |
| **System** | Initial contract creation | Owned fields after creation |

## Contract Schema

The contract is a JSON document tracking the complete state of an issue through the pipeline:

```json
{
  "schemaVersion": "1.0",
  "issue": { "number": 133, "title": "...", "url": "..." },
  "current_phase": "implement",
  "phases": [{
    "id": "phase-1",
    "name": "Core Library",
    "status": "in_progress",
    "tasks": [{
      "id": "task-1-1",
      "description": "Create contract schema",
      "status": "complete",
      "commit": "abc1234",
      "review_cycles": 1
    }]
  }],
  "workflow_owner": "jwbron",
  "audit_log": [...]
}
```

## HITL (Human-in-the-Loop) Mechanism

For detailed HITL workflow documentation, see [HITL Decisions](../hitl-decisions.md).

When escalation occurs, the system generates a decision block with checkboxes for human input. A 30-second debounce prevents accidental clicks.

Phase approval uses `<!-- egg-phase-approval -->` markers with a single approval checkbox, detected by the orchestrator's decision queue.

## Gateway Integration

### Contract API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/contract/{issue_number}` | GET | Retrieve contract state |
| `/api/v1/contract/mutate` | POST | Apply mutation with role enforcement |
| `/api/v1/contract/validate` | POST | Validate mutation without applying |
| `/api/v1/phase/advance` | POST | Advance to next phase |
| `/api/v1/phase/filter` | POST | Check if operation is allowed |

Role is determined from session metadata (set by launcher), not from the agent environment.

## Orchestrator Integration

The local distributed orchestrator (`orchestrator/` package) manages the full lifecycle:

- `orchestrator/dispatch.py` — Phase dispatch and management
- `orchestrator/container_spawner.py` — Agent container lifecycle
- `orchestrator/decision_queue.py` — HITL decision handling
- `orchestrator/state_store.py` — Git-backed pipeline state

## Security Properties

1. **Role isolation**: Agent cannot escalate its own role
2. **Mutation validation**: Every mutation is checked against role permissions
3. **Audit trail**: All changes are logged with actor and role
4. **Phase enforcement**: Operations are filtered based on current phase
5. **Human gates**: Critical transitions require human approval

## Files and Locations

| Component | Location |
|-----------|----------|
| Contract schema | `.egg/schemas/contract.schema.json` |
| Contract library | `shared/egg_contracts/` |
| Gateway endpoints | `gateway/contract_api.py`, `gateway/phase_api.py` |
| Orchestrator | `orchestrator/` |
| CLI tools | `sandbox/egg_lib/contract_cli.py` |
| HITL documentation | `docs/hitl-decisions.md` |

## Related Documentation

- [SDLC Pipeline Operational Guide](../guides/sdlc-pipeline.md) — Day-to-day usage
- [The Agentic Feedback Loop](agentic-feedback-loop.md) — Foundational work-review cycle
- [Architecture Overview](README.md) — System design
