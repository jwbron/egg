# Plan: Enforce per-task file restrictions from planner in implement phase

> Issue: #912 | Phase: plan | Revision: 2

## Summary

Connect the planner's per-task `files_affected` data to gateway enforcement so that
implement-phase coder agents can only commit files listed in their assigned tasks. This
closes a gap in Tier 3 parallel dispatch where agents can accidentally modify each
other's files. The implementation adds `allowed_files` to the Session model, threads it
from the container spawner through the gateway client, and enforces it at push time with
a warn-then-block escalation pattern that matches the existing agent-role enforcement
precedent.

## Review Feedback Addressed (Revision 2)

**1. fnmatch matching semantics (BLOCKING)**: The previous plan described directory-sibling
expansion as "shallow (`dir/*`, not `dir/**`)". This was incorrect — Python's
`fnmatch.fnmatch()` treats `*` as matching `/`, so `fnmatch('dir/sub/deep/file.py',
'dir/*')` returns `True`. The `dir/*` pattern is effectively **recursive**, covering all
descendants, not just immediate children. This is the existing production behavior used by
all phase restriction patterns (e.g., `.egg-state/contracts/*` already matches nested
files recursively at `phase_filter.py:244`).

**Resolution (option b)**: We acknowledge recursive matching throughout the plan. Listing
`dir/foo.py` grants access to the entire `dir/` subtree via the `dir/*` expansion
pattern. This aligns with the "guide, don't cage" philosophy — containment is coarser than
per-file but still prevents cross-module contamination (e.g., a task listing
`src/auth/login.py` cannot touch `src/payments/`). Changing to segment-aware matching
would be a breaking change to all existing phase restrictions and is out of scope.

The risk assessment now notes this explicitly: agents with any file in `dir/` can
recursively modify all of `dir/`. Test scenarios verify recursive matching behavior so
future readers understand the actual semantics.

**2. Missing Session model test task (BLOCKING)**: Added TASK-4-0 covering Session model
serialization round-trip tests: (a) `allowed_files` persists through
`to_dict_for_persistence()`/`from_persistence()`, (b) sessions without `allowed_files`
load with `None` (backward compat), (c) `_warned_files` initializes to empty dict and is
not persisted.

## Approach

**Selected design**: Session-level `allowed_files` (architect Option A). The data flow
after implementation:

```
Plan YAML (files: [...])
  → plan_parser → Task.files_affected in contract
  → container spawner computes union per plan_phase + dir expansion
  → passed as allowed_files to gateway_client.register_session()
  → stored on Session.allowed_files (persisted)
  → push validation: warn on 1st violation, block on 2nd (per-file)
  → post-agent auto-commit: filter out-of-scope files before committing
```

**Key design decisions** (from architect analysis):
- Warn threshold = 1 (one free pass per file, block on repeat)
- Directory-sibling expansion uses `dir/*` which is **recursive** via fnmatch (see above)
- Config files are planner responsibility — no implicit allowlist
- Escape hatch (`egg-contract request-file`) deferred to follow-up issue
- Warning counters are transient (fail-open on gateway restart)
- All validation layers AND together (most restrictive wins)
- Only implement-phase coder agents; tester/documenter/integrator unaffected
- Strict mode available via `EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true`

**fnmatch matching semantics**: `_matches_pattern()` at `gateway/phase_filter.py:230-250`
uses `fnmatch.fnmatch()` for any pattern containing `*`. Python's fnmatch does **not**
treat `*` as segment-aware — it matches `/` freely. This means:
- `fnmatch('dir/sub/deep/file.py', 'dir/*')` → `True` (recursive!)
- `fnmatch('dir/file.py', 'dir/*')` → `True`
- Listing `src/auth/login.py` → expansion adds `src/auth/*` → grants access to all of `src/auth/` recursively

This is the same behavior used by existing production phase patterns
(`.egg-state/contracts/*`, `.egg-state/drafts/*`). It provides coarse containment at the
directory subtree level, which aligns with the "guide, don't cage" philosophy.

## Implementation Phases

### Phase 1: Data Model and API Plumbing

