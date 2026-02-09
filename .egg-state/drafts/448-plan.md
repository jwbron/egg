# Plan: SDLC Unification 1/4: Contract Schema Extension & Check Scripts

> Issue: #448 | Phase: plan

## Summary

This plan implements Part 1 of 4 for the SDLC Unification effort. The work extends the contract schema with `PhaseConfig` and `CheckDefinition` Pydantic models, creates a check script infrastructure with Python-based check scripts, and establishes the foundation for the unified work loop. Per human decisions from the refine phase: check scripts will be written in Python (not shell), and the `human_review_mechanism` field will use an enum type.

## Implementation Phases

### Phase 1: Contract Schema Extension

**Goal**: Add new Pydantic models for phase configuration and check definitions, maintaining backward compatibility with existing contracts.

**Tasks**:
- [TASK-1-1] Add `CheckStatus` enum and `HumanReviewMechanism` enum to `models.py` — Acceptance: Enums follow existing StrEnum pattern with `pass`, `fail`, `skip` and `ISSUE_CHECKBOX`, `PR_REVIEW` values respectively
- [TASK-1-2] Add `CheckDefinition` model to `models.py` — Acceptance: Model has `id` (pattern `^check-[a-z0-9-]+$`), `name`, `script`, `required`, `retry_on_fail`, `max_retries` fields
- [TASK-1-3] Add `CheckResult` model to `models.py` — Acceptance: Model has `check_id`, `status` (CheckStatus), `message`, `details` (dict), `fixable` (bool) fields
- [TASK-1-4] Add `PhaseConfig` model to `models.py` — Acceptance: Model has `checks` (list[CheckDefinition]), `max_review_cycles`, `human_review_mechanism` (HumanReviewMechanism) fields
- [TASK-1-5] Add optional `phase_configs` field to `Contract` model — Acceptance: Field is optional dict[PipelinePhase, PhaseConfig], defaults to None, existing contracts remain valid

**Dependencies**: None

**Exit criteria**: All new models pass validation tests, existing contract tests continue to pass

### Phase 2: JSON Schema Update

**Goal**: Update the JSON schema to match the new Pydantic models while maintaining backward compatibility.

**Tasks**:
- [TASK-2-1] Add `checkDefinition` to `$defs` in schema — Acceptance: Schema matches Pydantic model structure with proper patterns and constraints
- [TASK-2-2] Add `checkResult` to `$defs` in schema — Acceptance: Schema matches Pydantic model with enum for status
- [TASK-2-3] Add `phaseConfig` to `$defs` in schema — Acceptance: Schema matches Pydantic model with proper nested structure
- [TASK-2-4] Add `phase_configs` property to root schema — Acceptance: Property is optional, uses correct pattern for phase keys, references phaseConfig definition
- [TASK-2-5] Validate schema against existing contracts — Acceptance: All existing contracts in `.egg-state/contracts/` pass validation

**Dependencies**: Phase 1

**Exit criteria**: JSON schema validates both old and new contract formats

### Phase 3: Phase Defaults Module

**Goal**: Create a module with default configurations for each pipeline phase.

**Tasks**:
- [TASK-3-1] Create `shared/egg_contracts/phase_defaults.py` — Acceptance: Module exports `get_default_phase_config(phase: PipelinePhase) -> PhaseConfig` function
- [TASK-3-2] Define default checks for `refine` phase — Acceptance: Includes `draft-validation-check` with appropriate settings
- [TASK-3-3] Define default checks for `plan` phase — Acceptance: Includes `plan-yaml-check` with appropriate settings
- [TASK-3-4] Define default checks for `implement` phase — Acceptance: Includes `merge-conflict-check`, `lint-check`, `test-check`, `check-fixer` with proper DAG ordering
- [TASK-3-5] Add `get_effective_phase_config()` helper — Acceptance: Function merges contract overrides with defaults, contract values take precedence

**Dependencies**: Phase 1

**Exit criteria**: Default configs are complete and can be loaded for any phase

### Phase 4: Check Script Infrastructure

**Goal**: Create the check runner framework and Python check scripts.

**Tasks**:
- [TASK-4-1] Create `.github/scripts/checks/` directory structure — Acceptance: Directory exists with `__init__.py` and `base.py` files
- [TASK-4-2] Implement `base.py` with `CheckRunner` base class — Acceptance: Base class defines `run()` method returning `CheckResult`, handles JSON output formatting
- [TASK-4-3] Create `run_check.py` entry point script — Acceptance: Script accepts check name and contract path, loads appropriate check class, outputs JSON result to stdout
- [TASK-4-4] Implement `merge_conflict_check.py` — Acceptance: Detects merge conflict markers in tracked files, returns CheckResult with fixable=false
- [TASK-4-5] Implement `draft_validation_check.py` — Acceptance: Validates draft file exists and has required sections, returns CheckResult
- [TASK-4-6] Implement `plan_yaml_check.py` — Acceptance: Validates plan has `# yaml-tasks` block and parses correctly, returns CheckResult
- [TASK-4-7] Implement `lint_check.py` — Acceptance: Runs configured linter (make lint or equivalent), captures output, returns CheckResult with fixable=true
- [TASK-4-8] Implement `test_check.py` — Acceptance: Runs configured test command, captures output, returns CheckResult with fixable=false
- [TASK-4-9] Implement `check_fixer.py` — Acceptance: Attempts auto-fix for checks marked fixable=true (e.g., `make fix`), returns CheckResult

