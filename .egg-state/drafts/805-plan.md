# Plan: Enforce per-task file restrictions from planner in implement phase

> Issue: #805 | Phase: plan | Pipeline: issue-805

## Approach

This PR adds per-task file restriction enforcement to the gateway, so that
implement-phase agents in Tier 3 (parallel) dispatch are scoped to only the
files listed in their assigned tasks' `files_affected` lists. The work follows
the architect's recommended Option A: session-scoped `allowed_files` with
warn-then-block enforcement.

**Stage 1 — Data model and API plumbing:** Extend the `Session` dataclass with
`allowed_files` and `file_warnings` fields, update the gateway HTTP API and
orchestrator client to accept `allowed_files` during session registration, and
wire the container spawner to read task `files_affected` from the contract and
forward them (with directory-sibling expansion) to the gateway.

**Stage 2 — Enforcement:** Add per-session file restriction checking to the
push validation flow (warn on first out-of-scope file, block on second for the
same file) and to the post-agent auto-commit filtering. Build the
`egg-contract request-file` escape hatch so agents can request access to
out-of-scope files with observability logging.

**Stage 3 — Documentation and tests:** Update the plan template to document
that `files:` is an enforcement boundary. Write comprehensive tests covering
warn-then-block escalation, directory-sibling expansion, glob patterns, graceful
fallback, post-agent commit filtering, and the escape hatch API.

### Key design decisions

1. **`allowed_files` on the Session dataclass** — follows the existing pattern
   for per-session metadata. Persists to JSON alongside the session. No new
   storage mechanism needed.
2. **Directory-sibling expansion at the container spawner** — the spawner has
   full plan context and expands `src/auth/login.py` to also allow `src/auth/`
   as a prefix. The gateway receives an already-expanded list and just does
   pattern matching.
3. **Immediate parent directory only** — listing `src/auth/login.py` allows
   `src/auth/*` but NOT `src/*`. Prevents over-permissive expansion.
4. **Per-session check runs AFTER phase and role checks** — the three
   restriction layers (phase, role, per-session) are independent and cumulative.
   Per-session is the most granular and runs last.
5. **Violation tracking persisted in session JSON** — `file_warnings: dict[str, int]`
   survives across pushes within a session. Resets on gateway restart are
   acceptable but persistence is preferred for robustness.
6. **No hardcoded config file allowlist** — planner responsibility, guided by
   updated template. Directory-sibling expansion handles the common case.
7. **Escape hatch auto-approves in v1** — `egg-contract request-file` logs the
   request and auto-adds the file to `allowed_files`. Strict mode (HITL
   approval) deferred to a future iteration.
8. **Graceful fallback** — empty `files_affected` = `allowed_files` is `None` =
   no per-file restriction. Missing or malformed entries are skipped, not fatal.

### Backward compatibility

Sessions without `allowed_files` (non-pipeline, non-implement, or pre-existing)
behave identically to today. All new fields use optional defaults (`None` or
empty dict). The gateway HTTP API change is additive (new optional parameter).
The `GatewayClient` passes `allowed_files` only when non-None.

## Phase breakdown

### Phase 1: Session model and gateway API plumbing

**Goal:** Extend the Session dataclass and HTTP API to accept and persist
per-session file allowlists. This is the data foundation for all enforcement.

The `Session` dataclass (session_manager.py:209) gains two fields:
`allowed_files: list[str] | None` (default None) and
`file_warnings: dict[str, int]` (default empty dict). Serialization/
deserialization are updated for backward compatibility. The `register_session()`
method and `/api/v1/sessions/create` endpoint accept the new `allowed_files`
parameter. The `GatewayClient` in the orchestrator is updated to pass it.

**Files:**
- `gateway/session_manager.py` — Session dataclass, register_session(), persistence
- `gateway/gateway.py` — /api/v1/sessions/create endpoint
- `orchestrator/gateway_client.py` — register_session() client method

### Phase 2: Container spawner — extract and forward task files

**Goal:** The container spawner reads `files_affected` from assigned tasks,
applies directory-sibling expansion, and passes the combined list to the
gateway during session registration.

When spawning an implement-phase agent, the spawner collects `files_affected`
from all tasks assigned to this agent (union across tasks). For each file path,
the immediate parent directory is added as an allowed prefix (e.g.,
`src/auth/login.py` -> also allow `src/auth/`). Glob patterns are passed
through as-is. When the combined list is empty, `None` is passed (graceful
fallback, no restriction).

**Files:**
- `orchestrator/container_spawner.py` — spawn_agent_container()

### Phase 3: Push validation — warn-then-block enforcement

**Goal:** The gateway checks per-session file restrictions during push
validation. First out-of-scope access per file warns; second blocks.

