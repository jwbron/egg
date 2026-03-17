# Analysis: Define agent team roster, roles, and access controls

> Issue: #1030 | Phase: refine

## Problem Statement

The current egg platform has 13 agent roles hard-wired to SDLC phases (refine, plan, implement). Issue #1030 calls for a comprehensive definition of the agent roster for the new async, self-organizing agent team model (#1027, #1028). This includes:

1. **Formalizing which agent types exist** — their responsibilities, access restrictions, and communication patterns
2. **Generalizing concurrency beyond the implement phase** — today `ConcurrentPhaseExecutor` already supports refine/plan/implement, but the issue demands universal concurrency for ALL agent categories and workflow types
3. **Codebase cleanup** — removing vestigial roles (`reviewer_unified`, `integrator`, `checker`)
4. **Documenting the shared worktree model** — role-level file isolation as the safety mechanism for concurrent execution
5. **Defining team composition templates** — how the orchestrator selects which agents to spawn based on task complexity

The desired outcome is a clear, authoritative roster document that drives implementation across the orchestrator, gateway, and sandbox components.

## Current Behavior

### Agent Role Definitions

The current roster is defined in three locations:

- **`shared/egg_contracts/agent_roles.py`** — The canonical source. Defines 13 roles via `AgentRole(StrEnum)`: `CODER`, `TESTER`, `DOCUMENTER`, `ARCHITECT`, `TASK_PLANNER`, `RISK_ANALYST`, `REFINER`, `REVIEWER_CODE`, `REVIEWER_CONTRACT`, `REVIEWER_AGENT_DESIGN`, `REVIEWER_REFINE`, `REVIEWER_PLAN`, `OVERSEER`. Each has an `AgentRoleDefinition` with responsibilities, dependencies, file access patterns, and I/O declarations.
- **`orchestrator/models.py`** — A duplicate `AgentRole` enum used by orchestrator models and Pydantic schemas.
- **`shared/egg_orchestrator/types.py`** — Another copy used by the shared orchestrator client types.

### Concurrent Execution Support

The `ConcurrentPhaseExecutor` in `orchestrator/concurrent_executor.py` already supports multiple phases:

- `get_agent_roles()` (lines 92–106) dynamically calls `get_roles_for_phase(phase)` — it is **not** hardcoded to implement-phase roles. The issue's claim of hardcoded `[CODER, TESTER, DOCUMENTER]` appears to be outdated.
- `is_concurrent_execution()` (lines 400–420) checks `pipeline.config.concurrent_execution` (all phases) or `pipeline.config.concurrent_phases` (defaults to `["refine", "plan", "implement"]`).

So the infrastructure is **already phase-agnostic** in its current form. The remaining work is about:
- Ensuring all phase/role combinations are tested and supported end-to-end
- Updating documentation that still describes concurrency as implement-only
- Adding new agent roles (autofixer, conflict resolver) to the roster

### Vestigial Role Cleanup Status

- **`reviewer_unified`**: Already removed from enums. Migration logic in `orchestrator/models.py` maps persisted `reviewer_unified` → `reviewer_code`. Tests validate this migration.
- **`checker`**: Already removed from enums. Migration logic maps `checker` → `tester`. Tests validate this.
- **`integrator`**: Not in any enum. References remain in `orchestrator/tests/test_concurrent_integration.py` (test fixtures using "integrator" as a role string), `orchestrator/tests/test_dag_visualizer.py`, and `orchestrator/tests/test_removal_validation_1165.py`. The `gateway/worktree_manager.py` has one comment reference. No mode files exist for integrator.

### Gateway File Restrictions

The gateway enforces per-role write restrictions via `AGENT_PATTERNS` in `gateway/agent_restrictions.py`. All 13 current roles have defined patterns. The pattern registry and enforcement logic are role-based (not phase-based), which already supports the shared worktree safety model described in the issue.

### Dependency Issues

- **#1027 (Cross-agent communication)**: Analysis recommends a polling-based message bus via orchestrator. Agents poll `egg-orch message poll`. Infrastructure (`orchestrator/message_store.py`, `routes/messages.py`) is already phase-agnostic.
- **#1028 (Agent team framework)**: Analysis recommends a Coordinator as an orchestrator extension with a `COORDINATOR` role. This introduces a new role not yet in the roster.
- **#1059 (Overseer agent)**: Analysis recommends a hybrid approach — deterministic tripwires in the orchestrator + an `OVERSEER` container for semantic analysis. The `OVERSEER` role already exists in the codebase.

## Constraints

- **Backward compatibility**: Persisted pipeline state may contain removed role names. Migration logic (`_REMOVED_ROLE_MIGRATION`) must be maintained for any roles being removed.
- **Three-location enum sync**: `AgentRole` is defined in three files. Any roster change must update all three, or ideally consolidate to a single source of truth.
- **Gateway enforcement mode**: File restrictions are currently warn-only by default (`EGG_AGENT_RESTRICTIONS_ENFORCE=true` to block). The shared worktree model's safety depends on strict enforcement becoming the default.
- **Shared worktree file conflicts**: Two roles currently have overlapping write patterns — coder and tester both can write `**/*.py`, `**/*.ts`, etc. (tester needs this for lint auto-fixes). The issue proposes more restrictive tester patterns but this may break the auto-fix workflow.
- **Coordinator role**: #1028's analysis recommends a `COORDINATOR` role with elevated permissions (spawn agents, skip phases). This is a new role type not covered in #1030's issue body — its scope and permissions need alignment.
- **Dependencies**: #1027 and #1028 define the communication and team frameworks that agents will use. The roster should be designed to work with both but not block on their implementation.

## Options Considered

