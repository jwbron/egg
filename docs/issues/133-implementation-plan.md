# Issue #133: Implementation Plan (Revised)

> Detailed implementation plan for Structurally Enforced Agent Checkpoints and Verification Gates.
> See [133-structurally-enforced-checkpoints.md](./133-structurally-enforced-checkpoints.md) for the full specification.

---

## Motivation: Incident #202

This plan directly addresses the incident documented in [issue #202](https://github.com/jwbron/egg/issues/202):

**What happened**: In issue #200, the agent was asked to "put together an analysis doc" but instead immediately implemented a full solution and opened a PR. The planning phase was bypassed entirely.

**Root cause**: No infrastructure-enforced planning phase. CLAUDE.md describes a workflow, but nothing in the gateway prevents skipping stages. Per agent-mode-design.md: "Prompt-level instructions aren't security controls—agents can ignore them."

**Solution**: This SDLC pipeline enforces phase-based restrictions at the gateway level:

| Phase | Allowed Operations | Blocked Operations |
|-------|-------------------|-------------------|
| **Refine** | `gh issue comment`, `gh issue edit` | `git push`, `gh pr create` |
| **Plan** | `gh issue comment`, `gh issue edit` | `git push`, `gh pr create` |
| **Implement** | `git push`, `egg-contract update` | `gh pr create` (until phase complete) |
| **PR** | `gh pr create`, `gh pr edit` | (standard gateway restrictions) |

Each phase must be explicitly approved before the agent gains access to the next phase's operations. This prevents the #202 incident by making it technically impossible to push code during the planning phase.

---

## Context: What Already Exists

This plan has been revised based on the current state of `main` (as of 2024-02). The codebase now includes substantial reviewer infrastructure:

### Existing Reviewer System

| Component | Location | Description |
|-----------|----------|-------------|
| PR review workflow | `.github/workflows/on-pull-request.yml` | Automated code review on PRs |
| Review prompt builder | `action/build-review-prompt.sh` | Minimal prompt telling agent to use `gh pr diff` |
| Review conventions | `action/review-conventions.md` | Guidelines for `gh pr review` usage |
| Autofixer workflow | `.github/workflows/on-check-failure.yml` | Auto-fix failing CI checks |
| Mention handler | `.github/workflows/on-mention.yml` | Respond to @mentions |

### Design Guidelines

Per `docs/guides/agent-mode-design.md`, the existing infrastructure follows these principles:

1. **Agent-mode over structured output**: Agents take action directly (post reviews, push code) rather than outputting JSON for post-processing
2. **Minimal prompts**: Tell agent *what* to do, not *how*—let it fetch its own context
3. **Sandbox is the constraint**: Security enforced at infrastructure level (gateway sidecar), not prompt-level instructions

### What This Changes

The original plan proposed parallel infrastructure (separate reviewer agent, structured verdict output, new CLI). **This revised plan integrates with existing systems instead.**

Key changes:
- **Remove**: Separate reviewer workflow (use existing `on-pull-request.yml` patterns)
- **Remove**: Structured verdict output parsing (agents take action directly)
- **Keep**: Contract schema and role-based field ownership
- **Keep**: Circuit breaker and HITL decision system
- **Adapt**: Use existing prompt builder patterns

---

## Revised Architecture

The SDLC pipeline will extend the existing reviewer infrastructure rather than replace it:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           SDLC PIPELINE                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐ │
│  │   REFINE    │───▶│    PLAN     │───▶│  IMPLEMENT  │───▶│ CREATE   │ │
│  │   ISSUE     │    │             │    │  (per phase)│    │   PR     │ │
│  └─────────────┘    └─────────────┘    └─────────────┘    └──────────┘ │
│        │                  │                  │                  │       │
│        ▼                  ▼                  ▼                  ▼       │
│   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐ │
│   │ REVIEW  │        │ REVIEW  │        │ REVIEW  │        │  HUMAN  │ │
│   │  (egg)  │        │ (HITL?) │        │  (egg)  │        │  MERGE  │ │
│   └─────────┘        └─────────┘        └─────────┘        └─────────┘ │
│                                                                          │
│   Contract tracks state; existing on-pull-request.yml handles reviews   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Integration Approach

1. **Contract schema**: New—tracks phases, tasks, decisions, audit log
2. **Role-based enforcement**: New—gateway validates mutations based on workflow context
3. **Phase-based operation filtering**: New—gateway blocks operations not permitted in current phase
4. **Pipeline orchestration**: New—coordinates phases using existing building blocks
5. **Review step**: Reuse—existing `on-pull-request.yml` workflow pattern
6. **Circuit breaker**: New—escalation thresholds and HITL triggers

---

## Phase 1: Contract Schema and Core Library

**Goal**: Establish the contract data model with role-based enforcement at the gateway level.

### 1.1 Contract JSON Schema

**File**: `.egg/schemas/contract.schema.json`

**Tasks**:
- [ ] Define JSON Schema for contract structure (phases, tasks, decisions, audit_log)
- [ ] Add `schemaVersion` field for future migrations
- [ ] Include field-level annotations for role ownership (`x-role-owner`)
- [ ] Add validation constraints (required fields, enum values, patterns)

### 1.2 Contract Library

**Directory**: `shared/egg_contracts/`

**Files**:
- [ ] `shared/egg_contracts/__init__.py` - Package exports
- [ ] `shared/egg_contracts/models.py` - Pydantic models matching JSON schema
- [ ] `shared/egg_contracts/loader.py` - Load/save contract from `.egg/contracts/{issue}.json`
- [ ] `shared/egg_contracts/roles.py` - Role enum and field ownership mapping
- [ ] `shared/egg_contracts/validator.py` - Validate mutations against role permissions
- [ ] `shared/egg_contracts/audit.py` - Audit log entry creation

### 1.3 Gateway Contract Endpoint

**File**: `gateway/contract.py` (new module, integrated into existing `gateway/gateway.py`)

**Endpoints**:
- [ ] `POST /api/v1/contract/mutate` - Validate role and apply mutation
- [ ] `GET /api/v1/contract/{issue}` - Retrieve contract state

**Role determination**:
- Read from GitHub Actions workflow context (job metadata), not agent-set env vars
- Validate mutations against allowed fields for that role
- Return structured error if unauthorized

**Note**: The gateway is Python-based (see existing `gateway/gateway.py`). Contract endpoints follow the same patterns.

### 1.4 Unit Tests

**Directory**: `tests/unit/egg_contracts/`

**Test files**:
- [ ] `test_models.py` - Pydantic model validation
- [ ] `test_roles.py` - Role ownership enforcement
- [ ] `test_validator.py` - Mutation validation logic

**Key test cases**:
- Implementer cannot modify `tasks[].status`
- Reviewer cannot modify `tasks[].commit`
- Human can modify all fields
- Audit log captures all mutations

---

## Phase 1.5: Phase-Based Operation Restrictions

**Goal**: Prevent agents from bypassing workflow phases by blocking operations not permitted in the current phase. This directly addresses [issue #202](https://github.com/jwbron/egg/issues/202).

### 1.5.1 Phase Definition Schema

**File**: `.egg/schemas/phase-permissions.schema.json`

Each phase defines which git/gh operations are permitted:

```json
{
  "phases": {
    "refine": {
      "description": "Clarify requirements and refine issue scope",
      "allowed_operations": [
        "gh issue comment",
        "gh issue edit",
        "egg-contract show",
        "egg-contract update-notes"
      ],
      "blocked_operations": [
        "git push",
        "gh pr create",
        "gh pr edit"
      ],
      "exit_requires": "human_approval"
    },
    "plan": {
      "description": "Design implementation approach, post analysis",
      "allowed_operations": [
        "gh issue comment",
        "gh issue edit",
        "egg-contract show",
        "egg-contract update-notes",
        "egg-contract add-decision"
      ],
      "blocked_operations": [
        "git push",
        "gh pr create"
      ],
      "exit_requires": "human_approval"
    },
    "implement": {
      "description": "Write code and tests",
      "allowed_operations": [
        "git push",
        "egg-contract add-commit",
        "egg-contract update-notes",
        "egg-contract mark-task"
      ],
      "blocked_operations": [
        "gh pr create"
      ],
      "exit_requires": "reviewer_approval"
    },
    "pr": {
      "description": "Create and manage pull request",
      "allowed_operations": [
        "gh pr create",
        "gh pr edit",
        "git push"
      ],
      "blocked_operations": [],
      "exit_requires": "human_merge"
    }
  }
}
```

### 1.5.2 Gateway Phase Enforcement

**File**: `gateway/phase_filter.py` (new module)

**Design**: The gateway reads the current phase from the contract and filters operations accordingly:

```python
# Pseudocode for phase enforcement
def filter_operation(operation: str, contract: Contract) -> bool:
    """Return True if operation is allowed in current phase."""
    current_phase = contract.get_current_phase()
    permissions = load_phase_permissions()

    if operation in permissions[current_phase]["blocked_operations"]:
        return False

    # Allow if explicitly permitted or not blocked
    return True
```

**Integration points**:
- [ ] Hook into existing `gateway/gateway.py` request handling
- [ ] Read phase from contract state (not agent-set env vars)
- [ ] Return structured error message explaining why operation is blocked
- [ ] Log blocked operations to audit trail

### 1.5.3 Phase Transition Logic

**File**: `gateway/phase_transition.py` (new module)

**Transitions are controlled by the contract state**:

| Transition | Trigger | Who Can Trigger |
|------------|---------|-----------------|
| refine → plan | Human approves refined issue | Human (checkbox) |
| plan → implement | Human approves plan | Human (checkbox) |
| implement → pr | Reviewer approves implementation | Reviewer |
| pr → done | Human merges PR | Human (GitHub UI) |

**Implementation**:
- [ ] `POST /api/v1/phase/advance` - Attempt phase transition
- [ ] Validate caller role matches `exit_requires` constraint
- [ ] Update contract phase state
- [ ] Emit audit log entry

### 1.5.4 Error Messages

When an agent attempts a blocked operation:

```
Error: Operation 'git push' is not permitted in phase 'plan'.
Current phase allows: gh issue comment, gh issue edit, egg-contract show
To advance to 'implement' phase, human approval is required.
See issue comment for approval checkbox.
```

This explicit error guides the agent to the correct behavior and prevents silent failures.

### 1.5.5 Unit Tests

**Directory**: `tests/unit/gateway/`

**Test cases**:
- [ ] `test_phase_filter.py` - Verify operations blocked/allowed per phase
- [ ] `test_phase_transition.py` - Verify transition authorization
- [ ] `test_phase_error_messages.py` - Verify helpful error messages

**Key scenarios**:
- Agent in "plan" phase tries `git push` → blocked with clear error
- Agent in "implement" phase tries `gh pr create` → blocked until phase complete
- Reviewer tries to advance from "plan" → denied (human required)
- Human advances from "plan" to "implement" → allowed

---

## Phase 2: Agent CLI and Prompt Integration

**Goal**: Give agents a way to interact with contracts, following agent-mode principles.

### 2.1 Contract CLI

**File**: `sandbox/egg_lib/contract_cli.py`

**Design principle**: The CLI is a *tool* the agent uses, not a constraint on agent behavior. Per agent-mode-design.md, the sandbox enforces constraints—the CLI just provides an interface.

**Commands**:
- [ ] `egg-contract show` - Display current contract state
- [ ] `egg-contract add-commit --task <id> --commit <sha>` - Link commit to task
- [ ] `egg-contract update-notes --task <id> --notes <text>` - Add implementation notes
- [ ] `egg-contract mark-task --task <id> --status <status>` - Mark task status
- [ ] `egg-contract mark-phase --phase <id> --passed <bool>` - Mark phase status
- [ ] `egg-contract add-decision --question <text>` - Create HITL decision point

**CLI routing**: All mutations go through the gateway endpoint, which enforces role-based access.

### 2.2 Agent Rules Update

**File**: `sandbox/.claude/rules/contract.md` (new)

**Content**: Brief guidance on contract usage, not prescriptive procedures. Per agent-mode-design.md section 4, specify *what* (track progress in contract, use CLI to update), not *how* (step-by-step instructions).

### 2.3 Prompt Builder Extension

**File**: `action/build-sdlc-prompt.sh` (new)

**Purpose**: Build minimal prompt for SDLC pipeline stages. Follows existing `build-review-prompt.sh` pattern:
- Orientation context only (issue number, current phase, branch)
- Tells agent to use `egg-contract` CLI for state updates
- Agent fetches its own context (issue details, diff, etc.)

---

## Phase 3: Pipeline Workflow

**Goal**: Orchestrate the SDLC pipeline using GitHub Actions, building on existing patterns.

### 3.1 Main Pipeline Workflow

**File**: `.github/workflows/sdlc-pipeline.yml`

**Triggers**:
- [ ] `workflow_dispatch` - Manual trigger with issue number
- [ ] `issues.labeled` - When `egg-sdlc` label is added

**Jobs**:
```yaml
jobs:
  init:
    # Initialize contract from issue, create branch

  work:
    needs: init
    # Single agent invocation per phase
    # Agent decides how to approach the work
    # Uses egg-contract CLI to track progress

  review:
    needs: work
    # Reuses patterns from on-pull-request.yml
    # Agent reviews implementation, updates contract
    # Creates HITL decision if needed
```

**Design note**: Per agent-mode-design.md, avoid pre-fetching data or specifying output formats. The workflow provides orientation context; agents fetch what they need.

### 3.2 HITL Decision Workflow

**File**: `.github/workflows/sdlc-hitl.yml`

**Triggers**:
- [ ] `issue_comment.edited` - Detect checkbox changes

**Logic**:
- [ ] Parse comment for checkbox state changes (follows existing marker pattern from `on-pull-request.yml`)
- [ ] Implement 30-second debounce
- [ ] Update contract via gateway endpoint (human role)
- [ ] Trigger pipeline resume

### 3.3 Contract State Management

**File**: `action/contract-state.sh`

**Functionality**:
- [ ] Load contract from branch
- [ ] Determine current phase/task
- [ ] Commit contract updates after agent run

---

## Phase 4: Circuit Breaker and Escalation

**Goal**: Prevent infinite loops and escalate to humans when needed.

### 4.1 Circuit Breaker Logic

**File**: `shared/egg_contracts/circuit_breaker.py`

**Thresholds** (configurable via `.egg/config.json`):
```python
DEFAULT_THRESHOLDS = {
    "per_phase_cycles": 3,
    "total_cycles": 10,
    "consecutive_failures": 2,
}
```

**State transitions**:
- CLOSED → OPEN: Threshold exceeded
- OPEN → HALF-OPEN: Human intervention
- HALF-OPEN → CLOSED: Next review passes
- HALF-OPEN → OPEN: Next review fails

### 4.2 Escalation Actions

**File**: `action/escalate.sh`

**Actions**:
- [ ] Label issue with `needs-human-intervention`
- [ ] Post context comment with current state and review history
- [ ] Create HITL decision checkboxes

### 4.3 HITL Checkbox Handling

**File**: `shared/egg_contracts/hitl.py`

**Functionality**:
- [ ] Generate markdown checkbox block for decisions
- [ ] Parse checkbox state from comment body
- [ ] Handle debounce timing

---

## Phase 5: Integration and Testing

**Goal**: Validate the full pipeline with tests and documentation.

### 5.1 Integration Tests

**Directory**: `integration_tests/sdlc/`

**Test scenarios**:
- [ ] `test_happy_path.py` - Full pipeline success
- [ ] `test_review_rejection.py` - Review fails, implements fixes
- [ ] `test_circuit_breaker.py` - Escalation triggers correctly
- [ ] `test_hitl_flow.py` - Human decision pauses and resumes
- [ ] `test_role_enforcement.py` - Gateway blocks unauthorized mutations

### 5.2 Documentation

**Files**:
- [ ] `docs/adr/ADR-SDLC-Pipeline.md` - Architecture decision record
- [ ] Update `docs/index.md` - Add SDLC pipeline documentation links

---

## What Was Removed from Original Plan

The following items from the original plan are **no longer needed** because they duplicate existing infrastructure or contradict agent-mode-design principles:

| Original Item | Reason Removed |
|---------------|----------------|
| Separate reviewer workflow (`sdlc-review.yml`) | Existing `on-pull-request.yml` pattern handles reviews |
| Reviewer system prompt | Existing `review-conventions.md` provides guidance |
| Structured verdict output parser | Agents take action directly per agent-mode-design.md |
| Pre-commit hook for contract validation | Hooks are disabled in sidecar due to security concerns ([issue #58](https://github.com/jwbron/egg/issues/58)); tracked in [issue #199](https://github.com/jwbron/egg/issues/199) |
| Stage-specific prompt builders (refine, plan, implement, review) | Single prompt builder with orientation context; agent fetches what it needs |
| Reviewer output parser | No structured output to parse; agent uses `gh pr review` directly |

---

## Implementation Priorities

**Phase 1 is foundational** and should be completed first. The contract schema and gateway enforcement enable all other phases.

**Phases 2-4 can proceed in parallel** once Phase 1 is complete:
- Phase 2 (CLI) enables agent interaction with contracts
- Phase 3 (Workflow) orchestrates the pipeline
- Phase 4 (Circuit breaker) adds safety rails

**Phase 5 validates** the complete system and should be last.

---

## Alignment with Agent-Mode Design

This plan follows the principles in `docs/guides/agent-mode-design.md`:

| Principle | How This Plan Applies It |
|-----------|-------------------------|
| **Pre-fetching is usually wrong** | Prompt builders provide orientation only; agents fetch context |
| **No structured output for human-facing** | Agents post reviews directly via `gh pr review` |
| **No post-processing pipelines** | Agents take action directly; no parsing of agent output |
| **Specify what, not how** | Contract rules define *what* fields can be modified, not *how* to do the work |
| **Sandbox is the constraint** | Gateway enforces role-based access; prompts don't try to enforce behavior |

---

## Success Criteria

Before considering implementation complete:

1. **Unit tests pass** for contract library with role enforcement
2. **Integration tests pass** for gateway mutation validation
3. **E2E test passes** for full SDLC pipeline
4. **Manual verification** of HITL checkbox flow
5. **Documentation** complete with ADR

---

*Authored-by: egg*