A new `check_session_file_restrictions()` function in `phase_filter.py` takes
the session's `allowed_files` and the list of changed files, using the existing
`_matches_pattern()` infrastructure for matching. In the push handler
(gateway.py, after phase-level checks at line 822), if the session has
`allowed_files`, each changed file is checked. Out-of-scope files increment
`session.file_warnings[path]`. Count 1 = structured warning in logs, push
allowed. Count >= 2 = push blocked with 403 and an actionable error message
identifying which task's `files_affected` the agent should check.

**Files:**
- `gateway/phase_filter.py` — check_session_file_restrictions() function
- `gateway/gateway.py` — Push handler integration after line 822

### Phase 4: Post-agent commit — filter by session allowed_files

**Goal:** The auto-commit filtering respects per-session file restrictions in
addition to phase restrictions, with clear logging.

`auto_commit_worktree()` in `post_agent_commit.py` gains an
`allowed_files` parameter. After phase-level filtering separates allowed and
blocked files, a second pass applies session-level filtering if `allowed_files`
is provided. Out-of-scope files are restored (not committed) with clear log
messages explaining why. The calling code looks up the session's
`allowed_files` at container exit time and passes them.

**Files:**
- `gateway/post_agent_commit.py` — auto_commit_worktree() filtering
- `gateway/gateway.py` — Pass session allowed_files to auto-commit trigger

### Phase 5: Escape hatch — egg-contract request-file

**Goal:** Agents can request access to out-of-scope files via CLI, with full
observability logging and auto-approval.

A new `POST /api/v1/sessions/{token}/request-file` endpoint accepts `path`
and `reason`. It validates the session, adds the path to
`session.allowed_files`, resets `file_warnings` for that path, and logs the
request with full context (session_id, agent_role, phase, path, reason). The
`egg-contract` CLI gains a `request-file` subcommand (following existing
patterns: argparse subparser, `make_gateway_request()`, Bearer token auth)
that accepts `--path <file>` and `--reason <why>`.

**Files:**
- `gateway/gateway.py` — New /api/v1/sessions/{token}/request-file endpoint
- `bin/egg-contract` — New request-file subcommand

### Phase 6: Plan template — document enforcement semantics

**Goal:** Update the plan template so planners know that `files:` is an
enforcement boundary and list files generously.

The template is updated to explain: `files:` entries are enforced at push time,
not just hints. Guidance includes: list files generously, use glob patterns
(e.g., `src/components/*.tsx`, `tests/**`), include test files and config files
the task will touch. Document directory-sibling expansion semantics. Note that
empty `files:` means no per-file restriction.

**Files:**
- `docs/templates/plan.md` — Enforcement documentation and examples

### Phase 7: Tests

**Goal:** Comprehensive test coverage for all changes. Existing tests continue
passing.

**Files:**
- `gateway/tests/` — Session allowed_files persistence, check_session_file_restrictions(),
  warn-then-block escalation, post-agent commit filtering, request-file endpoint,
  graceful fallback, directory-sibling expansion, glob pattern support
- `orchestrator/tests/` — Container spawner file extraction and forwarding,
  directory-sibling expansion logic, GatewayClient allowed_files parameter

## Test strategy

1. **Unit tests — gateway:**
   - Session `allowed_files` serialization/deserialization and backward compat
   - `check_session_file_restrictions()` with exact files, globs, directory prefixes
   - Warn-then-block escalation: first violation warns, second blocks
   - `request-file` endpoint: adds file, resets warnings, validation
   - Graceful fallback: `allowed_files=None` skips all per-session checks

2. **Unit tests — orchestrator:**
   - Container spawner reads `files_affected` from contract tasks
   - Directory-sibling expansion: `src/auth/login.py` -> adds `src/auth/` prefix
   - Union across multiple tasks' `files_affected`
   - Empty `files_affected` -> `None` (no restriction)
   - Glob patterns passed through unchanged

3. **Integration tests:**
   - Push with in-scope files succeeds (no warnings)
   - Push with out-of-scope file succeeds with warning (first violation)
   - Second push with same out-of-scope file blocked (second violation)
   - `request-file` adds file, subsequent push succeeds
   - Post-agent commit: out-of-scope files restored, in-scope files committed
   - Tier 3 dispatch: two agents with disjoint file lists, each scoped correctly

