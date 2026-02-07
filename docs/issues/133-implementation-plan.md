# Issue #133: Implementation Plan

> Detailed implementation plan for Structurally Enforced Agent Checkpoints and Verification Gates.
> See [133-structurally-enforced-checkpoints.md](./133-structurally-enforced-checkpoints.md) for the full specification.

---

## Overview

This plan breaks down the implementation into 5 phases with specific tasks, file locations, dependencies, and acceptance criteria. Each phase builds on the previous one, with clear handoff points.

**Estimated scope**: ~25-35 files, spanning Python libraries, shell scripts, GitHub Actions workflows, and JSON schemas.

---

## Phase 1: Contract Schema and Validation (Foundation)

**Goal**: Establish the contract data model, JSON schema, and CLI with role-based enforcement.

### 1.1 Contract JSON Schema

**File**: `.egg/schemas/contract.schema.json`

**Tasks**:
- [ ] Define JSON Schema for contract structure (phases, tasks, decisions, audit_log)
- [ ] Add `schemaVersion` field for future migrations
- [ ] Include field-level annotations for role ownership (`x-role-owner`)
- [ ] Add validation constraints (required fields, enum values, patterns)

**Schema structure**:
```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "schemaVersion": { "type": "string", "const": "1.0" },
    "issue": { "$ref": "#/definitions/issue" },
    "phases": { "type": "array", "items": { "$ref": "#/definitions/phase" } },
    "decisions": { "type": "array", "items": { "$ref": "#/definitions/decision" } },
    "circuit_breaker": { "$ref": "#/definitions/circuitBreaker" },
    "audit_log": { "type": "array", "items": { "$ref": "#/definitions/auditEntry" } }
  }
}
```

### 1.2 Contract Library

**Directory**: `shared/egg_contracts/`

**Files**:
- [ ] `shared/egg_contracts/__init__.py` - Package exports
- [ ] `shared/egg_contracts/models.py` - Pydantic models matching JSON schema
- [ ] `shared/egg_contracts/loader.py` - Load/save contract from `.egg/contracts/{issue}.json`
- [ ] `shared/egg_contracts/roles.py` - Role enum and field ownership mapping
- [ ] `shared/egg_contracts/validator.py` - Validate mutations against role permissions
- [ ] `shared/egg_contracts/audit.py` - Audit log entry creation and formatting

**Role ownership mapping** (in `roles.py`):
```python
FIELD_OWNERSHIP = {
    "tasks.*.commit": Role.IMPLEMENTER,
    "tasks.*.notes": Role.IMPLEMENTER,
    "tasks.*.status": Role.REVIEWER,
    "phases.*.review_feedback": Role.REVIEWER,
    "acceptance_criteria.*.verified": Role.REVIEWER,
    "decisions.*.resolved": Role.HUMAN,
    "decisions.*.resolution": Role.HUMAN,
}
```

### 1.3 Contract CLI

**File**: `sandbox/egg_lib/contract_cli.py`

**Commands**:
- [ ] `egg-contract init --issue <number>` - Initialize contract from issue
- [ ] `egg-contract add-commit --task <id> --commit <sha>` - Link commit to task (implementer)
- [ ] `egg-contract update-notes --task <id> --notes <text>` - Add notes (implementer)
- [ ] `egg-contract mark-task --task <id> --status <status>` - Mark task status (reviewer)
- [ ] `egg-contract mark-phase --phase <id> --passed <bool>` - Mark phase (reviewer)
- [ ] `egg-contract resolve-decision --decision <id> --resolution <value>` - Resolve HITL (human only via gateway)
- [ ] `egg-contract show` - Display current contract state

**CLI integration**:
- [ ] Register CLI as entry point in `sandbox/pyproject.toml`
- [ ] Route mutations through gateway for role validation

### 1.4 Gateway Contract Endpoint

**File**: `gateway/internal/handlers/contract.go` (new)

