# Analysis: Contract Schema Extension & Check Scripts

> Issue: #448 | Phase: refine

## Problem Statement

The SDLC pipeline needs a unified "work loop" that runs intermediate checks (linting, testing, merge conflict detection) between implement and review phases. Currently, the pipeline moves directly from implementation to review without structured validation gates. This issue (Part 1 of 4 for SDLC Unification) lays the foundation by:

1. Extending the contract schema with `PhaseConfig` and `CheckDefinition` models
2. Creating a check script infrastructure with a runner framework and individual check scripts

**Current state**: The contract schema (`shared/egg_contracts/models.py`) defines `Phase`, `Task`, and `Decision` models, but has no concept of intermediate checks or phase-specific configuration.

**Desired outcome**: The contract can express which checks run in each phase, with configurable retry behavior and a DAG-based execution order. Check scripts produce consistent JSON output for programmatic processing.

## Current Behavior

### Contract Schema

The existing contract models in `shared/egg_contracts/models.py:100-310` define:

- `Phase`: Implementation phases with tasks, review cycles, and escalation
- `Task`: Individual work items with status, commit tracking, and escalation
- `Decision`: HITL decision points with checkbox-based resolution
- `Contract`: Root model with phases, decisions, circuit breaker, and audit log

The JSON schema at `.egg/schemas/contract.schema.json` mirrors these Pydantic models with identical validation rules. Role-based field ownership is annotated via `x-role-owner` (e.g., `reviewer`, `implementer`, `human`).

### Existing Scripts

The `.github/scripts/` directory contains three utility scripts:

| Script | Purpose |
|--------|---------|
| `push-contract-update.sh` | Conflict-resistant contract pushing with retry logic |
| `setup-sdlc-labels.sh` | Idempotent label setup for SDLC phases |
| `transition-sdlc-label.sh` | Atomic label transitions for phase changes |

The `scripts/` directory contains 9 Python linting checks (e.g., `check-gh-cli-usage.py`, `check-workflow-secrets.py`) that enforce repository standards. These are invoked by `.github/workflows/lint.yml`.

### Existing Test Patterns

Tests in `tests/shared/egg_contracts/` follow pytest patterns:
- Class-based organization (`class TestPhase:`, `class TestContract:`)
- Individual test methods for valid/invalid cases
- `pytest.raises(ValidationError)` for validation failures
- Model helper method testing

## Constraints

- **Schema compatibility**: New models must integrate with existing `Contract` model without breaking serialization
- **Pydantic patterns**: Must follow existing Pydantic v2 patterns (Field validators, model_dump, model_validate)
- **JSON Schema sync**: Changes to Pydantic models must be reflected in `contract.schema.json`
- **Test coverage**: New models require comprehensive unit tests following existing patterns
- **No workflow changes**: This PR adds foundation code only; workflow integration is in Part 2
- **Check output format**: All checks must produce consistent JSON for automation parsing
- **DAG behavior**: Merge conflict check runs first, lint/test run in parallel, check fixer runs last

## Options Considered

### Option A: Embed Phase Configs in Contract Model

**Approach**: Add `phase_configs: dict[PipelinePhase, PhaseConfig]` field directly on the `Contract` model, with `PhaseConfig` containing a list of `CheckDefinition` objects.

```python
class CheckDefinition(BaseModel):
    id: str = Field(..., pattern=r"^check-[a-z0-9-]+$")
    name: str
    script: str  # Path to check script
    required: bool = True
    retry_on_fail: bool = False
    max_retries: int = 3

class PhaseConfig(BaseModel):
    checks: list[CheckDefinition] = []
    max_review_cycles: int = 3
    human_review_mechanism: str  # "issue_checkbox" | "pr_review"

class Contract(BaseModel):
    # ... existing fields ...
    phase_configs: dict[PipelinePhase, PhaseConfig] = Field(default_factory=dict)
```

**Pros**:
- Phase configuration travels with the contract
- Per-issue customization possible
- Single source of truth for phase behavior

**Cons**:
- Contracts become larger (verbose JSON)
- Most issues will use identical configs (duplication)
- Schema changes require migration for existing contracts

### Option B: Separate Phase Defaults Module with Optional Contract Overrides

**Approach**: Create `shared/egg_contracts/phase_defaults.py` with default `PhaseConfig` for each phase. The `Contract` model can optionally override specific phases via a sparse `phase_config_overrides` field.

```python
# phase_defaults.py
DEFAULT_PHASE_CONFIGS: dict[PipelinePhase, PhaseConfig] = {
    PipelinePhase.REFINE: PhaseConfig(
        checks=[],
        max_review_cycles=3,
        human_review_mechanism="issue_checkbox",
    ),
    PipelinePhase.IMPLEMENT: PhaseConfig(
        checks=[
            CheckDefinition(id="check-merge-conflict", name="Merge conflict", script=".github/scripts/checks/merge-conflict-check.sh"),
            CheckDefinition(id="check-lint", name="Lint", script=".github/scripts/checks/lint-check.sh"),
            CheckDefinition(id="check-test", name="Test", script=".github/scripts/checks/test-check.sh"),
        ],
        max_review_cycles=3,
        human_review_mechanism="pr_review",
    ),
    # ...
}

# Contract model adds optional sparse overrides
class Contract(BaseModel):
    # ... existing fields ...
    phase_config_overrides: dict[PipelinePhase, PhaseConfig] | None = None
```