### Option A: Incremental Roster Update (Recommended)

**Approach**: Update the existing `AgentRole` enum and role definitions in-place. Add new roles (`AUTOFIXER`, `CONFLICT_RESOLVER`), clean up remaining `integrator` test references, and update all documentation. Keep the three-location enum pattern but ensure sync. Formalize the tester's expanded scope (absorbing checker) in its role definition.

**Pros**:
- Minimal disruption — builds on the already-functional role system
- No migration needed for the 13 existing roles (they're already correct)
- Can be done incrementally without blocking on #1027 or #1028
- Gateway patterns already exist for all current roles

**Cons**:
- Three-location enum remains a sync hazard
- Doesn't address the coordinator role from #1028

### Option B: Consolidated Role Registry with Category Metadata

**Approach**: Consolidate the three `AgentRole` enums into a single source (in `shared/egg_contracts/agent_roles.py`). Add category metadata (execution, analysis, review, utility, interface) to `AgentRoleDefinition`. Import from the single source everywhere. Add new roles for autofixer, conflict resolver, and potentially coordinator.

**Pros**:
- Single source of truth eliminates sync bugs
- Category metadata enables dynamic team composition queries (e.g., "all review agents")
- Better foundation for the team framework (#1028)
- Cleaner separation of role definition from phase mapping

**Cons**:
- Requires updating all imports across orchestrator and shared packages
- Pydantic model validators in `orchestrator/models.py` may need special handling
- More work upfront

### Option C: Full Roster Redesign with Dynamic Role Capabilities

**Approach**: Replace the static enum + role-definition pattern with a capability-based system. Roles are defined by their capabilities (can_write_source, can_review, can_spawn_agents, etc.) and the orchestrator composes teams dynamically based on required capabilities.

**Pros**:
- Maximum flexibility for future agent types
- Natural fit for dynamic team composition
- Capabilities can be mixed/matched without new enum values

**Cons**:
- Significant architectural change — high risk for a specification issue
- Overkill for the current roster size (15-16 roles)
- Gateway enforcement is currently role-string-based; capability-based would require rearchitecting
- Premature abstraction — the team model is still being defined

## Recommended Approach

**Option B: Consolidated Role Registry with Category Metadata.**

This balances pragmatism with the structural improvements needed:

1. **Consolidate the `AgentRole` enum** to `shared/egg_contracts/agent_roles.py` as the single source. `orchestrator/models.py` and `shared/egg_orchestrator/types.py` import from there (or re-export for backward compatibility).
2. **Add category metadata** to `AgentRoleDefinition` — an `AgentCategory` enum with values `EXECUTION`, `ANALYSIS`, `REVIEW`, `UTILITY`, `INTERFACE`. This supports dynamic team queries without breaking the existing role-based access control.
3. **Add new roles**: `AUTOFIXER` and `CONFLICT_RESOLVER` as `UTILITY` category roles. Defer `COORDINATOR` until #1028 implementation clarifies its exact requirements.
4. **Clean up integrator references** in test files.
5. **Resolve tester write-pattern overlap** with the coder — the tester's `**/*.py` patterns exist for lint auto-fixes. The issue proposes restricting tester to test dirs + specific source dirs. This needs a human decision (see Open Questions).
6. **Update gateway patterns** to add entries for new roles (autofixer, conflict resolver).
7. **Update all documentation** — agent-roles.md, guides, architecture docs, CLAUDE.md instructions.
8. **Switch gateway enforcement to strict by default** — the shared worktree safety model requires it.

This approach keeps the current role-based model (which works well with the gateway) while adding the organizational structure needed for dynamic team composition.

## Open Questions

The following questions require human input and have been registered via `egg-contract`.

### Decisions (multiple choice)

1. **Tester source-file write access**: The tester currently has `**/*.py`, `**/*.ts`, etc. in its writable patterns (needed for lint/type-check auto-fixes). The issue proposes restricting the tester to test directories and specific source dirs (`src/`, `lib/`, etc.). Should the tester retain broad source-file write access for auto-fixes, or be restricted to test files only?

2. **Coordinator role inclusion**: #1028's analysis recommends a `COORDINATOR` role with elevated permissions. Should this role be defined in this issue's roster, deferred to #1028 implementation, or scoped as a placeholder with TBD permissions?

3. **Enum consolidation scope**: The `AgentRole` enum exists in three files. Should this issue consolidate them into a single source, or keep the current pattern and add a sync test?

4. **Gateway enforcement default**: The shared worktree safety model requires per-role file restrictions to be enforced (not warn-only). Should enforcement become the default in this issue, or remain opt-in?

### Feedback (open-ended)

5. **Missing agent types**: Are there agent types beyond the proposed roster that should be included? The issue mentions potential candidates: security auditor, performance profiler, dependency updater.

6. **Tester/coder docstring handoff**: Both coder and documenter may need to modify docstrings in source files. What coordination mechanism is preferred — message-based handoff, coder-owns-all-source, or granular per-region restrictions?

7. **Overseer restart authority**: Should the overseer autonomously restart agents after N failed redirects, or always escalate to HITL? What is the appropriate threshold?

## Complexity Assessment

**High.** This issue is a cross-cutting specification that affects the orchestrator (`models.py`, `concurrent_executor.py`), gateway (`agent_restrictions.py`), shared libraries (`agent_roles.py`, `types.py`), sandbox configuration, and extensive documentation. It introduces new roles, removes vestigial ones, and requires synchronizing multiple definitions. The impact spans every pipeline phase and touches the foundation of the shared worktree concurrency model. Multiple independent work streams (enum consolidation, new role patterns, cleanup, documentation) could be parallelized.

---

*Authored-by: egg*
