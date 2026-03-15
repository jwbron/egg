# Plan: Enforce Phase Restrictions on PR Creation

> Issue: #518 | Phase: plan

## Summary

The SDLC pipeline is creating PRs during the implement phase before checks pass, violating phase restrictions. This occurs because the gateway's `gh_pr_create` endpoint does not enforce phase restrictions defined in `phase-permissions.json`. The fix adds phase filtering to the gateway PR create endpoint, using session metadata to track the current pipeline phase. This provides defense-in-depth enforcement alongside prompt-based guidance.

## Implementation Phases

### Phase 1: Add Phase Field to Session Manager

**Goal**: Extend the session model to track pipeline phase, enabling phase-aware policy enforcement.

**Tasks**:
- [TASK-1-1] Add `phase` field to `Session` dataclass in `session_manager.py` — Acceptance: Field added with type `str | None`, persisted to disk
- [TASK-1-2] Update `to_dict_for_persistence()` and `from_persistence()` methods — Acceptance: Phase field is serialized/deserialized correctly
- [TASK-1-3] Add `update_phase()` method to `SessionManager` — Acceptance: Method updates phase and persists, returns success/failure

**Dependencies**: None

**Exit criteria**: Session model can store and retrieve phase information

### Phase 2: Create Session Phase Update API Endpoint

**Goal**: Provide an API for the launcher/workflow to update session phase as the pipeline progresses.

**Tasks**:
- [TASK-2-1] Add `PATCH /api/v1/sessions/{token}/phase` endpoint to `gateway.py` — Acceptance: Endpoint accepts phase string, validates against PipelinePhase enum, updates session
- [TASK-2-2] Add launcher auth requirement to the endpoint — Acceptance: Only launcher_secret authenticated requests can update phase

**Dependencies**: Phase 1

**Exit criteria**: Phase can be updated via authenticated API call

### Phase 3: Enforce Phase Restrictions in PR Create Endpoint

**Goal**: Block PR creation during phases where it is not permitted (refine, plan, implement).

**Tasks**:
- [TASK-3-1] Retrieve session phase in `gh_pr_create()` after auth validation — Acceptance: Phase is available from session object via `g.session_phase`
- [TASK-3-2] Call `filter_operation()` with phase and "pr create*" pattern — Acceptance: Returns FilterResult indicating allowed/blocked
- [TASK-3-3] Return 403 with clear error message when blocked — Acceptance: Error message matches format used in phase_filter.py, audit logged
- [TASK-3-4] Allow by default when phase is None (backward compatibility) — Acceptance: Existing sessions without phase continue to work, warning logged

**Dependencies**: Phase 1

**Exit criteria**: PR creation is blocked during refine, plan, and implement phases

### Phase 4: Pass Phase to Session on Creation

**Goal**: Initialize session with the correct phase at container startup.

**Tasks**:
- [TASK-4-1] Add optional `phase` parameter to `create_session()` in `sandbox/egg_lib/gateway.py` — Acceptance: Phase is passed in request body
- [TASK-4-2] Update gateway's `session_create` endpoint to accept and store phase — Acceptance: Phase from request is saved to session
- [TASK-4-3] Pass `EGG_PIPELINE_PHASE` to session creation in `exec_in_new_container()` — Acceptance: Phase from environment is passed through

**Dependencies**: Phase 1, Phase 2

**Exit criteria**: New sessions are created with the correct pipeline phase

### Phase 5: Add Unit Tests

**Goal**: Ensure the new functionality is tested and regression-proof.

**Tasks**:
- [TASK-5-1] Add test for session phase persistence in `test_session_manager.py` — Acceptance: Phase survives save/load cycle
- [TASK-5-2] Add test for phase restriction on PR create in gateway tests — Acceptance: 403 returned during implement phase, 200 during pr phase
- [TASK-5-3] Add test for backward compatibility (no phase = allowed) — Acceptance: Legacy sessions without phase can create PRs

**Dependencies**: Phases 1-4

**Exit criteria**: All new functionality has test coverage

## Test Strategy

- **Unit tests**:
  - Session manager: Test phase field persistence and update
  - Gateway: Test phase enforcement on `gh_pr_create` endpoint
  - Phase filter: Verify "pr create*" pattern blocks in implement phase
- **Integration tests**:
  - End-to-end test with container session having phase set
  - Verify PR create blocked during implement, allowed during pr phase
- **Manual testing**:
  - Run SDLC pipeline in implement phase, verify agent cannot create PR
  - Run pipeline through to human-gate-pr job, verify PR creation succeeds

## Rollback Plan

If issues arise after deployment:

