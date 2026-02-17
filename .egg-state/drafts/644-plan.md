# Plan: Enforce phase file restrictions via readonly mounts and commit-time validation

> Issue: #644 | Phase: plan | Revision: 2 (addresses reviewer feedback)

## Summary

This plan implements a four-layer defense-in-depth enforcement system to close three
gaps in the current phase restriction model: local modification bypass (Gap 1),
unrestricted branch switching (Gap 2), and late enforcement wasting tokens (Gap 3).

The layers, in order of enforcement earliness:

- **L0 — Branch lock**: Prevent agents from switching branches via gateway enforcement
- **L1 — Readonly mounts**: Mount phase-restricted directories as readonly in containers
- **L2 — Commit-time validation**: Check staged files against phase restrictions before allowing `git commit`
- **L4 — Post-agent auto-commit**: Automatically commit and push allowed uncommitted changes after agent session ends

The existing push-time validation (L3) remains unchanged as the final safety net.

All work ships in a single PR, organized into 5 phases. Phases are ordered so that
the branch lock and agent instruction updates ship atomically (Phase 1), and each
subsequent phase builds on the previous.

## Review Feedback Resolution

### Issue 1 (HIGH): Atomic branch lock + agent instructions

**Resolution**: Agent instruction updates (TASK-1-4) are in Phase 1 alongside the
branch lock enforcement (TASK-1-2). They ship in the same commit. This prevents a
window where agents follow old `git checkout -b` instructions and hit the new
enforcement.

### Issue 2 (MEDIUM): Warn-only mode for agent-role restrictions

**Resolution**: TASK-2-1 specifies warn-only mode as the initial behavior with
`EGG_AGENT_RESTRICTIONS_ENFORCE` config flag (default: `false`). In warn-only mode,
violations are logged but pushes are allowed. Enforcement requires explicitly setting
the flag to `true` after collecting production data for 1-2 weeks.

### Issue 3 (MEDIUM): Single PR vs. independent PRs

**Resolution**: The pipeline constraint requires a single PR. Within that PR, the work
is organized into 5 phases with clear boundaries. Each phase corresponds to a logical
enforcement layer that could be reverted via targeted `git revert` of specific commits
if needed, providing similar rollback granularity to independent PRs. The phases are
ordered by blast radius (smallest first): gateway-only changes (Phase 1-2) before
container mount changes (Phase 3) before session lifecycle changes (Phase 4).

### Minor gap: EGG_PHASE env var

**Resolution**: Added as TASK-3-2 in Phase 3 (container spawner changes). `EGG_PHASE`
is set alongside existing env vars (`EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`).

### Minor gap: Refine/plan readonly mount strategy

**Resolution**: Per AD-5, refine/plan whole-repo readonly is deferred. Phase 3 implements
readonly mounts only for the implement phase (4 specific `.egg-state/` subdirectory
mounts). TASK-3-1 acceptance criteria explicitly notes this deferral and includes a
TODO comment for future work. This avoids risk R9 (blocking agent tool operations).

### Minor gap: Feature flags

**Resolution**: `EGG_AGENT_RESTRICTIONS_ENFORCE` covers the highest-risk flag (agent-role
restrictions). Per-layer feature flags are not added since each phase's commits can be
individually reverted, providing equivalent rollback capability.

## Implementation Phases

### Phase 1: Branch Lock + Agent Instructions (L0)

**Goal**: Lock agents to their assigned branch and update agent instructions atomically.
This closes Gap 2 (determinism) and is the highest-value, lowest-risk change since it
only modifies gateway code and agent markdown files.

**Tasks**:
- [TASK-1-1] Add `assigned_branch` field to Session dataclass. Populate it during
  `register_session()` from the worktree branch name. Persist/restore in
  `to_dict_for_persistence()` / `from_persistence()`. Pass branch name from
  `create_worktree()` result into session registration.
  — Acceptance: Session has `assigned_branch` field; it is set, persisted, and restored correctly; unit tests verify.

- [TASK-1-2] Implement `is_branch_switching()` in `git_client.py` and wire into
  `git_execute()`. Use conservative heuristic per AD-4: block branch-create flags
  (`-b`, `-B`, `--create`, `--force-create`) and `git switch <branch>`; allow
  `git checkout -- <file>` and `git checkout <ref> -- <file>`; allow ambiguous cases
  with warning log. Return clear error including assigned branch name and alternatives.
  Only enforce when `session.assigned_branch` is set (non-pipeline sessions unaffected).
  — Acceptance: Definite branch ops blocked; file restores allowed; ambiguous cases allowed+logged; helpful error message; comprehensive unit tests.

