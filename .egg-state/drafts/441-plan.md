# Plan: Fix SDLC Contract Validation

> Issue: #441 | Phase: plan

## Summary

The gateway currently validates branch ownership but does not inspect which files are being modified in a push. This allowed an implementer agent to directly commit changes to contract files (`.egg-state/contracts/*.json`) in PR #439, bypassing the intended contract mutation API and its role-based access control. This plan implements pre-push file validation in the gateway to block unauthorized file modifications based on the caller's role, following the approved "Comprehensive" approach from the analysis phase.

## Implementation Phases

### Phase 1: Extend Phase Permissions Schema

**Goal**: Add file restriction configuration to the phase-permissions schema, allowing role-based file protection patterns.

**Tasks**:
- [TASK-1-1] Extend `.egg/schemas/phase-permissions.schema.json` with `file_restrictions` section — Acceptance: Schema validates file restriction patterns with role, blocked_patterns, and blocked_reason fields
- [TASK-1-2] Add `file_restrictions` configuration to `.egg/phase-permissions.json` for implementer role — Acceptance: Configuration blocks `.egg-state/contracts/*.json` for implementer role with clear error message

**Dependencies**: None

**Exit criteria**: Schema and configuration files are valid JSON and pass schema validation

### Phase 2: Implement File Restriction Checking in Phase Filter

**Goal**: Add file restriction validation logic to the phase_filter module.

**Tasks**:
- [TASK-2-1] Add `FileRestriction` dataclass to `gateway/phase_filter.py` — Acceptance: Dataclass captures role, blocked_patterns, blocked_reason
- [TASK-2-2] Extend `PhasePermissions.from_dict()` to parse `file_restrictions` — Acceptance: File restrictions are loaded from JSON configuration
- [TASK-2-3] Add `check_file_restrictions()` method to `PhaseFilter` class — Acceptance: Method takes role and file list, returns `FilterResult` indicating if any files are blocked
- [TASK-2-4] Add module-level `check_file_restrictions()` convenience function — Acceptance: Function provides simple API matching existing `filter_operation()` pattern

**Dependencies**: Phase 1 complete

**Exit criteria**: File restriction checking works with unit tests passing

### Phase 3: Integrate File Validation into Gateway Push Handler

**Goal**: Add file-level validation to `git_push()` before executing the push.

**Tasks**:
- [TASK-3-1] Add helper function to get modified files from commits — Acceptance: Function runs `git diff --name-only` to detect files changed between local and remote branch
- [TASK-3-2] Add role extraction from session context in `git_push()` — Acceptance: Role is retrieved from `g.session.agent_role` when available
- [TASK-3-3] Integrate file restriction check before push execution — Acceptance: Push is blocked with 403 if restricted files are modified by unauthorized role
- [TASK-3-4] Add audit logging for file restriction violations — Acceptance: Blocked pushes are logged with role, files, and reason

**Dependencies**: Phase 2 complete

**Exit criteria**: Pushes modifying contract files by implementer role are blocked with clear error message

### Phase 4: Add Comprehensive Tests

**Goal**: Ensure file restriction logic is thoroughly tested.

**Tasks**:
- [TASK-4-1] Add unit tests for `FileRestriction` dataclass and parsing — Acceptance: Tests verify dataclass creation and JSON parsing
- [TASK-4-2] Add unit tests for `check_file_restrictions()` method — Acceptance: Tests cover allowed files, blocked files, multiple patterns, edge cases
- [TASK-4-3] Add integration tests for gateway push with file restrictions — Acceptance: Tests verify push blocked/allowed based on role and files
- [TASK-4-4] Add test for graceful degradation when role is not set — Acceptance: Push succeeds when role is unavailable (backwards compatibility)

**Dependencies**: Phase 3 complete

**Exit criteria**: All tests pass, coverage includes happy path and error cases

### Phase 5: Documentation and Cleanup

**Goal**: Document the new file restriction feature and update related documentation.

**Tasks**:
- [TASK-5-1] Update `gateway/README.md` with file restriction documentation — Acceptance: README explains file restriction feature, configuration, and error messages
- [TASK-5-2] Add inline comments to new code explaining security rationale — Acceptance: Key security decisions are documented in code comments

**Dependencies**: Phase 4 complete

**Exit criteria**: Documentation is complete and accurate

## Test Strategy

- **Unit tests**:
  - `FileRestriction` dataclass creation and `from_dict()` parsing
  - `check_file_restrictions()` with various file/role combinations
  - Pattern matching with glob patterns (wildcards, nested paths)
  - Edge cases: empty file list, no restrictions configured, missing role

- **Integration tests**:
  - Gateway push endpoint with mocked git operations
  - Push allowed when role has access to modified files
  - Push blocked when implementer modifies contract files
  - Push allowed when reviewer modifies contract files
  - Push succeeds when no role is set (backwards compatibility)

- **Manual testing**:
  - Deploy gateway with new configuration
  - Attempt push with contract file changes as implementer role
  - Verify error message is clear and actionable
  - Verify push succeeds for allowed files

## Rollback Plan

1. **Configuration rollback**: Remove `file_restrictions` section from `.egg/phase-permissions.json` to disable file validation while keeping code in place
2. **Code rollback**: Revert the commit(s) introducing file validation if needed
3. **Feature flag**: The implementation checks for file_restrictions presence; if not configured, validation is skipped (safe default)

