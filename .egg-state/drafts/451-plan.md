# Plan: SDLC Unification 4/4: Cleanup & Documentation

> Issue: #451 | Phase: plan

## Summary

Based on human approval of **Option A: Full Removal of Deprecated Code**, this plan removes all deprecated code from the SDLC pipeline unification. The human confirmed:
1. Delete phase-specific review prompt scripts entirely (no thin wrappers)
2. Remove circuit breaker code entirely (not just deprecation)
3. Breaking changes are acceptable — deploy atomically

This is a cleanup-focused effort that removes ~1,500 lines of deprecated code and updates documentation to reflect the unified work loop architecture.

## Implementation Phases

### Phase 1: Remove Deprecated CLI Commands and Tests

**Goal**: Remove `mark-task` and `mark-phase` commands from the contract CLI, gateway filter, and phase permissions.

**Tasks**:
- [TASK-1-1] Remove `mark-task` and `mark-phase` from `phase-permissions.json` — Acceptance: Commands no longer listed in implement phase allowed_operations
- [TASK-1-2] Remove `mark-task` and `mark-phase` operations from `gateway/phase_filter.py` — Acceptance: Gateway no longer validates/blocks these operations
- [TASK-1-3] Remove `mark_task` and `mark_phase` functions from `sandbox/egg_lib/contract_cli.py` — Acceptance: CLI no longer has these subcommands
- [TASK-1-4] Remove corresponding tests from `tests/sandbox/test_contract_cli.py` — Acceptance: No orphaned tests for removed commands

**Dependencies**: None

**Exit criteria**: `egg-contract mark-task` and `egg-contract mark-phase` commands no longer exist; all tests pass.

### Phase 2: Remove Circuit Breaker Code

**Goal**: Fully remove circuit breaker module, exports, and contract schema references.

**Tasks**:
- [TASK-2-1] Delete `shared/egg_contracts/circuit_breaker.py` — Acceptance: File no longer exists
- [TASK-2-2] Remove circuit breaker imports/exports from `shared/egg_contracts/__init__.py` — Acceptance: No `circuit_breaker` imports in package init
- [TASK-2-3] Remove `CircuitBreaker` and `CircuitBreakerStatus` models from `shared/egg_contracts/models.py` — Acceptance: Models no longer defined
- [TASK-2-4] Remove `circuit_breaker` field from `Contract` model in `models.py` — Acceptance: Field no longer in Contract class
- [TASK-2-5] Update `.egg/schemas/contract.schema.json` to remove circuit_breaker — Acceptance: Schema no longer includes circuit_breaker property
- [TASK-2-6] Delete `tests/shared/egg_contracts/test_circuit_breaker.py` — Acceptance: Test file no longer exists
- [TASK-2-7] Remove circuit breaker functions from `action/contract-state.sh` — Acceptance: `check-circuit-breaker`, `open-circuit-breaker`, `close-circuit-breaker` removed

**Dependencies**: Phase 1

**Exit criteria**: No references to circuit breaker in codebase; `make lint` and `make test` pass.

### Phase 3: Remove Deprecated Action Scripts

**Goal**: Delete deprecated action scripts and clean up remaining deprecated code.

**Tasks**:
- [TASK-3-1] Delete `action/escalate.sh` — Acceptance: File no longer exists
- [TASK-3-2] Delete `action/build-refine-review-prompt.sh` — Acceptance: File no longer exists
- [TASK-3-3] Delete `action/build-plan-review-prompt.sh` — Acceptance: File no longer exists
- [TASK-3-4] Remove `check-review-status` from `action/contract-state.sh` — Acceptance: Command no longer in switch statement
- [TASK-3-5] Remove deprecated CLI commands from documentation references in `sandbox/.claude/rules/contract.md` — Acceptance: No mention of deprecated commands
- [TASK-3-6] Remove deprecated command references from `docs/architecture/README.md` — Acceptance: No references to deprecated scripts/commands

**Dependencies**: Phase 2

**Exit criteria**: No deprecated action scripts exist; documentation references updated.

### Phase 4: Documentation Updates

**Goal**: Update documentation to reflect unified work loop architecture and remove deprecation notices.

