# Implementation Plan: Enforce per-task file restrictions in implement phase

> Issue: #805 | Phase: plan | Pipeline: issue-805

## Approach

**Option A (Session-scoped `allowed_files`)** from the architecture analysis. The
orchestrator computes a per-session file allowlist from the plan's `files_affected`
union across all tasks assigned to the agent's plan phase, then passes it to the
gateway at session registration. The gateway enforces warn-then-block semantics
during push validation, and the post-agent auto-commit respects the same allowlist.

This follows existing patterns: session metadata for scoping, `PhaseFileRestriction`
for matching, and the `EGG_AGENT_RESTRICTIONS_ENFORCE` env var precedent for
warn/enforce toggling.

## Design Decisions

These resolve the open questions from the architecture analysis:

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Warn threshold = 1** (warn on 1st push with file, block on 2nd+). Configurable via `EGG_TASK_FILE_WARN_THRESHOLD` (default `1`). | Matches issue spec. One warning gives the agent a chance to self-correct. |
| 2 | **Violation tracking is per-file, per-session.** A `warned_files: dict[str, int]` on Session counts violations per file path. Not persisted — resets on gateway restart (fail-open). | Per-session is the natural scope. In-memory is sufficient since sessions are ephemeral. |
| 3 | **Strict mode via `EGG_TASK_FILE_RESTRICTIONS_ENFORCE`** env var (`true` = block immediately, no warnings). Mirrors `EGG_AGENT_RESTRICTIONS_ENFORCE`. | Consistent pattern. Teams can opt in to strict enforcement. |
| 4 | **Directory-sibling expansion: one level deep.** Listing `src/auth/login.py` implicitly allows `src/auth/*` (direct children only), not `src/auth/utils/*`. | Balances flexibility with control. Planner should list subdirs explicitly if needed. |
| 5 | **No global config-file allowlist.** The planner is prompted to include config files (`pyproject.toml`, `Makefile`, etc.) in task file lists. The escape hatch (`request-file`) covers unexpected needs. | Keeps the system simple and predictable. Avoids implicit magic. |
| 6 | **Escape hatch auto-approves by default.** `egg-contract request-file` adds the file to the session's allowlist permanently (for that session). Logs a structured audit event. Optional HITL mode queues a decision instead. | Provides observability without hard-blocking. Matches "guide, don't cage" philosophy. |
| 7 | **Gateway API endpoint for file requests** (`POST /api/v1/sessions/request-file`). Cleaner than contract mutation — the gateway owns session state. | Consistent with session-scoped enforcement. |
| 8 | **Intersection semantics.** Per-session `allowed_files` is intersected with phase-level rules (blocked patterns always win). If the phase blocks `.egg-state/contracts/*`, no task-level allowlist can override it. | Defense in depth. Phase blocks are security boundaries; task allowlists are organizational. |
| 9 | **Post-agent-commit reads persisted session.** `auto_commit_worktree` already receives `session_token` — it loads the persisted session from disk to get `allowed_files`. | Session persistence is already in place for crash recovery. |
| 10 | **Restored files are logged clearly.** Post-agent-commit logs each restored file with a message explaining it was outside the task's allowlist. | Silent drops cause confusion. Clear logging aids debugging. |
| 11 | **`files_affected` remains optional.** Empty list = no per-file restriction (only phase-level). | Backward compatible. Exploratory tasks or legacy plans are unaffected. |
| 12 | **Glob patterns supported in `files_affected`.** Planner encouraged to use `tests/**`, `src/components/*.tsx`, etc. Matching uses `fnmatch` (consistent with `_matches_pattern`). | Globs are more resilient than exhaustive file lists. |

## Phase Breakdown

### Phase 1: Session Model & API Plumbing

**Goal:** Extend the Session data model to carry per-session file allowlists and
wire the data path from orchestrator to gateway.

**Tasks:**

- **[TASK-1-1] Add `allowed_files` and violation tracking to Session model.**
  Add `allowed_files: list[str] | None = None` and `_warned_files: dict[str, int]`
  (non-persisted, in-memory) to the `Session` dataclass in `session_manager.py`.
  Update `to_dict_for_persistence()` to serialize `allowed_files` (not `_warned_files`).
  Update `from_persistence()` to deserialize it.
  — *Acceptance:* Session round-trips `allowed_files` through persistence. `_warned_files`
  initializes empty on deserialization. Existing sessions without the field load cleanly
  with `allowed_files=None`.

