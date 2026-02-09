# Analysis: SDLC Unification 4/4: Cleanup & Documentation

> Issue: #451 | Phase: refine

## Problem Statement

This is the final cleanup phase of the SDLC pipeline unification project (parent issue #436). The prior issues (#447, #449, #450) introduced a unified work loop workflow and migrated all phases to use it. This issue addresses the remaining deprecated code and documentation that needs updating to reflect the new architecture.

**Current state:** The codebase has deprecated code paths that are no longer actively used but remain implemented. Documentation partially reflects the unified architecture but needs updates to be comprehensive.

**Desired outcome:** Clean removal or clear deprecation of unused code, updated documentation reflecting the unified work loop architecture, and verification that all phases work end-to-end.

## Current Behavior

### Deprecated Code Artifacts

1. **`mark-task` and `mark-phase` commands** (`sandbox/egg_lib/contract_cli.py:784-808`)
   - Still implemented and functional
   - Still listed in `.egg/phase-permissions.json:97-103` as allowed operations
   - Still enforced by gateway filter (`gateway/phase_filter.py:326-327`)
   - **Not called by any active workflow** — replaced by PR-based reviews (PR #285)

2. **Circuit breaker code** (`shared/egg_contracts/circuit_breaker.py`)
   - Fully implemented with threshold logic (3 per-task, 10 total pipeline cycles)
   - Functions exported and tested
   - Documentation already marks it as deprecated (PR #285)
   - **Still referenced in contract schema** — `circuit_breaker` field exists

3. **Legacy action scripts**
   - `action/escalate.sh` — explicitly marked deprecated in header (lines 4-14)
   - `action/contract-state.sh` — partially deprecated (lines 17-24), legacy functions remain

4. **Review prompt scripts**
   - `action/build-refine-review-prompt.sh` (220 lines)
   - `action/build-plan-review-prompt.sh` (229 lines)
   - Both are **still referenced by the workflow** but duplicative of `action/build-unified-review-prompt.sh`
   - The unified workflow uses `build-unified-review-prompt.sh` by default

### Documentation State

1. **`docs/guides/sdlc-pipeline.md`**
   - References the unified work loop workflow (`.github/workflows/sdlc-work-loop.yml`)
   - Documents circuit breaker as deprecated (lines 268-279)
   - Documents `mark-task`/`mark-phase` as deprecated (line 613)
   - Missing: Check DAG configuration details, unified work loop architecture diagram

2. **`docs/adr/implemented/ADR-SDLC-Pipeline.md`**
   - Documents circuit breaker deprecation (lines 171-202)
   - References PR-based reviews replacing dedicated reviewer agent
   - Missing: Unified work loop decision rationale, check DAG architecture

## Constraints

- **Backward compatibility**: Some external tooling may reference deprecated commands — need deprecation path, not immediate removal
- **Test coverage**: Tests exist for deprecated code (`tests/sandbox/test_contract_cli.py`) — removing code requires removing or skipping tests
- **Gateway enforcement**: Phase filter still includes deprecated operations — needs coordinated update
- **Documentation coherence**: Changes must not create broken internal links or orphaned references

## Options Considered

### Option A: Full Removal of Deprecated Code

**Approach**: Remove all deprecated code entirely — `mark-task`, `mark-phase`, circuit breaker functions, and legacy scripts.

**Pros**:
- Clean codebase with no dead code
- Reduces maintenance burden
- Clear signal that migration is complete

**Cons**:
- Breaking change if any external tooling uses deprecated commands
- Requires updating all tests that cover deprecated paths
- More invasive change with higher risk

### Option B: Soft Deprecation with Runtime Warnings

**Approach**: Keep code but add `@deprecated` decorators, emit runtime warnings, update documentation to clearly mark deprecated paths.

**Pros**:
- Non-breaking for any external consumers
- Provides migration path with warnings
- Lower risk — can iterate to full removal later

**Cons**:
- Dead code remains in codebase
- Maintenance overhead continues
- May confuse developers about what's active

### Option C: Hybrid — Remove Unused Internals, Keep CLI Commands

**Approach**:
1. Remove `escalate.sh` and legacy functions in `contract-state.sh` (never exposed externally)
2. Mark `circuit_breaker.py` functions as deprecated but keep them (used by contract schema)
3. Remove `mark-task`/`mark-phase` from `phase-permissions.json` and gateway filter
4. Make `build-refine-review-prompt.sh` and `build-plan-review-prompt.sh` thin wrappers calling the unified script
5. Update documentation comprehensively

**Pros**:
- Removes truly unused code
- Maintains backward compatibility for any edge cases
- Makes review scripts maintainable (single source of truth)
- Aligns with issue requirements

**Cons**:
- Still leaves some deprecated code (circuit_breaker.py)
- Requires careful verification that nothing calls removed code

## Recommended Approach

**Option C: Hybrid — Remove Unused Internals, Keep CLI Commands**

This approach directly addresses the issue tasks while minimizing risk:

### Code Cleanup Tasks

| Task | Action | Rationale |
|------|--------|-----------|
| `mark-task`/`mark-phase` in `phase-permissions.json` | Remove | No workflow uses these; gateway still blocks anyway |
| `circuit_breaker.py` | Add deprecation notice in docstring | Contract schema still references it; full removal needs schema migration |
| `escalate.sh` | Delete or move to `action/deprecated/` | Explicitly marked deprecated, not called |
| `contract-state.sh` deprecated functions | Remove deprecated functions, keep active ones | Reduce dead code while preserving utilities |
| `build-refine-review-prompt.sh` | Convert to thin wrapper calling unified script | Issue requirement; reduces duplication |
| `build-plan-review-prompt.sh` | Convert to thin wrapper calling unified script | Issue requirement; reduces duplication |

### Documentation Tasks

| Document | Updates Needed |
|----------|----------------|
| `docs/guides/sdlc-pipeline.md` | Add unified work loop section, check DAG configuration, update architecture diagram |
| `docs/adr/implemented/ADR-SDLC-Pipeline.md` | Add unified work loop decision, document check DAG order, note PR #285 context |

### Validation Tasks

1. Run refine phase end-to-end with a test issue
2. Run plan phase end-to-end
3. Run implement phase end-to-end
4. Verify escalation flow (circuit breaker trigger → human intervention → resume)

## Open Questions

### 1. Review Script Wrapper Behavior

The issue requests making `build-refine-review-prompt.sh` and `build-plan-review-prompt.sh` "thin wrappers calling unified script." Two approaches exist:

**a) Export EGG_PIPELINE_PHASE and call unified script:**
```bash
#!/usr/bin/env bash
export EGG_PIPELINE_PHASE="refine"
exec "$(dirname "$0")/build-unified-review-prompt.sh"
```

**b) Delete scripts and update any callers to use unified script directly:**
Remove the files entirely and update any workflow references.

The current `sdlc-work-loop.yml` already defaults to `build-unified-review-prompt.sh` (line 49), so the phase-specific scripts may only exist for backward compatibility with the old `sdlc-pipeline.yml`.

**Question**: Should the phase-specific review prompt scripts be thin wrappers or deleted entirely? The answer depends on whether any external workflows call them directly.

### 2. Circuit Breaker Removal Scope

The circuit breaker code in `shared/egg_contracts/circuit_breaker.py` is deprecated but still referenced by:
- Contract schema (`circuit_breaker` field in contracts)
- Pydantic models (`CircuitBreakerStatus`, `CircuitBreaker` classes)
- Audit log entries (references circuit breaker state changes)

**Question**: Should the circuit breaker field remain in contracts for audit trail purposes, or should it be removed entirely in a future schema migration?

### 3. Gateway Phase Filter Coordination

Removing `mark-task`/`mark-phase` from `phase-permissions.json` should be coordinated with removing them from `gateway/phase_filter.py`. However, the gateway may have deployment dependencies.

**Question**: Can the gateway changes be deployed atomically with the phase-permissions changes, or should they be sequenced?

---

*Authored-by: egg*