**Endpoints**:
- [ ] `POST /api/v1/contract/mutate` - Validate role and apply mutation
- [ ] `GET /api/v1/contract/{issue}` - Retrieve contract state

**Role determination**:
- Read from `EGG_AGENT_ROLE` header (set by GitHub Actions job context)
- Validate against allowed mutations for that role
- Reject with structured error if unauthorized

### 1.5 Pre-commit Hook

**File**: `.egg/hooks/validate-contract.sh`

**Validation**:
- [ ] Parse contract diff to identify modified fields
- [ ] Check modifications against expected role (from commit metadata or env)
- [ ] Block commits that violate role boundaries
- [ ] Allow bypass with `--no-verify` for human overrides

### 1.6 Unit Tests

**Directory**: `tests/unit/egg_contracts/`

**Test files**:
- [ ] `test_models.py` - Pydantic model validation
- [ ] `test_roles.py` - Role ownership enforcement
- [ ] `test_validator.py` - Mutation validation logic
- [ ] `test_cli.py` - CLI command parsing and execution

**Key test cases**:
- Implementer cannot modify `tasks[].status`
- Reviewer cannot modify `tasks[].commit`
- Human can modify all fields
- Audit log captures all mutations

---

## Phase 2: Review Agent Infrastructure

**Goal**: Create the reviewer agent with its own system prompt and action support.

### 2.1 Reviewer System Prompt

**File**: `sandbox/.claude/rules/reviewer.md`

**Content**:
- [ ] Define reviewer role and constraints
- [ ] Specify what reviewers can and cannot do
- [ ] Provide structured output format for verdicts
- [ ] Reference `egg-contract` CLI commands

**Key rules**:
```markdown
# Reviewer Role

You are a code reviewer agent. Your job is to evaluate implementation quality.

## Constraints
- You CANNOT modify code, only review it
- You CANNOT mark your own work as complete
- You MUST use `egg-contract` CLI to record verdicts

## Output Format
Provide structured verdict:
- PASS: All acceptance criteria met
- FAIL: List specific issues to address
- ESCALATE: Cannot determine, human review needed
```

### 2.2 Action Role Support

**File**: `action/action.yml` (modify)

**Changes**:
- [ ] Add `role` input parameter (implementer, reviewer)
- [ ] Add `rules-file` input for role-specific rules
- [ ] Pass role to container via environment variable

**File**: `action/entrypoint.sh` (modify)

**Changes**:
- [ ] Set `EGG_AGENT_ROLE` environment variable from input
- [ ] Load role-specific rules file if specified
- [ ] Pass role to gateway health check

### 2.3 Reviewer Output Parser

**File**: `shared/egg_contracts/reviewer_output.py`

**Functionality**:
- [ ] Parse structured verdict from reviewer agent output
- [ ] Extract pass/fail status and feedback
- [ ] Map feedback to specific tasks/phases
- [ ] Generate contract update commands

### 2.4 Integration Tests

**Directory**: `tests/integration/`

**Test files**:
- [ ] `test_reviewer_role.py` - Verify reviewer restrictions work end-to-end
- [ ] `test_role_handoff.py` - Verify implementer → reviewer handoff

---

## Phase 3: Pipeline Workflow

**Goal**: Create GitHub Actions workflows for the full SDLC pipeline.

### 3.1 Main Pipeline Workflow

**File**: `.github/workflows/sdlc-pipeline.yml`

**Triggers**:
- [ ] `workflow_dispatch` - Manual trigger with issue number
- [ ] `issues.labeled` - When `egg-sdlc` label is added

