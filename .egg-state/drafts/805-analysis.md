# Analysis: Enforce per-task file restrictions from planner in implement phase

> Issue: #805 | Phase: refine

## Problem Statement

When multiple implement agents work in parallel (Tier 3 dispatch), each agent is only restricted by the **phase-level** file rules: the implement phase blocks `.egg-state/` directories but allows modification of *any* code file. This means agent A can accidentally modify files that belong to agent B's task, creating cross-contamination that defeats the purpose of task-scoped parallelism.

The planner already produces per-task `files_affected` lists, but these are purely informational today. The goal is to make them an **enforced boundary** at the gateway level, following a "guide, don't cage" philosophy: warn first, block on repeated violations, and provide escape hatches for legitimate edge cases.

## Current Behavior

### Phase-level restrictions (implement phase)

The implement phase uses a `PhaseFileRestriction` with `blocked_patterns` only (`gateway/phase_filter.py:510-519`):

```python
PipelinePhase.IMPLEMENT: PhaseFileRestriction(
    blocked_patterns=[
        ".egg-state/contracts/*",
        ".egg-state/drafts/*",
        ".egg-state/pipelines/*",
        ".egg-state/reviews/*",
    ],
    # No allowed_patterns = allow everything except blocked
)
```

This means any implement agent can modify any code file — there is no per-task scoping.

### Enforcement points

File restrictions are checked at two enforcement points:

1. **Push-time** (`gateway/gateway.py:754-822`): `check_phase_file_restrictions()` validates files on every `git push`. Blocked pushes return HTTP 403 with details.

2. **Post-agent auto-commit** (`gateway/post_agent_commit.py:175-231`): When a container exits, uncommitted changes are auto-committed. Blocked files are restored via `git checkout --` and only allowed files are staged.

Both enforcement points use the same `PhaseFileRestriction` evaluation logic.

### Agent role restrictions (parallel system)

There is a separate agent-role restriction system (`gateway/agent_restrictions.py`) that scopes file access by role (coder, tester, documenter, etc.). This is currently **warn-only by default** (`EGG_AGENT_RESTRICTIONS_ENFORCE=false`). It operates on role-wide patterns (e.g., coders can write `src/`, testers can write `tests/`) — not per-task patterns.

### Session model

The `Session` dataclass (`gateway/session_manager.py:209-245`) carries `phase`, `agent_role`, `pipeline_id`, and `complexity_tier`, but has **no field** for per-session file allowlists. The `register_session()` method (`gateway/session_manager.py:453-530`) accepts these fields but not `allowed_files`.

### Container spawner

`ContainerSpawner` (`orchestrator/container_spawner.py:350-364`) calls `gateway.register_session()` with `phase`, `pipeline_id`, `agent_role`, and `complexity_tier`. It does **not** read task `files_affected` from the contract or pass them to the session.

### Plan parser and contract model

The plan parser (`shared/egg_contracts/plan_parser.py`) already extracts `files_affected` from task YAML into `ParsedTask.files_affected`, which maps to `Task.files_affected` on the contract model (`shared/egg_contracts/models.py`). This data is available but unused at enforcement time.

## Constraints

- **Backward compatibility**: Sessions without `allowed_files` (non-pipeline sessions, non-implement phases) must behave identically to today. Empty `files_affected` = no per-file restriction.
- **Performance**: File checks run on every `git push`. Pattern matching must remain fast — the existing `fnmatch`-based approach handles this well, but the check must not become O(N*M) with large file lists and many patterns.
- **Security**: This is a **defense-in-depth** layer, not the primary security boundary. Phase-level restrictions remain the authoritative gate. Per-task restrictions catch accidental cross-contamination, not malicious bypass.
- **Planner quality**: Enforcement is only as good as the planner's file lists. The system must tolerate imprecise or incomplete file lists gracefully — overly strict enforcement will cause agents to spin.
- **Two enforcement points**: Both push-time and post-agent-commit must apply per-task restrictions consistently. The session's `allowed_files` must be accessible at both points.
- **Gateway API surface**: Adding `allowed_files` to session registration requires changes to the gateway HTTP API (`/api/v1/sessions/create`), the `GatewayClient` in the orchestrator, and the `SessionManager`.
- **Persistence**: Sessions are persisted to disk (JSON). The `allowed_files` list must survive gateway restarts.
- **Escape hatch**: The `egg-contract request-file` CLI mentioned in the issue does not exist yet and needs to be built.

## Options Considered

### Option A: Session-scoped allowed_files with warn-then-block

**Approach**: Add `allowed_files: list[str] | None` to `Session`. The container spawner reads task `files_affected` from the contract, applies directory-sibling expansion, and passes the combined list to `register_session()`. During push validation, the gateway builds a `PhaseFileRestriction` that layers session `allowed_files` on top of the existing implement-phase `blocked_patterns`. First violation per file logs a warning; second blocks.

