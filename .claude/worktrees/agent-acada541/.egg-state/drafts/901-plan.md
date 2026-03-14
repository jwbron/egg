# Plan: Fix Pipeline Push Failures, Runaway Commands, and Wrong-Branch Completion

> Issue: #901 | Phase: plan | Revision: 2

## Summary

Issue #901 documents three cascading failures that caused a 26-minute agent hang
during the refine phase of issue #805: (1) push rejections from branch history
contamination, (2) a runaway `grep -rn / ...` that consumed 100% CPU for 22+
minutes, and (3) the agent improvising a push to the wrong branch name while the
orchestrator accepted the completion signal without verifying the commit location.

This plan implements the architect's recommended approach (Options B + E + F + I + H):

- **Option B**: Pre-phase worktree sync in the orchestrator
- **Option E**: Agent prompt guardrails against unbounded filesystem searches
- **Option F**: System-level per-command timeout wrapper in the sandbox
- **Option I**: Gateway push-target branch enforcement for pipeline sessions
- **Option H**: Completion signal branch verification in the orchestrator

The work is organized into five phases within a single PR, ordered by risk and
dependency: prompt guardrails and error messages first (zero-risk), then gateway
enforcement, then sandbox timeout, then orchestrator sync, then completion signal
verification.

## Review Feedback Addressed

The prior plan revision claimed to implement Option F (Shell Command Timeout Wrapper)
but had no implementation tasks for it. The analysis recommended E + F together as a
defense-in-depth pair: prompt guardrails (soft) + command timeout (hard). Without
Option F, the only protection against runaway commands is the prompt guardrail, which
the risk analyst assessed as "medium" likelihood of being ignored by the model.

**Resolution**: This revision adds a dedicated phase (Phase 3) for Option F with three
concrete tasks: (1) investigate Claude Code's actual shell invocation path in the
sandbox, (2) implement a system-level timeout wrapper, and (3) add tests. The architect's
revision 2 analysis confirms that Claude Code uses subprocess execution (not login
shells), so `/etc/profile.d/` is NOT viable — the implementation must use an approach
that covers the actual invocation path (BASH_ENV, shell binary wrapping, or process
monitoring).

## Implementation Phases

### Phase 1: Prompt Guardrails and Improved Error Messages

**Goal**: Quick-win soft guardrails that address the most common agent behavior patterns
and improve error actionability. Zero code risk — only documentation and error string
changes.

**Tasks**:

- **[TASK-1-1]** Add filesystem search guardrails and timeout documentation to
  `sandbox/.claude/rules/environment.md` — Add a "Shell Command Safety" section with:
  (a) guidance to always scope searches to `~/repos/` or `$EGG_REPO_PATH`,
  (b) explanation that searching from `/` scans the entire filesystem and commands
  exceeding 120s will be killed by the system timeout,
  (c) DO/DON'T examples (`grep -rn "pattern" /` vs `grep -rn "pattern" ~/repos/`),
  (d) warning against improvising branch names on push failure — instead use
  `egg-orch signal error`.
  - **Acceptance**: Rules file contains Shell Command Safety section with all four
    elements. Language explains WHY (not just NEVER) per risk analyst R-8 guidance.

- **[TASK-1-2]** Add brief filesystem safety reminder to
  `sandbox/.claude/rules/mission.md` — In the "Git Safety" section, add 1-2 lines
  reminding agents to scope all filesystem operations to `~/repos/` and referencing
  `environment.md` for details.
  - **Acceptance**: Reminder exists in mission.md in the safety section.

