# Issue #133: Implementation Plan

> Implementation plan for Structurally Enforced Agent Checkpoints and Verification Gates.
> See [133-structurally-enforced-checkpoints.md](./133-structurally-enforced-checkpoints.md) for the full specification.

---

## Phase 1: Contract Schema and Core Library

### 1.1 Contract JSON Schema

**Create** `.egg/schemas/contract.schema.json`:
- Define schema for phases, tasks, decisions, audit_log
- Add `schemaVersion` field for migrations
- Add `x-role-owner` annotations for role-based access
- Add validation constraints (required fields, enums, patterns)

### 1.2 Contract Library

**Create** `shared/egg_contracts/`:

| File | Purpose |
|------|---------|
| `__init__.py` | Package exports |
| `models.py` | Pydantic models matching JSON schema |
| `loader.py` | Load/save contract from `.egg/contracts/{issue}.json` |
| `roles.py` | Role enum and field ownership mapping |
| `validator.py` | Validate mutations against role permissions |
| `audit.py` | Audit log entry creation |

> **Note**: The `.egg/contracts/` directory is created per-branch during pipeline initialization. Contracts are committed to the feature branch (`egg-{issue}`) and not to `main`. The directory is created by the `init` job in the SDLC pipeline workflow.

### 1.3 Gateway Contract Endpoint

**Add to** `gateway/gateway.py`:
- `POST /api/v1/contract/mutate` - Validate role and apply mutation
- `GET /api/v1/contract/{issue}` - Retrieve contract state
- Read role from GitHub Actions workflow context (not agent env vars)

### 1.4 Unit Tests

**Create** `tests/unit/egg_contracts/`:
- `test_models.py` - Pydantic model validation
- `test_roles.py` - Role ownership enforcement
- `test_validator.py` - Mutation validation logic

---

## Phase 2: Phase-Based Operation Restrictions

### 2.1 Phase Permissions Schema

**Create** `.egg/schemas/phase-permissions.schema.json`:

| Phase | Allowed | Blocked | Exit Requires |
|-------|---------|---------|---------------|
| refine | `gh issue comment/edit` | `git push`, `gh pr create` | Human approval |
| plan | `gh issue comment/edit`, `egg-contract add-decision` | `git push`, `gh pr create` | Human approval |
| implement | `git push`, `egg-contract add-commit/mark-task` | `gh pr create` | Reviewer approval |
| pr | `gh pr create/edit`, `git push` | — | Human merge |

### 2.2 Gateway Phase Enforcement

**Create** `gateway/phase_filter.py`:
- Read current phase from contract state
- Filter operations against phase permissions
- Return structured error messages for blocked operations
- Log blocked operations to audit trail

### 2.3 Phase Transition Logic

**Create** `gateway/phase_transition.py`:
- `POST /api/v1/phase/advance` endpoint
- Validate caller role matches `exit_requires` constraint
- Update contract phase state
- Emit audit log entry

### 2.4 Unit Tests

**Create** `tests/unit/gateway/`:
- `test_phase_filter.py` - Operations blocked/allowed per phase
- `test_phase_transition.py` - Transition authorization
- `test_phase_error_messages.py` - Error message validation

---

## Phase 3: Agent CLI and Prompt Integration

> **Note**: The `sandbox/` directory is the agent's working environment within the container. Files placed here are available to the agent at runtime. This directory is created during container initialization if it doesn't exist.

### 3.1 Contract CLI

**Create** `sandbox/egg_lib/contract_cli.py`:

| Command | Purpose |
|---------|---------|
| `egg-contract show` | Display current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract mark-task --task <id> --status <status>` | Mark task status (reviewer only) |
| `egg-contract mark-phase --phase <id> --passed <bool>` | Mark phase status (reviewer only) |
| `egg-contract add-decision --question <text>` | Create HITL decision point |

All mutations route through gateway endpoint for role enforcement.

### 3.2 Agent Rules

**Create** `sandbox/.claude/rules/contract.md`:
- Brief guidance on contract CLI usage
- Track progress via `egg-contract` commands

### 3.3 Document Templates

**Create** `docs/templates/analysis.md`:
- Standard template for refine phase output
- Sections: Problem Statement, Current Behavior, Constraints, Options Considered, Recommended Approach, Open Questions

**Create** `docs/templates/plan.md`:
- Standard template for plan phase output
- Sections: Summary, Implementation Phases (with Goal, Tasks, Dependencies, Exit criteria), Test Strategy, Rollback/Risk, Migration

### 3.4 Prompt Builder

**Create** `action/build-sdlc-prompt.sh`:
- Provide orientation context (issue number, current phase, branch)
- Tell agent to use `egg-contract` CLI for state updates
- Include phase-specific document template in prompt
- For refine phase: instruct agent to follow analysis template
- For plan phase: instruct agent to follow plan template

### 3.5 Plan-to-Contract Extraction

**Add to** `shared/egg_contracts/`:
- `plan_parser.py` - Parse plan document to extract phases and tasks
- Extract task IDs, descriptions, acceptance criteria, and file paths from plan markdown
- Write extracted tasks to contract `phases[].tasks[]` on plan approval

---

## Phase 4: Pipeline Workflow

### 4.1 Main Pipeline Workflow

**Create** `.github/workflows/sdlc-pipeline.yml`:

**Triggers**:
- `workflow_dispatch` with issue number
- `issues.labeled` when `egg-sdlc` label added

**Jobs**:
- `init`: Initialize contract from issue, create branch
- `implement`: Execute all tasks in plan sequentially
- `review`: Review implementation, kick incomplete tasks back to implementer
- `loop`: Repeat implement→review cycle until all tasks pass or timeout threshold reached

**Reviewer Kick-Back Pattern**:
- Reviewer evaluates each task against acceptance criteria
- Incomplete tasks are marked with feedback and returned to implementer
- Implementer receives reviewer feedback and addresses issues
- Cycle continues until reviewer marks all tasks complete
- Human review only triggered if single task exceeds cycle threshold

### 4.2 HITL Decision Workflow

**Create** `.github/workflows/sdlc-hitl.yml`:

**Trigger**: `issue_comment.edited`

**Logic**:
- Parse checkbox state changes
- 30-second debounce
- Update contract via gateway (human role)
- Trigger pipeline resume

### 4.3 Contract State Management

**Create** `action/contract-state.sh`:
- Load contract from branch
- Determine current phase/task
- Track implement→review cycle count per task
- Commit contract updates after agent run

---

## Phase 5: Circuit Breaker and Escalation

### 5.1 Circuit Breaker Logic

**Create** `shared/egg_contracts/circuit_breaker.py`:

**Thresholds**:
- Per-task cycles: 3 (implement→review→kick-back counts as one cycle)
- Total pipeline cycles: 10
- Single task timeout: triggers human review

**State transitions**:
- CLOSED → OPEN: Per-task threshold exceeded
- OPEN: Human review required for stuck task
- OPEN → CLOSED: Human provides guidance, cycle resumes

### 5.2 Escalation Script

**Create** `action/escalate.sh`:
- Label issue with `needs-human-intervention`
- Post context comment with task history and reviewer feedback
- Create HITL decision checkboxes for stuck task

### 5.3 HITL Checkbox Handling

**Create** `shared/egg_contracts/hitl.py`:
- Generate markdown checkbox block
- Parse checkbox state from comment body
- Handle debounce timing

---

## Phase 6: Integration and Testing

### 6.1 Integration Tests

**Create** `integration_tests/sdlc/`:
- `test_happy_path.py` - Full pipeline success
- `test_review_rejection.py` - Review fails, implements fixes
- `test_circuit_breaker.py` - Escalation triggers correctly
- `test_hitl_flow.py` - Human decision pauses and resumes
- `test_role_enforcement.py` - Gateway blocks unauthorized mutations

### 6.2 Documentation

**Create**:
- `docs/adr/ADR-SDLC-Pipeline.md` - Architecture decision record
- Update `docs/index.md` with SDLC pipeline links

---

## Success Criteria

1. Unit tests pass for contract library with role enforcement
2. Integration tests pass for gateway mutation validation
3. E2E test passes for full SDLC pipeline
4. Manual verification of HITL checkbox flow
5. Documentation complete with ADR
6. Document templates produce consistent analysis and plan outputs
7. Plan parser correctly extracts tasks into contract JSON

---

*Authored-by: egg*