**Dependencies**: Phase 1 (for CheckResult model)

**Exit criteria**: All check scripts are executable and produce consistent JSON output

### Phase 5: Unit Tests

**Goal**: Add comprehensive unit tests for all new code.

**Tasks**:
- [TASK-5-1] Add tests for `CheckStatus`, `HumanReviewMechanism` enums — Acceptance: Tests cover all enum values and string serialization
- [TASK-5-2] Add tests for `CheckDefinition` model — Acceptance: Tests cover valid creation, ID pattern validation, default values
- [TASK-5-3] Add tests for `CheckResult` model — Acceptance: Tests cover valid creation, status enum validation, JSON serialization
- [TASK-5-4] Add tests for `PhaseConfig` model — Acceptance: Tests cover valid creation, nested CheckDefinition list, defaults
- [TASK-5-5] Add tests for `Contract` with `phase_configs` field — Acceptance: Tests cover optional field, serialization roundtrip, backward compatibility
- [TASK-5-6] Add tests for `phase_defaults` module — Acceptance: Tests cover default config retrieval for all phases, override merging
- [TASK-5-7] Add tests for check scripts — Acceptance: Each check script has tests verifying correct JSON output format

**Dependencies**: Phases 1-4

**Exit criteria**: All tests pass, coverage meets project standards

## Test Strategy

- **Unit tests**: Comprehensive tests for all new Pydantic models in `tests/shared/egg_contracts/test_models.py`, following existing patterns with pytest and ValidationError assertions
- **Integration tests**: Tests for `phase_defaults.py` ensuring defaults load correctly and merge with overrides
- **Check script tests**: Unit tests for each check script in `tests/github_scripts/test_checks.py`, mocking subprocess calls where needed
- **Backward compatibility**: Verify existing contracts in `.egg-state/contracts/` still validate against updated schema
- **Manual testing**: Run each check script manually against a test repository to verify JSON output format

## Rollback Plan

This PR adds new code without modifying existing workflows, making rollback straightforward:

1. **Revert commit**: `git revert <commit-sha>` to remove all changes
2. **Schema revert**: If only schema changes need rollback, revert `.egg/schemas/contract.schema.json` to previous version
3. **No data migration**: Since `phase_configs` is optional with default None, existing contracts require no migration

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Pydantic model breaks existing serialization | Low | High | Extensive roundtrip tests, optional fields only |
| Check scripts produce inconsistent output | Medium | Medium | Shared base class with validated output format |
| Schema validation too strict | Low | Medium | Test against all existing contracts before merge |
| Import cycles with new modules | Low | Low | Keep phase_defaults.py imports minimal |

## Migration Notes

No migration required. All new fields are optional with sensible defaults:
- `phase_configs`: Optional, defaults to None (use phase_defaults module)
- Existing contracts continue to work unchanged
- Schema version remains `1.0` (additive, backward-compatible change)

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add PhaseConfig, CheckDefinition models and check scripts"
  description: |
    Part 1 of 4 for SDLC Unification. Extends the contract schema with PhaseConfig
    and CheckDefinition models, creates Python-based check script infrastructure.
    This foundation enables the unified work loop in subsequent PRs.

    Issue: #448