- **[TASK-1-3]** Improve push denial error message in `gateway/gateway.py` to be
  pipeline-aware — When in pipeline mode and push is denied due to phase file
  restrictions (lines 805-827), replace the generic hint ("Create a clean branch from
  origin/main") with: "Push contains files from prior pipeline phases that this phase
  cannot modify. This indicates the worktree was not properly synced. Signal an error
  with `egg-orch signal error` and include this message." Non-pipeline sessions
  continue to see the original hint.
  - **Acceptance**: Pipeline sessions see the pipeline-specific hint; non-pipeline
    sessions see the original hint; blocked file list is included for diagnostics.

**Dependencies**: None (first phase).

**Exit criteria**: All three rule/error-message changes committed. No code logic changes.

---

### Phase 2: Gateway Push-Target Enforcement

**Goal**: Prevent pipeline agents from pushing to branch names other than their
assigned branch, blocking the "improvised branch name" failure mode at the gateway
enforcement boundary.

**Tasks**:

- **[TASK-2-1]** Add push-target validation in `gateway/gateway.py:git_push()` —
  After extracting the branch from the refspec (using existing
  `extract_branch_from_refspec()` at `gateway/policy.py:961-989`) and before the
  branch ownership check (~line 617), add: if the session has `pipeline_id` AND
  `assigned_branch`, verify the extracted branch matches `assigned_branch`. Reject
  mismatches with HTTP 403: "Pipeline sessions must push to their assigned branch
  '{assigned_branch}'. Got '{branch}'."

  Implementation details:
  - Add `PUSH_TARGET_ENFORCEMENT` env var (default `true`) as a killswitch for
    instant rollback without code deploy (risk analyst R-7).
  - Handle all push syntaxes: bare branch, `local:remote`, `HEAD:remote`, `+force`
    prefix, `refs/heads/` prefix, delete refspec (`:branch`).
  - Verify compatibility with `auto_commit_worktree()` temporary sessions
    (`gateway/post_agent_commit.py:286-315`) and `push_worktree_branch()` temporary
    sessions (`orchestrator/gateway_client.py:553-618`). Both use session tokens —
    ensure they either set `assigned_branch` correctly or are exempted (risk analyst
    R-5 and architect AD-6).
  - Skip enforcement when `session.pipeline_id` is null OR `session.assigned_branch`
    is null (backward compatibility for in-flight pipelines, cross-cutting concern CC-3).

  - **Acceptance**: Pipeline pushes to wrong branch return 403 with expected branch
    in error; pushes to correct branch succeed; non-pipeline pushes unaffected;
    auto-commit pushes verified compatible; `PUSH_TARGET_ENFORCEMENT=false` disables;
    all push syntaxes handled.

- **[TASK-2-2]** Add tests for push-target enforcement in
  `gateway/tests/test_assigned_branch.py` — Test cases:
  (a) pipeline session push to assigned branch succeeds,
  (b) pipeline session push to different branch returns 403,
  (c) `local:remote` refspec where remote matches assigned branch succeeds,
  (d) mismatched `local:remote` refspec returns 403,
  (e) non-pipeline session push to any branch is unaffected,
  (f) session with `pipeline_id` but no `assigned_branch` skips check,
  (g) `PUSH_TARGET_ENFORCEMENT=false` disables check,
  (h) auto-commit compatible session push succeeds.
  - **Acceptance**: All eight test cases pass; coverage includes the full matrix of
    session types and refspec formats.

**Dependencies**: None (independent of Phase 1).

**Exit criteria**: Gateway enforces push-target matching for pipeline sessions;
killswitch works; auto-commit path verified; all tests pass.

---

### Phase 3: Sandbox Per-Command Timeout Wrapper (Option F)

**Goal**: Add a system-level per-command timeout that kills commands exceeding
`BASH_COMMAND_TIMEOUT` (default 120 seconds), providing hard enforcement against
runaway shell commands regardless of model behavior.

**Rationale**: This phase is the hard enforcement layer for RC-2 (runaway commands).
The architect and risk analyst agree that prompt guardrails alone (Phase 1) are
insufficient — the model can ignore them (assessed "medium" likelihood). The 22-minute
runaway grep consumed $8+ in compute. A system-level timeout guarantees bounded
resource consumption. Claude Code's own Bash tool default timeout is 120000ms, but
the model can set it up to 600000ms (10 min) per call. A system-level backup is
needed.

**Tasks**:

- **[TASK-3-1]** Investigate Claude Code's Bash tool shell invocation path in the
  sandbox — Before implementing the wrapper, verify how Claude Code executes Bash tool
  commands. Key questions:
  (a) Does it spawn a login shell, interactive shell, or non-interactive shell?
  (b) Is `/etc/profile.d/` sourced? Is `BASH_ENV` respected?
  (c) What shell binary is used (`/bin/bash`, `/bin/sh`)?
  (d) What is the effective default timeout in the current sandbox build?

  Investigation approach: add diagnostic logging to trace the invocation, or inspect
  Claude Code's behavior directly. The architect's research indicates Claude Code uses
  `subprocess` execution (not login shells), so `/etc/profile.d/` is NOT viable.
  Confirm this and choose the implementation approach accordingly.

  Candidate implementation approaches (in order of preference):
  1. If `BASH_ENV` is respected: set it to a script that wraps execution with `timeout`
  2. If a specific shell binary is used: wrap or replace it with a timeout-enforcing
     wrapper
  3. Add a process monitor daemon that watches child processes
  4. Use Claude Code's settings-based configuration if supported

  - **Acceptance**: Shell invocation path documented; `/etc/profile.d/` and `BASH_ENV`
    behavior confirmed; effective default timeout verified; implementation approach
    chosen with rationale.

- **[TASK-3-2]** Implement system-level per-command timeout wrapper — Based on
  TASK-3-1 findings, implement the timeout mechanism. Requirements:
  (a) Default `BASH_COMMAND_TIMEOUT=120` (seconds), aligning with Claude Code's
  default (architect AD-4, risk analyst correction).
  (b) Configurable via environment variable for per-container override.
  (c) Send SIGTERM first, wait 10s grace period, then SIGKILL.
  (d) Write descriptive error to stderr: "Command killed: exceeded
  BASH_COMMAND_TIMEOUT={n}s. Use a more targeted command or request a longer timeout."
  (e) Legitimate commands completing within the timeout are unaffected.
  (f) The wrapper covers Claude Code's actual subprocess invocation path (verified,
  not assumed).
  (g) `BASH_COMMAND_TIMEOUT=0` disables the timeout.
  - **Acceptance**: A `grep -rn 'pattern' /` is killed after 120s; default is 120s;
    env var overrides work; error message is descriptive; SIGTERM then SIGKILL
    sequence; legitimate commands unaffected; invocation path coverage verified.

