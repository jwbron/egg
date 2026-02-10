# Plan: SDLC Unification 4/4 - Cleanup & Documentation

> Issue: #451 | Phase: plan

## Summary

This is the final cleanup phase for the SDLC unification effort. All prior work has merged: contract schema (#454), unified work loop (#457), and pipeline migration (#460). The remaining work involves removing deprecated code (circuit breaker, mark-task/mark-phase commands, phase-specific review scripts) and updating documentation to reflect the unified architecture. The approved approach is full removal with no soft deprecation or thin wrappers.

## Implementation Phases

### Phase 1: Remove mark-task/mark-phase Commands

**Goal**: Remove deprecated CLI commands and their permissions from the codebase

**Tasks**:
- [TASK-1-1] Remove mark-task and mark-phase from `.egg/phase-permissions.json` — Acceptance: Commands no longer listed in implement phase allowed_operations
- [TASK-1-2] Remove mark-task and mark-phase operations from `gateway/phase_filter.py` default permissions — Acceptance: Operations removed from IMPLEMENT phase permissions
- [TASK-1-3] Remove `cmd_mark_task()` and `cmd_mark_phase()` functions and CLI parser setup from `sandbox/egg_lib/contract_cli.py` — Acceptance: Functions and parser entries deleted
- [TASK-1-4] Remove mark-task and mark-phase tests from `tests/sandbox/test_contract_cli.py` — Acceptance: Test cases for both commands deleted

**Dependencies**: None

**Exit criteria**: No references to mark-task or mark-phase remain in permissions, gateway, CLI, or tests

### Phase 2: Remove Circuit Breaker

**Goal**: Delete all circuit breaker code, models, schema fields, and shell functions

**Tasks**:
- [TASK-2-1] Delete `shared/egg_contracts/circuit_breaker.py` module — Acceptance: File no longer exists
- [TASK-2-2] Delete `tests/shared/egg_contracts/test_circuit_breaker.py` — Acceptance: File no longer exists
- [TASK-2-3] Remove `CircuitBreaker` model and `CircuitBreakerStatus` enum from `shared/egg_contracts/models.py` — Acceptance: Classes deleted
- [TASK-2-4] Remove circuit breaker imports and exports from `shared/egg_contracts/__init__.py` — Acceptance: No circuit breaker symbols in imports or `__all__`
- [TASK-2-5] Remove `circuit_breaker` field and `circuitBreaker` definition from `.egg/schemas/contract.schema.json` — Acceptance: Schema validates without circuit_breaker field
- [TASK-2-6] Remove circuit breaker shell functions (`cmd_check_circuit_breaker`, `cmd_open_circuit_breaker`, `cmd_close_circuit_breaker`, `cmd_check_review_status`) from `action/contract-state.sh` — Acceptance: Functions and related case statements deleted; header comments updated

**Dependencies**: None (can run in parallel with Phase 1)

**Exit criteria**: No circuit breaker code, tests, models, or shell functions remain

### Phase 3: Delete Deprecated Scripts

**Goal**: Remove phase-specific review prompt scripts and escalation script

**Tasks**:
- [TASK-3-1] Delete `action/escalate.sh` — Acceptance: File no longer exists
- [TASK-3-2] Delete `action/build-refine-review-prompt.sh` — Acceptance: File no longer exists
- [TASK-3-3] Delete `action/build-plan-review-prompt.sh` — Acceptance: File no longer exists

**Dependencies**: None (can run in parallel with Phases 1 and 2)

**Exit criteria**: All deprecated scripts deleted

### Phase 4: Documentation Updates

**Goal**: Update documentation to reflect unified work loop architecture without deprecated references

**Tasks**:
- [TASK-4-1] Update `docs/guides/sdlc-pipeline.md` — Acceptance: Circuit breaker section removed; Key Files table updated (remove deleted scripts, keep `build-unified-review-prompt.sh`); deprecation notices removed; `circuit_breaker` removed from contract schema example
- [TASK-4-2] Update `docs/adr/implemented/ADR-SDLC-Pipeline.md` — Acceptance: "Circuit Breaker (Deprecated)" section removed; contract schema examples updated; implementation status updated
- [TASK-4-3] Update `docs/architecture/README.md` — Acceptance: mark-task/mark-phase removed from Contract CLI table (not just marked deprecated); circuit breaker reference removed from supporting scripts section
- [TASK-4-4] Update `sandbox/.claude/rules/contract.md` — Acceptance: mark-task and mark-phase rows removed entirely from Commands table

**Dependencies**: Phases 1, 2, 3 (documentation should reflect final code state)

**Exit criteria**: All documentation reflects unified work loop architecture with no deprecated references

### Phase 5: Validation

**Goal**: Verify the cleanup doesn't break existing functionality

**Tasks**:
- [TASK-5-1] Run `make lint` and fix any import or syntax errors — Acceptance: Lint passes
- [TASK-5-2] Run `make test` and verify no tests fail due to removed code — Acceptance: All tests pass
- [TASK-5-3] Verify contract schema validates existing contracts — Acceptance: Pydantic ignores extra `circuit_breaker` fields in existing contracts

**Dependencies**: Phases 1, 2, 3, 4

**Exit criteria**: Lint and tests pass; existing contracts remain valid

## Test Strategy

- **Unit tests**: No new tests required. Existing tests for removed functionality will be deleted. Remaining tests must continue to pass.
- **Integration tests**: The unified work loop (already merged) exercises the non-deprecated code paths. Existing contracts with `circuit_breaker` field will be validated to ensure Pydantic ignores extra fields.
- **Manual testing**:
  1. Verify `egg-contract show` works on an existing contract with `circuit_breaker` field
  2. Verify `egg-contract add-commit` and `egg-contract update-notes` still work
  3. Confirm `egg-contract mark-task` and `egg-contract mark-phase` are no longer recognized commands

## Rollback Plan

1. All changes are in a single PR on the `egg/issue-451` branch
2. If issues are discovered post-merge:
   - Create hotfix branch from the commit before merge
   - Cherry-pick any subsequent non-breaking commits
   - Revert the cleanup PR
3. Since this is purely code deletion with no new functionality, rollback is straightforward: restore deleted files from git history

```bash
# To restore deleted files if needed:
git checkout <commit-before-merge>^ -- shared/egg_contracts/circuit_breaker.py
git checkout <commit-before-merge>^ -- tests/shared/egg_contracts/test_circuit_breaker.py
# etc.
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Existing contracts with `circuit_breaker` fail to load | Low | Medium | Pydantic ignores extra fields by default; validated in TASK-5-3 |
| External tooling depends on removed commands | Low | Low | Tool isn't used externally (per approved decisions); atomic deployment |
| Documentation links break | Low | Low | All updates are to existing files, not URL changes |
| Shell script references missed | Low | Medium | Grep for `check-circuit-breaker`, `mark-task`, `mark-phase` before completing |

## Migration Notes

- **Breaking changes**: `egg-contract mark-task` and `egg-contract mark-phase` commands will no longer work. Gateway will reject these operations.
- **Backwards compatibility**: Existing contracts with `circuit_breaker` field will continue to load because Pydantic's default behavior ignores extra fields not in the model.
- **No database migrations**: This is purely code/docs cleanup with no data model changes.
- **Atomic deployment**: Gateway changes (phase_filter.py) deploy atomically with phase-permissions.json changes, so there's no window where permissions are inconsistent.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Remove deprecated SDLC code and update docs"
  description: |
    Final cleanup for SDLC unification (Part 4 of 4). Removes deprecated code
    that is no longer used after the unified work loop migration:
    - Circuit breaker module, models, schema fields, and shell functions
    - mark-task/mark-phase CLI commands and permissions
    - Phase-specific review prompt scripts (build-refine-review-prompt.sh, etc.)

    Updates documentation to reflect the unified architecture without deprecation notices.

    Closes #451
phases:
  - id: 1
    name: Remove mark-task/mark-phase Commands
    goal: Remove deprecated CLI commands and their permissions from the codebase
    tasks:
      - id: TASK-1-1
        description: Remove mark-task and mark-phase from phase-permissions.json
        acceptance: Commands no longer listed in implement phase allowed_operations
        files:
          - .egg/phase-permissions.json
      - id: TASK-1-2
        description: Remove mark-task and mark-phase operations from gateway default permissions
        acceptance: Operations removed from IMPLEMENT phase permissions
        files:
          - gateway/phase_filter.py
      - id: TASK-1-3
        description: Remove cmd_mark_task() and cmd_mark_phase() functions and CLI parser setup
        acceptance: Functions and parser entries deleted
        files:
          - sandbox/egg_lib/contract_cli.py
      - id: TASK-1-4
        description: Remove mark-task and mark-phase tests
        acceptance: Test cases for both commands deleted
        files:
          - tests/sandbox/test_contract_cli.py
  - id: 2
    name: Remove Circuit Breaker
    goal: Delete all circuit breaker code, models, schema fields, and shell functions
    tasks:
      - id: TASK-2-1
        description: Delete circuit_breaker.py module
        acceptance: File no longer exists
        files:
          - shared/egg_contracts/circuit_breaker.py
      - id: TASK-2-2
        description: Delete circuit breaker tests
        acceptance: File no longer exists
        files:
          - tests/shared/egg_contracts/test_circuit_breaker.py
      - id: TASK-2-3
        description: Remove CircuitBreaker model and CircuitBreakerStatus enum
        acceptance: Classes deleted from models
        files:
          - shared/egg_contracts/models.py
      - id: TASK-2-4
        description: Remove circuit breaker imports and exports from __init__.py
        acceptance: No circuit breaker symbols in imports or __all__
        files:
          - shared/egg_contracts/__init__.py
      - id: TASK-2-5
        description: Remove circuit_breaker field and circuitBreaker definition from schema
        acceptance: Schema validates without circuit_breaker field
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-6
        description: Remove circuit breaker shell functions from contract-state.sh
        acceptance: Functions and related case statements deleted; header comments updated
        files:
          - action/contract-state.sh
  - id: 3
    name: Delete Deprecated Scripts
    goal: Remove phase-specific review prompt scripts and escalation script
    tasks:
      - id: TASK-3-1
        description: Delete escalate.sh
        acceptance: File no longer exists
        files:
          - action/escalate.sh
      - id: TASK-3-2
        description: Delete build-refine-review-prompt.sh
        acceptance: File no longer exists
        files:
          - action/build-refine-review-prompt.sh
      - id: TASK-3-3
        description: Delete build-plan-review-prompt.sh
        acceptance: File no longer exists
        files:
          - action/build-plan-review-prompt.sh
  - id: 4
    name: Documentation Updates
    goal: Update documentation to reflect unified work loop architecture
    tasks:
      - id: TASK-4-1
        description: Update sdlc-pipeline.md guide
        acceptance: Circuit breaker section removed; Key Files table updated; deprecation notices removed
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-4-2
        description: Update ADR-SDLC-Pipeline.md
        acceptance: Circuit Breaker section removed; contract schema examples updated
        files:
          - docs/adr/implemented/ADR-SDLC-Pipeline.md
      - id: TASK-4-3
        description: Update architecture README.md
        acceptance: mark-task/mark-phase removed from CLI table; circuit breaker reference removed
        files:
          - docs/architecture/README.md
      - id: TASK-4-4
        description: Update sandbox contract.md rules
        acceptance: mark-task and mark-phase rows removed from Commands table
        files:
          - sandbox/.claude/rules/contract.md
  - id: 5
    name: Validation
    goal: Verify the cleanup doesn't break existing functionality
    tasks:
      - id: TASK-5-1
        description: Run make lint and fix any errors
        acceptance: Lint passes
        files: []
      - id: TASK-5-2
        description: Run make test and verify all tests pass
        acceptance: All tests pass
        files: []
      - id: TASK-5-3
        description: Verify contract schema validates existing contracts
        acceptance: Pydantic ignores extra circuit_breaker fields
        files: []
```

---

*Authored-by: egg*
