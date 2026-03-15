# Analysis: SDLC Unification 4/4 - Cleanup & Documentation

> Issue: #451 | Phase: refine

## Problem Statement

This is the final cleanup phase (Part 4 of 4) for the SDLC unification effort. Prior work has merged:
- Contract schema unification (#454)
- Unified work loop (#457)
- Pipeline migration (#460)

The remaining work involves removing deprecated code and updating documentation to reflect the unified architecture. Dead code remains in the codebase (circuit breaker, mark-task/mark-phase commands, phase-specific review prompt scripts), and documentation still references deprecated functionality.

## Current Behavior

### Deprecated Code Still Present

**1. mark-task and mark-phase CLI commands**

These commands were deprecated in PR #285 when PR-based code review replaced the dedicated reviewer agent:

| Location | What Exists |
|----------|-------------|
| `sandbox/egg_lib/contract_cli.py:391-471` | `cmd_mark_task()` and `cmd_mark_phase()` implementations |
| `sandbox/egg_lib/contract_cli.py:784-808` | CLI argument parser setup for both commands |
| `tests/sandbox/test_contract_cli.py:77-103` | Tests for mark-task and mark-phase command parsing |
| `tests/sandbox/test_contract_cli.py:460-475` | Error path tests for both commands |
| `.egg/phase-permissions.json:97-104` | Commands listed in phase-1 (implement) allowed operations |
| `gateway/phase_filter.py:326-327` | Operations in default permissions |

**2. Circuit breaker functionality**

The circuit breaker tracked implementation cycles and escalated to humans when thresholds were exceeded. This was deprecated in PR #285 in favor of PR-based reviews with human-visible feedback at every cycle:

| Location | What Exists |
|----------|-------------|
| `shared/egg_contracts/circuit_breaker.py` | Complete circuit breaker module (469 lines) |
| `tests/shared/egg_contracts/test_circuit_breaker.py` | Full test suite (414 lines) |
| `shared/egg_contracts/models.py:183-190` | `CircuitBreaker` model |
| `shared/egg_contracts/models.py:50-54` | `CircuitBreakerStatus` enum |
| `shared/egg_contracts/__init__.py:43-57, 234-246` | Imports and exports |
| `.egg/schemas/contract.schema.json:68-69` | `circuit_breaker` field definition |
| `action/contract-state.sh:293-386` | `check-circuit-breaker`, `open-circuit-breaker`, `close-circuit-breaker` functions |
| `action/contract-state.sh:131-168` | `check-review-status` function (uses circuit breaker data) |

**3. Phase-specific review prompt scripts**

These were superseded by the unified work loop:

| Location | Status |
|----------|--------|
| `action/build-refine-review-prompt.sh` | 220 lines, still exists |
| `action/build-plan-review-prompt.sh` | 229 lines, still exists |
| `action/escalate.sh` | 21 lines, deprecated notice at top |

**4. Documentation with deprecated references**

| Location | Issue |
|----------|-------|
| `docs/guides/sdlc-pipeline.md:196-200` | Contract schema example includes `circuit_breaker` field |
| `docs/guides/sdlc-pipeline.md:266-279` | Full "Circuit Breaker and Escalation" section marked deprecated |
| `docs/guides/sdlc-pipeline.md:526-527, 537` | Key Files table references `build-refine-review-prompt.sh` and `build-plan-review-prompt.sh` |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md:77-78, 160-163` | Contract schema examples with `circuit_breaker` |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md:171-203` | Full "Circuit Breaker (Deprecated)" section |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md:369` | Implementation status lists circuit breaker as done |
| `docs/architecture/README.md:73-74` | Commands marked deprecated but still listed |
| `docs/architecture/README.md:129-133` | References circuit breaker and escalation in supporting scripts |
| `sandbox/.claude/rules/contract.md:12-13` | Commands listed as "(deprecated)" |

## Constraints

1. **Breaking changes are acceptable** - Per approved decisions, this tool isn't used externally; deploy atomically
2. **No soft deprecation** - Full removal of deprecated code, no thin wrappers
3. **Pydantic backwards compatibility** - Existing contracts with `circuit_breaker` field will still load (Pydantic ignores extra fields by default)
4. **Atomic deployment** - Gateway changes deploy atomically with phase-permissions changes
5. **No database migrations** - Purely code/docs cleanup

## Options Considered

Per the issue description, the following decisions were already approved during a previous refine cycle:

### Option A: Full Removal of Deprecated Code (Approved)

**Approach**: Complete removal of all deprecated code with no transition period

**Pros**:
- Clean codebase with no dead code
- No maintenance burden for deprecated functionality
- Clear signal that new architecture is canonical

**Cons**:
- Breaking change for any external consumers (deemed acceptable per approved decisions)

### Option B: Soft Deprecation with Wrappers (Rejected)

**Approach**: Keep thin wrappers that log deprecation warnings

**Cons**:
- Adds maintenance burden
- Delays full cleanup
- Already rejected in prior approval

## Recommended Approach

Proceed with **Option A: Full Removal** as approved. The implementation order should be:

### Phase 1: Code Cleanup

1. **Remove mark-task/mark-phase from CLI** (`sandbox/egg_lib/contract_cli.py`)
   - Delete `cmd_mark_task()` and `cmd_mark_phase()` functions
   - Remove CLI parser setup for both commands

2. **Remove mark-task/mark-phase tests** (`tests/sandbox/test_contract_cli.py`)
   - Delete all test cases for these commands

3. **Remove from permissions**
   - `.egg/phase-permissions.json` - remove from allowed operations
   - `gateway/phase_filter.py` - remove from default permissions

4. **Delete circuit breaker module** (`shared/egg_contracts/circuit_breaker.py`)

5. **Delete circuit breaker tests** (`tests/shared/egg_contracts/test_circuit_breaker.py`)

6. **Remove circuit breaker from models** (`shared/egg_contracts/models.py`)
   - Delete `CircuitBreaker` class
   - Delete `CircuitBreakerStatus` enum

7. **Remove circuit breaker exports** (`shared/egg_contracts/__init__.py`)
   - Remove imports and `__all__` entries

8. **Remove from contract schema** (`.egg/schemas/contract.schema.json`)
   - Remove `circuit_breaker` field definition

9. **Remove circuit breaker shell functions** (`action/contract-state.sh`)
   - Delete `cmd_check_circuit_breaker()`, `cmd_open_circuit_breaker()`, `cmd_close_circuit_breaker()`
   - Delete `cmd_check_review_status()` (uses circuit breaker data)
   - Remove case statements and help text

10. **Delete deprecated scripts**
    - `action/escalate.sh`
    - `action/build-refine-review-prompt.sh`
    - `action/build-plan-review-prompt.sh`

### Phase 2: Documentation Updates

1. **Update `docs/guides/sdlc-pipeline.md`**
   - Remove circuit breaker section (lines 266-279)
   - Update Key Files table to remove references to deleted scripts
   - Remove deprecation notices throughout
   - Remove `circuit_breaker` from contract schema example
   - Add check DAG configuration section explaining the order: merge-fix → parallel lint/test → fixer → review

2. **Update `docs/adr/implemented/ADR-SDLC-Pipeline.md`**
   - Remove "Circuit Breaker (Deprecated)" section (lines 171-203)
   - Update contract schema example to remove `circuit_breaker`
   - Add note about unified work loop decision
   - Update role permissions table (remove deprecated reviewer commands reference)
   - Update implementation status to remove circuit breaker reference

3. **Update `docs/architecture/README.md`**
   - Remove deprecated commands from Contract CLI table
   - Remove circuit breaker references from supporting scripts section

4. **Update `sandbox/.claude/rules/contract.md`**
   - Remove mark-task and mark-phase from Commands table entirely

### Phase 3: Validation

1. Run `make lint` - verify no import errors or syntax issues
2. Run `make test` - verify no tests depend on removed code
3. Verify contract schema validates (Pydantic ignores extra fields)

## Open Questions

All decisions were approved in a prior refine cycle. No further questions require human input.

**Approved decisions:**
1. **Full removal** of deprecated code (no soft deprecation, no thin wrappers)
2. **Delete phase-specific review prompt scripts** entirely
3. **Remove circuit breaker code entirely** (delete module, models, schema fields, tests, shell functions)
4. **Breaking changes are acceptable** (tool isn't used externally; deploy atomically)

## Files to Modify

### Delete Entirely
- `shared/egg_contracts/circuit_breaker.py`
- `tests/shared/egg_contracts/test_circuit_breaker.py`
- `action/escalate.sh`
- `action/build-refine-review-prompt.sh`
- `action/build-plan-review-prompt.sh`

### Modify
- `sandbox/egg_lib/contract_cli.py` - remove mark-task/mark-phase
- `tests/sandbox/test_contract_cli.py` - remove related tests
- `.egg/phase-permissions.json` - remove deprecated commands
- `gateway/phase_filter.py` - remove from default permissions
- `shared/egg_contracts/models.py` - remove CircuitBreaker, CircuitBreakerStatus
- `shared/egg_contracts/__init__.py` - remove circuit breaker imports/exports
- `.egg/schemas/contract.schema.json` - remove circuit_breaker field
- `action/contract-state.sh` - remove circuit breaker commands
- `docs/guides/sdlc-pipeline.md` - comprehensive update
- `docs/adr/implemented/ADR-SDLC-Pipeline.md` - comprehensive update
- `docs/architecture/README.md` - remove deprecated references
- `sandbox/.claude/rules/contract.md` - remove deprecated commands

---

*Authored-by: egg*
