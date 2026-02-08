# ADR: Structurally Enforced SDLC Pipeline for Autonomous Agents

**Status:** Implemented
**Date:** 2026-02-07

---

## Executive Summary

This document describes the architecture for structurally enforced agent checkpoints and verification gates in the egg autonomous software engineering system. The SDLC (Software Development Lifecycle) Pipeline ensures that agents follow a defined workflow with mandatory review points, preventing uncontrolled execution and ensuring human oversight.

For operational details, CLI commands, and triggering instructions, see the [SDLC Pipeline Guide](../../guides/sdlc-pipeline.md).

**The core guarantee**: An agent cannot bypass verification gates or self-approve its own work. All state transitions are enforced structurally through role-based mutations and gateway policy enforcement.

**Key properties:**
- **Phased execution**: Work progresses through defined phases (refine → plan → implement → pr)
- **Role-based control**: Implementer, Reviewer, and Human roles have distinct permissions
- **Automatic escalation**: Circuit breaker triggers human intervention when thresholds are exceeded
- **Audit trail**: All mutations are logged for accountability

---

## Motivation

Autonomous agents operating on codebases require oversight to ensure quality and prevent runaway execution. Behavioral controls (instructions) are insufficient because:

1. **Prompt injection risk**: Agents may be tricked into bypassing instructions
2. **Model drift**: Agent behavior may vary across runs
3. **Infinite loops**: Without circuit breakers, agents may cycle indefinitely

This architecture implements **structural enforcement**: the agent physically cannot perform certain operations without appropriate role authorization, regardless of its instructions.

---

## Threat Model

### Threats Addressed

| Threat | Mitigation |
|--------|------------|
| Agent self-approves work | Role-based mutations prevent implementer from marking tasks complete |
| Agent skips review phase | Phase transitions require reviewer or human role |
| Agent loops indefinitely | Circuit breaker opens after threshold cycles |
| Agent modifies own permissions | Role comes from workflow context, not agent environment |
| Changes lack accountability | Audit log tracks all mutations with role and actor |

### Explicit Non-Goals

This architecture does **not** protect against:
- Compromised gateway sidecar (trusted component)
- Malicious workflow definitions (managed by repository owners)
- Agent producing low-quality code that passes review (quality is reviewer responsibility)

---

## Architecture Overview