- **[TASK-3-3]** Add tests for the timeout wrapper — Test cases:
  (a) Command sleeping longer than `BASH_COMMAND_TIMEOUT` is killed,
  (b) Command completing within timeout succeeds normally,
  (c) `BASH_COMMAND_TIMEOUT=0` disables the timeout,
  (d) Timeout error message is written to stderr.
  Tests should exercise the actual invocation path, not just unit-test the wrapper
  in isolation.
  - **Acceptance**: All four test cases pass using the actual sandbox shell invocation
    mechanism.

**Dependencies**: None (independent of Phases 1-2).

**Exit criteria**: Per-command timeout is enforced via the verified invocation path;
default 120s; configurable; tests pass.

**Human review needed**: The implementation approach depends on TASK-3-1 investigation
findings. The implementer must test the chosen approach in the sandbox before
committing (risk analyst R-1, severity: high).

---

### Phase 4: Pre-Phase Worktree Sync Fix

**Goal**: Ensure `_sync_worktree_with_remote()` always brings the worktree and remote
branch into sync before spawning a new phase agent, eliminating push denials caused
by prior phases' commits appearing in the diff.

**Tasks**:

- **[TASK-4-1]** Modify `_sync_worktree_with_remote()` in
  `orchestrator/routes/pipelines.py` to push local-ahead commits and handle
  divergence — Currently (line 1906-1914), the function skips reset when
  `local_ahead > 0`. Change this to:

  1. **Check prior agent execution status** (risk analyst R-2, architect AD-8): if
     prior phase completed successfully, push local-ahead commits to remote via
     `push_worktree_branch()` (`orchestrator/gateway_client.py:553-618`). If prior
     phase failed or was killed, reset to remote (discard incomplete work).
  2. **Handle divergence** (risk analyst divergence correction): when both
     `local_ahead > 0` AND `remote_behind > 0`, attempt fast-forward merge. If merge
     fails, signal pipeline error via `egg-orch` rather than silently proceeding.
  3. **Push failure handling**: log warning but do not block phase start (graceful
     degradation). The improved error message from TASK-1-3 helps the agent recover.
  4. After sync, `origin/{branch}..HEAD` diff should contain no files from prior phases.

  - **Acceptance**: Prior-phase-succeeded path pushes then resets; prior-phase-failed
    path discards and resets; divergence attempts merge then errors on failure; push
    failure logged but non-blocking; existing behavior preserved when local is behind
    or in-sync with remote.

