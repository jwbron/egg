# Analysis: Enforce per-task file restrictions from planner in implement phase

> Issue: #805 | Phase: refine

## Problem Statement

The SDLC pipeline's implement phase currently enforces **phase-level** file restrictions (e.g., implement agents cannot modify `.egg-state/` directories) but has no **task-level** enforcement. When the planner specifies `files:` per task, this metadata is passed to agents only as informational context in their prompt. In Tier 3 (parallel agent) dispatch, multiple implement agents working on different plan phases can accidentally modify files outside their assigned scope, causing cross-contamination.

The goal is to make the planner's per-task `files` list an **enforced boundary** at the gateway level, following a "guide, don't cage" philosophy: warn on first violation, block on repeated violations, and provide escape hatches for legitimate edge cases.

## Current Behavior

### Data Flow: Plan files to Agent

1. **Plan template** (`docs/templates/plan.md`) defines `files:` per task in YAML blocks
2. **Plan parser** (`shared/egg_contracts/plan_parser.py`) extracts `files_affected` into the `ParsedTask` model, which converts to `Task.files_affected: list[str]` in the contract (`shared/egg_contracts/models.py:130`)
3. **Tier 3 dispatch** (`orchestrator/routes/pipelines.py:_run_tier3_implement()`) loads the contract, iterates over phases, and calls `_build_phase_scoped_prompt()` which embeds task files as informational text in the agent's prompt (lines ~3003-3004: `Files: {', '.join(task.files_affected)}`)
4. **No enforcement** — the gateway has no knowledge of per-task file lists during push validation

### Three-Layer Push Validation (gateway/gateway.py:684-875)

The gateway currently validates pushes through three layers:

1. **Role-based** (`FileRestriction`): Blocks implementer role from `.egg-state/contracts/`
2. **Agent-role** (`check_agent_restrictions`): Per-role file access patterns for coder, tester, documenter, etc. Has a **warn-only mode** controlled by `EGG_AGENT_RESTRICTIONS_ENFORCE` env var (default: warn-only)
3. **Phase-based** (`PhaseFileRestriction`): Implement phase blocks `.egg-state/` directories but allows all code files

None of these layers have per-session or per-task file scoping.

### Session Model (gateway/session_manager.py)

`Session` stores `phase`, `agent_role`, `pipeline_id`, `complexity_tier` but has no field for per-session allowed files. `register_session()` accepts these fields from the orchestrator's `GatewayClient.register_session()` call in `container_spawner.py:350`.

### Post-Agent Auto-Commit (gateway/post_agent_commit.py)

`auto_commit_worktree()` applies phase-based file restrictions to filter uncommitted changes — blocked files are restored via `git checkout`, allowed files are committed and pushed. This function receives `phase` but has no per-session file context.

### Existing Warn-Then-Block Precedent

The agent-role restriction layer already implements a warn/enforce toggle (`EGG_AGENT_RESTRICTIONS_ENFORCE`). In default mode, violations are logged as warnings but pushes proceed. This is a proven pattern that the per-task restriction can model.

## Constraints

- **Backward compatibility**: Sessions without `allowed_files` (non-pipeline, non-implement, or pre-existing) must behave exactly as today — no per-file restriction applied
- **Session persistence**: `Session.to_dict_for_persistence()` and `from_persistence()` must handle the new field for crash recovery
- **Performance**: Glob matching against the file list runs on every push; must not introduce latency for large file lists
- **Gateway API surface**: `register_session()` is called from `GatewayClient` in the orchestrator — both sides need the new field
- **Plan parser**: Already supports `files_affected` as `list[str]`; needs to support glob patterns (`*`, `**`) if not already
- **Post-agent-commit runs outside normal request context**: No Flask `g` object — needs explicit `allowed_files` parameter or access to session data
- **`egg-contract request-file`**: Does not exist yet — needs a new CLI command, contract model field, and gateway endpoint or HITL decision mechanism
- **Test infrastructure**: Gateway tests use a custom module loader in `conftest.py` due to hyphenated package names; new tests must follow this pattern

