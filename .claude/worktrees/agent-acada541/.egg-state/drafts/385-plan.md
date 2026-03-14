# Plan: Handle merge conflicts in contract push-with-retry logic

> Issue: #385 | Phase: plan

## Summary

The push-with-retry logic in SDLC workflows fails when `git pull --rebase` encounters merge conflicts on contract JSON files. This plan implements a shared helper script that uses the "reset-and-reapply" pattern: on push failure, abort any in-progress rebase, fetch the latest remote state, and re-apply the jq transformation from scratch. This ensures clean git history and eliminates the vulnerability to merge conflicts.

Based on the analysis from the refine phase, there are **9 locations** across two workflow files that need updating. Per human feedback, we will use a shared script to maintain consistency and clean git history.

## Implementation Phases

### Phase 1: Create Shared Helper Script

**Goal**: Create a reusable script that encapsulates the reset-and-reapply retry logic, making it easy to invoke from workflow steps.

**Tasks**:
- [TASK-1-1] Create `.github/scripts/` directory structure — Acceptance: Directory exists
- [TASK-1-2] Implement `push-contract-update.sh` helper script — Acceptance: Script handles reset-and-reapply pattern, accepts jq filter and commit message as parameters, supports both simple and complex (multi-step) jq transformations

**Dependencies**: None

**Exit criteria**: Script exists, is executable, and handles the full retry logic with reset-and-reapply pattern.

### Phase 2: Update sdlc-pipeline.yml Push Locations

**Goal**: Replace all 6 push-with-retry blocks in `sdlc-pipeline.yml` with calls to the shared helper script.

**Tasks**:
- [TASK-2-1] Add retry logic to line 363 (populate contract tasks) — Acceptance: Uses helper script, no longer a bare `git push`
- [TASK-2-2] Update line 438 (checkpoint push) — Acceptance: Uses helper script with soft-failure mode (warns but continues)
- [TASK-2-3] Update line 940 (advance to PR phase) — Acceptance: Uses helper script, exits on failure
- [TASK-2-4] Update line 1465 (update refine review state) — Acceptance: Uses helper script, handles complex multi-step jq
- [TASK-2-5] Update line 1784 (populate contract from plan) — Acceptance: Uses helper script, exits on failure
- [TASK-2-6] Update line 2144 (update plan review state) — Acceptance: Uses helper script, handles complex multi-step jq

**Dependencies**: Phase 1

**Exit criteria**: All 6 locations in `sdlc-pipeline.yml` use the helper script.

### Phase 3: Update sdlc-hitl.yml Push Locations

**Goal**: Replace all 3 push-with-retry blocks in `sdlc-hitl.yml` with calls to the shared helper script.

**Tasks**:
- [TASK-3-1] Update line 230 (resolve HITL decision) — Acceptance: Uses helper script, handles multi-step jq transformation
- [TASK-3-2] Update line 331 (advance to next phase) — Acceptance: Uses helper script, exits on failure
- [TASK-3-3] Update line 552 (approve and advance phase) — Acceptance: Uses helper script, handles audit log entry

**Dependencies**: Phase 1

**Exit criteria**: All 3 locations in `sdlc-hitl.yml` use the helper script.

### Phase 4: Testing and Verification

**Goal**: Verify the fix works correctly and document the changes.

**Tasks**:
- [TASK-4-1] Run workflow linting (if available) — Acceptance: No lint errors in modified workflow files
- [TASK-4-2] Add inline comments explaining the reset-and-reapply pattern — Acceptance: Comments explain why this approach prevents merge conflicts

**Dependencies**: Phases 2 and 3

**Exit criteria**: Linting passes, changes are documented.

## Test Strategy

- **Unit tests**: The helper script will be tested manually by reviewing the bash logic for correctness. Shell scripts in `.github/scripts/` are not typically unit tested.
- **Integration tests**: GitHub Actions workflows cannot be tested locally. The fix will be verified by:
  1. Code review to ensure the reset-and-reapply pattern is correctly implemented
  2. Observing subsequent pipeline runs for successful conflict recovery
- **Manual testing**: After merge, monitor the next few pipeline runs that experience concurrent pushes to verify conflicts are handled gracefully.

## Rollback Plan

If something goes wrong after merge:

1. **Immediate rollback**: Revert the PR commit:
   ```bash
   git revert <commit-sha>
   git push origin main
   ```

2. **Partial rollback**: If only the helper script has issues, inline the retry logic temporarily by cherry-picking the original workflow patterns.

