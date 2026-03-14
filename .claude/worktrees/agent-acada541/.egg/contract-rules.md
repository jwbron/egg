# Contract Verification Rules

Guidelines for verifying SDLC contract compliance.

## Task Verification

For each task in the contract, verify:

### 1. Implementation Completeness
- The described functionality exists in the codebase
- All files listed in `files_affected` have been modified
- The implementation matches the task description

### 2. Acceptance Criteria
- Each task has specific acceptance criteria
- Verify the criteria are objectively met, not partially or loosely
- If criteria mentions tests, verify tests exist and pass

### 3. Commit Linkage
- If a commit is linked to a task, verify the commit's changes relate to the task
- Commits should be atomic and focused on their linked task
- Multiple commits per task is acceptable if they build on each other

## Phase Consistency

Check that phase status reflects task completion:

- **pending**: No tasks started
- **in_progress**: At least one task started, not all complete
- **complete**: All tasks marked complete and verified
- **blocked**: Task(s) cannot proceed due to external dependency

### Red Flags
- Phase marked complete but tasks are pending
- Tasks marked complete but code is missing
- Orphaned code not covered by any task

## Acceptance Criteria Verification

The contract contains top-level `acceptance_criteria` that map to tasks:

```
[TASK-1-1] Description here
```

For each criterion:

1. **Read carefully**: Understand exactly what must be true
2. **Examine evidence**: Find the code, test, or artifact that proves compliance
3. **Verify objectively**: Don't assume—check that it actually works
4. **Mark verified**: Use `egg-contract verify-criterion --criterion ac-N`

### Verification Standards

**Verified** means:
- Code exists and is syntactically correct
- Logic matches the requirement
- Edge cases are handled where specified
- Tests pass (if testing is part of criteria)

**Not Verified** means:
- Code is missing or incomplete
- Logic doesn't match requirement
- Obvious bugs or missing error handling
- Tests fail or don't exist where required

## Contract Integrity on Re-review

When re-reviewing after changes:

1. **Check regressions**: Ensure previously verified criteria still hold
2. **Verify new work**: Apply full verification to newly completed tasks
3. **Flag violations**: If a change breaks a verified criterion, flag it clearly

## Review Verdict Guidelines

### Approve
- All acceptance criteria are verified
- All tasks are complete and correctly implemented
- No contract violations found

### Request Changes
- One or more acceptance criteria not met
- Task implementation doesn't match description
- Contract violations found
- Missing tests for tasks that require them

### Comment
- Minor suggestions that don't block approval
- Questions about implementation choices
- Notes for human reviewer