- [TASK-1-3] Add commit-time staged-file validation. When `git_execute()` handles
  `commit` and `session.phase` is set, run `git diff --cached --name-only` to get
  staged files, check against `check_phase_file_restrictions()`. Reject with actionable
  error listing blocked files, unstage command, and alternative CLI. Add
  `get_staged_files()` helper to `git_client.py` with 10-second timeout; fail-open on
  timeout. Skip for non-pipeline commits.
  — Acceptance: Blocked staged files rejected; allowed files pass; timeout fails open; non-pipeline commits unaffected; unit tests for all cases.

- [TASK-1-4] Update agent instructions in `mission.md` and `environment.md`. Remove all
  `git checkout -b` branch-switching instructions. Add branch lock section, phase
  restriction guidance, and auto-commit note. Add phase-specific guidance for implement,
  refine/plan, and PR phases.
  — Acceptance: No `git checkout -b` for branch switching in mission.md/environment.md; branch lock, phase guidance, and auto-commit sections present.

**Dependencies**: None
**Files**: `gateway/session_manager.py`, `gateway/worktree_manager.py`, `gateway/git_client.py`, `gateway/gateway.py`, `sandbox/.claude/rules/mission.md`, `sandbox/.claude/rules/environment.md`
**Exit criteria**: All existing gateway tests pass plus new tests in `test_branch_lock.py` and `test_commit_validation.py`.

### Phase 2: Agent-Role Restriction Wiring (L2 extension)

**Goal**: Wire the existing-but-unused `check_agent_restrictions()` into `git_push()`
in warn-only mode, enabling data collection without risk.

**Tasks**:
- [TASK-2-1] Wire `check_agent_restrictions()` into `git_push()` handler. Call it when
  `session.agent_role` is set. Default mode is warn-only
  (`EGG_AGENT_RESTRICTIONS_ENFORCE=false` or unset). In warn-only mode: log audit event
  with role, files, blocked files, action=warn; allow push. In enforce mode: block push
  with existing `check_agent_restrictions()` behavior. Unknown roles pass (backwards
  compatibility).
  — Acceptance: Called in `git_push()` for sessions with `agent_role`; default warn-only; audit logging; enforce mode behind flag; unknown roles pass; unit tests.

**Dependencies**: Phase 1
**Files**: `gateway/gateway.py`
**Exit criteria**: Existing gateway tests pass plus new tests for warn/enforce modes.

### Phase 3: Readonly Filesystem Mounts (L1)

**Goal**: Mount phase-restricted `.egg-state/` subdirectories as readonly in implement-
phase containers. This is the earliest enforcement point for file restrictions — the
agent gets an OS-level "Read-only file system" error immediately.

**Scope limitation**: Only implement-phase mounts. Refine/plan whole-repo readonly is
deferred per AD-5 (risk R9: may block agent tool operations). A TODO comment marks
this for future work after implement-phase mounts are validated in production.

**Tasks**:
- [TASK-3-1] Implement `phase_readonly_mounts()` in `shared/egg_container/__init__.py`.
  Returns readonly `MountSpec` list for the given phase. For implement phase: return
  readonly mounts for `.egg-state/contracts/`, `.egg-state/drafts/`,
  `.egg-state/pipelines/`, `.egg-state/reviews/`. For unknown/None phase: return empty
  list with warning log (fail-open). Skip mounts for non-existent source directories
  (with warning log). Include TODO comment for refine/plan phase future work.
  — Acceptance: 4 readonly MountSpec for implement phase; empty for unknown/None; skips missing dirs; fail-open; unit tests.

- [TASK-3-2] Add `ensure_egg_state_dirs()` to `gateway/worktree_manager.py` and
  `EGG_PHASE` env var to container spawner. `ensure_egg_state_dirs(worktree_path)`
  creates all `.egg-state/` subdirectories expected by `phase_readonly_mounts()` using
  a shared constant. Call before mount assembly in `spawn_agent_container()`. Add
  `EGG_PHASE` to container environment variables alongside `EGG_PIPELINE_ID` etc.
  — Acceptance: Directories created idempotently; shared constant for dir list; called before mounts; `EGG_PHASE` set on containers; unit tests.

