# Analysis: Enforce per-task file restrictions from planner in implement phase

> Issue: #912 | Phase: refine

## Problem Statement

When the orchestrator dispatches multiple implement agents in parallel (Tier 3 dispatch), each agent can modify **any** code file — the only restrictions in place are phase-level (implement phase blocks `.egg-state/` directories) and agent-role-level (coder vs tester file scopes). This means an implement agent working on task A can accidentally modify files belonging to task B, creating cross-contamination.

The planner already produces per-task `files:` lists in the plan YAML, and the contract model stores these as `files_affected` on each `Task`. However, this data is purely informational — it is never passed to the gateway, and no enforcement occurs at commit/push time.

**Desired outcome**: The gateway should restrict each implement agent's commits to only the files listed in its assigned tasks, using a generous "guide, don't cage" approach with warn-then-block escalation.

## Current Behavior

### Data flow (what exists)

1. **Plan template** (`docs/templates/plan.md`) requires `files:` per task in the YAML block
2. **Plan parser** (`shared/egg_contracts/plan_parser.py:475-527`) extracts these into `files_affected` on the `ParsedTask` model, which flows to `Task.files_affected` (`shared/egg_contracts/models.py:130`)
3. **Container spawner** (`orchestrator/container_spawner.py:350-364`) registers sessions with the gateway, passing `phase`, `agent_role`, `complexity_tier` — but **not** `files_affected`
4. **Gateway client** (`orchestrator/gateway_client.py`) — `register_session()` has no `allowed_files` parameter
5. **Session model** (`gateway/session_manager.py:209-246`) — no `allowed_files` field
6. **Push validation** (`gateway/gateway.py:743-816`) enforces agent-role and phase restrictions, but no per-task file restrictions
7. **Post-agent auto-commit** (`gateway/post_agent_commit.py:175-228`) filters by phase restrictions only

### Enforcement layers (what's already enforced)

| Layer | Location | Scope | Behavior |
|-------|----------|-------|----------|
| Role-based | `gateway.py:710` | Contract files | Hard block |
| Agent-role | `gateway.py:743-791` | Coder/tester/documenter file scopes | Warn-only by default (`EGG_AGENT_RESTRICTIONS_ENFORCE`) |
| Phase-level | `gateway.py:793-816` | `.egg-state/` directories in implement phase | Hard block |
| **Per-task** | **Not implemented** | **Files from plan** | **Not enforced** |

### Infrastructure that's ready to reuse

- `PhaseFileRestriction` class (`gateway/phase_filter.py:160-228`) already supports `allowed_patterns` with glob matching, directory prefix matching, and path normalization with traversal prevention
- `check_phase_file_restrictions()` and `check_agent_restrictions()` return `FileRestrictionResult` with `blocked_files` lists
- Agent-role enforcement already implements the warn-vs-enforce toggle pattern (`EGG_AGENT_RESTRICTIONS_ENFORCE`)

## Constraints

- **Backward compatibility**: Sessions without `allowed_files` (non-pipeline, non-implement) must behave identically to today — no per-file restriction when the field is `None` or empty
- **Fail-open on empty**: If a task has empty `files_affected`, no per-file restriction applies to avoid locking agents out entirely
- **Phase blocks are immutable**: Per-task `allowed_files` cannot override phase-level `blocked_patterns` (e.g., cannot allow writing to `.egg-state/contracts/` during implement)
- **Glob support**: `files_affected` entries may contain `*` and `**` patterns (e.g., `tests/**`, `src/components/*.tsx`)
- **Directory-sibling expansion**: Listing `dir/foo.py` should implicitly allow other files in `dir/` so agents can create helper files
- **Performance**: Push validation runs on every push — must not add significant latency (no contract file I/O during push; data must be precomputed on session)
- **Persistence**: `allowed_files` must survive gateway restarts (persisted with session). Warning counters can be transient (fail-open on restart is acceptable per the "guide, don't cage" philosophy)
- **Cross-cutting files**: Multiple tasks may list the same file; each agent's session gets the union of files from all its assigned tasks
- **Only implement-phase coder agents**: This restriction should not apply to tester, reviewer, or documenter agents, or to non-implement phases

## Options Considered

### Option A: Session-level `allowed_files` (issue's proposed design)

**Approach**: Add `allowed_files: list[str] | None` to the `Session` dataclass. The container spawner computes the union of `files_affected` from all tasks assigned to the agent, applies directory-sibling expansion, and passes the list to `register_session()`. The gateway builds a `PhaseFileRestriction` from the session's `allowed_files` during push validation. Warn-then-block escalation uses a per-session `_warned_files` dict (transient, not persisted).

**Pros**:
- All data needed for enforcement is available on the session object — no contract I/O during push
- Matches the existing `PhaseFileRestriction` pattern (allowed/blocked patterns with glob matching)
- Warn-then-block escalation is self-contained in gateway push validation
- Naturally extends the existing 4-layer validation chain to a 5th layer
- Session persistence means restrictions survive gateway restarts

**Cons**:
- Session object grows (one more list field + transient dict)
- Requires changes across 5 components: session model, gateway endpoint, gateway client, container spawner, post-agent commit
- If the planner updates the plan after the session is registered, the session's `allowed_files` becomes stale (though this is unlikely in practice — plans are finalized before implement)

### Option B: Gateway queries contract at push time

**Approach**: Instead of storing `allowed_files` on the session, the gateway fetches the contract from the orchestrator API at push time. It extracts `files_affected` for the agent's assigned tasks and validates against those.

