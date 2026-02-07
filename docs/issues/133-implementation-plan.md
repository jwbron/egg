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

### 3.3 Prompt Builder

**Create** `action/build-sdlc-prompt.sh`:
- Provide orientation context (issue number, current phase, branch)
- Tell agent to use `egg-contract` CLI for state updates

---

## Phase 4: Pipeline Workflow

### 4.1 Main Pipeline Workflow

**Create** `.github/workflows/sdlc-pipeline.yml`:

**Triggers**:
- `workflow_dispatch` with issue number
- `issues.labeled` when `egg-sdlc` label added

**Jobs**:
- `init`: Initialize contract from issue, create branch
- `work`: Agent invocation per phase, uses `egg-contract` CLI
- `review`: Review implementation, update contract

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
- Commit contract updates after agent run

---

## Phase 5: Circuit Breaker and Escalation

### 5.1 Circuit Breaker Logic

**Create** `shared/egg_contracts/circuit_breaker.py`:

**Thresholds**:
- Per-phase cycles: 3
- Total cycles: 10
- Consecutive failures: 2

**State transitions**:
- CLOSED → OPEN: Threshold exceeded
- OPEN → HALF-OPEN: Human intervention
- HALF-OPEN → CLOSED: Next review passes
- HALF-OPEN → OPEN: Next review fails

### 5.2 Escalation Script

**Create** `action/escalate.sh`:
- Label issue with `needs-human-intervention`
- Post context comment with state and review history
- Create HITL decision checkboxes

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

---

*Authored-by: egg*