- [TASK-3-3] Wire readonly mounts into container spawn. Call `phase_readonly_mounts()`
  in `spawn_agent_container()` during mount assembly. Extend mounts list after repo
  bind mounts and git shadow mounts. Place `.egg-readonly` marker files in readonly
  directories explaining the restriction and phase.
  — Acceptance: Readonly mounts added for implement phase; marker files present; integration test verifies EROFS on readonly paths and write on allowed paths; full orchestrator test suite passes.

**Dependencies**: Phase 1 (for session.phase)
**Files**: `shared/egg_container/__init__.py`, `gateway/worktree_manager.py`, `orchestrator/container_spawner.py`
**Exit criteria**: Full test suites pass (gateway, orchestrator, shared). Integration test validates mount behavior.

### Phase 4: Post-Agent Auto-Commit (L4)

**Goal**: After agent exit, automatically commit allowed uncommitted changes and push
via gateway API. This ensures no work is silently lost. Session stays alive during
auto-commit per AD-7, providing defense-in-depth via gateway push validation.

**Tasks**:
- [TASK-4-1] Create `gateway/post_agent_commit.py` with
  `auto_commit_allowed_changes(session, worktree_path)`. Steps: detect uncommitted
  changes via `git status --porcelain`; get file list via `git diff --name-only` +
  `git diff --cached --name-only`; filter via `check_phase_file_restrictions()` (same
  function as push-time enforcement — not reimplemented); restore blocked files via
  `git checkout --`; stage and commit allowed files with
  `"auto-commit: uncommitted changes from agent {container_id}"`; push via gateway API
  using session token (session kept alive per AD-7). Comprehensive audit logging. No-op
  if no uncommitted changes. Handle errors gracefully (log + continue to cleanup).
  Return `AutoCommitResult` with `committed_files`, `rejected_files`, `commit_sha`,
  `errors`.
  — Acceptance: Reuses `check_phase_file_restrictions()`; blocked files restored; allowed files committed; push via gateway; audit log; no-op when clean; errors don't block cleanup; safety assertion (committed subset of detected); unit tests.

- [TASK-4-2] Integrate auto-commit into session cleanup flow. Modify
  `delete_session()` / `delete_session_by_container()` per AD-7: auto-commit runs
  BEFORE checkpoint capture (so auto-commit SHA is in checkpoint); session stays alive
  during auto-commit (token valid for gateway push); session deleted after checkpoint;
  worktree cleanup after session deletion. Auto-commit is synchronous. Auto-commit
  failure does not prevent checkpoint capture or cleanup.
  — Acceptance: Ordering verified (auto-commit -> checkpoint -> session delete -> worktree cleanup); session alive during auto-commit; auto-commit SHA in checkpoint metadata; failure isolation; unit tests.

**Dependencies**: Phase 1 (for `session.assigned_branch`), Phase 3 (for worktree path lookup)
**Files**: `gateway/post_agent_commit.py` (new), `gateway/session_manager.py`, `gateway/worktree_manager.py`
**Exit criteria**: Full gateway test suite passes. Tests cover all auto-commit scenarios.

### Phase 5: Test Suite and Final Validation

**Goal**: Run the full test suite, fix any regressions, and ensure all layers work
together.

**Tasks**:
- [TASK-5-1] Run full test suite (`pytest gateway/tests/ orchestrator/tests/ tests/`)
  and fix any regressions introduced by Phases 1-4.
  — Acceptance: All existing tests pass. No test modifications except to fix genuine regressions caused by new enforcement.

- [TASK-5-2] Run linter (`make lint` or equivalent) and fix any issues.
  — Acceptance: Linter passes cleanly.

**Dependencies**: Phases 1-4
**Files**: Various test files as needed for fixes
**Exit criteria**: Full test suite and linter pass.

## Test Strategy

### Unit Tests (per phase)

| Phase | Test File | Coverage |
|-------|-----------|----------|
| 1 | `gateway/tests/test_branch_lock.py` (new) | `is_branch_switching()` for all detection cases: `-b`, `-B`, `--create`, `--`, ambiguous, `git switch` |
| 1 | `gateway/tests/test_commit_validation.py` (new) | `get_staged_files()` helper, commit-time validation for blocked/allowed/timeout/non-pipeline |
| 2 | `gateway/tests/test_gateway.py` (extend) | Agent-role restriction wiring: warn mode, enforce mode, unknown role, no role |
| 3 | Tests in shared/orchestrator dirs | `phase_readonly_mounts()` for implement/unknown/None/missing-dir; `ensure_egg_state_dirs()` idempotency |
| 4 | `gateway/tests/test_post_agent_commit.py` (new) | `auto_commit_allowed_changes()`: changes present, no changes, blocked only, mixed, errors |