1. **Quick rollback**: Set `session_phase = None` for all sessions to disable enforcement (allowed by default)
2. **Full rollback**: Revert commits in order:
   - Remove phase check from `gh_pr_create()`
   - Remove phase update endpoint
   - Remove phase field from session (data migration not needed - extra fields ignored)
3. **Commands**:
   ```bash
   git revert HEAD~N..HEAD  # Where N is number of commits
   git push origin egg/issue-518
   ```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaks existing agent workflows | Low | High | Backward compat: allow when phase is None, log warning |
| Pipeline PR creation breaks | Low | High | human-gate-pr runs on GHA, not gateway - verified in analysis |
| Phase not propagated correctly | Medium | Medium | Add logging/metrics for phase at session creation |
| Performance impact from phase check | Low | Low | Phase check is in-memory lookup, <1ms |

## Migration Notes

- **No database migration required**: Phase field is optional, defaults to None
- **Backward compatible**: Existing sessions continue to work (allowed when phase is None)
- **No config changes**: Phase permissions already defined in `phase-permissions.json`
- **No workflow changes**: Pipeline already sets `EGG_PIPELINE_PHASE` environment variable

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Enforce phase restrictions on PR creation in gateway"
  description: |
    Prevents agents from creating PRs during refine, plan, and implement phases
    by adding phase filtering to the gateway's gh_pr_create endpoint. Sessions
    now track pipeline phase, and the existing phase-permissions.json rules are
    enforced at the gateway level. Fixes #518.
phases:
  - id: 1
    name: Add Phase Field to Session Manager
    goal: Extend session model to track pipeline phase
    tasks:
      - id: TASK-1-1
        description: Add phase field to Session dataclass
        acceptance: Field added with type str | None, persisted to disk
        files:
          - gateway/session_manager.py
      - id: TASK-1-2
        description: Update persistence methods for phase field
        acceptance: Phase field is serialized/deserialized correctly
        files:
          - gateway/session_manager.py
      - id: TASK-1-3
        description: Add update_phase method to SessionManager
        acceptance: Method updates phase and persists, returns success/failure
        files:
          - gateway/session_manager.py
  - id: 2
    name: Create Session Phase Update API Endpoint
    goal: Provide API for updating session phase
    tasks:
      - id: TASK-2-1
        description: Add PATCH /api/v1/sessions/{token}/phase endpoint
        acceptance: Endpoint validates phase, updates session
        files:
          - gateway/gateway.py
      - id: TASK-2-2
        description: Add launcher auth requirement to endpoint
        acceptance: Only launcher_secret can update phase
        files:
          - gateway/gateway.py
  - id: 3
    name: Enforce Phase Restrictions in PR Create Endpoint
    goal: Block PR creation during non-permitted phases
    tasks:
      - id: TASK-3-1
        description: Retrieve session phase in gh_pr_create after auth
        acceptance: Phase available from session via g.session_phase
        files:
          - gateway/gateway.py
      - id: TASK-3-2
        description: Call filter_operation with phase and pr create pattern
        acceptance: Returns FilterResult indicating allowed/blocked
        files:
          - gateway/gateway.py
      - id: TASK-3-3
        description: Return 403 with clear error message when blocked
        acceptance: Error matches phase_filter format, audit logged
        files:
          - gateway/gateway.py
      - id: TASK-3-4
        description: Allow by default when phase is None for backward compat
        acceptance: Legacy sessions work, warning logged
        files:
          - gateway/gateway.py
  - id: 4
    name: Pass Phase to Session on Creation
    goal: Initialize session with correct phase at startup
    tasks:
      - id: TASK-4-1
        description: Add phase parameter to create_session in launcher
        acceptance: Phase passed in request body
        files:
          - sandbox/egg_lib/gateway.py
      - id: TASK-4-2
        description: Update session_create endpoint to accept phase
        acceptance: Phase from request saved to session
        files:
          - gateway/gateway.py
      - id: TASK-4-3
        description: Pass EGG_PIPELINE_PHASE to session creation
        acceptance: Phase from environment passed through
        files:
          - sandbox/egg_lib/runtime.py
  - id: 5
    name: Add Unit Tests
    goal: Ensure new functionality is tested
    tasks:
      - id: TASK-5-1
        description: Add test for session phase persistence
        acceptance: Phase survives save/load cycle
        files:
          - gateway/tests/test_session_manager.py
      - id: TASK-5-2
        description: Add test for phase restriction on PR create
        acceptance: 403 during implement, 200 during pr phase
        files:
          - gateway/tests/test_gateway.py
      - id: TASK-5-3
        description: Add test for backward compatibility
        acceptance: Legacy sessions without phase can create PRs
        files:
          - gateway/tests/test_gateway.py
```

---

*Authored-by: egg*