phases:
  - id: 1
    name: Contract Schema Extension
    goal: Add new Pydantic models for phase configuration and check definitions
    tasks:
      - id: TASK-1-1
        description: Add CheckStatus and HumanReviewMechanism enums to models.py
        acceptance: Enums follow existing StrEnum pattern with correct values
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-2
        description: Add CheckDefinition model to models.py
        acceptance: Model has id, name, script, required, retry_on_fail, max_retries fields
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-3
        description: Add CheckResult model to models.py
        acceptance: Model has check_id, status, message, details, fixable fields
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-4
        description: Add PhaseConfig model to models.py
        acceptance: Model has checks, max_review_cycles, human_review_mechanism fields
        files:
          - shared/egg_contracts/models.py
      - id: TASK-1-5
        description: Add optional phase_configs field to Contract model
        acceptance: Field is optional dict, defaults to None, existing contracts valid
        files:
          - shared/egg_contracts/models.py
  - id: 2
    name: JSON Schema Update
    goal: Update the JSON schema to match the new Pydantic models
    tasks:
      - id: TASK-2-1
        description: Add checkDefinition to $defs in schema
        acceptance: Schema matches Pydantic model structure with proper patterns
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-2
        description: Add checkResult to $defs in schema
        acceptance: Schema matches Pydantic model with enum for status
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-3
        description: Add phaseConfig to $defs in schema
        acceptance: Schema matches Pydantic model with proper nested structure
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-4
        description: Add phase_configs property to root schema
        acceptance: Property is optional, uses correct pattern for phase keys
        files:
          - .egg/schemas/contract.schema.json
      - id: TASK-2-5
        description: Validate schema against existing contracts
        acceptance: All existing contracts pass validation
        files:
          - .egg/schemas/contract.schema.json
  - id: 3
    name: Phase Defaults Module
    goal: Create a module with default configurations for each pipeline phase
    tasks:
      - id: TASK-3-1
        description: Create phase_defaults.py with get_default_phase_config function
        acceptance: Module exports function returning PhaseConfig for any phase
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-3-2
        description: Define default checks for refine phase
        acceptance: Includes draft-validation-check with appropriate settings
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-3-3
        description: Define default checks for plan phase
        acceptance: Includes plan-yaml-check with appropriate settings
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-3-4
        description: Define default checks for implement phase
        acceptance: Includes merge-conflict, lint, test, check-fixer checks
        files:
          - shared/egg_contracts/phase_defaults.py
      - id: TASK-3-5
        description: Add get_effective_phase_config helper for merging overrides
        acceptance: Function merges contract overrides with defaults correctly
        files:
          - shared/egg_contracts/phase_defaults.py
  - id: 4
    name: Check Script Infrastructure
    goal: Create the check runner framework and Python check scripts
    tasks:
      - id: TASK-4-1
        description: Create .github/scripts/checks/ directory with base structure
        acceptance: Directory has __init__.py and base.py files
        files:
          - .github/scripts/checks/__init__.py
          - .github/scripts/checks/base.py
      - id: TASK-4-2
        description: Implement base.py with CheckRunner base class
        acceptance: Base class defines run() method returning CheckResult
        files:
          - .github/scripts/checks/base.py
      - id: TASK-4-3
        description: Create run_check.py entry point script
        acceptance: Script accepts check name and contract path, outputs JSON
        files:
          - .github/scripts/checks/run_check.py
      - id: TASK-4-4
        description: Implement merge_conflict_check.py
        acceptance: Detects merge conflict markers, returns CheckResult
        files:
          - .github/scripts/checks/merge_conflict_check.py
      - id: TASK-4-5
        description: Implement draft_validation_check.py
        acceptance: Validates draft file exists and has required sections
        files:
          - .github/scripts/checks/draft_validation_check.py
      - id: TASK-4-6
        description: Implement plan_yaml_check.py
        acceptance: Validates plan has yaml-tasks block and parses correctly
        files:
          - .github/scripts/checks/plan_yaml_check.py
      - id: TASK-4-7
        description: Implement lint_check.py
        acceptance: Runs linter, captures output, returns CheckResult with fixable=true
        files:
          - .github/scripts/checks/lint_check.py
      - id: TASK-4-8
        description: Implement test_check.py
        acceptance: Runs test command, captures output, returns CheckResult
        files:
          - .github/scripts/checks/test_check.py
      - id: TASK-4-9
        description: Implement check_fixer.py
        acceptance: Attempts auto-fix for fixable checks, returns CheckResult
        files:
          - .github/scripts/checks/check_fixer.py
  - id: 5
    name: Unit Tests
    goal: Add comprehensive unit tests for all new code
    tasks:
      - id: TASK-5-1
        description: Add tests for CheckStatus and HumanReviewMechanism enums
        acceptance: Tests cover all enum values and string serialization
        files:
          - tests/shared/egg_contracts/test_models.py
      - id: TASK-5-2
        description: Add tests for CheckDefinition model
        acceptance: Tests cover valid creation, ID pattern validation, defaults
        files:
          - tests/shared/egg_contracts/test_models.py
      - id: TASK-5-3
        description: Add tests for CheckResult model
        acceptance: Tests cover valid creation, status enum, JSON serialization
        files:
          - tests/shared/egg_contracts/test_models.py
      - id: TASK-5-4
        description: Add tests for PhaseConfig model
        acceptance: Tests cover valid creation, nested list, defaults
        files:
          - tests/shared/egg_contracts/test_models.py
      - id: TASK-5-5
        description: Add tests for Contract with phase_configs field
        acceptance: Tests cover optional field, roundtrip, backward compatibility
        files:
          - tests/shared/egg_contracts/test_models.py
      - id: TASK-5-6
        description: Add tests for phase_defaults module
        acceptance: Tests cover default retrieval for all phases, override merging
        files:
          - tests/shared/egg_contracts/test_phase_defaults.py
      - id: TASK-5-7
        description: Add tests for check scripts
        acceptance: Each check script has tests verifying JSON output format
        files:
          - tests/github_scripts/test_checks.py
```

---

*Authored-by: egg*