### System Components

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
│  │  .egg-state/contracts/{issue}.json                              │    │
│  │  - Current phase                                                │    │
│  │  - Phases with tasks                                            │    │
│  │  - Task status and commits                                      │    │
│  │  - Circuit breaker state                                        │    │
│  │  - Audit log                                                    │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
└──────────────────────────────│──────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Gateway Sidecar                                  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Contract API                                                   │    │
│  │  POST /api/v1/contract/mutate                                   │    │
│  │  GET /api/v1/contract/{issue}                                   │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
│                              ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Role Enforcement                                               │    │
│  │  - Role from workflow context (not agent env)                   │    │
│  │  - Field ownership mapping                                      │    │
│  │  - Mutation validation                                          │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
│                              ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Phase Filter                                                   │    │
│  │  - Operation allowlist per phase                                │    │
│  │  - Block operations not valid for current phase                 │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                         │
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
| **Reviewer** | Task status, phase status, current_phase, acceptance_criteria.verified (deprecated as of PR #285) | Task commit, notes, decision resolution |
| **Human** | All fields | — |
| **System** | Initial contract creation | Owned fields after creation |

---

## Contract Schema

The contract is a JSON document that tracks the complete state of an issue through the SDLC pipeline.

```json
{
  "schemaVersion": "1.0",
  "issue": {
    "number": 133,
    "title": "Add structurally enforced checkpoints",
    "url": "https://github.com/owner/repo/issues/133"
  },
  "current_phase": "implement",
  "phases": [
    {
      "id": "phase-1",
      "name": "Core Library",
      "status": "in_progress",
      "tasks": [
        {
          "id": "task-1-1",
          "description": "Create contract schema",
          "status": "complete",
          "commit": "abc1234",
          "review_cycles": 1
        }
      ]
    }
  ],
  "circuit_breaker": {
    "status": "closed",
    "total_cycles": 3
  },
  "audit_log": [...]
}
```

---

## Circuit Breaker (Deprecated)

**Note:** Circuit breaker functionality is deprecated as of PR #285. The pipeline now relies on PR-based reviews with human-visible feedback at every cycle, reducing the need for automated escalation thresholds.

The circuit breaker was designed to prevent infinite implement→review cycles by triggering human intervention when thresholds were exceeded.

### Legacy Thresholds

| Threshold | Default | Description |
|-----------|---------|-------------|
| Per-task cycles | 3 | Max rejections before task escalates |
| Phase cycles | 3 | Max phase-level review cycles |
| Pipeline total | 10 | Max total cycles across all tasks |

### Legacy State Transitions

```
     ┌──────────────────────────────────────────┐
     │                                          │
     ▼                                          │
┌─────────┐  threshold exceeded   ┌─────────┐   │
│ CLOSED  │ ────────────────────▶ │  OPEN   │   │
└─────────┘                       └─────────┘   │
     ▲                                 │        │
     │                                 │        │
     │     human provides guidance     │        │
     └─────────────────────────────────┘        │
                                                │
     pipeline resumes ──────────────────────────┘
```

This functionality has been replaced by the PR-based review workflow, which provides continuous human visibility without requiring explicit escalation triggers.

---

## HITL (Human-in-the-Loop) Mechanism

When escalation occurs, the system generates a decision block with checkboxes for human input.

### Checkbox Categories

1. **Guidance**: Provide additional context, adjust criteria, break into subtasks
2. **Override**: Mark complete, skip tasks, cancel pipeline
3. **Manual**: Complete manually, reassign

### Debounce

A 30-second debounce prevents accidental clicks:
- Checkbox changes reset the timer
- Decision is processed only after debounce expires
- Comment is updated with countdown status

---

## Gateway Integration

### Contract API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/contract/{issue}` | GET | Retrieve contract state |
| `/api/v1/contract/mutate` | POST | Apply mutation with role enforcement |
| `/api/v1/contract/validate` | POST | Validate mutation without applying |
| `/api/v1/phase/advance` | POST | Advance to next phase |
| `/api/v1/phase/filter` | POST | Check if operation is allowed |

### Role Resolution

Role is determined from workflow context in this priority:
1. Session metadata (set by launcher)
2. X-Egg-Role header (testing only, when enabled)
3. EGG_AGENT_ROLE environment variable (development fallback)

**Security**: Role cannot be set by the agent itself, preventing privilege escalation.

---

## Pipeline Workflow

### GitHub Actions Integration

```yaml
# .github/workflows/sdlc-pipeline.yml
on:
  workflow_dispatch:
    inputs:
      issue_number:
        required: true
  issues:
    types: [labeled]

jobs:
  init:
    # Initialize contract from issue

  implement:
    # Execute tasks with implementer role, push to draft PR

  wait-for-checks:
    # Wait for CI and PR review (reusable-review.yml)

  finalize-pr:
    # Mark PR ready for human merge when all checks pass
```

**Note:** As of PR #285, the dedicated `review` and `loop` jobs were replaced with PR-based review via `reusable-review.yml`. Code review now happens through GitHub PR comments instead of a separate reviewer agent.

### HITL Workflow

```yaml
# .github/workflows/sdlc-hitl.yml
on:
  issue_comment:
    types: [edited]

jobs:
  process_decision:
    # Parse checkbox state
    # Check debounce expiration
    # Update contract and resume pipeline
```

---

## External Failure Handling

### Rate Limiting

The gateway tracks GitHub API rate limits:
- Parse `X-RateLimit-*` headers from responses
- Return `Retry-After` header when rate limited
- Agent implementations should respect retry headers

### Retry with Backoff

Transient failures use exponential backoff:
- Initial delay: 1 second
- Maximum delay: 30 seconds
- Maximum retries: 3

### Timeout Checkpoints

Long-running jobs checkpoint state before timeout:
- Check remaining time periodically
- Checkpoint at T-10 minutes
- Preserve state for resume on next run

---

## Security Properties

1. **Role isolation**: Agent cannot escalate its own role
2. **Mutation validation**: Every mutation is checked against role permissions
3. **Audit trail**: All changes are logged with actor and role
4. **Phase enforcement**: Operations are filtered based on current phase
5. **Human gates**: Critical transitions require human approval

---

## Files and Locations

| Component | Location |
|-----------|----------|
| Contract schema | `.egg/schemas/contract.schema.json` |
| Phase permissions | `.egg/schemas/phase-permissions.schema.json` |
| Contract library | `shared/egg_contracts/` |
| Gateway endpoints | `gateway/contract_api.py`, `gateway/phase_api.py` |
| CLI tools | `sandbox/egg_lib/contract_cli.py` |
| PR review workflow | `.github/workflows/reusable-review.yml` |
| Workflow files | `.github/workflows/sdlc-*.yml` |
| Templates | `docs/templates/analysis.md`, `docs/templates/plan.md` |

---

## Implementation Status

All components have been implemented:

- [x] Contract JSON schema
- [x] Pydantic models for contracts
- [x] Loader for contract persistence
- [x] Role-based field ownership
- [x] Mutation validation
- [x] Audit log generation
- [x] Phase filter logic
- [x] Phase transition logic
- [x] Agent CLI for contracts
- [x] SDLC pipeline workflow
- [x] HITL checkbox handling
- [x] Circuit breaker logic
- [x] External failure handling
- [x] Integration tests
- [x] Documentation (this ADR + [operational guide](../../guides/sdlc-pipeline.md))

---

## References

- [Issue #133: Structurally Enforced Checkpoints](https://github.com/jwbron/egg/issues/133)
- [SDLC Pipeline Operational Guide](../../guides/sdlc-pipeline.md)

---

*This document describes the SDLC pipeline architecture for the egg autonomous agent system. For questions or contributions, see the project repository.*