### Integration Tests

- Phase 3: Integration test (`pytest.mark.integration`) that creates a container with
  implement-phase mounts and verifies: (a) agent can write to allowed paths, (b) gets
  EROFS on readonly paths, (c) `.egg-readonly` marker files present and readable.

### Regression Testing

- Phase 5: Full suite run (`pytest gateway/tests/ orchestrator/tests/ tests/`) to catch
  any regressions across the existing 1080+ gateway tests and other test suites.

## Rollback Plan

Each phase corresponds to a logical commit (or small group of commits) that can be
individually reverted:

| Phase | Revert Impact |
|-------|---------------|
| Phase 1 | Remove branch lock, commit validation, instruction changes. Agents return to unrestricted branch switching + push-time-only enforcement. |
| Phase 2 | Remove agent-role restriction wiring. Returns to current (no role enforcement). |
| Phase 3 | Remove `phase_readonly_mounts()` call. Containers return to fully writable mounts. |
| Phase 4 | Remove auto-commit call. Uncommitted changes lost on worktree removal (current behavior). |

No data migration required for any rollback. All changes are code/config only.

## Architecture Decisions Applied

| ID | Decision | Impact |
|----|----------|--------|
| AD-1 | Phases within single PR (adapted from multi-PR) | Organizes work for incremental commits within one PR |
| AD-2 | Atomic branch lock + instructions (Phase 1) | TASK-1-4 ships with TASK-1-2, addressing reviewer Issue 1 |
| AD-3 | Warn-only agent-role restrictions | TASK-2-1 defaults to warn-only, addressing reviewer Issue 2 |
| AD-4 | Conservative branch-switching heuristic | Block definite branch ops, allow ambiguous with logging |
| AD-5 | Implement-phase readonly only | Refine/plan deferred, addressing R9 risk |
| AD-6 | EGG_PHASE container env var | TASK-3-2, addressing reviewer minor gap |
| AD-7 | Session alive during auto-commit | TASK-4-2, push routed through gateway for defense-in-depth |

## Risk Mitigations

| Risk | Mitigation in Plan |
|------|-------------------|
| R1 (readonly mount misconfiguration) | Fail-open for unknown phases; skip missing dirs; implement-phase only (4 mounts, not whole-repo) |
| R2 (branch lock vs agent instructions) | Atomic deployment in Phase 1 (TASK-1-2 + TASK-1-4 same phase) |
| R3 (detection heuristic ambiguity) | Conservative heuristic; allow ambiguous cases + log; push-time safety net |
| R4 (auto-commit bypass) | Session kept alive; push routed through gateway; reuses same restriction function |
| R5 (commit subprocess latency) | 10-second timeout; fail-open; skip non-pipeline |
| R6 (agent-role false positives) | Warn-only mode; config flag for enforcement toggle |
| R7 (race condition in cleanup) | Explicit ordering: auto-commit -> checkpoint -> session delete -> worktree cleanup |
| R8 (missing .egg-state/ dirs) | `ensure_egg_state_dirs()` called before mount assembly; shared constant |
| R9 (refine/plan readonly) | Deferred; implement-phase only |
| R10 (integration risk) | Phased implementation; smallest blast radius first; full test suite per phase |

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Enforce phase restrictions via mounts and commit validation"
  description: |
    Implements four-layer defense-in-depth enforcement for phase file restrictions:
    branch lock (L0), readonly filesystem mounts (L1), commit-time validation (L2),
    and post-agent auto-commit (L4). Closes three gaps: local modification bypass,
    unrestricted branch switching, and late enforcement wasting agent tokens.

    Closes #644