**Tasks**:
- [TASK-4-1] Update `docs/guides/sdlc-pipeline.md` — Remove circuit breaker section, update Key Files table, remove deprecation notices — Acceptance: No references to deprecated code; accurate architecture description
- [TASK-4-2] Update `docs/adr/implemented/ADR-SDLC-Pipeline.md` — Remove circuit breaker section, add unified work loop decision, update role permissions — Acceptance: ADR reflects current architecture; no deprecated references
- [TASK-4-3] Add check DAG configuration section to `docs/guides/sdlc-pipeline.md` — Acceptance: Documents check execution order (merge-fix → parallel lint/test → fixer → review)

**Dependencies**: Phases 1-3

**Exit criteria**: Documentation accurately reflects current architecture with no deprecation notices.

### Phase 5: Validation and Cleanup

**Goal**: Run regression tests on all phases and verify no TODO comments reference old structure.

**Tasks**:
- [TASK-5-1] Search and remove TODO comments referencing old structure — Acceptance: `grep -r "TODO.*old\|TODO.*deprecated\|TODO.*remove" .` returns no false positives in active code
- [TASK-5-2] Run `make lint` to verify no import errors — Acceptance: Linter passes with no errors
- [TASK-5-3] Run `make test` for full test suite — Acceptance: All tests pass
- [TASK-5-4] Verify contract schema validates existing contracts — Acceptance: `python -c "from egg_contracts import load_contract; ..."` works for sample contracts

**Dependencies**: Phase 4

**Exit criteria**: All checks pass; codebase clean of deprecated references.

## Test Strategy

- **Unit tests**: Remove tests for deleted code (`test_circuit_breaker.py`, mark-task/mark-phase tests). Existing tests for active functionality should continue passing.
- **Integration tests**: Verify contract loading still works after schema changes. The `tests/shared/egg_contracts/` test suite covers contract operations.
- **Manual testing**: The issue mentions running full regression tests on refine, plan, and implement phases. This is handled externally by the SDLC pipeline after PR merge.

## Rollback Plan

If issues arise after merge:

1. **Revert commit**: `git revert <commit-sha>` for the PR merge commit
2. **Redeploy**: The changes are self-contained in this PR, so reverting restores all functionality
3. **Gateway sync**: No gateway deployment dependencies — all changes are in the codebase

Since the human confirmed breaking changes are acceptable and deployment will be atomic, no phased rollout is needed.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Existing contracts have `circuit_breaker` field | High | Low | Pydantic models should ignore extra fields by default; verify with `model_config` |
| External workflow references deleted scripts | Low | Medium | Human confirmed this is internal-only; breaking changes acceptable |
| Import errors from removed exports | Medium | Low | Thorough grep search before removal; CI will catch |
| Documentation broken links | Low | Low | Search for internal references before updating |

## Migration Notes

**Schema migration**: Existing contracts with `circuit_breaker` field will still load. Pydantic's default behavior is to ignore extra fields when the model doesn't define them. No explicit migration script needed.

**Gateway deployment**: The human confirmed atomic deployment. Remove operations from `phase-permissions.json` and `phase_filter.py` in the same PR.