- **[TASK-4-2]** Add/update tests for `_sync_worktree_with_remote()` in
  `orchestrator/tests/test_sync_worktree.py` — Update existing tests
  (`test_skips_reset_when_local_ahead`, `test_skips_reset_when_local_diverged`) and
  add new ones:
  (a) local ahead + prior phase succeeded → pushes then resets,
  (b) local ahead + prior phase failed → resets without pushing,
  (c) local behind remote → fetches then resets (existing, verify unchanged),
  (d) local in sync → no push needed (existing, verify unchanged),
  (e) push fails → logs warning, continues with reset,
  (f) diverged → attempts merge,
  (g) diverged + merge fails → signals error.
  - **Acceptance**: All seven test cases pass; existing passing tests not broken.

**Dependencies**: Phase 2 should be deployed first (push-target enforcement ensures
the sync push targets the correct branch), but the code changes are independent and
can be committed in the same PR.

**Exit criteria**: `_sync_worktree_with_remote()` always brings worktree and remote
into sync; all tests pass.

---

### Phase 5: Completion Signal Branch Verification

**Goal**: Add verification to the orchestrator's completion signal handler that checks
whether the reported commit exists on the expected branch. Hard-block when a commit
SHA is provided but not found; skip check when commit is `None`.

**Tasks**:

- **[TASK-5-1]** Record branch tip SHA at phase start (`phase_start_sha`) — In
  `orchestrator/routes/pipelines.py`, after `_sync_worktree_with_remote()` completes,
  record the current `origin/{branch}` tip SHA. Store it on the `PhaseExecution`
  model in `orchestrator/models.py` (add `phase_start_sha: str | None = None` field).
  This provides a baseline for the completion signal handler.
  - **Acceptance**: SHA is recorded at phase start; accessible from the completion
    signal handler via `pipeline.phases[phase].phase_start_sha`.

- **[TASK-5-2]** Add branch commit verification in `handle_complete_signal()` at
  `orchestrator/routes/signals.py` — When the completion signal includes a commit SHA
  (`commit != None`):
  1. Fetch the expected branch.
  2. Verify the commit exists on it (e.g., `git branch --contains {sha}`
     filtered to the expected branch).
  3. If NOT found: reject with HTTP 409 Conflict: "Completion rejected: commit {sha}
     not found on expected branch {branch}. The agent may have pushed to a different
     branch." (risk analyst correction — hard-block, not warning, per architect AD-7).
  4. If found: accept normally.
  5. When `commit` is `None`: skip verification (some phases legitimately complete
     without committing).
  6. Compare current branch tip to `phase_start_sha` — if unchanged, log a warning
     (no new commits) but don't block.
  7. Branch fetch failure: log warning, don't block (best-effort).
  - **Acceptance**: Commit on expected branch → accepted; commit NOT on expected
    branch → 409 rejected; commit=None → accepted without check; no new commits →
    warning logged; fetch failure → non-blocking warning.

- **[TASK-5-3]** Add tests for completion signal branch verification — Test cases:
  (a) completion with commit on correct branch → accepted,
  (b) completion with commit NOT on correct branch → 409 rejected,
  (c) completion with `commit=None` → accepted without check,
  (d) branch fetch fails → signal accepted with warning,
  (e) no new commits since phase start → warning logged but accepted.
  - **Acceptance**: All five test cases pass.

**Dependencies**: Phase 4 (uses the sync infrastructure and `phase_start_sha`
recording). Can be committed independently but should be tested after Phase 4.

**Exit criteria**: Completion signal handler verifies commit location; hard-block on
wrong-branch commit; existing signal behavior preserved for `commit=None` paths.

---

## Test Strategy

- **Unit tests**: Each phase includes dedicated test tasks. Tests follow existing
  patterns:
  - Orchestrator tests: `unittest.mock.patch` for subprocess calls (see
    `orchestrator/tests/test_sync_worktree.py` for pattern).
  - Gateway tests: `conftest.py` fixtures (see `gateway/tests/` for pattern).
  - Sandbox tests: exercise actual invocation mechanism for timeout tests.