phases:
  - id: 1
    name: Branch Lock + Agent Instructions
    goal: Lock agents to assigned branch and update instructions atomically
    tasks:
      - id: TASK-1-1
        description: Add assigned_branch field to Session dataclass, populate during register_session from worktree branch name, persist and restore
        acceptance: Session has assigned_branch field that is set, persisted, and restored correctly with unit tests
        files:
          - gateway/session_manager.py
          - gateway/worktree_manager.py
      - id: TASK-1-2
        description: Implement is_branch_switching() helper in git_client.py and wire into git_execute() for checkout/switch operations with conservative heuristic
        acceptance: Branch-create flags blocked, file restores allowed, ambiguous cases allowed with warning log, helpful error message, comprehensive unit tests, only enforced when assigned_branch is set
        files:
          - gateway/git_client.py
          - gateway/gateway.py
          - gateway/tests/test_branch_lock.py
      - id: TASK-1-3
        description: Add commit-time staged-file validation in git_execute() using get_staged_files() helper with 10-second timeout and fail-open
        acceptance: Blocked staged files rejected with actionable error, allowed files pass, timeout fails open, non-pipeline commits unaffected, unit tests
        files:
          - gateway/gateway.py
          - gateway/git_client.py
          - gateway/tests/test_commit_validation.py
      - id: TASK-1-4
        description: Update mission.md and environment.md to remove git checkout -b branch-switching instructions and add branch lock, phase restriction, and auto-commit guidance
        acceptance: No git checkout -b for branch switching in mission.md or environment.md, branch lock and phase guidance sections present
        files:
          - sandbox/.claude/rules/mission.md
          - sandbox/.claude/rules/environment.md
  - id: 2
    name: Agent-Role Restriction Wiring
    goal: Wire check_agent_restrictions() into git_push() in warn-only mode for data collection
    tasks:
      - id: TASK-2-1
        description: Wire check_agent_restrictions() into git_push() with EGG_AGENT_RESTRICTIONS_ENFORCE config flag defaulting to false (warn-only mode logs violations but allows push)
        acceptance: Called when session has agent_role, default warn-only with audit log, enforce mode behind flag, unknown roles pass, unit tests for warn mode, enforce mode, unknown role, no-role cases
        files:
          - gateway/gateway.py
  - id: 3
    name: Readonly Filesystem Mounts
    goal: Mount phase-restricted .egg-state/ subdirectories as readonly in implement-phase containers
    tasks:
      - id: TASK-3-1
        description: Implement phase_readonly_mounts() in egg_container alongside git_shadow_mounts() returning readonly MountSpec for implement-phase .egg-state/ subdirs, fail-open for unknown phases
        acceptance: 4 readonly MountSpec for implement phase, empty for unknown/None, skips missing source dirs with warning, TODO for refine/plan, unit tests
        files:
          - shared/egg_container/__init__.py
      - id: TASK-3-2
        description: Add ensure_egg_state_dirs() with shared constant for directory list, call before mount assembly, add EGG_PHASE to container environment variables
        acceptance: Directories created idempotently, shared constant, called before mounts in spawn_agent_container, EGG_PHASE set on containers, unit tests
        files:
          - gateway/worktree_manager.py
          - orchestrator/container_spawner.py
      - id: TASK-3-3
        description: Wire phase_readonly_mounts() into spawn_agent_container mount assembly and place .egg-readonly marker files in readonly directories
        acceptance: Readonly mounts added after repo and git shadow mounts, marker files explain restriction and phase, integration test verifies EROFS on readonly and write on allowed paths
        files:
          - orchestrator/container_spawner.py
  - id: 4
    name: Post-Agent Auto-Commit
    goal: Automatically commit allowed uncommitted changes after agent exit and push via gateway
    tasks:
      - id: TASK-4-1
        description: Create post_agent_commit.py with auto_commit_allowed_changes() that detects uncommitted changes, filters via check_phase_file_restrictions(), restores blocked files, commits allowed files, pushes via gateway API
        acceptance: Reuses check_phase_file_restrictions (not reimplemented), blocked files restored, allowed files committed with egg author, push via gateway using session token, audit log, no-op when clean, errors don't block cleanup, unit tests
        files:
          - gateway/post_agent_commit.py
      - id: TASK-4-2
        description: Integrate auto-commit into session cleanup flow with ordering auto-commit then checkpoint then session-delete then worktree-cleanup, keeping session alive during auto-commit
        acceptance: Auto-commit runs before checkpoint capture, session alive during auto-commit, auto-commit SHA in checkpoint metadata, failure does not prevent cleanup, unit tests verify ordering
        files:
          - gateway/session_manager.py
          - gateway/worktree_manager.py
  - id: 5
    name: Final Validation
    goal: Run full test suite and linter, fix any regressions
    tasks:
      - id: TASK-5-1
        description: Run full test suite (pytest gateway/tests/ orchestrator/tests/ tests/) and fix any regressions from Phases 1-4
        acceptance: All existing tests pass with no modifications except genuine regression fixes
        files:
          - gateway/tests/
      - id: TASK-5-2
        description: Run linter and fix any issues
        acceptance: Linter passes cleanly
        files:
          - gateway/
          - shared/
          - orchestrator/
```

---

*Authored-by: egg*