4. **Backward compatibility:**
   - Existing sessions without `allowed_files` field load correctly (None default)
   - Sessions with `allowed_files=None` skip per-session checks entirely
   - Existing phase-level and role-level restriction tests unchanged

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Planner produces incomplete `files_affected`, agents spin on blocks | Medium | High | Directory-sibling expansion, warn-then-block (not immediate block), escape hatch auto-approve, empty files = no restriction |
| Directory-sibling expansion too permissive for shallow paths (e.g., `src/foo.py` -> `src/`) | Medium | Medium | Parent-only expansion (TD-6). Planner guidance to use specific subdirectories. Root files handled specially |
| Push validation latency increase from per-session matching | Low | Low | Reuses efficient fnmatch. Typical: 5-50 patterns x 1-20 files = negligible |
| Post-agent auto-commit loses session context if session expired | Medium | Low | Capture `allowed_files` at container exit before session cleanup. Fallback to phase-only filtering |
| Session JSON size grows from `allowed_files` | Low | Low | Cap at 500 entries. Validated at creation and request-file |

```yaml
# yaml-tasks
pr:
  title: "Enforce per-task file restrictions in implement phase"
  description: |
    Adds per-task file restriction enforcement to the gateway for implement-phase
    agents. When the orchestrator spawns an implement agent, the gateway restricts
    that agent's commits to only the files listed in its assigned tasks'
    files_affected lists, with directory-sibling expansion and glob support. Uses
    warn-then-block enforcement (first out-of-scope access warns, second blocks)
    and an egg-contract request-file escape hatch for observability. Prevents
    accidental cross-contamination in Tier 3 parallel agent dispatch.
phases:
  - id: 1
    name: Session model and gateway API plumbing
    goal: Extend Session dataclass with allowed_files and file_warnings fields, update HTTP API and orchestrator client to accept allowed_files
    tasks:
      - id: TASK-1-1
        description: Add allowed_files (list[str] | None, default None) and file_warnings (dict[str, int], default empty dict) fields to Session dataclass in session_manager.py
        acceptance: Session accepts both fields; to_dict_for_persistence() serializes them; from_persistence() deserializes with backward-compat defaults (None and {})
        files:
          - gateway/session_manager.py
      - id: TASK-1-2
        description: Add record_file_warning(path) method to Session that increments and returns the violation count, and is_file_in_scope(path) method using _matches_pattern() logic
        acceptance: record_file_warning increments count correctly; is_file_in_scope returns True for files matching allowed_files patterns (including directory prefixes and globs)
        files:
          - gateway/session_manager.py
      - id: TASK-1-3
        description: Update register_session() in SessionManager to accept optional allowed_files parameter and pass it to Session constructor
        acceptance: register_session() with allowed_files creates session with the field set; without it, defaults to None
        files:
          - gateway/session_manager.py
      - id: TASK-1-4
        description: Update /api/v1/sessions/create endpoint in gateway.py to accept and validate allowed_files in request body (list of strings, max 500 entries, max 500 chars each)
        acceptance: Endpoint accepts allowed_files, validates constraints, passes to register_session(); omitting allowed_files from request body defaults to None
        files:
          - gateway/gateway.py
      - id: TASK-1-5
        description: Add allowed_files parameter to GatewayClient.register_session() in gateway_client.py; include in POST body when not None
        acceptance: GatewayClient sends allowed_files when provided; omits it when None; gateway receives and stores it correctly
        files:
          - orchestrator/gateway_client.py
  - id: 2
    name: Container spawner — extract and forward task files
    goal: Container spawner reads files_affected from assigned tasks, applies directory-sibling expansion, and passes to register_session()
    tasks:
      - id: TASK-2-1
        description: Add utility function to compute union of files_affected across a list of tasks with directory-sibling expansion (parent directory prefix for each file)
        acceptance: Union includes all unique file patterns; each file's parent directory is added as a prefix pattern; glob patterns passed through; empty input returns None
        files:
          - orchestrator/container_spawner.py
      - id: TASK-2-2
        description: In spawn_agent_container(), read files_affected from tasks assigned to this agent, apply expansion, and pass as allowed_files to gateway.register_session()
        acceptance: Implement-phase agents receive scoped allowed_files; non-implement agents and empty files lists pass None; Tier 3 dispatch with multiple agents each gets their own task union
        files:
          - orchestrator/container_spawner.py
  - id: 3
    name: Push validation — warn-then-block enforcement
    goal: Gateway checks per-session file restrictions during push with warn-on-first, block-on-second enforcement
    tasks:
      - id: TASK-3-1
        description: Add check_session_file_restrictions() function to phase_filter.py that takes allowed_files list and changed_files list, returns result with out-of-scope files identified, reusing _matches_pattern()
        acceptance: Function correctly identifies out-of-scope files using fnmatch glob matching and directory prefix matching; returns empty result when allowed_files is None
        files:
          - gateway/phase_filter.py
      - id: TASK-3-2
        description: Integrate per-session file check into push handler in gateway.py after phase-level checks; implement warn-then-block using session.file_warnings
        acceptance: First push with out-of-scope file logs warning and succeeds; second push with same file returns 403 with actionable error; in-scope files always pass; sessions without allowed_files skip this check entirely
        files:
          - gateway/gateway.py
  - id: 4
    name: Post-agent commit — filter by session allowed_files
    goal: Auto-commit filtering respects per-session restrictions with clear logging
    tasks:
      - id: TASK-4-1
        description: Add allowed_files parameter to auto_commit_worktree() and apply session-level filtering after phase-level filtering; log each out-of-scope file being restored
        acceptance: Out-of-scope files are restored (not committed) with log messages; in-scope files are committed; when allowed_files is None, only phase filtering applies (current behavior)
        files:
          - gateway/post_agent_commit.py
      - id: TASK-4-2
        description: Update the auto-commit trigger in gateway.py to look up session's allowed_files at container exit time and pass to auto_commit_worktree()
        acceptance: Session allowed_files are captured before session cleanup and forwarded to auto-commit; if session already cleaned up, falls back to phase-only filtering
        files:
          - gateway/gateway.py
  - id: 5
    name: Escape hatch — egg-contract request-file
    goal: Agents can request access to out-of-scope files via CLI with auto-approval and observability logging
    tasks:
      - id: TASK-5-1
        description: Add POST /api/v1/sessions/{token}/request-file endpoint to gateway.py that accepts path and reason, adds path to session.allowed_files, resets file_warnings for that path, and logs with full context
        acceptance: Endpoint adds file to allowed_files; resets violation count for that file; logs request with session_id, agent_role, phase, path, reason; returns updated allowed_files
        files:
          - gateway/gateway.py
      - id: TASK-5-2
        description: Add request-file subcommand to egg-contract CLI following existing patterns (argparse subparser, make_gateway_request, Bearer token auth) with --path and --reason arguments
        acceptance: CLI calls gateway request-file endpoint; prints confirmation; handles errors gracefully
        files:
          - bin/egg-contract
  - id: 6
    name: Plan template — document enforcement semantics
    goal: Update plan template so planners understand files is an enforced boundary
    tasks:
      - id: TASK-6-1
        description: Update docs/templates/plan.md to explain that files entries are enforced at push time, with guidance on generous listing, glob patterns, directory-sibling expansion, and config file inclusion
        acceptance: Template includes enforcement documentation, glob pattern examples, directory-sibling expansion explanation, and note about empty files fallback
        files:
          - docs/templates/plan.md
  - id: 7
    name: Tests
    goal: Comprehensive test coverage for all changes; existing tests pass
    tasks:
      - id: TASK-7-1
        description: Write tests for Session allowed_files and file_warnings persistence (serialization, deserialization, backward compat with sessions missing the fields)
        acceptance: Tests cover to_dict_for_persistence, from_persistence, default values, and loading old-format sessions
        files:
          - gateway/tests/test_session_manager.py
      - id: TASK-7-2
        description: Write tests for check_session_file_restrictions() covering exact files, globs, directory prefixes, None allowed_files, and empty lists
        acceptance: All matching scenarios tested including edge cases (repo-root files, nested paths, wildcard patterns)
        files:
          - gateway/tests/test_phase_filter.py
      - id: TASK-7-3
        description: Write tests for warn-then-block push validation (first violation warns, second blocks, different files tracked independently, request-file resets count)
        acceptance: Tests verify warning on first violation, block on second, independent per-file tracking, and reset after request-file
        files:
          - gateway/tests/test_gateway.py
      - id: TASK-7-4
        description: Write tests for post-agent commit filtering with allowed_files (out-of-scope restored, in-scope committed, None fallback to phase-only)
        acceptance: Tests verify file filtering, log messages for restored files, and backward-compat behavior
        files:
          - gateway/tests/test_post_agent_commit.py
      - id: TASK-7-5
        description: Write tests for request-file gateway endpoint (adds file, resets warnings, validation, logging)
        acceptance: Tests cover successful request, invalid session, missing params, and warning reset
        files:
          - gateway/tests/test_gateway.py
      - id: TASK-7-6
        description: Write tests for container spawner file extraction (union across tasks, directory-sibling expansion, empty files -> None, glob pass-through)
        acceptance: Tests cover multi-task union, expansion logic, empty/None handling, and glob patterns
        files:
          - orchestrator/tests/test_container_spawner.py
      - id: TASK-7-7
        description: Write tests for GatewayClient.register_session() with allowed_files parameter (sent when present, omitted when None)
        acceptance: Tests verify request body includes allowed_files when provided and omits when None
        files:
          - orchestrator/tests/test_gateway_client.py
      - id: TASK-7-8
        description: Verify existing tests pass (session_manager, phase_filter, gateway push, post_agent_commit, container_spawner)
        acceptance: All pre-existing test suites pass without modification
        files:
          - gateway/tests/
          - orchestrator/tests/
```