- **[TASK-1-2] Update `SessionManager.register_session` to accept `allowed_files`.**
  Add `allowed_files: list[str] | None = None` parameter to `register_session()` in
  `session_manager.py`. Pass it through to Session construction.
  — *Acceptance:* `register_session(allowed_files=["src/auth/*"])` creates a Session
  with the field set. `register_session()` without the param creates `allowed_files=None`.

- **[TASK-1-3] Update gateway `/api/v1/sessions/create` endpoint to accept `allowed_files`.**
  In `gateway.py`, read `allowed_files` from the session creation request body and pass
  it to `register_session()`.
  — *Acceptance:* POST to `/api/v1/sessions/create` with `allowed_files: ["src/*"]`
  creates a session with the field set. Omitting it works as before.

- **[TASK-1-4] Update `GatewayClient.register_session` to pass `allowed_files`.**
  Add `allowed_files: list[str] | None = None` parameter to `register_session()` in
  `orchestrator/gateway_client.py`. Include it in the request body when not None.
  — *Acceptance:* `GatewayClient.register_session(allowed_files=["src/*"])` sends
  the field in the HTTP request.

- **[TASK-1-5] Collect `files_affected` in container spawner and pass to gateway.**
  In `orchestrator/container_spawner.py`, when spawning an implement-phase agent:
  (a) Load the contract and find the agent's assigned plan phase.
  (b) Compute the union of `files_affected` across all tasks in that phase.
  (c) Auto-expand entries: for each file path (not already a glob), add the parent
  directory as `dir/*` to the list.
  (d) Pass the combined list as `allowed_files` to `gateway.register_session()`.
  When `files_affected` is empty across all tasks, pass `None` (no restriction).
  — *Acceptance:* An implement agent spawned for a phase with tasks listing
  `["src/auth/login.py", "tests/test_auth.py"]` gets a session with
  `allowed_files=["src/auth/login.py", "src/auth/*", "tests/test_auth.py", "tests/*"]`.
  A phase with no files listed gets `allowed_files=None`.

- **[TASK-1-6] Write tests for Session model and API changes.**
  In `gateway/tests/test_session_manager.py`: test `allowed_files` persistence
  round-trip, `None` handling, and `_warned_files` initialization.
  In `gateway/tests/`: test session creation endpoint with and without `allowed_files`.
  In `orchestrator/tests/`: test spawner `files_affected` collection and directory
  expansion logic.
  — *Acceptance:* All new tests pass. Existing session tests remain green.

**Files:**
- `gateway/session_manager.py`
- `gateway/gateway.py`
- `orchestrator/gateway_client.py`
- `orchestrator/container_spawner.py`
- `gateway/tests/test_session_manager.py`
- `gateway/tests/test_agent_restrictions_enforce.py`
- `orchestrator/tests/`

**Exit criteria:** Session carries `allowed_files` end-to-end from spawner through
gateway registration to persisted session. All tests pass.

---

### Phase 2: Gateway Push Enforcement

**Goal:** Enforce per-session file restrictions during push validation with
warn-then-block semantics, and filter post-agent auto-commits.

**Tasks:**

- **[TASK-2-1] Add helper to build `PhaseFileRestriction` from session's `allowed_files`.**
  In `gateway/phase_filter.py`, add a function
  `build_session_file_restriction(allowed_files: list[str], phase: str) -> PhaseFileRestriction`
  that:
  (a) Starts with the existing phase's `blocked_patterns` (implement phase blocks
  `.egg-state/` dirs).
  (b) Sets `allowed_patterns` from the session's `allowed_files` list.
  (c) Blocked patterns always win (intersection semantics).
  Reuse existing `_matches_pattern()` and `is_file_allowed()` logic.
  — *Acceptance:* `build_session_file_restriction(["src/auth/*"], "implement")`
  returns a restriction that allows `src/auth/login.py`, blocks `src/other/foo.py`,
  and still blocks `.egg-state/contracts/foo.json`.

