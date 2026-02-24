# Plan: Refine Phase Agent Runaway Grep + Push Branch Mismatch

> Issue: #901 | Phase: plan

## Summary

Issue #901 documents three cascading failures that caused a 26-minute agent hang:
(1) push rejections from branch history contamination, (2) agent improvising a push
to the wrong branch name, and (3) a runaway `grep -rn /` consuming 100% CPU for 22+
minutes. This plan addresses all three root causes following the architect's
recommended approach (Option B + E + F + I + H from the analysis).

The implementation is organized into four phases: fix the pre-phase worktree sync to
prevent push denials from accumulated branch history, enforce push target branch
matching in the gateway, add agent prompt guardrails against unbounded filesystem
searches, and add branch commit verification on completion signals.

## Implementation Phases

### Phase 1: Fix Pre-Phase Worktree Sync (Root Cause 1)

**Goal**: Ensure `_sync_worktree_with_remote()` always resets the worktree to match
the remote branch before spawning a new phase agent, eliminating push denials caused
by prior phases' commits appearing in the diff.

**Tasks**:

- [TASK-1-1] Modify `_sync_worktree_with_remote()` to force-reset when local is
  ahead of remote — Currently (line 1907), the function skips the reset when
  `local_ahead > 0`. Change this to log a warning about discarding local-only commits,
  then proceed with the `git reset --hard origin/{branch}` anyway. The auto-commit
  hook should have already pushed these commits; if they exist locally only, they are
  from a crashed agent and are safe to discard.
  - Acceptance: Function performs `git reset --hard` even when local is ahead; warning
    is logged with the number of local-only commits being discarded.

- [TASK-1-2] Update existing tests in `orchestrator/tests/test_sync_worktree.py` —
  The `test_skips_reset_when_local_ahead` and `test_skips_reset_when_local_diverged`
  tests currently assert the old behavior (skip reset). Update them to verify that
  reset is now performed with a warning log.
  - Acceptance: Both tests verify that `git reset --hard` is called (mock_run call
    count is 4) and that a warning is logged about discarding local commits.

- [TASK-1-3] Add a test for the new force-reset behavior when local has diverged
  (ahead AND behind) — Verify that when the branch has diverged (local ahead > 0 AND
  remote behind > 0), the function still performs the reset and logs appropriately.
  - Acceptance: New test passes verifying reset occurs even when diverged.

**Dependencies**: None (first phase)

**Exit criteria**: `_sync_worktree_with_remote()` always resets the worktree to match
remote, all tests pass.

### Phase 2: Gateway Push Target Enforcement (Root Cause 3a)

**Goal**: Prevent pipeline agents from pushing to branch names other than their
assigned branch, blocking the "improvised branch name" failure mode.

**Tasks**:

- [TASK-2-1] Add push refspec validation in `gateway/gateway.py:git_push()` — After
  the branch is extracted from the refspec (line 539) and before the branch ownership
  check (line 617), add a check: if the session has `pipeline_id` and
  `assigned_branch`, verify that the extracted branch matches `assigned_branch`. If
  the push uses a `local:remote` refspec format, the remote portion must match. Return
  HTTP 403 with a clear error message: "Pipeline sessions must push to their assigned
  branch '{assigned_branch}'. Got '{branch}'."
  - Acceptance: Pushing to a branch other than the assigned branch returns 403 with
    the descriptive error. Checkpoint pushes are still exempt. Non-pipeline sessions
    are unaffected.

- [TASK-2-2] Add tests for push refspec enforcement in
  `gateway/tests/test_assigned_branch.py` — Test cases: (a) pipeline session push to
  assigned branch succeeds, (b) pipeline session push to different branch returns 403,
  (c) pipeline session push with `local:remote` refspec where remote matches assigned
  branch succeeds, (d) pipeline session push with mismatched `local:remote` refspec
  returns 403, (e) non-pipeline session push to any branch is unaffected, (f)
  checkpoint push bypasses the check.
  - Acceptance: All six test cases pass.