- **Existing test suites to update**:
  - `orchestrator/tests/test_sync_worktree.py` — update + add tests (Phase 4)
  - `orchestrator/tests/test_signals.py` — add branch verification tests (Phase 5)
- **New test files**:
  - `gateway/tests/test_assigned_branch.py` — push enforcement tests (Phase 2)
  - `sandbox/tests/test_command_timeout.py` — timeout wrapper tests (Phase 3)
- **Regression**: Run full test suites (`pytest` in each component) before PR to
  ensure no regressions.
- **Manual verification**: After implementation, run a pipeline and confirm:
  (a) worktree is cleanly synced between phases,
  (b) pushing to a non-assigned branch is rejected,
  (c) runaway commands are killed after 120s,
  (d) completion signals log warnings when appropriate.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Timeout wrapper doesn't activate for Claude Code's invocation path | Med | High | TASK-3-1 investigation before implementation; verify experimentally |
| Pre-phase push pushes broken work from crashed agent | Low-Med | Med | Check prior phase status; only push on success (AD-8) |
| Push-target refspec parsing causes false rejections | Med | Med | Use existing `extract_branch_from_refspec()`; comprehensive tests; killswitch |
| Push-target enforcement blocks auto-commit pushes | Med | High | Explicitly verify auto-commit path; exemption handling; integration test |
| Per-command timeout kills legitimate long operations | Med | Med | 120s default sufficient for most ops; configurable via env var; Claude Code per-call override |
| Prompt guardrails ignored by model | Med | Low | Defense-in-depth with hard timeout (Phase 3) backing soft guardrails (Phase 1) |
| Push-target killswitch bug blocks ALL pipeline pushes | Low | High | `PUSH_TARGET_ENFORCEMENT=false` for instant rollback |

## Rollback Plan

All changes are additive and independently reversible:

- **Phase 1**: Revert prompt/error-message text changes. No code impact.
- **Phase 2**: Set `PUSH_TARGET_ENFORCEMENT=false` in gateway env (instant). Or
  revert the code change.
- **Phase 3**: Set `BASH_COMMAND_TIMEOUT=0` or a high value (3600) to disable. Or
  remove the wrapper.
- **Phase 4**: Revert `_sync_worktree_with_remote()` to skip-when-ahead behavior.
- **Phase 5**: Remove branch tip tracking and verification. Completion signals return
  to unconditional acceptance.

No database migrations required. No configuration changes for existing deployments.

## Migration Notes

The worktree sync behavior change (Phase 4) takes effect immediately for new pipeline
runs. In-flight pipelines are unaffected because the sync runs at phase start, before
the agent is spawned. Push-target enforcement (Phase 2) only activates when session
has both `pipeline_id` and `assigned_branch` set — old sessions missing these fields
are exempt.

---

## Structured Task Appendix

