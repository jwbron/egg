# Implementation Plan: Enforce Phase File Restrictions (#644)

## Approach

This PR closes three enforcement gaps in the gateway's phase file restriction system:

1. **Local modification bypass** — agents can modify protected files in their worktree without pushing, and the orchestrator may read those modifications (critical for #641 DinD validation).
2. **Unrestricted branch switching** — agents can leave their assigned branch (`egg/{container_id}/work`), breaking deterministic post-agent commit/push.
3. **Late enforcement wastes tokens** — restrictions only fire at push time, so agents spend tokens modifying/staging/committing protected files before discovering the block.

The solution is a four-layer defense-in-depth approach, plus an agent instructions update. Each layer addresses a distinct gap and is independently testable. A bonus fix wires the existing-but-unused `check_agent_restrictions()` into the push handler.

### Delivery order rationale

The layers are ordered by value-to-risk ratio:

- **Phase 1 (L0 — branch lock)**: Smallest scope, gateway-only changes, unblocks deterministic post-agent work capture. No mount or container changes.
- **Phase 2 (L2 — commit-time validation + agent-role wiring)**: Moderate scope, gateway-only changes, immediate token savings. Can be developed independently.
- **Phase 3 (L1 — readonly filesystem mounts)**: Larger scope, touches container spawner and shared library, needs integration testing. Prevents the local modification bypass at the OS level.
- **Phase 4 (L4 — post-agent auto-commit)**: New module, depends on L0 for `assigned_branch`. Safety net for work loss prevention.
- **Phase 5 (agent instructions + tests)**: Cross-cutting docs and final test sweep.

All layers ship in a single PR. Each phase produces one or more commits.

---

## Phase 1: Branch Lock (Layer 0)

**Goal**: Lock agents to their assigned branch so post-agent commit/push is deterministic.

### Changes

**`gateway/git_client.py`** — Add `is_branch_switching(operation, args, assigned_branch, repo_path)` helper:
- If `--` separator present → everything after is file paths (allowed)
- If `-b`, `-B`, `--create`, `--force-create`, `-c`, `-C` flags present → branch create (blocked)
- If `--detach` or `-d` on `switch` → blocked
- Otherwise → check if first non-flag argument resolves as a ref different from `assigned_branch` via `git rev-parse --verify`
- Conservative: if uncertain, allow (git will error naturally)

**`gateway/gateway.py`** — In `git_execute()`, after existing arg validation for `checkout`/`switch` operations:
- Read `g.session.assigned_branch`
- Call `is_branch_switching()`
- If blocked, return 403 with actionable error message

**`gateway/session_manager.py`** — Add `assigned_branch: str | None = None` to `Session` dataclass. Populate during `register_session()`.

**`gateway/worktree_manager.py`** — Ensure `create_worktree()` returns branch name (already does via `WorktreeInfo`). Session registration flow passes it through.

### Tests

**`gateway/tests/test_git_client.py`** — Unit tests for `is_branch_switching()`:
- `-b` flag detection → blocked
- `--` separator → allowed
- File-targeting checkout (`git checkout -- file.txt`) → allowed
- Ref-targeting checkout (`git checkout other-branch`) → blocked
- Checkout to assigned branch → allowed (no-op)
- Bare checkout (no args) → allowed

**`gateway/tests/test_gateway.py`** — Integration-style tests:
- `git_execute()` blocks branch switch when session has `assigned_branch`
- `git_execute()` allows file checkout
- No enforcement when `assigned_branch` is None (backward compat)

---

## Phase 2: Commit-Time Validation + Agent-Role Wiring (Layer 2)

**Goal**: Shift file restriction enforcement from push-time to commit-time, saving agent tokens. Also wire the unused `check_agent_restrictions()` into the push handler.

### Changes

**`gateway/git_client.py`** — Add `get_staged_files(repo_path)` helper:
- Runs `git diff --cached --name-only` in the worktree
- Returns list of staged file paths

**`gateway/gateway.py`** — In `git_execute()`, when operation is `commit`:
- Call `get_staged_files()` using the session's repo path
- Apply `check_phase_file_restrictions(session.phase, staged_files)` if session has a phase
- Apply `check_agent_restrictions(session.agent_role, staged_files)` if session has an agent_role
- If blocked, return 403 with actionable error (blocked files listed, unstage command, alternative CLI)

**`gateway/gateway.py`** — In `git_push()`, after existing role-based check:
- Add `check_agent_restrictions(session.agent_role, changed_files)` call
- This wires the existing dead code into the enforcement path

### Tests

**`gateway/tests/test_gateway.py`** — Commit-time validation tests:
- Commit with blocked staged files → 403 with actionable error
- Commit with allowed staged files → proceeds
- Commit without phase (non-pipeline) → no enforcement
- Error message includes unstage command and alternative CLI

**`gateway/tests/test_phase_filter.py`** — Agent-role wiring tests:
- `check_agent_restrictions()` called during push for known roles
- Unknown roles → allowed (backward compat)

---

## Phase 3: Readonly Filesystem Mounts (Layer 1)

**Goal**: Prevent agents from modifying protected files at the OS level by mounting restricted directories as readonly in the container.

### Changes

**`shared/egg_container/__init__.py`** — Add `phase_readonly_mounts(repo_volumes, phase)` function:
- Returns `list[MountSpec]` of readonly (and writable overlay) mounts based on phase
- **Implement phase**: 4 individual readonly mounts for `.egg-state/contracts/`, `.egg-state/drafts/`, `.egg-state/pipelines/`, `.egg-state/reviews/`
- **Refine/plan phases**: Entire repo mounted readonly, plus 5 writable overlay mounts for `.egg-state/contracts/`, `.egg-state/drafts/`, `.egg-state/checkpoints/`, `.egg-state/agent-outputs/`, `.egg-state/reviews/`
- **PR phase**: No readonly mounts (full access)
- Unknown phases: No readonly mounts + log warning (fail-open)

**`orchestrator/container_spawner.py`** — In `spawn_agent_container()`:
- Call `phase_readonly_mounts()` during mount assembly, after `git_shadow_mounts()`
- Pass phase parameter (already available)

**`gateway/worktree_manager.py`** — Add `ensure_egg_state_dirs(worktree_path)`:
- Creates required `.egg-state/` subdirectories before container spawn
- Prerequisite for bind mounts (source path must exist)

**Marker files**: Generate `.egg-readonly` marker files in readonly directories during worktree setup, with phase-specific explanatory content.

### Tests

**`tests/shared/egg_container/test_config_builder.py`** — Unit tests for `phase_readonly_mounts()`:
- Implement phase → 4 readonly mounts, source code writable
- Refine/plan phase → repo readonly + 5 writable overlays
- PR phase → no readonly mounts
- Unknown phase → no readonly mounts (fail-open)
- Mount paths use correct source/destination mapping

**`gateway/tests/test_worktree_manager.py`** — `ensure_egg_state_dirs()`:
- Creates expected subdirectories
- Idempotent (no error if dirs exist)

---

## Phase 4: Post-Agent Auto-Commit (Layer 4)

**Goal**: After agent exits, automatically commit and push any uncommitted changes to allowed files, preventing silent work loss.

### Changes

**`gateway/post_agent_commit.py`** (new file):
- `auto_commit_and_push(container_id, worktree_path, assigned_branch, phase, agent_role)`:
  1. `git status --porcelain` to detect uncommitted changes
  2. Get modified file list via `git diff --name-only` + `git diff --cached --name-only`
  3. Filter against phase restrictions (import `PhaseFileRestriction` logic)
  4. Blocked files modified locally → log warning, `git checkout -- <blocked_files>` to restore
  5. Stage allowed files → `git add <allowed_files>`
  6. Commit with `"auto-commit: uncommitted changes from agent {container_id}"`, author `egg <egg@localhost>`
  7. Push to `origin {assigned_branch}`
  8. Return commit SHA or None if nothing to commit
  9. On push failure → log error, return error info for checkpoint

**`gateway/session_manager.py`** — In `_capture_and_cleanup_session()`:
- Call `auto_commit_and_push()` before worktree cleanup
- Include auto-commit SHA in checkpoint if applicable

**`gateway/worktree_manager.py`** — Expose `get_worktree_info(container_id)` (may already exist via `_active_worktrees`).

### Tests

**`gateway/tests/test_post_agent_commit.py`** (new test file):
- No uncommitted changes → no commit, returns None
- Uncommitted allowed files → committed and pushed
- Uncommitted blocked files → restored (checkout), not committed, warning logged
- Mixed allowed/blocked → only allowed committed
- Push failure → error logged, commit still succeeds locally
- Uses mocked subprocess/git calls

---

## Phase 5: Agent Instructions + Final Test Sweep

**Goal**: Update agent instructions to reflect new constraints and run full test suite.

### Changes

**`sandbox/.claude/rules/mission.md`** — Add new sections:
- **Branch lock**: Explain agents are on a fixed branch, switching is blocked
- **Phase restrictions**: Explain readonly filesystem errors and `.egg-readonly` marker files
- **Auto-commit/push**: Explain post-session auto-commit safety net
- **Phase-specific guidance**: What each phase can/cannot modify

### Verification

- Run `make test` or `pytest` across gateway and shared test suites
- Verify all new tests pass
- Verify existing tests still pass (no regressions)

---

## Test Strategy Summary

| Layer | Test Location | Type | What's Tested |
|-------|--------------|------|---------------|
| L0 (branch lock) | `gateway/tests/test_git_client.py` | Unit | `is_branch_switching()` heuristic |
| L0 (branch lock) | `gateway/tests/test_gateway.py` | Unit | `git_execute()` branch enforcement |
| L2 (commit-time) | `gateway/tests/test_gateway.py` | Unit | Staged file validation at commit |
| L2 (agent-role) | `gateway/tests/test_phase_filter.py` | Unit | `check_agent_restrictions()` wiring |
| L1 (readonly mounts) | `tests/shared/egg_container/test_config_builder.py` | Unit | Mount spec generation per phase |
| L1 (ensure dirs) | `gateway/tests/test_worktree_manager.py` | Unit | Directory creation |
| L4 (auto-commit) | `gateway/tests/test_post_agent_commit.py` | Unit | Commit/push logic, filtering |
| All | Full suite | Regression | No broken existing tests |

---

## Risk Mitigations

1. **Readonly mounts block all writes**: Fail-open for unknown phases. Unit test mount specs per phase.
2. **Branch lock misclassifies file checkout**: Conservative heuristic — if uncertain, allow. `--` separator always allowed.
3. **Commit-time validation adds latency**: `git diff --cached --name-only` is sub-100ms. Only runs for pipeline sessions.
4. **Auto-commit creates noisy commits**: Only runs if uncommitted changes exist. Clear auto-commit message attribution.
5. **Docker nested mount ordering**: Inner writable mounts override outer readonly (documented Docker behavior). Test in CI.

---

```yaml
# yaml-tasks
pr:
  title: "Enforce phase file restrictions via mounts and commit-time validation"
  description: |
    Closes three enforcement gaps in the phase file restriction system: local modification
    bypass (agents modify protected files without pushing), unrestricted branch switching
    (breaks deterministic post-agent commit/push), and late enforcement token waste
    (restrictions only discovered at push time). Implements a four-layer defense-in-depth
    approach: branch lock, readonly filesystem mounts, commit-time gateway validation,
    and post-agent auto-commit. Also wires the unused check_agent_restrictions() into
    the push handler.
phases:
  - id: 1
    name: Branch Lock (Layer 0)
    goal: Lock agents to their assigned branch for deterministic post-agent work capture
    tasks:
      - id: TASK-1-1
        description: Add is_branch_switching() helper to gateway/git_client.py that classifies checkout/switch args as branch-targeting vs file-targeting
        acceptance: Function correctly identifies branch-create flags (-b, -B, --create, --force-create), -- separator for file paths, and ref-targeting args. Unit tests pass in test_git_client.py.
        files:
          - gateway/git_client.py
          - gateway/tests/test_git_client.py
      - id: TASK-1-2
        description: Add assigned_branch field to Session dataclass in session_manager.py and populate it during session registration
        acceptance: Session.assigned_branch is set when session is registered with a worktree. Field persists across session serialization/deserialization. Unit tests pass in test_session_manager.py.
        files:
          - gateway/session_manager.py
          - gateway/tests/test_session_manager.py
      - id: TASK-1-3
        description: Integrate branch lock enforcement in git_execute() for checkout/switch operations in gateway.py
        acceptance: git checkout other-branch returns 403 with actionable error when session has assigned_branch. git checkout -- file.txt is allowed. No enforcement when assigned_branch is None. Tests pass in test_gateway.py.
        files:
          - gateway/gateway.py
          - gateway/tests/test_gateway.py

  - id: 2
    name: Commit-Time Validation + Agent-Role Wiring (Layer 2)
    goal: Shift file restriction enforcement to commit time and wire unused agent-role restrictions
    tasks:
      - id: TASK-2-1
        description: Add get_staged_files() helper to gateway/git_client.py that extracts staged file list via git diff --cached --name-only
        acceptance: Function returns list of staged file paths. Handles empty staging area (returns empty list). Unit tests pass.
        files:
          - gateway/git_client.py
          - gateway/tests/test_git_client.py
      - id: TASK-2-2
        description: Add commit-time phase and agent-role file restriction checks in git_execute() for commit operations
        acceptance: git commit with blocked staged files returns 403 with actionable error listing blocked files, unstage command, and alternative CLI. Allowed files proceed normally. Non-pipeline commits (no phase) are unaffected. Tests pass.
        files:
          - gateway/gateway.py
          - gateway/tests/test_gateway.py
      - id: TASK-2-3
        description: Wire check_agent_restrictions() into git_push() handler alongside existing check_file_restrictions() call
        acceptance: Agent-role restrictions are enforced at push time for sessions with agent_role. Unknown roles are allowed (backward compat). Tests pass in test_phase_filter.py.
        files:
          - gateway/gateway.py
          - gateway/tests/test_phase_filter.py

  - id: 3
    name: Readonly Filesystem Mounts (Layer 1)
    goal: Prevent local modification bypass by mounting restricted directories as readonly in containers
    tasks:
      - id: TASK-3-1
        description: Add phase_readonly_mounts() function to shared/egg_container/__init__.py that returns phase-appropriate readonly MountSpec entries
        acceptance: Implement phase returns 4 readonly mounts for .egg-state/ subdirs. Refine/plan phases return repo-wide readonly mount plus 5 writable overlays. PR phase returns empty list. Unknown phases return empty list (fail-open). Unit tests pass.
        files:
          - shared/egg_container/__init__.py
          - tests/shared/egg_container/test_config_builder.py
      - id: TASK-3-2
        description: Add ensure_egg_state_dirs() to gateway/worktree_manager.py that creates required .egg-state/ subdirectories before container spawn
        acceptance: Function creates contracts/, drafts/, pipelines/, reviews/, checkpoints/, agent-outputs/ under .egg-state/. Idempotent. Tests pass.
        files:
          - gateway/worktree_manager.py
          - gateway/tests/test_worktree_manager.py
      - id: TASK-3-3
        description: Integrate phase_readonly_mounts() into mount assembly in orchestrator/container_spawner.py and call ensure_egg_state_dirs() before spawn
        acceptance: spawn_agent_container() includes phase-based readonly mounts in container config. .egg-state/ directories are created before mount. Existing tests still pass.
        files:
          - orchestrator/container_spawner.py
      - id: TASK-3-4
        description: Generate .egg-readonly marker files in readonly directories during worktree setup with phase-specific explanatory content
        acceptance: Marker files exist in readonly directories, contain phase name, restriction explanation, and alternative CLI guidance. Created during ensure_egg_state_dirs() or pre-spawn step.
        files:
          - gateway/worktree_manager.py

  - id: 4
    name: Post-Agent Auto-Commit (Layer 4)
    goal: Automatically commit and push uncommitted allowed changes after agent exits to prevent work loss
    tasks:
      - id: TASK-4-1
        description: Create gateway/post_agent_commit.py with auto_commit_and_push() function that commits allowed uncommitted changes and pushes to remote
        acceptance: Function detects uncommitted changes, filters against phase restrictions, commits only allowed files, restores blocked files, pushes to assigned branch. Handles push failures gracefully. Unit tests pass with mocked git calls.
        files:
          - gateway/post_agent_commit.py
          - gateway/tests/test_post_agent_commit.py
      - id: TASK-4-2
        description: Integrate auto_commit_and_push() into session cleanup flow in session_manager.py, running after container exit but before worktree removal
        acceptance: Auto-commit runs during session cleanup. Auto-commit SHA included in checkpoint if applicable. Worktree info accessible for container_id lookup. Tests pass.
        files:
          - gateway/session_manager.py
          - gateway/worktree_manager.py

  - id: 5
    name: Agent Instructions + Final Verification
    goal: Update agent instructions and verify all tests pass
    tasks:
      - id: TASK-5-1
        description: Update sandbox/.claude/rules/mission.md with branch lock, phase restriction, auto-commit, and phase-specific guidance sections
        acceptance: Agent instructions document branch lock, readonly filesystem behavior, .egg-readonly marker files, auto-commit safety net, and per-phase file access rules. Content is concise and actionable.
        files:
          - sandbox/.claude/rules/mission.md
      - id: TASK-5-2
        description: Run full test suite and fix any regressions
        acceptance: All existing tests pass. All new tests pass. No regressions introduced by any layer.
        files: []
```