**No database migrations**: This is purely a code/docs cleanup — no persistent state changes.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Remove deprecated SDLC code and update documentation"
  description: |
    Final cleanup of the SDLC pipeline unification (issue #451). Removes all
    deprecated code including circuit breaker, mark-task/mark-phase commands,
    and legacy action scripts. Updates documentation to reflect unified work
    loop architecture.

    Closes #451
phases:
  - id: 1
    name: Remove Deprecated CLI Commands and Tests
    goal: Remove mark-task and mark-phase commands from CLI, gateway, and permissions
    tasks:
      - id: TASK-1-1
        description: Remove mark-task and mark-phase from phase-permissions.json
        acceptance: Commands no longer listed in implement phase allowed_operations
        files:
          - .egg/phase-permissions.json
      - id: TASK-1-2
        description: Remove mark-task and mark-phase operations from gateway/phase_filter.py
        acceptance: Gateway no longer validates/blocks these operations
        files:
          - gateway/phase_filter.py
      - id: TASK-1-3
        description: Remove mark_task and mark_phase functions from contract_cli.py
        acceptance: CLI no longer has these subcommands
        files:
          - sandbox/egg_lib/contract_cli.py
      - id: TASK-1-4
        description: Remove corresponding tests from test_contract_cli.py
        acceptance: No orphaned tests for removed commands
        files:
          - tests/sandbox/test_contract_cli.py
  - id: 2
    name: Remove Circuit Breaker Code
    goal: Fully remove circuit breaker module, exports, and contract schema references
    tasks:
      - id: TASK-2-1
        description: Delete shared/egg_contracts/circuit_breaker.py
        acceptance: File no longer exists
        files:
          - shared/egg_contracts/circuit_breaker.py
      - id: TASK-2-2
        description: Remove circuit breaker imports/exports from __init__.py
        acceptance: No circuit_breaker imports in package init
        files:
          - shared/egg_contracts/__init__.py
      - id: TASK-2-3
        description: Remove CircuitBreaker and CircuitBreakerStatus models
        acceptance: Models no longer defined
        files:
          - shared/egg_contracts/models.py
      - id: TASK-2-4
        description: Remove circuit_breaker field from Contract model
        acceptance: Field no longer in Contract class
        files:
          - shared/egg_contracts/models.py
      - id: TASK-2-5
        description: Update contract.schema.json to remove circuit_breaker
        acceptance: Schema no longer includes circuit_breaker property
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-6
        description: Delete test_circuit_breaker.py
        acceptance: Test file no longer exists
        files:
          - tests/shared/egg_contracts/test_circuit_breaker.py
      - id: TASK-2-7
        description: Remove circuit breaker functions from contract-state.sh
        acceptance: check-circuit-breaker, open-circuit-breaker, close-circuit-breaker removed
        files:
          - action/contract-state.sh
  - id: 3
    name: Remove Deprecated Action Scripts
    goal: Delete deprecated action scripts and clean up remaining deprecated code
    tasks:
      - id: TASK-3-1
        description: Delete action/escalate.sh
        acceptance: File no longer exists
        files:
          - action/escalate.sh
      - id: TASK-3-2
        description: Delete action/build-refine-review-prompt.sh
        acceptance: File no longer exists
        files:
          - action/build-refine-review-prompt.sh
      - id: TASK-3-3
        description: Delete action/build-plan-review-prompt.sh
        acceptance: File no longer exists
        files:
          - action/build-plan-review-prompt.sh
      - id: TASK-3-4
        description: Remove check-review-status from contract-state.sh
        acceptance: Command no longer in switch statement
        files:
          - action/contract-state.sh
      - id: TASK-3-5
        description: Remove deprecated CLI commands from sandbox/.claude/rules/contract.md
        acceptance: No mention of deprecated commands
        files:
          - sandbox/.claude/rules/contract.md
      - id: TASK-3-6
        description: Remove deprecated command references from docs/architecture/README.md
        acceptance: No references to deprecated scripts/commands
        files:
          - docs/architecture/README.md
  - id: 4
    name: Documentation Updates
    goal: Update documentation to reflect unified work loop architecture
    tasks:
      - id: TASK-4-1
        description: Update sdlc-pipeline.md - remove circuit breaker section and deprecation notices
        acceptance: No references to deprecated code; accurate architecture description
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-4-2
        description: Update ADR-SDLC-Pipeline.md - remove circuit breaker section, add unified work loop decision
        acceptance: ADR reflects current architecture; no deprecated references
        files:
          - docs/adr/implemented/ADR-SDLC-Pipeline.md
      - id: TASK-4-3
        description: Add check DAG configuration section to sdlc-pipeline.md
        acceptance: Documents check execution order
        files:
          - docs/guides/sdlc-pipeline.md
  - id: 5
    name: Validation and Cleanup
    goal: Run regression tests and verify no TODO comments reference old structure
    tasks:
      - id: TASK-5-1
        description: Search and remove TODO comments referencing old structure
        acceptance: No false positives in active code
        files: []
      - id: TASK-5-2
        description: Run make lint to verify no import errors
        acceptance: Linter passes with no errors
        files: []
      - id: TASK-5-3
        description: Run make test for full test suite
        acceptance: All tests pass
        files: []
      - id: TASK-5-4
        description: Verify contract schema validates existing contracts
        acceptance: Contract loading works for sample contracts
        files: []
```

---

*Authored-by: egg*
