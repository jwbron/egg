# Analysis: Enforce per-task file restrictions from planner in implement phase

> Issue: #805 | Phase: refine

## Problem Statement

The SDLC pipeline (#800) enforces **phase-scoped** file restrictions during the implement phase — blocking `.egg-state/` directories but allowing modification of any code file. When multiple agents work in parallel (Tier 3 / high-complexity dispatch), each agent working on a separate plan phase can accidentally modify files that belong to another phase's tasks. The planner already declares `files_affected` per task, but this is currently informational only — not enforced at the gateway level.

The goal is to make the planner's per-task `files:` list an **enforced boundary** at the gateway, so each implement-phase agent can only commit files listed in its assigned tasks (with generous matching and graceful fallback).

## Current Behavior

The existing plumbing is largely in place, but the final enforcement link is missing:

1. **Plan template** (`docs/templates/plan.md`) requires `files:` per task in the YAML block.
2. **Plan parser** (`shared/egg_contracts/plan_parser.py:475-481`) extracts these into `files_affected` on the `ParsedTask` model, which flows into the contract `Task` model (`shared/egg_contracts/models.py:130`).
3. **Phase file restrictions** (`gateway/phase_filter.py:160-250`) support `allowed_patterns` and `blocked_patterns` with glob matching via `PhaseFileRestriction`. The implement phase currently only has `blocked_patterns` (`.egg-state/` subdirs) and an empty `allowed_patterns` list (meaning all non-blocked code files are allowed).
4. **Gateway push validation** (`gateway/gateway.py:764-824`) calls `check_phase_file_restrictions()` on every push, checking against the phase-level patterns.
5. **Post-agent auto-commit** (`gateway/post_agent_commit.py:175-206`) filters files by the same phase-level restrictions before auto-committing.
6. **Session model** (`gateway/session_manager.py:225-245`) stores `phase`, `agent_role`, `pipeline_id`, `complexity_tier`, etc. — but has no `allowed_files` field.
7. **Container spawner** (`orchestrator/container_spawner.py:343-376`) passes `phase` and `agent_role` to `gateway.register_session()` — but does not pass task-level file restrictions.
8. **Tier 3 prompt building** (`orchestrator/routes/pipelines.py:2831-2917`) scopes coder prompts to a single plan phase's tasks, including `files_affected` in the prompt — but this is advisory, not enforced.

**Gap**: `files_affected` data exists in the contract but never reaches the gateway's push/commit validation. There is no per-session file allowlist, no warn-then-block escalation, no directory-sibling expansion, and no escape hatch mechanism.

## Constraints

- **Backwards compatibility**: Non-pipeline sessions, non-implement phases, and pipelines without `files_affected` must work unchanged. The new field must be optional with graceful fallback.
- **No hard blocking on first attempt**: The issue explicitly requires warn-then-block escalation — first violation per file logs a warning; second violation for the same file blocks. This prevents agents from spinning on legitimate edge cases.
- **Performance**: Push validation runs synchronously in the gateway on every `git push`. Adding per-session file matching must not introduce measurable latency (glob matching on a reasonable file list is fine, but avoid loading the full contract JSON on every push).
- **Glob pattern support**: `files_affected` entries may contain `*` and `**` patterns (e.g., `tests/**`, `src/components/*.tsx`). The existing `_matches_pattern()` in `phase_filter.py:230-250` supports fnmatch-style wildcards already.
- **Session persistence**: Any new `Session` fields must be serialized in `to_dict_for_persistence()` and deserialized in `from_persistence()` for gateway restarts.
- **Gateway API contract**: The `register_session()` API endpoint (`/api/v1/sessions/create`) and `gateway_client.py` must be updated in sync.
- **Config files**: Common config files (`pyproject.toml`, `package.json`, `Makefile`) are frequently touched as side effects. The issue notes these should either be implicitly allowed or the planner must be prompted to include them.
- **The `egg-contract request-file` CLI subcommand** does not exist yet and must be implemented as the escape hatch.

## Options Considered

### Option A: Session-scoped allowed_files (issue's proposed design)

**Approach**: Add `allowed_files: list[str] | None` to `Session`. The container spawner reads `files_affected` from the contract, auto-expands with directory-sibling prefixes, and passes the union to `register_session()`. The gateway combines this with existing phase patterns at push-time. Warn-then-block escalation is tracked per-session via a `file_violation_counts` dict on the Session.

**Data flow**: Contract tasks → container spawner (union + expand) → `register_session(allowed_files=...)` → Session field → gateway push validation → `PhaseFileRestriction` augmented with session allowlist → warn/block.

**Pros**:
- Clean separation: gateway doesn't need to read contracts directly
- Session is the single source of truth for per-agent restrictions at validation time
- `allowed_files` is computed once at spawn, not on every push
- Existing `PhaseFileRestriction` machinery handles the glob matching
- Violation count tracking on the session is natural (session is per-container)

**Cons**:
- Session dataclass grows with two more fields (`allowed_files`, `file_violation_counts`)
- The spawner must parse the contract to extract and union files — coupling the orchestrator to contract schema at spawn time
- Directory-sibling expansion logic must be implemented (e.g., `src/auth/login.py` → also allow `src/auth/*`)
- `allowed_files` list could be large if the plan is verbose; serialization overhead

### Option B: Gateway reads contract directly at push time

**Approach**: Instead of passing `allowed_files` through the session, the gateway reads the contract JSON from the `.egg-state/contracts/` directory at push time, extracts `files_affected` for the relevant phase's tasks, and validates against them.

**Pros**:
- No session model changes needed
- Always uses the latest contract data (if contract is updated mid-session)
- No coupling between spawner and contract schema

**Cons**:
- Gateway must read and parse contract JSON on every push — I/O + parsing overhead
- Gateway needs to know which phase/tasks the current session maps to (still needs some session context)
- Contract files are readonly-mounted during implement phase; gateway needs filesystem access to the worktree's `.egg-state/`
- Breaks the clean separation where the gateway doesn't understand contract semantics
- Race condition: contract may be in an intermediate state during plan revisions

### Option C: Environment variable–based restrictions

**Approach**: The spawner computes the allowed file list and passes it as an environment variable (e.g., `EGG_ALLOWED_FILES=src/auth/*,tests/test_auth.py`) to the container. The gateway reads this env var from the session registration or container metadata.

**Pros**:
- Simple implementation; no gateway API changes
- Easy to debug (visible in container env)

**Cons**:
- Environment variables have size limits; long file lists could be truncated
- No mechanism for warn-then-block without session state
- Loses the ability to track violation counts across pushes
- Fragile: env var parsing is error-prone with complex glob patterns containing commas

## Recommended Approach

**Option A (Session-scoped allowed_files)** is the right choice. It aligns with the issue's proposed design, follows the existing pattern of enriching sessions with policy metadata (like `phase`, `assigned_branch`, `complexity_tier`), and keeps the gateway's validation path simple and fast — no I/O at push time, no contract parsing, just pattern matching against a pre-computed list.

The key implementation areas are:

1. **Session model** — add `allowed_files: list[str] | None` and `file_violation_counts: dict[str, int]` (or a simpler `warned_files: set[str]`)
2. **Session registration API** — accept `allowed_files` in `register_session()`
3. **Container spawner** — collect `files_affected` from assigned phase tasks, auto-expand with directory-level patterns, pass to session registration
4. **Gateway push validation** — when `allowed_files` is set, build a `PhaseFileRestriction` with both the phase's `blocked_patterns` and the session's `allowed_patterns`, then apply warn-then-block
5. **Post-agent auto-commit** — pass `allowed_files` through to the filtering logic (already receives session data)
6. **Escape hatch** — implement `egg-contract request-file` CLI subcommand that registers an additional file with the gateway session
7. **Plan template** — document enforcement semantics

## Open Questions

### Design Decisions

1. **Warn-then-block tracking mechanism**: The issue specifies "first violation per file warns, second blocks." Should violation counts be tracked on the `Session` object (in-memory, lost on gateway restart) or persisted to disk? If the gateway restarts mid-session, should violations reset (more permissive) or be preserved (stricter)?

2. **Directory-sibling expansion granularity**: The issue says listing `dir/foo.py` should implicitly allow `dir/*`. Should this expansion be:
   - **Shallow only** (`dir/*` — only direct children of that directory)?
   - **Recursive** (`dir/**` — all descendants)?
   - **Configurable** per plan?

3. **Escape hatch auto-approve vs HITL**: The issue mentions `egg-contract request-file` should "auto-approve (or queue a HITL decision for strict mode)." What should the **default** behavior be? Auto-approve with logging, or HITL queue?

4. **Config file implicit allowlist**: Should certain common config files (`pyproject.toml`, `package.json`, `Makefile`, `setup.cfg`, `tsconfig.json`, etc.) be implicitly allowed for all implement-phase agents regardless of `files_affected`, or should the planner be responsible for including them?

5. **Interaction with post-agent auto-commit**: When the auto-commit encounters files outside the allowlist, should it:
   - **Restore them silently** (current phase-restriction behavior) with a log line?
   - **Restore them and warn** (visible in the agent's checkpoint)?
   - **Treat them the same as push violations** (warn-then-block)?

6. **Glob pattern normalization**: Should `files_affected` entries be normalized before storage on the session? For example, should `./src/foo.py` and `src/foo.py` be treated identically? Should leading `/` be stripped? The existing `_normalize_path()` in `phase_filter.py` handles some of this.

### Scope Boundaries

7. **Test file generation**: An agent implementing `src/auth/login.py` will almost certainly create `tests/test_login.py`. If only `src/auth/login.py` is in `files_affected`, should `tests/` be implicitly allowed? Or should the planner always include test files in `files_affected`?

8. **Multi-push sessions**: In a typical session, an agent may push multiple times. Should violation counts accumulate across all pushes, or reset per push? Accumulating means the second push touching an out-of-scope file blocks; resetting means each push gets one free warning.

9. **Shared utility files**: Multiple agents may need to modify a shared file (e.g., `src/utils.py`). The issue says "each agent's session should include the union of files from its assigned tasks," but what about files listed in tasks belonging to *other* agents? Should the gateway detect this conflict and warn, or is this purely the planner's responsibility?

10. **`request-file` persistence**: When an agent calls `egg-contract request-file`, the gateway session's `allowed_files` must be updated. Should this update survive gateway restarts (persist to disk), or is in-memory sufficient since sessions already have a TTL?

---

*Authored-by: egg*

<!-- yaml-metadata
# metadata
complexity_tier: high
parallel_phases: true
-->