**Goal**: Establish the `allowed_files` field on Session, accept it through the gateway
API, and thread it from the orchestrator's gateway client. After this phase the data can
flow end-to-end but is not yet enforced.

**Task 1-1: Add `allowed_files` to Session dataclass**

Add `allowed_files: list[str] | None = None` to `Session` (after `complexity_tier` at
line ~245). Persist it via `to_dict_for_persistence()` / `from_persistence()`. Add
transient `_warned_files: dict[str, int]` (initialized in `__post_init__`, not persisted)
for per-file violation counting. Ensure backward compat: sessions loaded from disk without
the field default to `None`.

**Task 1-2: Accept `allowed_files` in `/sessions/create` endpoint**

In the session creation handler (gateway.py line ~3198), extract `allowed_files` from
the request JSON. Validate: must be `None`, omitted, or a list of non-empty strings.
Pass to `session_manager.register_session()`.

**Task 1-3: Add `allowed_files` to `GatewayClient.register_session()`**

Add `allowed_files: list[str] | None = None` parameter to the orchestrator's
`GatewayClient.register_session()` (line ~230). Include in the POST payload when
non-None.

**Dependencies**: 1-1 → 1-2 → 1-3 (sequential; each extends the prior)
**Exit criteria**: A session created with `allowed_files` persists and reloads correctly. The gateway client can pass the field through to the gateway.

### Phase 2: Orchestrator Integration

**Goal**: The container spawner computes `allowed_files` from the contract's
`files_affected` data and passes it when registering implement-phase coder sessions.

**Task 2-1: Add `_compute_allowed_files()` helper and integrate with spawner**

Add a helper function `_compute_allowed_files(contract, plan_phase_id, agent_role)` to
`container_spawner.py` that:
1. Returns `None` if `agent_role != "coder"` or phase is not implement
2. Loads tasks for the given `plan_phase_id` from the contract
3. Collects the union of `files_affected` across all tasks
4. Applies directory-sibling expansion: for each explicit file `dir/foo.py`, add
   `dir/*` to the pattern set. This grants **recursive** access to `dir/` and all
   subdirectories (due to fnmatch semantics). Glob patterns already in `files_affected`
   (e.g., `tests/**`) pass through unchanged.
5. Returns `None` if the union is empty (graceful fallback)

Integrate into `spawn_agent_container()` — call the helper before
`register_session()` and pass the result.

**Task 2-2: Thread `plan_phase_id` to spawner**

`_spawn_and_wait()` (pipelines.py line ~4059) already receives `plan_phase_id`. Add it
as a parameter to `spawn_agent_container()` so the spawner can look up the correct
tasks. Only needed for the `_compute_allowed_files()` call — it does not change the
container itself.

**Dependencies**: 2-1 depends on Phase 1 (gateway client must accept `allowed_files`). 2-2 is tightly coupled with 2-1.
**Exit criteria**: An implement-phase coder container is registered with `allowed_files` derived from its plan phase tasks. Non-coder agents and non-implement phases get `None`.

### Phase 3: Gateway Enforcement

**Goal**: Push validation warns/blocks on out-of-scope files. Post-agent auto-commit
filters them. This is the core behavioral change.

**Task 3-1: Per-task push validation layer (warn-then-block)**

In gateway.py push validation (after agent-role checks at line ~791, before phase checks
at line ~793), add a new validation layer:
1. Skip if `session.allowed_files` is None or empty
2. Build a `PhaseFileRestriction(allowed_patterns=session.allowed_files)` to reuse
   existing glob matching (fnmatch-based, recursive within directories)
3. For each changed file not matching the allowed patterns:
   - Check `session._warned_files[file]` count
   - If count < threshold (default 1, from `EGG_TASK_FILE_WARN_THRESHOLD`): increment,
     log structured warning, allow push
   - If count >= threshold: add to blocked list
4. If `EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true`: skip warnings, block immediately
5. Checkpoint pushes (`is_checkpoint_push=True`) bypass this layer
6. Error message includes: blocked files, allowed patterns, recovery hint

**Task 3-2: Per-task filtering in post-agent auto-commit**

