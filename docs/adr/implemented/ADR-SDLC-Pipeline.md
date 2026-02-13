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
- **Human-in-the-loop**: Critical transitions pause for human approval
- **Audit trail**: All mutations are logged for accountability

---

## Motivation

Autonomous agents operating on codebases require oversight to ensure quality and prevent runaway execution. Behavioral controls (instructions) are insufficient because:

1. **Prompt injection risk**: Agents may be tricked into bypassing instructions
2. **Model drift**: Agent behavior may vary across runs
3. **Infinite loops**: Without human oversight, agents may cycle indefinitely

This architecture implements **structural enforcement**: the agent physically cannot perform certain operations without appropriate role authorization, regardless of its instructions. PR-based reviews provide human visibility at every cycle, preventing runaway execution.

---

## Threat Model

### Threats Addressed

| Threat | Mitigation |
|--------|------------|
| Agent self-approves work | Role-based mutations prevent implementer from marking tasks complete |
| Agent skips review phase | Phase transitions require reviewer or human role |
| Agent loops indefinitely | PR-based reviews provide human visibility at every cycle |
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
│  │  .egg-state/contracts/{identifier}.json                         │    │
│  │  - Current phase                                                │    │
│  │  - Phases with tasks                                            │    │
│  │  - Task status and commits                                      │    │
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
│  │  GET /api/v1/contract/{issue_number}                              │    │
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
| **Reviewer** | Task status, phase status, current_phase | Task commit, notes, decision resolution |
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
  "workflow_owner": "jwbron",
  "audit_log": [...]
}
```

---

## HITL (Human-in-the-Loop) Mechanism

For detailed HITL workflow documentation, see [HITL Decisions](../../hitl-decisions.md).

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

### Phase Approval

In addition to formal HITL decisions, a simpler phase approval mechanism exists for advancing between phases:
- Uses `<!-- egg-phase-approval -->` markers in comments
- Single checkbox: `- [ ] Approve and advance to next phase`
- Detected by the `handle-approval` job in `sdlc-hitl.yml`
- See [Phase Completion Template](../../templates/phase-completion.md) for the comment format

### Token-Gated Approvals

For local interactive sessions, token-gated approvals provide an additional security layer to prevent agents from self-approving phase transitions. This mechanism is available when starting the pipeline with `egg --sdlc <issue>`.

**Architecture:**

1. **Token Generation**: On startup, the entrypoint generates two 3-word human-memorizable tokens (e.g., `APPLE-HORSE-RIVER`) via the orchestrator's `/api/v1/sdlc-tokens/generate` endpoint
2. **Display to Human**: Tokens are displayed in the terminal before the agent launches
3. **Hook Installation**: A UserPromptSubmit hook (`sdlc-approve.sh`) is installed as root-owned (0555) to intercept `!approve <phase>` commands
4. **Token Entry**: When the agent types `!approve refine`, the hook prompts the human via `/dev/tty` for the approval token
5. **Server-Side Validation**: The token is sent to `/api/v1/sdlc-tokens/approve` with SHA-256 hashing and timing-safe comparison
6. **Auto-Resolution**: On successful validation, pending HITL decisions for that phase are auto-resolved

**Security Properties:**

- **Agent Isolation**: Tokens are never visible to the agent (displayed before launch, entered via `/dev/tty`)
- **Tamper Resistance**: Hook script is root-owned; agent runs as `egg` user and cannot modify it
- **Watchdog Protection**: Background thread monitors `settings.json` for hook removal and re-adds if necessary
- **Privileged Endpoint Auth**: `/generate` and `/reset` endpoints require launcher secret authentication, preventing agents from calling them directly
- **Timing-Safe Validation**: SHA-256 hashing with `secrets.compare_digest` prevents timing side-channel attacks

**Failure Recovery:**

If the orchestrator restarts while a pipeline is token-gated, in-memory tokens are lost but the persistent `Pipeline.sdlc_token_gated` flag remains set in storage. The `/api/v1/sdlc-tokens/reset` endpoint (requires launcher secret auth) clears both the in-memory tokens and the persistent flag to allow the pipeline to proceed.

---

## Gateway Integration

### Contract API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/contract/{issue_number}` | GET | Retrieve contract state |
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
  resolve-inputs:
    # Resolve inputs for reusable workflow support

  init:
    # Initialize contract from issue, create branch

  work-loop:
    # Unified work/review/respond cycle for all phases
```

**Note:** As of PR #460, the pipeline was consolidated from separate phase jobs (implement, wait-for-checks, finalize-pr) into a unified `work-loop` job. Code review now happens through PR comments via `reusable-review.yml` (introduced in PR #285) instead of a separate reviewer agent.

### Unified Work Loop Architecture

The pipeline uses a single reusable workflow (`.github/workflows/sdlc-work-loop.yml`) for all SDLC phases. This design:

- **Reduces duplication**: Single workflow handles refine, plan, and implement phases
- **Simplifies maintenance**: Changes to the work/review/respond cycle are made in one place
- **Collocates check scripts**: Phase validation scripts live in `.github/scripts/` alongside workflows
- **Provides check DAG**: Checks run in dependency order (merge-fix → parallel lint/test → fixer → review)

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
| Templates | `docs/templates/analysis.md`, `docs/templates/plan.md`, `docs/templates/phase-completion.md` |
| HITL documentation | `docs/hitl-decisions.md` |
| HITL integration tests | `tests/workflows/test_hitl_integration.py` |

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
- [x] External failure handling
- [x] Integration tests
- [x] Documentation (this ADR + [operational guide](../../guides/sdlc-pipeline.md))

---

## References

- [Issue #133: Structurally Enforced Checkpoints](https://github.com/jwbron/egg/issues/133)
- [SDLC Pipeline Operational Guide](../../guides/sdlc-pipeline.md)

---

*This document describes the SDLC pipeline architecture for the egg autonomous agent system. For questions or contributions, see the project repository.*
