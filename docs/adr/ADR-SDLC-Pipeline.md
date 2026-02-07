# ADR: SDLC Pipeline with Structurally Enforced Agent Checkpoints

**Status**: Proposed

**Context**: Issue #133, PR #184

**Date**: 2026-02-07

## Decision

Implement a structurally enforced SDLC pipeline that prevents agents from bypassing review gates through:

1. **Contract-as-Code**: All pipeline state stored in `.egg/contracts/{issue}.json`
2. **Role-Based Field Access**: Gateway enforces which roles can modify which fields
3. **Phase-Based Operation Filtering**: Gateway blocks operations not allowed in current phase
4. **Reviewer Kick-Back Pattern**: Implementer and reviewer run in separate jobs, with reviewer kicking incomplete tasks back

## Context

### Problem

In incident #202, an agent pushed code during the planning phase, bypassing human review of the plan. Prompt-based instructions to "wait for approval" were insufficient - structural enforcement is required.

### Requirements

1. Agents cannot push code during refine/plan phases
2. Only reviewers can mark tasks complete
3. Only humans can advance between major phases
4. Pipeline must be resilient to agent mistakes (retry with feedback)
5. Human can intervene at any point

## Architecture

### Pipeline Flow

```
REFINE --> PLAN --> IMPLEMENT --> PR --> MERGE (human only)
   |         |          |          |
   v         v          v          v
 REVIEW   REVIEW     REVIEW     HUMAN
 (auto)   (HITL)     (auto)     MERGE
```

### Contract Schema

```json
{
  "schemaVersion": "1.0",
  "issue": { "number": 123, "title": "...", "url": "..." },
  "currentPhase": "implement",
  "phases": [
    {
      "id": "phase-1",
      "tasks": [
        { "id": "task-1", "status": "complete", "commit": "abc1234" }
      ]
    }
  ],
  "decisions": [],
  "circuit_breaker": { "status": "closed", "total_cycles": 0 }
}
```

### Role Enforcement

| Role | Can Modify | Cannot Modify |
|------|------------|---------------|
| Implementer | task.commit, task.notes | task.status, phase.status |
| Reviewer | task.status, task.feedback | task.commit, decisions |
| Human | All fields | (none) |

### Phase Permissions

| Phase | Allowed | Blocked |
|-------|---------|---------|
| Refine | gh issue, git fetch | git push, gh pr create |
| Plan | gh issue, egg-contract add-decision | git push, gh pr create |
| Implement | git push/commit, egg-contract add-commit | gh pr create |
| PR | gh pr create/edit, git push | gh pr merge |

## Components

### Gateway Modules

- `gateway/contract_api.py`: REST endpoints for contract mutations
- `gateway/phase_filter.py`: Phase-based operation filtering
- `gateway/phase_transition.py`: Phase transition validation

### Contract Library

- `shared/egg_contracts/models.py`: Pydantic models
- `shared/egg_contracts/roles.py`: Role-based access control
- `shared/egg_contracts/validator.py`: Mutation validation
- `shared/egg_contracts/circuit_breaker.py`: Escalation logic

### Agent Integration

- `sandbox/egg_lib/contract_cli.py`: CLI for agents
- `sandbox/.claude/rules/contract.md`: Agent instructions

### Workflows

- `.github/workflows/sdlc-pipeline.yml`: Main pipeline
- `.github/workflows/sdlc-review.yml`: Reviewer workflow
- `.github/workflows/sdlc-hitl.yml`: HITL decision handler

## Alternatives Considered

### Pre-commit Hooks

**Rejected**: Pre-commit hooks run locally and can be bypassed. Gateway enforcement is more reliable.

### Single Agent with Prompt Instructions

**Rejected**: Incident #202 showed prompt-based controls are insufficient.

### Database-Backed State

**Rejected**: Contract-in-repo is simpler, auditable, and doesn't require external infrastructure.

## Consequences

### Positive

- Agents cannot bypass review gates (structural enforcement)
- Complete audit trail via contract + git history
- Clear separation of concerns (implementer vs reviewer)
- Resilient to agent mistakes (retry with feedback)
- Human can intervene at any point

### Negative

- Additional complexity in gateway
- New dependency on contract CLI in agent prompts
- Longer pipeline due to separate implement/review jobs

### Risks

- **Gateway becomes SPOF**: Mitigated by gateway health checks and graceful degradation
- **Contract state corruption**: Mitigated by schema validation and audit log
- **Infinite loops**: Mitigated by circuit breaker with max cycles

## Implementation

See the implementation plan at `docs/issues/133-implementation-plan.md` and the full specification at `docs/issues/133-structurally-enforced-checkpoints.md`.

### Phase 1: Contract Schema and Core Library
- JSON schema, Pydantic models, role enforcement

### Phase 2: Phase-Based Operation Restrictions
- Gateway filtering, phase transitions

### Phase 3: Agent CLI and Prompt Integration
- Contract CLI, document templates

### Phase 4: Pipeline Workflow
- GitHub Actions workflows

### Phase 5: Circuit Breaker and Escalation
- Escalation logic, HITL handling

### Phase 6: Integration and Testing
- Integration tests, documentation

## References

- Issue #133: Structurally Enforced Agent Checkpoints
- Issue #202: Agent pushed code during planning (incident)
- Spec: `docs/issues/133-structurally-enforced-checkpoints.md`
- Plan: `docs/issues/133-implementation-plan.md`