```yaml
# yaml-tasks
pr:
  title: "Fix push branch mismatch, runaway commands, and stale completion signals"
  description: |
    Fixes three cascading failures from issue #805 that caused a 26-minute agent hang:
    (1) push denials from branch history contamination — fixed by syncing worktree to
    remote before each phase, (2) runaway grep -rn / — fixed by adding prompt guardrails
    AND a system-level per-command timeout wrapper (120s default), (3) agent pushing to
    wrong branch — fixed by enforcing push-target matching in the gateway and verifying
    commit location on completion signals.

    Closes #901.
phases:
  - id: 1
    name: Prompt Guardrails and Improved Error Messages
    goal: Add soft guardrails against unbounded filesystem searches and improve push denial error actionability
    tasks:
      - id: TASK-1-1
        description: Add Shell Command Safety section to sandbox/.claude/rules/environment.md with DO/DON'T examples and timeout documentation
        acceptance: Rules file warns against root-level searches with examples, documents 120s system timeout, warns against improvising branch names
        files:
          - sandbox/.claude/rules/environment.md
      - id: TASK-1-2
        description: Add brief filesystem safety reminder to sandbox/.claude/rules/mission.md
        acceptance: Reminder exists in safety section pointing to environment.md
        files:
          - sandbox/.claude/rules/mission.md
      - id: TASK-1-3
        description: Improve push denial error message in gateway/gateway.py to be pipeline-aware
        acceptance: Pipeline sessions see pipeline-specific hint with egg-orch guidance; non-pipeline sessions see original hint
        files:
          - gateway/gateway.py
  - id: 2
    name: Gateway Push-Target Enforcement
    goal: Prevent pipeline agents from pushing to branch names other than their assigned branch
    tasks:
      - id: TASK-2-1
        description: Add push-target validation in git_push() enforcing pipeline sessions push only to assigned branch, with PUSH_TARGET_ENFORCEMENT killswitch
        acceptance: Wrong-branch push returns 403; correct branch succeeds; non-pipeline unaffected; auto-commit compatible; killswitch works; all push syntaxes handled
        files:
          - gateway/gateway.py
      - id: TASK-2-2
        description: Add tests for push-target enforcement covering 8 test cases (assigned, mismatched, refspec, non-pipeline, no-assigned-branch, killswitch, auto-commit)
        acceptance: All eight test cases pass
        files:
          - gateway/tests/test_assigned_branch.py
  - id: 3
    name: Sandbox Per-Command Timeout Wrapper
    goal: Add system-level per-command timeout (120s default) that kills runaway shell commands regardless of model behavior
    tasks:
      - id: TASK-3-1
        description: Investigate Claude Code's Bash tool shell invocation path in the sandbox and choose timeout implementation approach
        acceptance: Invocation path documented; /etc/profile.d/ and BASH_ENV behavior confirmed; implementation approach chosen with rationale
        files:
          - sandbox/entrypoint.py
          - sandbox/llm/runner.py
          - sandbox/llm/claude/runner.py
      - id: TASK-3-2
        description: Implement system-level per-command timeout wrapper with BASH_COMMAND_TIMEOUT env var (default 120s), SIGTERM+SIGKILL sequence, descriptive error
        acceptance: Runaway commands killed after 120s; configurable; SIGTERM then SIGKILL; clear error message; legitimate commands unaffected; covers actual invocation path
        files:
          - sandbox/entrypoint.py
      - id: TASK-3-3
        description: Add tests verifying timeout wrapper kills long-running commands and respects configuration
        acceptance: All four test cases pass (timeout kill, normal completion, disable via env, error message)
        files:
          - sandbox/tests/test_command_timeout.py
  - id: 4
    name: Pre-Phase Worktree Sync Fix
    goal: Ensure worktree is always synced with remote before spawning a new phase agent, eliminating push denials from prior-phase history
    tasks:
      - id: TASK-4-1
        description: Modify _sync_worktree_with_remote() to push local-ahead commits (if prior phase succeeded) or discard (if failed), and handle divergence with fast-forward merge
        acceptance: Prior-succeeded pushes then resets; prior-failed discards and resets; divergence attempts merge then errors; push failure non-blocking; existing behind/in-sync behavior preserved
        files:
          - orchestrator/routes/pipelines.py
      - id: TASK-4-2
        description: Add/update tests for _sync_worktree_with_remote() covering 7 scenarios (push-on-success, discard-on-failure, behind, in-sync, push-fail, diverge-merge, diverge-error)
        acceptance: All seven test cases pass; existing passing tests not broken
        files:
          - orchestrator/tests/test_sync_worktree.py
  - id: 5
    name: Completion Signal Branch Verification
    goal: Verify reported commit exists on expected branch when agent signals completion; hard-block on mismatch when commit SHA provided
    tasks:
      - id: TASK-5-1
        description: Record origin/{branch} tip SHA at phase start as phase_start_sha on PhaseExecution model
        acceptance: SHA recorded at phase start; accessible from completion signal handler
        files:
          - orchestrator/routes/pipelines.py
          - orchestrator/models.py
      - id: TASK-5-2
        description: Add branch commit verification in handle_complete_signal() — reject 409 when commit SHA not on expected branch; skip when commit is None
        acceptance: Commit on branch accepted; commit not on branch rejected 409; commit=None accepted; no new commits warns; fetch failure non-blocking
        files:
          - orchestrator/routes/signals.py
      - id: TASK-5-3
        description: Add tests for completion signal branch verification covering 5 scenarios
        acceptance: All five test cases pass
        files:
          - orchestrator/tests/test_signals.py
```

---

*Authored-by: egg*