## Options Considered

### Option A: Session-scoped allowed_files (as described in issue)

**Approach**: Add `allowed_files: list[str] | None` to the `Session` model. The orchestrator reads `files_affected` from all tasks assigned to the agent's plan phase, computes the union, auto-expands to directory-level, and passes the list to `register_session()`. The gateway builds a `PhaseFileRestriction` combining existing blocked_patterns with session-specific allowed_patterns during push validation. Warn-then-block escalation tracks violations per file per session.

**Pros**:
- Follows existing architecture patterns (session metadata, `PhaseFileRestriction`, three-layer validation)
- Clean separation: orchestrator computes allowlist, gateway enforces it
- Minimal new API surface — one new field on an existing endpoint
- Reuses proven warn-then-block pattern from agent restrictions
- Post-agent-commit can access session data from disk (already persisted)

**Cons**:
- Session-level scoping means all tasks in a phase share one allowlist (no per-task isolation within a single agent)
- Directory-sibling expansion logic adds complexity to the matching
- Violation state (which files have been warned) needs to be stored somewhere (in-memory on Session, or persisted)

### Option B: Gateway-side plan-aware validation

**Approach**: Instead of passing `allowed_files` through the session, have the gateway directly read the contract JSON to determine file restrictions. The gateway would look up the pipeline's contract, find the agent's assigned phase, and extract `files_affected` from its tasks.

**Pros**:
- No changes to the session registration API
- Gateway has the full contract context, enabling richer validation messages

**Cons**:
- Breaks separation of concerns — gateway currently doesn't read contracts (that's the orchestrator/shared layer's job)
- Couples gateway to contract schema, creating a tight dependency
- Contract files are in `.egg-state/contracts/` which may be readonly in implement phase
- Harder to test (requires contract fixtures instead of simple session data)
- Doesn't work for non-orchestrated sessions or future session types

### Option C: Filesystem-level enforcement (readonly mounts)

**Approach**: Use Docker volume mounts to make directories outside the task's scope readonly, similar to how `.egg-state/` is already readonly-mounted during implement phase.

**Pros**:
- Strongest enforcement — kernel-level, impossible to bypass
- No gateway changes needed

**Cons**:
- Extremely rigid — no warn-then-block, no escape hatch, no graceful fallback
- Requires dynamic mount point computation per container, which is complex with many tasks
- Cannot support glob patterns or directory-sibling expansion
- Mount changes require container recreation — no runtime adjustment
- Contradicts "guide, don't cage" philosophy
- Files not in the plan but legitimately needed would completely block the agent

## Recommended Approach

**Option A: Session-scoped allowed_files** is recommended. It follows the existing architectural patterns, maintains clean separation of concerns between orchestrator and gateway, and supports the "guide, don't cage" philosophy with warn-then-block escalation. The session-based approach is already battle-tested for phase, role, and complexity_tier — adding `allowed_files` is a natural extension.

Key design decisions within Option A:

1. **Allowlist granularity**: Per-phase (union of all tasks in the agent's assigned phase), not per-task. This is appropriate because each agent container handles one plan phase.

2. **Matching strategy**: Combine exact path match, directory-prefix match (listing `dir/foo.py` allows `dir/*`), and glob pattern match (`**/*.py`). Reuse `PhaseFileRestriction._matches_pattern()` which already supports prefix and wildcard matching.

3. **Violation tracking**: In-memory on the `Session` object (a `set[str]` of warned file paths). No need to persist — violation state is per-session and resets if the gateway restarts (fail-open on restart is acceptable given the "guide" philosophy).

4. **Escape hatch**: New `egg-contract request-file` CLI command that logs the request and auto-approves in default mode. This provides observability without hard blocking.

## Open Questions

### Enforcement Semantics

1. **Should the warn-then-block threshold be configurable?** The issue says "first violation warns, repeated violations block." Should this be exactly 2 attempts (warn on 1st, block on 2nd+), or configurable (e.g., `EGG_FILE_RESTRICTION_WARN_THRESHOLD=3`)?

2. **What constitutes a "repeated violation for the same file"?** Is it per-file (warn once for `foo.py`, then block all future `foo.py` touches) or per-push (each push gets one warning, second push with the same file blocks)?

3. **Should there be a strict mode (block immediately, no warnings)?** The agent restriction layer has `EGG_AGENT_RESTRICTIONS_ENFORCE` for this. Should per-task restrictions have an analogous `EGG_TASK_FILE_RESTRICTIONS_ENFORCE`?

### Directory-Sibling Expansion

4. **How deep should directory-sibling expansion go?** If the plan lists `src/auth/login.py`, should the agent also be able to modify `src/auth/utils/helpers.py` (subdirectory), or only direct siblings in `src/auth/`?

5. **Should expansion apply to new file creation differently than modification?** Creating a new helper file in the same directory feels more natural than modifying an existing file that belongs to another task.

### Common Config Files

6. **Should there be a global allowlist of "common config files" that all agents can modify?** Files like `pyproject.toml`, `package.json`, `Makefile`, `requirements.txt`, `.gitignore` are frequently touched as side effects. The issue mentions this but doesn't specify the list.

7. **If yes, where should this list be defined?** Options: hardcoded in the gateway, configurable per-repo in `.egg/config.yaml`, or left to the planner to include in every task.

### Escape Hatch (`egg-contract request-file`)

8. **Should `request-file` auto-approve by default (observability only) or queue a HITL decision?** The issue suggests auto-approve with optional strict mode. In auto-approve mode, does the file get added to the session's allowlist permanently, or just for the current push?

9. **Should the gateway expose an API endpoint for file requests, or should the CLI modify the contract and the gateway re-reads it?** The former is cleaner but requires a new endpoint; the latter reuses existing contract infrastructure.

### Interaction with Existing Layers

10. **How should per-session allowed_files interact with agent-role restrictions?** If the agent-role restriction allows `src/**` but the per-task restriction only allows `src/auth/`, which takes precedence? The natural answer is intersection (most restrictive wins), but this should be explicit.

11. **Should the per-task restriction replace or augment the phase-level `blocked_patterns`?** The implement phase blocks `.egg-state/` directories. If a task somehow lists a file in `.egg-state/` (unlikely but possible), should the phase block still take precedence?

### Post-Agent-Commit

12. **How should `auto_commit_worktree()` access the session's `allowed_files`?** It currently receives `phase` and `session_token` as parameters. Options: (a) add `allowed_files` parameter, (b) look up session from token via gateway API, (c) read persisted session from disk. The function already uses the gateway API for pushing — option (b) would be consistent.

13. **Should files outside the allowlist be silently restored (current behavior for phase-blocked files) or should the agent see a clear error message?** Silent restoration can cause confusion; logging is important but may not be visible to the agent.

### Plan Template and Planner Behavior

14. **Should the planner be required to list files for every task, or is it optional?** Currently `files_affected` can be empty. Making it required would improve enforcement but could block plans for exploratory tasks where files aren't known upfront.

15. **Should the plan template encourage glob patterns (e.g., `tests/**`) or explicit file lists?** Globs are more resilient but less precise. The issue suggests the planner should "list files generously using globs."

### Testing and Rollout

16. **Should per-task file restriction be opt-in initially (disabled by default) or opt-in to disable?** Given the "guide, don't cage" philosophy, starting with warn-only for all pipelines and allowing teams to enable strict mode seems safest.

---

*Authored-by: egg*

<!-- HITL decisions and feedback are posted as separate comments -->

# metadata
complexity_tier: high
parallel_phases: true