This is the approach outlined in the issue.

**Pros**:
- Leverages the existing `PhaseFileRestriction` and `_matches_pattern()` infrastructure — minimal new code
- Two-strike warn-then-block is forgiving for agents acting in good faith
- Directory-sibling expansion handles the common case of creating helper/test files next to listed files
- `allowed_files` on the session model is clean and self-contained
- Persists naturally with existing session serialization

**Cons**:
- Violation tracking (which files have had a first warning) requires per-session state that must survive across pushes — adds complexity to the session model
- Warn-then-block has a subtle race: if an agent pushes the same out-of-scope file in two rapid pushes, the second push might not see the first warning yet (though this is unlikely in practice since agents push sequentially)
- Directory-sibling expansion could be overly permissive (listing one file in `src/` doesn't mean the agent should touch everything in `src/`)

### Option B: Session-scoped allowed_files with immediate soft-block + escape hatch

**Approach**: Same session model changes, but instead of warn-then-block, the first out-of-scope file immediately triggers a soft block with an actionable error that tells the agent to use `egg-contract request-file` to request access. The request auto-approves (logging the request for observability) and the agent retries.

**Pros**:
- Simpler implementation — no violation counter state to track per session
- Clear observability: every out-of-scope access is explicitly logged via the escape hatch
- No ambiguity about when a block happens — consistent behavior from the first push

**Cons**:
- More friction for agents: every out-of-scope file requires an explicit `egg-contract request-file` call + retry
- Could cause agents to spin if the planner's file lists are significantly incomplete
- The escape hatch (auto-approve + retry loop) adds latency per out-of-scope file
- Contradicts the issue's "guide, don't cage" philosophy — immediate blocking is stricter

### Option C: Warn-only mode with structured logging (no blocking)

**Approach**: Add `allowed_files` to sessions and check them at push time, but only ever log warnings — never block. Rely on post-agent-commit filtering to prevent out-of-scope files from being committed. The escape hatch is not needed since nothing is blocked.

**Pros**:
- Zero risk of agents spinning due to incorrect file lists
- Provides full observability into cross-contamination without enforcement overhead
- Can be promoted to blocking later once confidence in planner quality is established
- Simplest implementation — no violation tracking, no escape hatch

**Cons**:
- Post-agent-commit filtering silently drops files, which is confusing when the agent thinks it committed successfully
- Doesn't prevent cross-contamination at push time — only catches it at auto-commit
- If an agent pushes successfully, another agent pulling the branch will see out-of-scope changes
- Provides less value as a safety boundary

## Recommended Approach

**Option A: Session-scoped allowed_files with warn-then-block**, as outlined in the issue. This balances enforcement with agent ergonomics:

1. The existing `PhaseFileRestriction` infrastructure already supports `allowed_patterns` + `blocked_patterns` with glob matching and path normalization. Adding per-session patterns on top of phase-level patterns is a natural extension.

2. Warn-then-block matches the "guide, don't cage" philosophy. The first warning gives the agent a chance to self-correct (or signals that the planner's file list needs updating). The second violation blocks, preventing repeated cross-contamination.

3. The `egg-contract request-file` escape hatch provides observability even when the agent legitimately needs a file outside its scope. Auto-approval (with logging) keeps the agent from being stuck while creating an audit trail.

4. Directory-sibling expansion should be scoped to the **parent directory** of each listed file, not arbitrary ancestors. Listing `src/auth/login.py` allows other files in `src/auth/` but not all of `src/`. This is generous enough for helper files and test files co-located with implementation files.

**Violation tracking** can be implemented as a `dict[str, int]` on the `Session` object mapping file paths to violation counts. This needs to persist across pushes but not across gateway restarts (restarting the gateway within a single agent session is unusual and resetting the counter is acceptable).

## Open Questions

1. **Config file allowlisting**: The issue mentions common config files (`pyproject.toml`, `package.json`, `Makefile`) should be "implicitly allowed or the planner should be prompted to include them." Should the gateway maintain a hardcoded allowlist of common config files that bypass per-task restrictions, or should this be entirely the planner's responsibility?

2. **Strict mode for HITL escape hatch**: The issue mentions `request-file` could "auto-approve or queue a HITL decision for strict mode." Is strict mode (HITL approval required) needed in the initial implementation, or should we start with auto-approve only and add strict mode later?

3. **Interaction with agent-role restrictions**: Agent-role restrictions and per-task file restrictions are independent systems that both check files at push time. Should per-task restrictions be evaluated before or after role restrictions? If a file is allowed by the task scope but blocked by the role, should the role restriction still win?

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: high
parallel_phases: true
```