Extend `auto_commit_worktree()` (post_agent_commit.py line ~126) to accept
`allowed_files: list[str] | None` parameter. After phase restriction filtering (line
~193), apply a second filter using the same `PhaseFileRestriction` pattern. Files
outside `allowed_files` are restored (not committed). Log filtered files clearly with
structured event for debugging. The caller reads `allowed_files` from the session.

**Dependencies**: Depends on Phase 1 (Session model). Can be developed in parallel with Phase 2.
**Exit criteria**: First push with out-of-scope file succeeds with warning. Second push with same file is blocked. Auto-commit excludes out-of-scope files with clear logging. Strict mode blocks immediately.

### Phase 4: Tests

**Goal**: Comprehensive test coverage for all new behavior. Follows existing pytest
patterns with `MagicMock` and `patch`.

**Task 4-0: Session model persistence tests for `allowed_files`**

Add tests to `gateway/tests/test_session_manager.py` covering:
- `allowed_files` round-trip: set `allowed_files=["src/auth/*", "tests/test_auth.py"]`
  on a Session, call `to_dict_for_persistence()`, then `from_persistence()` — the
  reconstructed Session must have the same `allowed_files` list
- Backward compat: `from_persistence()` with a dict that has no `allowed_files` key
  produces a Session with `allowed_files=None`
- `_warned_files` initializes to empty dict on construction and is **not** present in
  the output of `to_dict_for_persistence()`
- `_warned_files` is **not** passed to `from_persistence()` (transient state)

**Task 4-1: Gateway tests for per-session file restriction enforcement**

New test file `gateway/tests/test_session_file_restrictions.py` covering:
- Push with allowed files passes
- Push with disallowed file warns on first attempt
- Same disallowed file blocked on second attempt
- `EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true` blocks immediately
- Checkpoint pushes bypass per-task restrictions
- `None`/empty `allowed_files` = no restriction
- Glob patterns in `allowed_files` match correctly
- Directory-sibling expansion: `dir/*` covers `dir/newfile.py`
- **Recursive matching verification**: `dir/*` also covers `dir/sub/deep/file.py`
  (explicit test to document fnmatch behavior for future readers)

**Task 4-2: Post-agent commit tests with `allowed_files`**

Extend `gateway/tests/test_post_agent_commit.py` (or create companion file) covering:
- Files outside `allowed_files` are restored, not committed
- Files inside `allowed_files` are committed
- `None` `allowed_files` = no per-task filtering (phase filtering still applies)
- Logging verifies filtered files are reported

**Task 4-3: Orchestrator tests for `_compute_allowed_files()` and spawner**

Extend `orchestrator/tests/test_container_spawner.py` covering:
- Union of `files_affected` from multiple tasks in a phase
- Directory-sibling expansion (`dir/foo.py` → includes `dir/*`, which is recursive)
- Empty `files_affected` across all tasks → returns `None`
- Non-coder agents → returns `None`
- Non-implement phases → returns `None`
- Glob patterns in `files_affected` preserved unchanged
- `register_session()` called with computed `allowed_files`

**Dependencies**: Depends on Phases 1-3 being implemented. Tests can be written in parallel with implementation if stubs are available.
**Exit criteria**: All new tests pass. Existing tests continue to pass (backward compat).

### Phase 5: Documentation

**Goal**: Update the plan template so planners know `files:` is enforced.

**Task 5-1: Update plan template documentation**

Update `docs/templates/plan.md` to:
- State that `files:` is an **enforcement boundary** at push time, not just a hint
- Add guidance: list files generously, use globs (`tests/**`, `src/module/*.py`)
- Note that directory access is **recursive**: listing `dir/foo.py` grants the agent
  access to all files under `dir/` (including subdirectories) via `dir/*` expansion and
  fnmatch semantics
- Remind planners to include config files (`pyproject.toml`, `Makefile`) when tasks
  modify build configuration or add dependencies
- Clarify that empty `files:` = no restriction (fallback to phase-level only)

**Dependencies**: None (can be done at any time).
**Exit criteria**: Template clearly documents enforcement semantics with practical guidance including recursive matching behavior.

## Test Strategy