**Jobs**:
```yaml
jobs:
  init:
    # Initialize contract from issue

  refine:
    needs: init
    # Refine issue requirements (implementer)

  refine-review:
    needs: refine
    uses: ./.github/workflows/sdlc-review.yml
    # Auto-review refinement

  plan:
    needs: refine-review
    # Create implementation plan (implementer)

  plan-review:
    needs: plan
    uses: ./.github/workflows/sdlc-review.yml
    # Review plan (may trigger HITL)

  implement:
    needs: plan-review
    strategy:
      matrix:
        phase: ${{ fromJson(needs.plan-review.outputs.phases) }}
    # Implement each phase (implementer)

  implement-review:
    needs: implement
    uses: ./.github/workflows/sdlc-review.yml
    # Review implementation (reviewer)

  create-pr:
    needs: implement-review
    # Create pull request
```

### 3.2 Reviewer Workflow (Reusable)

**File**: `.github/workflows/sdlc-review.yml`

**Inputs**:
- [ ] `issue_number` - Issue being worked on
- [ ] `phase_id` - Phase to review
- [ ] `branch` - Branch with implementation
- [ ] `review_type` - auto, hitl, or both

**Outputs**:
- [ ] `verdict` - PASS, FAIL, or ESCALATE
- [ ] `feedback` - Structured feedback JSON
- [ ] `requires_hitl` - Whether HITL decision is needed

### 3.3 HITL Decision Workflow

**File**: `.github/workflows/sdlc-hitl.yml`

**Triggers**:
- [ ] `issue_comment.edited` - Detect checkbox changes

**Logic**:
- [ ] Parse comment for checkbox state changes
- [ ] Implement 30-second debounce
- [ ] Update contract with resolution
- [ ] Trigger pipeline resume

### 3.4 Stage-Specific Prompt Builders

**Directory**: `action/prompts/`

**Files**:
- [ ] `action/prompts/refine.sh` - Build refinement prompt
- [ ] `action/prompts/plan.sh` - Build planning prompt
- [ ] `action/prompts/implement.sh` - Build implementation prompt
- [ ] `action/prompts/review.sh` - Build review prompt

### 3.5 Contract State Management

**File**: `action/contract-state.sh`

**Functionality**:
- [ ] Load contract from branch
- [ ] Determine current phase and task
- [ ] Pass state to agent via prompt context
- [ ] Commit contract updates after agent run

---

## Phase 4: Circuit Breaker and Escalation

**Goal**: Implement safeguards against infinite loops and escalation to humans.

### 4.1 Circuit Breaker Logic

**File**: `shared/egg_contracts/circuit_breaker.py`

**State machine**:
- [ ] CLOSED → OPEN when threshold exceeded
- [ ] OPEN → HALF-OPEN on human intervention
- [ ] HALF-OPEN → CLOSED on next pass
- [ ] HALF-OPEN → OPEN on next fail

**Thresholds**:
```python
THRESHOLDS = {
    "per_phase_cycles": 3,
    "total_cycles": 10,
    "consecutive_failures": 2,
}
```

### 4.2 Escalation Actions

**File**: `action/escalate.sh`

**Actions**:
- [ ] Label issue with `needs-human-intervention`
- [ ] Post context comment with:
  - Current phase and task
  - Review feedback history
  - Suggested resolution paths
- [ ] Create HITL decision checkboxes
- [ ] Notify via configured channels (Slack if available)

### 4.3 HITL Checkbox Rendering

**File**: `shared/egg_contracts/hitl.py`

**Functionality**:
- [ ] Generate markdown checkbox block for decisions
- [ ] Parse checkbox state from comment body
- [ ] Track which option was selected
- [ ] Handle edge cases (multiple checked, unchecked after resolve)

### 4.4 Debounce Implementation

**File**: `.github/workflows/sdlc-hitl.yml` (modify)

**Logic**:
- [ ] On checkbox change detection, set `debounce_until` in contract
- [ ] Wait 30 seconds before processing
- [ ] If comment edited during wait, reset timer
- [ ] Only resolve when debounce expires with stable state

### 4.5 Resume Logic

**File**: `action/resume.sh`

**Functionality**:
- [ ] Load contract and find last completed task/phase
- [ ] Determine resume point
- [ ] Build resume prompt with context
- [ ] Trigger appropriate workflow stage