- **[TASK-2-2] Add per-session file restriction check to push validation.**
  In `gateway/gateway.py`, after the existing three validation layers (role, agent,
  phase), add a fourth layer that:
  (a) Checks if `g.session.allowed_files` is set and non-empty.
  (b) Builds a restriction via `build_session_file_restriction()`.
  (c) For each changed file not in the allowlist:
    - Increment `g.session._warned_files[file]`.
    - If count <= threshold (default 1, from `EGG_TASK_FILE_WARN_THRESHOLD`): log
      structured warning, allow push.
    - If count > threshold: block with actionable error.
  (d) If `EGG_TASK_FILE_RESTRICTIONS_ENFORCE` is `true`: block immediately (skip warnings).
  (e) Skip for checkpoint pushes and non-implement phases.
  — *Acceptance:* First push with an out-of-scope file logs warning and succeeds.
  Second push with the same file returns 403. Strict mode blocks on first attempt.
  Files in the allowlist always succeed. `allowed_files=None` skips the check entirely.

- **[TASK-2-3] Update post-agent auto-commit to respect session `allowed_files`.**
  In `gateway/post_agent_commit.py`, update `auto_commit_worktree()` to:
  (a) Load the persisted session using the `session_token` parameter.
  (b) If `session.allowed_files` is set, build a restriction and filter uncommitted
  files the same way phase restrictions are filtered.
  (c) Restore files outside the allowlist via `git checkout --`.
  (d) Log each restored file with a message: "Restored <file>: outside task allowlist
  (allowed: <patterns>)".
  — *Acceptance:* Auto-commit only commits files within the session's allowlist.
  Files outside are restored and logged. When `allowed_files` is None, behavior
  is unchanged (only phase-level filtering applies).

- **[TASK-2-4] Write tests for push enforcement and post-agent-commit filtering.**
  New test file `gateway/tests/test_task_file_restrictions.py`:
  - Warn on first out-of-scope file, block on second
  - Strict mode blocks immediately
  - Glob pattern matching (`src/components/*.tsx`, `tests/**`)
  - Directory-sibling expansion via allowed_files
  - `allowed_files=None` skips check
  - Phase blocked patterns still win
  - Checkpoint pushes skip check
  Update `gateway/tests/test_post_agent_commit.py`:
  - Auto-commit filters by session allowed_files
  - Restored files are logged
  - `allowed_files=None` uses only phase filtering
  — *Acceptance:* All new and existing tests pass.

**Files:**
- `gateway/phase_filter.py`
- `gateway/gateway.py`
- `gateway/post_agent_commit.py`
- `gateway/tests/test_task_file_restrictions.py`
- `gateway/tests/test_post_agent_commit.py`

**Exit criteria:** Push validation enforces per-session file restrictions with
warn-then-block. Post-agent auto-commit respects the same restrictions. All tests pass.

---

### Phase 3: Escape Hatch & Observability

**Goal:** Provide an escape hatch for agents that legitimately need files outside
their allowlist, with audit logging for observability.

**Tasks:**

- **[TASK-3-1] Add `request-file` command to `egg-contract` CLI.**
  In `sandbox/egg_lib/contract_cli.py`, add a `request-file` subcommand:
  `egg-contract request-file --path <file> --reason <why>`
  This calls the gateway API to add the file to the session's allowlist.
  — *Acceptance:* `egg-contract request-file --path src/utils/new.py --reason "New helper needed"`
  succeeds and subsequent pushes including that file are allowed.

- **[TASK-3-2] Add gateway endpoint `POST /api/v1/sessions/request-file`.**
  In `gateway/gateway.py`, add an endpoint that:
  (a) Validates the session token.
  (b) Adds the requested file path (and its parent directory pattern) to the session's
  `allowed_files` list.
  (c) Logs a structured audit event: `file_request` with path, reason, session, pipeline.
  (d) Re-persists the session to disk.
  (e) Returns 200 with the updated allowlist.
  In strict mode (`EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true`), instead of auto-approving,
  queue a HITL decision via the orchestrator and return 202 (pending approval).
  — *Acceptance:* In default mode, the endpoint auto-approves and adds the file.
  In strict mode, it queues a decision. Audit log contains the request.

- **[TASK-3-3] Add method to Session for dynamic allowlist expansion.**
  In `gateway/session_manager.py`, add `Session.add_allowed_file(path: str)` that
  appends the path (and its directory glob) to `allowed_files`, deduplicating.
  Also add `SessionManager.update_session_allowed_files(token_hash, files)` to
  persist the change.
  — *Acceptance:* `session.add_allowed_file("src/new.py")` adds both `"src/new.py"`
  and `"src/*"` to `allowed_files`. Changes are persisted.