| Scope | Location | What |
|-------|----------|------|
| Unit: Session model | `gateway/tests/test_session_manager.py` | `allowed_files` round-trip, backward compat, `_warned_files` init (TASK-4-0) |
| Unit: Push validation | `gateway/tests/test_session_file_restrictions.py` | Warn-then-block, strict mode, globs, recursive dir matching, checkpoint bypass (TASK-4-1) |
| Unit: Post-agent commit | `gateway/tests/test_post_agent_commit.py` | Filtering, restore, logging, None fallback (TASK-4-2) |
| Unit: `_compute_allowed_files` | `orchestrator/tests/test_container_spawner.py` | Union, expansion, fallbacks, glob preservation (TASK-4-3) |
| Integration: Tier 3 | `orchestrator/tests/test_container_spawner.py` | Spawner passes `allowed_files` to `register_session()` (TASK-4-3) |

Existing test suites must continue passing — all changes are backward-compatible
(`allowed_files=None` preserves current behavior).

## Rollback Plan

All changes are behind the `allowed_files` field which defaults to `None`. If issues
arise:
1. **Immediate**: Set `EGG_TASK_FILE_WARN_THRESHOLD=999` to effectively disable blocking
2. **Quick**: Stop passing `allowed_files` from container spawner (one-line change)
3. **Full**: Revert the PR — no database migrations or persistent state changes needed

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Planner produces too-narrow file lists → agents spin | Medium | Medium | Dir expansion (recursive via fnmatch), warn-then-block, template guidance |
| `dir/*` grants recursive access to entire subtree | Accepted | Low | This is the existing fnmatch behavior for all phase patterns. Containment is at directory-subtree level, not per-file. Sufficient for preventing cross-module contamination. |
| Session model changes break existing tests | Low | Low | `allowed_files` defaults to `None`; backward compatible |
| Warn-then-block creates inconsistent state | Medium | Low | Post-agent commit provides final filter; integrator cleans up |
| Performance impact on push validation | Low | Low | O(patterns × files) is negligible; data pre-computed on session |
| Confusing errors from layered restrictions | Low | Medium | Each layer's error clearly identifies its type |

## Migration Notes

- No database migrations required
- No config file changes required (new env vars are optional)
- New env vars: `EGG_TASK_FILE_WARN_THRESHOLD` (default: 1), `EGG_TASK_FILE_RESTRICTIONS_ENFORCE` (default: false)
- No breaking changes — all existing behavior preserved when `allowed_files` is `None`

---

