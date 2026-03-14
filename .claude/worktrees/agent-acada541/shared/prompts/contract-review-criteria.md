<!-- Shared contract verification criteria: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Default Contract Verification Rules

### Task Verification

For each task in the contract, verify:

1. **Implementation exists**: The described functionality is present in the code
2. **Acceptance criteria met**: The specific acceptance criteria for the task is satisfied
3. **Commit linked**: If a commit is linked, verify it relates to the task
4. **Tests present**: Where applicable, tests cover the new functionality

### Phase Consistency

Check that:
- All tasks in completed phases are actually implemented
- Phase status matches task completion state
- No orphaned code exists that isn't covered by any task

### Acceptance Criteria Verification

For each acceptance criterion in the contract:
1. Read the criterion description
2. Examine the implementation to verify it meets the criterion
3. Note any gaps in your review

### Contract Integrity

Verify:
- No implementation changes violate previously verified criteria
- New changes don't break existing contract compliance
- All required files listed in tasks are present