Commands for rollback:
```bash
# Option 1: Disable via configuration (preferred)
# Edit .egg/phase-permissions.json and remove "file_restrictions" section

# Option 2: Full code rollback
git revert <commit-sha>
git push origin egg/issue-441
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Performance impact from git diff on every push | Low | Low | git diff is fast; only runs when file_restrictions configured |
| False positives blocking legitimate changes | Low | Medium | Test thoroughly; patterns are explicit in config |
| Role not available in some push scenarios | Medium | Low | Graceful degradation - skip validation when role unavailable |
| Pattern matching edge cases (paths with spaces, unicode) | Low | Low | Use Python's fnmatch which handles these correctly |
| Breaking existing workflows without SDLC | Low | Medium | Feature only activates when file_restrictions configured |

## Migration Notes

- **No database migrations required**
- **Configuration change**: `.egg/phase-permissions.json` gains new `file_restrictions` section
- **Schema change**: `.egg/schemas/phase-permissions.schema.json` updated with new definitions
- **Breaking changes**: None - feature is additive and backwards compatible
- **Deployment**: Deploy gateway with new code first, then update configuration

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.

```yaml
# yaml-tasks
pr:
  title: "Block implementer from modifying contract files via git push"
  description: |
    Adds pre-push file validation to the gateway to prevent implementer agents
    from directly modifying contract files (.egg-state/contracts/*.json) via
    git push. Contract modifications must go through the contract mutation API
    which enforces role-based access control.

    Fixes #441
phases:
  - id: 1
    name: Extend Phase Permissions Schema
    goal: Add file restriction configuration to phase-permissions schema
    tasks:
      - id: TASK-1-1
        description: Extend phase-permissions.schema.json with file_restrictions section
        acceptance: Schema validates file restriction patterns with role, blocked_patterns, and blocked_reason fields
        files:
          - .egg/schemas/phase-permissions.schema.json
      - id: TASK-1-2
        description: Add file_restrictions configuration to phase-permissions.json
        acceptance: Configuration blocks .egg-state/contracts/*.json for implementer role
        files:
          - .egg/phase-permissions.json
  - id: 2
    name: Implement File Restriction Checking
    goal: Add file restriction validation logic to phase_filter module
    tasks:
      - id: TASK-2-1
        description: Add FileRestriction dataclass to phase_filter.py
        acceptance: Dataclass captures role, blocked_patterns, blocked_reason
        files:
          - gateway/phase_filter.py
      - id: TASK-2-2
        description: Extend PhasePermissions.from_dict() to parse file_restrictions
        acceptance: File restrictions are loaded from JSON configuration
        files:
          - gateway/phase_filter.py
      - id: TASK-2-3
        description: Add check_file_restrictions() method to PhaseFilter class
        acceptance: Method takes role and file list, returns FilterResult
        files:
          - gateway/phase_filter.py
      - id: TASK-2-4
        description: Add module-level check_file_restrictions() convenience function
        acceptance: Function provides simple API matching filter_operation() pattern
        files:
          - gateway/phase_filter.py
  - id: 3
    name: Integrate File Validation into Gateway
    goal: Add file-level validation to git_push() before executing push
    tasks:
      - id: TASK-3-1
        description: Add helper function to get modified files from commits
        acceptance: Function runs git diff --name-only to detect changed files
        files:
          - gateway/gateway.py
      - id: TASK-3-2
        description: Add role extraction from session context in git_push()
        acceptance: Role is retrieved from g.session.agent_role when available
        files:
          - gateway/gateway.py
      - id: TASK-3-3
        description: Integrate file restriction check before push execution
        acceptance: Push blocked with 403 if restricted files modified by unauthorized role
        files:
          - gateway/gateway.py
      - id: TASK-3-4
        description: Add audit logging for file restriction violations
        acceptance: Blocked pushes logged with role, files, and reason
        files:
          - gateway/gateway.py
  - id: 4
    name: Add Comprehensive Tests
    goal: Ensure file restriction logic is thoroughly tested
    tasks:
      - id: TASK-4-1
        description: Add unit tests for FileRestriction dataclass and parsing
        acceptance: Tests verify dataclass creation and JSON parsing
        files:
          - gateway/tests/test_phase_filter.py
      - id: TASK-4-2
        description: Add unit tests for check_file_restrictions() method
        acceptance: Tests cover allowed files, blocked files, multiple patterns
        files:
          - gateway/tests/test_phase_filter.py
      - id: TASK-4-3
        description: Add integration tests for gateway push with file restrictions
        acceptance: Tests verify push blocked/allowed based on role and files
        files:
          - gateway/tests/test_gateway_file_restrictions.py
      - id: TASK-4-4
        description: Add test for graceful degradation when role is not set
        acceptance: Push succeeds when role unavailable (backwards compatibility)
        files:
          - gateway/tests/test_gateway_file_restrictions.py
  - id: 5
    name: Documentation and Cleanup
    goal: Document the new file restriction feature
    tasks:
      - id: TASK-5-1
        description: Update gateway/README.md with file restriction documentation
        acceptance: README explains feature, configuration, and error messages
        files:
          - gateway/README.md
      - id: TASK-5-2
        description: Add inline comments explaining security rationale
        acceptance: Key security decisions documented in code comments
        files:
          - gateway/gateway.py
          - gateway/phase_filter.py
```

---

*Authored-by: egg*