**Pros**:
- Contracts remain compact (only store overrides)
- Centralized defaults are easily maintained
- Backward compatible (existing contracts don't need migration)
- Separation of concerns (config vs. state)

**Cons**:
- Two places to look for configuration
- Runtime merging of defaults + overrides adds complexity

### Option C: External Configuration File (No Contract Changes)

**Approach**: Keep phase configurations entirely outside the contract in a static YAML/JSON file (e.g., `.egg/phase-configs.yml`). Contracts don't reference configurations at all.

**Pros**:
- Zero contract schema changes
- Configuration can be updated without touching contracts
- Simpler contract model

**Cons**:
- No per-issue customization
- Configuration not versioned with contract
- Harder to correlate which config was used for a given issue

## Recommended Approach

**Option B: Separate Phase Defaults Module with Optional Contract Overrides**

**Justification**:

1. **Backward compatibility**: Existing contracts remain valid; new field is optional
2. **Compact contracts**: Default case (no overrides) adds zero bytes to contract JSON
3. **Maintainability**: Defaults live in code, easily updated and version-controlled
4. **Flexibility**: Specific issues can override checks if needed (e.g., skip tests for docs-only PRs)
5. **Alignment with parent issue**: Parent #436 specifies this exact design pattern

**Implementation Plan**:

1. **New models** (`models.py`):
   - `CheckStatus` enum: `pass`, `fail`, `skip`
   - `CheckResult` model for script output
   - `CheckDefinition` model per parent issue spec
   - `PhaseConfig` model per parent issue spec

2. **Phase defaults** (`phase_defaults.py`):
   - `DEFAULT_PHASE_CONFIGS` dictionary
   - `get_phase_config(phase, overrides)` helper function

3. **Contract extension** (`models.py`):
   - Add optional `phase_config_overrides` field to `Contract`
   - Add `get_phase_config(phase)` helper method

4. **Schema update** (`contract.schema.json`):
   - Add `checkDefinition`, `phaseConfig` definitions
   - Add `phase_config_overrides` property to root

5. **Check scripts** (`.github/scripts/checks/`):
   - `run-check.sh`: Framework script that runs a check and formats output
   - `merge-conflict-check.sh`: Detects merge conflicts
   - `draft-validation-check.sh`: Validates draft documents
   - `plan-yaml-check.sh`: Validates plan YAML syntax
   - `lint-check.sh`: Runs project linters
   - `test-check.sh`: Runs project tests
   - `check-fixer.sh`: Attempts to auto-fix failures

6. **Tests** (`tests/shared/egg_contracts/test_phase_config.py`):
   - Test `CheckDefinition` validation (ID pattern, required fields)
   - Test `PhaseConfig` defaults
   - Test `get_phase_config` merging logic
   - Test Contract with and without overrides

## Check Script Design

All check scripts will output JSON conforming to this schema:

```json
{
  "check_id": "check-lint",
  "status": "pass|fail|skip",
  "message": "Human readable message",
  "details": {},
  "fixable": true
}
```

**Runner framework** (`run-check.sh`):
- Accepts check script path as argument
- Captures script output and exit code
- Wraps output in consistent JSON envelope
- Handles timeouts and script errors

**DAG execution order** (to be implemented in Part 2):
1. `merge-conflict-check.sh` runs first (blocking)
2. `lint-check.sh` and `test-check.sh` run in parallel
3. `check-fixer.sh` runs last if any checks failed with `fixable: true`

## Open Questions

**Question 1** (multiple-choice via HITL):

How should the `human_review_mechanism` field be typed?

- **Enum**: Create `HumanReviewMechanism` enum with `ISSUE_CHECKBOX` and `PR_REVIEW` values
- **Literal string**: Use `Literal["issue_checkbox", "pr_review"]` type annotation
- **Free string with validation**: String field with pattern validation
- **Other (explain in reply)**

---

**Question 2** (multiple-choice via HITL):

Should check scripts be shell scripts or Python scripts?

- **Shell scripts**: Simpler, runs in any environment, aligns with existing `.github/scripts/` patterns
- **Python scripts**: Richer error handling, easier testing, access to egg_contracts library
- **Mixed**: Use shell for simple checks (merge conflict), Python for complex checks (plan validation)
- **Other (explain in reply)**

---

**Question 3** (multiple-choice via HITL):

How should the check runner handle check script timeouts?

- **Per-check timeout**: Each `CheckDefinition` includes a `timeout_seconds` field (default: 300)
- **Global timeout**: Single timeout value in `PhaseConfig` applies to all checks
- **No timeout**: Rely on GitHub Actions job timeout; checks run until complete
- **Other (explain in reply)**

---

*Authored-by: egg*