```yaml
# yaml-tasks
pr:
  title: "Enforce per-task file restrictions in implement phase"
  description: |
    Connect the planner's per-task files_affected data to gateway push enforcement.
    When the orchestrator spawns an implement-phase coder agent, it computes the
    union of files_affected from the agent's assigned tasks and passes it as
    allowed_files to the gateway session. The gateway then enforces warn-then-block
    at push time and filters out-of-scope files during post-agent auto-commit.
    This prevents cross-contamination in Tier 3 parallel dispatch where multiple
    agents work on disjoint file sets. Directory-sibling expansion uses dir/*
    which is recursive via fnmatch semantics (matching existing phase restriction
    behavior). Closes #912.
phases:
  - id: 1
    name: Data Model and API Plumbing
    goal: Establish allowed_files on Session and thread it through the API
    tasks:
      - id: TASK-1-1
        description: Add allowed_files field to Session dataclass with persistence and transient _warned_files counter
        acceptance: Session.allowed_files persists through to_dict_for_persistence()/from_persistence(). _warned_files initializes to empty dict and is not persisted. Existing sessions without allowed_files load with None.
        files:
          - gateway/session_manager.py
      - id: TASK-1-2
        description: Accept and validate allowed_files in /api/v1/sessions/create endpoint
        acceptance: POST /sessions/create with allowed_files=[...] stores them on Session. None and omitted are handled. Invalid entries (empty strings, non-strings) are rejected with 400.
        files:
          - gateway/gateway.py
      - id: TASK-1-3
        description: Add allowed_files parameter to GatewayClient.register_session()
        acceptance: register_session() accepts allowed_files and includes it in POST payload when non-None.
        files:
          - orchestrator/gateway_client.py
  - id: 2
    name: Orchestrator Integration
    goal: Compute allowed_files from contract and pass to gateway at container spawn
    tasks:
      - id: TASK-2-1
        description: Add _compute_allowed_files() helper with recursive dir expansion (dir/* via fnmatch) and integrate with spawn_agent_container()
        acceptance: For implement-phase coder agents with a plan_phase_id, computes union of files_affected with dir/* expansion (recursive via fnmatch) and passes to register_session(). Non-coder agents and non-implement phases get None. Empty files_affected returns None.
        files:
          - orchestrator/container_spawner.py
      - id: TASK-2-2
        description: Thread plan_phase_id from _spawn_and_wait() to spawn_agent_container()
        acceptance: spawn_agent_container() accepts plan_phase_id parameter. _spawn_and_wait() passes it through. plan_phase_id is available for _compute_allowed_files() lookup.
        files:
          - orchestrator/container_spawner.py
          - orchestrator/routes/pipelines.py
  - id: 3
    name: Gateway Enforcement
    goal: Enforce per-task file restrictions at push time and in post-agent auto-commit
    tasks:
      - id: TASK-3-1
        description: Add per-task file restriction validation layer in gateway push handler with warn-then-block escalation
        acceptance: Push with out-of-scope file succeeds with warning on first attempt. Same file blocked on second attempt. EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true blocks immediately. Checkpoint pushes bypass. Sessions without allowed_files unaffected. Error includes blocked files, allowed patterns, and recovery hint.
        files:
          - gateway/gateway.py
          - gateway/phase_filter.py
      - id: TASK-3-2
        description: Add per-task filtering to auto_commit_worktree() alongside phase filtering
        acceptance: Uncommitted files outside allowed_files are restored (not committed) with clear structured logging. Phase restrictions still apply first. None allowed_files means no per-task filtering.
        files:
          - gateway/post_agent_commit.py
  - id: 4
    name: Tests
    goal: Comprehensive test coverage for all new per-task file restriction behavior
    tasks:
      - id: TASK-4-0
        description: Add Session model persistence tests for allowed_files round-trip, backward compat, and _warned_files transient init
        acceptance: Tests verify allowed_files round-trips through to_dict_for_persistence()/from_persistence(). Sessions without allowed_files key load with None. _warned_files initializes to empty dict and is not present in persisted output.
        files:
          - gateway/tests/test_session_manager.py
      - id: TASK-4-1
        description: Add gateway tests for per-session file restriction enforcement (push validation) including recursive fnmatch verification
        acceptance: Tests cover allowed files pass, disallowed warns then blocks, strict mode, checkpoint bypass, None/empty fallback, glob patterns, directory-sibling expansion, and explicit recursive matching (dir/* covers dir/sub/deep/file.py). All pass.
        files:
          - gateway/tests/test_session_file_restrictions.py
      - id: TASK-4-2
        description: Add tests for post-agent commit filtering with allowed_files
        acceptance: Tests cover files outside allowed_files are restored, files inside are committed, None means no filtering, logging verifies filtered files reported.
        files:
          - gateway/tests/test_post_agent_commit.py
      - id: TASK-4-3
        description: Add orchestrator tests for _compute_allowed_files() and spawner integration
        acceptance: Tests cover union of files from multiple tasks, dir-sibling expansion (recursive via fnmatch), empty returns None, non-coder returns None, non-implement returns None, glob preservation, register_session called with computed allowed_files.
        files:
          - orchestrator/tests/test_container_spawner.py
  - id: 5
    name: Documentation
    goal: Document that files in the plan template is an enforced boundary with recursive matching semantics
    tasks:
      - id: TASK-5-1
        description: Update plan template to document files as enforcement boundary with guidance on generous listing and recursive directory expansion
        acceptance: Template states files is enforced at push time. Includes guidance on generous listing, globs, config files. Documents that directory expansion is recursive (dir/* matches all descendants via fnmatch). Empty files means no restriction.
        files:
          - docs/templates/plan.md
```

*Authored-by: egg*