- **[TASK-3-4] Write tests for escape hatch.**
  In `gateway/tests/test_task_file_restrictions.py`: test the `request-file` endpoint
  (auto-approve and strict mode paths). Test that requested files are subsequently
  allowed in push validation.
  — *Acceptance:* All tests pass.

**Files:**
- `sandbox/egg_lib/contract_cli.py`
- `gateway/gateway.py`
- `gateway/session_manager.py`
- `gateway/tests/test_task_file_restrictions.py`

**Exit criteria:** Agents can request additional files via CLI. Gateway auto-approves
by default with audit logging. Strict mode queues HITL decisions.

---

### Phase 4: Documentation & Template Updates

**Goal:** Update the plan template and planner guidance so that `files:` is understood
as an enforcement boundary, not just a hint.

**Tasks:**

- **[TASK-4-1] Update plan template to document enforcement semantics.**
  In `docs/templates/plan.md`:
  (a) Add a note above the YAML example explaining that `files:` entries are enforced
  by the gateway during the implement phase.
  (b) Add guidance: list files generously, include test files and config files the task
  will touch, use glob patterns (`tests/**`, `src/components/*.tsx`), prefer
  directory-level globs for broad tasks.
  (c) Note that unlisted files in the same directory as a listed file are automatically
  allowed (directory-sibling expansion).
  (d) Note that empty `files:` means no per-file restriction for that task.
  — *Acceptance:* Template clearly communicates that `files:` is enforced and how to
  list files effectively.

- **[TASK-4-2] Add inline comments in YAML example showing glob usage.**
  Update the YAML example in `docs/templates/plan.md` to show:
  ```yaml
  files:
    - src/auth/login.py
    - src/auth/middleware.py
    - tests/test_auth/**     # glob: all test files in directory
    - pyproject.toml          # include config files you'll touch
  ```
  — *Acceptance:* YAML example demonstrates glob patterns and config file inclusion.

**Files:**
- `docs/templates/plan.md`

**Exit criteria:** Plan template and planner guidance updated. Clear documentation
of enforcement semantics.

---

## Test Strategy

| Layer | Test File | What's Tested |
|-------|-----------|---------------|
| Session model | `gateway/tests/test_session_manager.py` | `allowed_files` persistence, `_warned_files` init |
| Push validation | `gateway/tests/test_task_file_restrictions.py` | Warn-then-block, strict mode, glob matching, dir expansion, None fallback, phase blocked override |
| Post-agent commit | `gateway/tests/test_post_agent_commit.py` | File filtering by allowlist, restore logging, None fallback |
| Escape hatch | `gateway/tests/test_task_file_restrictions.py` | `request-file` endpoint, auto-approve, strict HITL |
| Spawner | `orchestrator/tests/` | `files_affected` collection, directory expansion, None when empty |
| Plan parser | `shared/egg_contracts/tests/` | Glob patterns in `files_affected` (already supported by parser) |

**End-to-end verification:** After all phases, a Tier 3 pipeline where two agents
have disjoint file lists should show that each can only commit their own files
(manual verification in PR test plan).

## Risk Mitigation

- **Fail-open on empty allowlist:** If `files_affected` is empty or `allowed_files`
  is None, no per-file restriction is applied. This prevents lockout from legacy
  plans or exploratory tasks.
- **Warn-only default:** The warn-then-block default means agents are never
  hard-blocked on first attempt. This matches the "guide, don't cage" philosophy.
- **Escape hatch:** `egg-contract request-file` provides a safety valve for
  legitimate edge cases.
- **No breaking changes:** All new fields are optional with `None` defaults.
  Existing sessions, plans, and pipelines are unaffected.

---