3. **Monitoring**: Watch for `::error::Push failed after 3 attempts` in workflow logs. If this appears more frequently than before, investigate and consider rollback.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Helper script has a bug | Low | High | Thorough code review; the logic is straightforward bash |
| Workflows fail to invoke helper script | Low | Medium | Verify script path and permissions; test in staging if possible |
| Complex jq transformations break when parameterized | Medium | Medium | Use heredoc variables for complex filters; preserve exact existing jq logic |
| Retry loop runs indefinitely | Low | Low | MAX_RETRIES=3 is hardcoded; loop will exit after 3 attempts |

## Migration Notes

- No database migrations required
- No configuration changes required
- No breaking changes for users
- Workflows will automatically use the new script on next run

## Design Decisions

**Why a shared script instead of inline fixes?**
- Per human feedback: "if we can cleanly handle the share and git history, we should"
- Reduces code duplication across 9 locations
- Single source of truth for the retry logic
- Easier to update if further improvements are needed

**Why reset-and-reapply instead of trying to resolve conflicts?**
- JSON merge conflicts are complex to resolve automatically
- The jq transformations are idempotent, so re-applying them is safe
- This pattern guarantees a clean commit history with no merge conflict markers

**Script interface design:**
The script will accept:
1. Branch name
2. Contract file path
3. Commit message
4. jq filter (can be multi-line via heredoc)
5. Optional: soft-failure flag (for checkpoint step)

This allows all 9 locations to use the same script with different parameters.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above.

```yaml
# yaml-tasks
pr:
  title: "Fix merge conflicts in contract push-with-retry logic"
  description: |
    Fixes #385: The push-with-retry logic fails when git rebase encounters merge
    conflicts on contract JSON files. This PR introduces a shared helper script
    that uses the "reset-and-reapply" pattern: on push failure, abort any in-progress
    rebase, reset to remote HEAD, and re-apply the jq transformation from scratch.

    This eliminates the merge conflict vulnerability across all 9 affected locations
    in sdlc-pipeline.yml and sdlc-hitl.yml.
phases:
  - id: 1
    name: Create Shared Helper Script
    goal: Create a reusable script that encapsulates the reset-and-reapply retry logic
    tasks:
      - id: TASK-1-1
        description: Create .github/scripts/ directory structure
        acceptance: Directory exists
        files:
          - .github/scripts/
      - id: TASK-1-2
        description: Implement push-contract-update.sh helper script
        acceptance: Script handles reset-and-reapply pattern, accepts jq filter and commit message as parameters
        files:
          - .github/scripts/push-contract-update.sh
  - id: 2
    name: Update sdlc-pipeline.yml Push Locations
    goal: Replace all 6 push-with-retry blocks with calls to the shared helper script
    tasks:
      - id: TASK-2-1
        description: Add retry logic to line 363 (populate contract tasks)
        acceptance: Uses helper script, no longer a bare git push
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-2
        description: Update line 438 (checkpoint push) with soft-failure mode
        acceptance: Uses helper script with soft-failure mode that warns but continues
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-3
        description: Update line 940 (advance to PR phase)
        acceptance: Uses helper script, exits on failure
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-4
        description: Update line 1465 (update refine review state)
        acceptance: Uses helper script, handles complex multi-step jq
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-5
        description: Update line 1784 (populate contract from plan)
        acceptance: Uses helper script, exits on failure
        files:
          - .github/workflows/sdlc-pipeline.yml
      - id: TASK-2-6
        description: Update line 2144 (update plan review state)
        acceptance: Uses helper script, handles complex multi-step jq
        files:
          - .github/workflows/sdlc-pipeline.yml
  - id: 3
    name: Update sdlc-hitl.yml Push Locations
    goal: Replace all 3 push-with-retry blocks with calls to the shared helper script
    tasks:
      - id: TASK-3-1
        description: Update line 230 (resolve HITL decision)
        acceptance: Uses helper script, handles multi-step jq transformation
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-3-2
        description: Update line 331 (advance to next phase)
        acceptance: Uses helper script, exits on failure
        files:
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-3-3
        description: Update line 552 (approve and advance phase)
        acceptance: Uses helper script, handles audit log entry
        files:
          - .github/workflows/sdlc-hitl.yml
  - id: 4
    name: Testing and Verification
    goal: Verify the fix works correctly and document the changes
    tasks:
      - id: TASK-4-1
        description: Run workflow linting if available
        acceptance: No lint errors in modified workflow files
        files:
          - .github/workflows/sdlc-pipeline.yml
          - .github/workflows/sdlc-hitl.yml
      - id: TASK-4-2
        description: Add inline comments explaining the reset-and-reapply pattern
        acceptance: Comments explain why this approach prevents merge conflicts
        files:
          - .github/scripts/push-contract-update.sh
```

---

## Phase Approval

<!-- egg-phase-approval -->
- [ ] Approve and advance to implement phase

---

*Authored-by: egg*