- [TASK-2-3] Improve the push denial hint when phase restrictions block a push — When
  the push fails due to phase file restrictions (gateway.py:808-814), update the hint
  to be pipeline-aware. Instead of suggesting "Create a clean branch from origin/main"
  (which the agent can't do in pipeline mode), say: "Push contains files from prior
  pipeline phases that this phase cannot modify. This usually indicates the worktree
  was not properly synced before this phase started. Signal an error with
  `egg-orch signal error` and include this message."
  - Acceptance: The hint is pipeline-aware; non-pipeline sessions still see the
    original hint suggesting a clean branch.

**Dependencies**: None (independent of Phase 1)

**Exit criteria**: Gateway enforces push target matching for pipeline sessions; hint
message is pipeline-aware.

### Phase 3: Agent Prompt Guardrails (Root Cause 2)

**Goal**: Add explicit instructions warning agents against unbounded filesystem
searches. This is a soft guardrail that addresses the most common failure mode.

**Tasks**:

- [TASK-3-1] Add filesystem search guardrails to `sandbox/.claude/rules/environment.md`
  — Add a new "Shell Command Safety" section with explicit warnings:
  (a) NEVER run `grep`, `find`, or similar search commands starting from `/` or
  outside `~/repos/`. (b) Always scope searches to `~/repos/` or a specific
  subdirectory. (c) If a search takes longer than expected, kill it and narrow the
  scope. Include examples of what NOT to do (`grep -rn "pattern" /`) and what to do
  instead (`grep -rn "pattern" ~/repos/`).
  - Acceptance: The new section exists in `environment.md` with clear examples.

- [TASK-3-2] Add the same guardrail to `sandbox/.claude/rules/mission.md` in the
  "Git Safety" or general safety section — Add a brief reminder (1-2 lines) about
  scoping all filesystem operations to `~/repos/`, pointing to `environment.md` for
  details.
  - Acceptance: The reminder exists in `mission.md`.

**Dependencies**: None (independent of Phases 1-2)

**Exit criteria**: Both rule files contain filesystem search guardrails.

### Phase 4: Completion Signal Branch Verification (Root Cause 3b)

**Goal**: Add a verification step to the orchestrator's completion signal handler
that checks whether new commits exist on the expected branch, logging a warning if
the agent appears to have pushed to the wrong branch or not pushed at all.

**Tasks**:

- [TASK-4-1] Add branch tip tracking to pipeline phase start — In
  `orchestrator/routes/pipelines.py`, after `_sync_worktree_with_remote()` is called
  (line 4840), record the current `origin/{branch}` commit SHA as
  `pipeline.phase_start_sha` (or equivalent metadata). This provides a baseline to
  compare against when the agent signals completion.
  - Acceptance: The branch tip SHA is recorded when a phase starts and is accessible
    from the completion signal handler.

- [TASK-4-2] Add branch commit verification in `handle_complete_signal()` — In
  `orchestrator/routes/signals.py`, after accepting the completion signal (before
  returning success at line 206), fetch the branch and compare the current
  `origin/{branch}` tip to `phase_start_sha`. If they match (no new commits), log a
  warning: "Agent signaled completion but no new commits on branch '{branch}'." This
  is a warning only, not a hard block, because some phases may legitimately produce
  no new commits.
  - Acceptance: Warning is logged when no new commits found; completion signal still
    succeeds; no warning when new commits exist.

- [TASK-4-3] Add tests for completion signal branch verification — Test cases:
  (a) completion with new commits on branch produces no warning, (b) completion with
  no new commits logs a warning, (c) completion when branch fetch fails does not
  block the signal (best-effort).
  - Acceptance: All three test cases pass.

**Dependencies**: Phase 1 (uses the sync infrastructure)

**Exit criteria**: Completion signal handler logs warnings for suspicious
completions; existing signal behavior is not broken.

## Test Strategy

- **Unit tests**: Each phase includes dedicated test tasks. Tests follow existing
  patterns: `unittest.mock.patch` for subprocess calls in orchestrator tests,
  `conftest.py` fixtures for gateway tests.
- **Existing test suites**:
  - `orchestrator/tests/test_sync_worktree.py` — Update existing + add new tests
    (Phase 1)
  - `gateway/tests/test_assigned_branch.py` — Add push enforcement tests (Phase 2)
  - `orchestrator/tests/` — Add new completion signal verification tests (Phase 4)
- **Manual testing**: After implementation, verify by running a pipeline and
  confirming: (a) worktree is cleanly synced between phases, (b) pushing to a
  non-assigned branch is rejected, (c) completion signals log warnings when no new
  commits exist.
- **Regression**: Run full `make test` (or `pytest` in each component) to ensure no
  regressions.

## Rollback Plan

All changes are additive and backward-compatible:
- Phase 1: Revert the sync function to skip reset when local is ahead (restore the
  `return` statement).
- Phase 2: Remove the refspec check in `git_push()`. The gateway still enforces
  branch ownership via the egg-prefix check.
- Phase 3: Revert the prompt file changes (no code impact).
- Phase 4: Remove the branch tip tracking and verification (completion signals
  return to unconditional acceptance).

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Force-reset discards legitimate local commits | Low | Med | Auto-commit hook should push before container exit; log warning for auditing |
| Push refspec enforcement breaks edge-case refspec formats | Low | Med | `extract_branch_from_refspec()` already handles all common formats; test thoroughly |
| Prompt guardrails ignored by model | Med | Low | This is defense-in-depth alongside other hard fixes; the model usually follows explicit rules |
| Branch tip tracking adds latency to phase start | Low | Low | A single `git rev-parse` is < 100ms; best-effort so failures don't block |

## Migration Notes

No database migrations required. No configuration changes for existing deployments.
The worktree sync behavior change (Phase 1) takes effect immediately for all new
pipeline runs. In-flight pipelines are unaffected because the sync runs at phase
start, before the agent is spawned.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Fix push branch mismatch and runaway grep in pipeline agents"
  description: |
    Fixes three cascading failures that caused a 26-minute agent hang in pipeline
    issue #805: (1) push denials from branch history contamination by always resetting
    the worktree to match remote before each phase, (2) agents improvising push to
    wrong branch names by enforcing push target matching in the gateway, and (3) a
    runaway grep -rn / by adding agent prompt guardrails. Also adds completion signal
    branch verification as a detection layer.

    Closes #901.
phases:
  - id: 1
    name: Fix Pre-Phase Worktree Sync
    goal: Ensure worktree is always reset to match remote before spawning a new phase agent
    tasks:
      - id: TASK-1-1
        description: Modify _sync_worktree_with_remote() to force-reset when local is ahead of remote instead of skipping
        acceptance: Function performs git reset --hard even when local is ahead; warning is logged with count of discarded commits
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-1-2
        description: Update existing tests for the new force-reset behavior (test_skips_reset_when_local_ahead, test_skips_reset_when_local_diverged)
        acceptance: Both tests verify that git reset --hard is called and warning is logged
        files:
          - orchestrator/tests/test_sync_worktree.py
      - id: TASK-1-3
        description: Add test for force-reset when local has diverged (ahead AND behind remote)
        acceptance: New test passes verifying reset occurs even when diverged
        files:
          - orchestrator/tests/test_sync_worktree.py
  - id: 2
    name: Gateway Push Target Enforcement
    goal: Prevent pipeline agents from pushing to branch names other than their assigned branch
    tasks:
      - id: TASK-2-1
        description: Add push refspec validation in git_push() to enforce that pipeline sessions push only to their assigned branch
        acceptance: Push to non-assigned branch returns 403; checkpoint pushes exempt; non-pipeline sessions unaffected
        files:
          - gateway/gateway.py
      - id: TASK-2-2
        description: Add tests for push refspec enforcement (6 test cases covering assigned/mismatched/refspec/non-pipeline/checkpoint)
        acceptance: All six test cases pass
        files:
          - gateway/tests/test_assigned_branch.py
      - id: TASK-2-3
        description: Update push denial hint to be pipeline-aware instead of suggesting branch creation the agent cannot do
        acceptance: Pipeline sessions see actionable hint; non-pipeline sessions see original hint
        files:
          - gateway/gateway.py
  - id: 3
    name: Agent Prompt Guardrails
    goal: Add explicit agent instructions warning against unbounded filesystem searches
    tasks:
      - id: TASK-3-1
        description: Add Shell Command Safety section to sandbox/.claude/rules/environment.md with examples
        acceptance: New section exists with warnings against searching from / and examples of correct scoping
        files:
          - sandbox/.claude/rules/environment.md
      - id: TASK-3-2
        description: Add brief filesystem safety reminder to sandbox/.claude/rules/mission.md
        acceptance: Reminder exists pointing to environment.md for details
        files:
          - sandbox/.claude/rules/mission.md
  - id: 4
    name: Completion Signal Branch Verification
    goal: Add warning-level verification that new commits exist on the expected branch when an agent signals completion
    tasks:
      - id: TASK-4-1
        description: Record origin/{branch} commit SHA at phase start as phase_start_sha for baseline comparison
        acceptance: Branch tip SHA is recorded and accessible from completion signal handler
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/models.py
      - id: TASK-4-2
        description: Add branch commit verification in handle_complete_signal() comparing current tip to phase_start_sha
        acceptance: Warning logged when no new commits found; signal still succeeds; no warning when new commits exist
        files:
          - orchestrator/routes/signals.py
      - id: TASK-4-3
        description: Add tests for completion signal branch verification (3 test cases)
        acceptance: All three test cases pass
        files:
          - orchestrator/tests/test_signals.py
```

---

*Authored-by: egg*