```yaml
# yaml-tasks
pr:
  title: "Enforce per-task file restrictions in implement phase"
  description: |
    Makes the planner's per-task `files` list an enforced boundary at the gateway
    level. When the orchestrator spawns an implement agent, it collects
    `files_affected` from the plan's tasks and passes them to the gateway session.
    The gateway enforces warn-then-block semantics on push validation and
    post-agent auto-commit, with an escape hatch via `egg-contract request-file`.
    Resolves cross-contamination risk in Tier 3 parallel agent dispatch.
phases:
  - id: 1
    name: Session Model & API Plumbing
    goal: Extend Session to carry per-session file allowlists and wire the data path from orchestrator to gateway
    tasks:
      - id: TASK-1-1
        description: Add allowed_files and violation tracking to Session dataclass
        acceptance: Session round-trips allowed_files through persistence; _warned_files initializes empty; existing sessions load with allowed_files=None
        files:
          - gateway/session_manager.py
      - id: TASK-1-2
        description: Update SessionManager.register_session to accept allowed_files parameter
        acceptance: register_session(allowed_files=["src/auth/*"]) creates Session with field set; omitting param yields None
        files:
          - gateway/session_manager.py
      - id: TASK-1-3
        description: Update gateway /api/v1/sessions/create endpoint to accept allowed_files
        acceptance: POST with allowed_files creates session with field set; omitting works as before
        files:
          - gateway/gateway.py
      - id: TASK-1-4
        description: Update GatewayClient.register_session to pass allowed_files
        acceptance: GatewayClient.register_session(allowed_files=["src/*"]) includes field in HTTP request
        files:
          - orchestrator/gateway_client.py
      - id: TASK-1-5
        description: Collect files_affected in container spawner and pass to gateway with directory expansion
        acceptance: Implement agent for phase with files ["src/auth/login.py"] gets allowed_files=["src/auth/login.py", "src/auth/*"]; empty files yields None
        files:
          - orchestrator/container_spawner.py
      - id: TASK-1-6
        description: Write tests for Session model, API, and spawner changes
        acceptance: All new tests pass; existing session and spawner tests remain green
        files:
          - gateway/tests/test_session_manager.py
          - gateway/tests/test_agent_restrictions_enforce.py
          - orchestrator/tests/
  - id: 2
    name: Gateway Push Enforcement
    goal: Enforce per-session file restrictions during push validation with warn-then-block and filter post-agent auto-commits
    tasks:
      - id: TASK-2-1
        description: Add build_session_file_restriction helper to phase_filter.py
        acceptance: Returns PhaseFileRestriction that allows listed files, blocks unlisted, and preserves phase blocked_patterns
        files:
          - gateway/phase_filter.py
      - id: TASK-2-2
        description: Add per-session file restriction check as fourth push validation layer
        acceptance: First out-of-scope push warns and succeeds; second blocks with 403; strict mode blocks immediately; None skips check
        files:
          - gateway/gateway.py
      - id: TASK-2-3
        description: Update post-agent auto-commit to respect session allowed_files
        acceptance: Auto-commit only commits allowlisted files; out-of-scope files restored with clear log messages
        files:
          - gateway/post_agent_commit.py
      - id: TASK-2-4
        description: Write tests for push enforcement and post-agent-commit filtering
        acceptance: All new tests pass covering warn-then-block, strict mode, globs, dir expansion, None fallback, checkpoint skip
        files:
          - gateway/tests/test_task_file_restrictions.py
          - gateway/tests/test_post_agent_commit.py
  - id: 3
    name: Escape Hatch & Observability
    goal: Provide escape hatch for agents needing files outside their allowlist with audit logging
    tasks:
      - id: TASK-3-1
        description: Add request-file subcommand to egg-contract CLI
        acceptance: "egg-contract request-file --path <file> --reason <why>" calls gateway API and succeeds
        files:
          - sandbox/egg_lib/contract_cli.py
      - id: TASK-3-2
        description: Add gateway endpoint POST /api/v1/sessions/request-file
        acceptance: Auto-approves and adds file to session allowlist; strict mode queues HITL decision; audit event logged
        files:
          - gateway/gateway.py
      - id: TASK-3-3
        description: Add Session.add_allowed_file method and SessionManager persistence update
        acceptance: add_allowed_file appends path and directory glob, deduplicates, and persists
        files:
          - gateway/session_manager.py
      - id: TASK-3-4
        description: Write tests for escape hatch endpoint and CLI
        acceptance: All tests pass covering auto-approve, strict HITL path, and subsequent push allowance
        files:
          - gateway/tests/test_task_file_restrictions.py
  - id: 4
    name: Documentation & Template Updates
    goal: Update plan template so files list is understood as an enforcement boundary
    tasks:
      - id: TASK-4-1
        description: Update plan template to document enforcement semantics and file listing guidance
        acceptance: Template explains files are enforced, recommends generous listing with globs, notes directory-sibling expansion
        files:
          - docs/templates/plan.md
      - id: TASK-4-2
        description: Add inline comments in YAML example showing glob usage and config file inclusion
        acceptance: YAML example shows glob patterns and config file entries
        files:
          - docs/templates/plan.md
```