---

## Phase 5: Integration and Testing

**Goal**: End-to-end testing, documentation, and observability.

### 5.1 Integration Test Suite

**Directory**: `integration_tests/sdlc/`

**Test scenarios**:
- [ ] `test_happy_path.py` - Full pipeline success
- [ ] `test_review_rejection.py` - Reviewer rejects, implementer fixes
- [ ] `test_circuit_breaker.py` - Escalation triggers correctly
- [ ] `test_hitl_flow.py` - Human decision pauses and resumes pipeline
- [ ] `test_role_enforcement.py` - Role violations are blocked

### 5.2 E2E Test Workflow

**File**: `.github/workflows/test-sdlc-e2e.yml`

**Functionality**:
- [ ] Create test issue with known requirements
- [ ] Trigger pipeline
- [ ] Verify contract state at each stage
- [ ] Clean up test artifacts

### 5.3 Observability

**Files**:
- [ ] `shared/egg_contracts/metrics.py` - Contract state metrics
- [ ] `action/log-contract-state.sh` - Log contract at each stage

**Metrics to track**:
- Pipeline stage durations
- Review cycle counts
- Escalation rates
- HITL decision latencies

### 5.4 Documentation

**Files**:
- [ ] `docs/adr/in-progress/ADR-Structurally-Enforced-Checkpoints.md` - Architecture decision record
- [ ] `docs/guides/sdlc-pipeline.md` - User guide for pipeline usage
- [ ] Update `docs/index.md` - Add links to new documentation

### 5.5 Migration and Rollout

**Tasks**:
- [ ] Feature flag for gradual rollout
- [ ] Migration script for existing in-flight issues
- [ ] Rollback procedure documentation

---

## Dependency Graph

```
Phase 1 (Foundation)
    │
    ├── 1.1 Schema ──────────┐
    ├── 1.2 Library ─────────┼── Required for all other phases
    ├── 1.3 CLI ─────────────┤
    └── 1.4 Gateway ─────────┘
           │
           ▼
Phase 2 (Review Agent)
    │
    ├── 2.1 System Prompt
    ├── 2.2 Action Role Support
    └── 2.3 Output Parser
           │
           ▼
Phase 3 (Pipeline) ◄──────── Phase 4 (Circuit Breaker)
    │                              │
    ├── 3.1 Main Workflow          ├── 4.1 Circuit Breaker Logic
    ├── 3.2 Review Workflow        ├── 4.2 Escalation
    ├── 3.3 HITL Workflow          ├── 4.3 Checkbox Rendering
    └── 3.4 Prompt Builders        └── 4.4 Debounce
           │                              │
           └──────────┬───────────────────┘
                      ▼
              Phase 5 (Integration)
                      │
                      ├── 5.1 Integration Tests
                      ├── 5.2 E2E Workflow
                      └── 5.3 Documentation
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Role bypass via env var manipulation | Gateway validates role from workflow context, not env vars |
| Infinite review loops | Circuit breaker with hard limits, escalation to human |
| HITL decision missed | Label issue, post prominent comment, optional Slack notify |
| Contract corruption | Schema validation on every load/save, audit log for forensics |
| Workflow complexity | Modular design, reusable workflows, comprehensive testing |

---

## Success Criteria

Before considering implementation complete:

1. **Unit tests pass** for all new modules (100% coverage on role enforcement)
2. **Integration tests pass** for worker ↔ reviewer handoff
3. **E2E test passes** for full pipeline happy path
4. **Manual verification** of HITL checkbox flow
5. **Documentation complete** with ADR and user guide
6. **Rollout plan approved** by stakeholders

---

## Next Steps

1. **Review this plan** - Confirm approach and priorities
2. **Phase 1 implementation** - Start with schema and library
3. **Iterative delivery** - Ship each phase incrementally with tests

---

*Authored-by: egg*