**Pros**:
- Always up-to-date with the latest contract state
- No session model changes needed
- Simpler container spawner (no need to compute/pass allowed files)

**Cons**:
- Adds network I/O (HTTP call to orchestrator) on every push — latency and failure mode concerns
- Gateway becomes dependent on orchestrator availability for push validation
- Requires gateway to know which tasks are assigned to which agent (this mapping isn't currently exposed)
- Breaks the principle that the gateway should be self-contained for enforcement

### Option C: Readonly filesystem mounts (kernel-level enforcement)

**Approach**: Use Docker readonly bind mounts to physically prevent agents from writing to files outside their task scope. The container spawner computes the allowed file set and configures mount permissions accordingly.

**Pros**:
- Kernel-level enforcement — impossible to bypass from userspace
- No gateway code changes needed
- No push-time validation overhead

**Cons**:
- Docker bind mounts operate at directory level, not file level — cannot allow `dir/foo.py` while blocking `dir/bar.py`
- Extremely complex mount configuration for arbitrary file lists
- No warn-then-block — just hard failures that confuse the agent
- Cannot support dynamic file requests (escape hatch)
- Agents commonly create new files (test files, helper modules) that wouldn't exist at mount time

## Recommended Approach

**Option A: Session-level `allowed_files`** — This is the design proposed in the issue and it's the right approach. It:

- Fits naturally into the existing enforcement architecture (session → push validation → `PhaseFileRestriction`)
- Pre-computes all data at session registration time so push validation is fast
- Supports the "guide, don't cage" philosophy through warn-then-block without filesystem-level rigidity
- Enables the escape hatch pattern (agent can request additional files via `egg-contract request-file`, which updates the session)

The implementation touches 5 components but each change is well-scoped and follows established patterns:

| Component | Change |
|-----------|--------|
| `Session` model | Add `allowed_files` field + persistence |
| Gateway `/sessions/create` | Accept + validate `allowed_files` |
| `GatewayClient.register_session()` | Accept + pass `allowed_files` |
| `ContainerSpawner` | Compute `allowed_files` from contract, expand directories |
| Gateway push validation | Add 5th validation layer (warn-then-block) |
| Post-agent commit | Filter by session `allowed_files` |
| Plan template docs | Document enforcement semantics |

## Open Questions

### Design decisions

1. **Warn threshold default**: The issue specifies warn-on-first, block-on-second. Should the default `EGG_TASK_FILE_WARN_THRESHOLD` be `1` (warn once, block on 2nd attempt for the same file)? Or should it be higher (e.g., `2`) to be more lenient? A threshold of 1 means one push with an out-of-scope file succeeds, the second is blocked.

2. **Directory-sibling expansion semantics**: Listing `dir/foo.py` should allow other files in `dir/`. Should this be:
   - (a) `dir/*` (shallow — only immediate children), or
   - (b) `dir/**` (recursive — all descendants)?
   The issue says "sibling files (new helpers, test files) are reachable" which suggests shallow (`dir/*`), but nested packages might need recursive.

3. **Config file implicit allowlist**: The issue mentions "Common config files (pyproject.toml, package.json, Makefile, etc.) should be implicitly allowed or the planner should be prompted to include them." Which approach?
   - (a) Hardcode an implicit allowlist of common config files that are always allowed during implement
   - (b) Require the planner to list config files explicitly (and update planner prompts to remind it)
   - (c) Both: planner lists them, but a small implicit set exists as safety net

4. **Escape hatch (`egg-contract request-file`) scope**: The issue describes this as an observability mechanism that "auto-approves (or queues a HITL decision for strict mode)". Should the initial implementation include:
   - (a) Full escape hatch with auto-approve (CLI command + gateway endpoint + session update)
   - (b) Just the CLI command that logs the request (observability only, no enforcement change)
   - (c) Defer entirely to a follow-up issue

5. **Plan staleness**: If the plan is amended after the implement agent's session is registered (e.g., during a review cycle), should the session's `allowed_files` be automatically updated? Or is it acceptable to require re-registration?

### Clarifications needed

6. **Tester agent file access**: The issue says "This restriction only applies to implement-phase coder agents. Tester and reviewer agents already have their own role-based restrictions." Testers currently have role-based file restrictions (test files only). Should testers also get per-task `allowed_files` from the plan, or rely solely on their role-based patterns?

7. **Warning counter persistence**: The issue implies `_warned_files` should be transient (reset on gateway restart = fail-open). Is this correct, or should warning state persist across restarts to prevent agents from circumventing restrictions by triggering a gateway restart?

8. **Existing agent-role restrictions interaction**: Agent-role restrictions (coder blocked from docs/tests) and per-task file restrictions may conflict. For example, if the plan lists `tests/test_auth.py` in a coder task's `files_affected`, should the per-task allowlist override the agent-role restriction? Or should all layers be AND'd together (most restrictive wins)?

9. **Strict mode default**: Should `EGG_TASK_FILE_RESTRICTIONS_ENFORCE` (always block, no warnings) be available from day one, or added later? Is there a deployment that would want strict mode immediately?

10. **Error message and recovery UX**: When a push is blocked, the error should tell the agent exactly what to do. Should the message include:
    - (a) The list of blocked files + the allowlist they violated
    - (b) The specific task IDs whose `files_affected` they should check
    - (c) The `egg-contract request-file` escape hatch command (if implemented)
    - (d) All of the above

---

*Authored-by: egg*

<!-- HITL decisions and feedback are posted as separate comments -->

# metadata
complexity_tier: high
parallel_phases: true
