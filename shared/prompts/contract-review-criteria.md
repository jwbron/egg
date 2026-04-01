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

### Artifact Verification

For each task in the contract that lists `files_affected`, verify that every listed file
exists **on the remote branch**, not just in the local worktree:

1. Run `git fetch origin` to ensure the remote ref is up to date
2. For each required file path, run `git show origin/$EGG_BRANCH:<file_path>`
3. If the command fails (file not found on remote), the task artifact has **not been pushed** and the task is **not complete**

**CRITICAL**: Do NOT use local file existence checks (`ls`, `cat`, `test -f`, `Path.exists()`)
to verify task artifacts. In shared worktree environments, unpushed files from other agents
may be visible locally but absent from the remote branch. Only `git show origin/...` confirms
the artifact was actually committed and pushed.

If any required artifact is missing from the remote branch, NACK the proposal and list
the missing files with an explanation that they must be committed and pushed before the
task can be considered complete.

### Contract Integrity

Verify:
- No implementation changes violate previously verified criteria
- New changes don't break existing contract compliance
- All required files listed in tasks are present on the remote branch (see Artifact Verification above)
