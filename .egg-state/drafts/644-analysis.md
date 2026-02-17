# Analysis: Enforce phase file restrictions via readonly mounts and commit-time validation

> Issue: #644 | Phase: refine

## Problem Statement

The gateway's file restriction enforcement has three gaps that allow agents to bypass phase and role constraints:

1. **Local modification bypass**: Phase file restrictions only fire on `git push`. An agent can modify protected files locally without ever pushing, and if the orchestrator reads from the worktree (e.g., running `docker-compose up` against it per #641), it trusts the modified state.

2. **Unrestricted branch switching**: `git checkout` and `git switch` are allowed with no branch validation. Agents can leave their assigned branch (`egg/{container_id}/work`), making it impossible to deterministically auto-commit and auto-push their work after completion.

3. **Late enforcement wastes tokens**: Agents discover restrictions only at push time, after spending tokens on reading, modifying, staging, committing, and pushing protected files. Earlier enforcement (filesystem or commit-time) would cut this waste.

Additionally, a related bug surfaced in the issue comment: **agent-role restrictions** (`agent_restrictions.py`) are defined but never wired into the push handler in `gateway.py`. The function `check_agent_restrictions()` exists but is never called, making coder/tester/documenter file boundaries unenforced.

## Current Behavior

### Phase file restrictions (push-time only)

The `git_push()` handler in `gateway/gateway.py:627-754` enforces two types of file restrictions:

1. **Role-based** (`check_file_restrictions()`, line 661): Checks `session.agent_role` against `file_restrictions` config (e.g., "implementer" blocked from `.egg-state/contracts/`).
2. **Phase-based** (`check_phase_file_restrictions()`, line 721): Checks `session.phase` against per-phase allowed/blocked patterns defined in `phase_filter.py:480-525` and `.egg/phase-permissions.json`.

Both only run during push. The `git_execute()` endpoint (`gateway.py:942-1165`) handles `checkout`, `switch`, `commit`, and other operations with no phase or file restriction checks.

### Branch management (no enforcement)

`checkout` and `switch` are in `GIT_ALLOWED_COMMANDS` (`git_client.py:207-610`) with flag allowlists but no branch validation. An agent assigned to `egg/{container_id}/work` can freely switch to any branch via `git checkout -b other-branch` or `git switch --create other-branch`.

### Agent-role restrictions (dead code for push)

`agent_restrictions.py` defines `CODER_PATTERNS`, `TESTER_PATTERNS`, `DOCUMENTER_PATTERNS`, etc. with detailed allowed/blocked file lists. The `validate_agent_push()` function (line 545) and `check_agent_restrictions()` wrapper in `phase_filter.py` exist but are never called from `gateway.py`'s push handler.

### Mount infrastructure

Mounts use the `MountSpec` dataclass (`shared/egg_container/__init__.py:40-46`) with a `readonly` flag. The existing `git_shadow_mounts()` function already creates readonly bind mounts (`.git` directories shadowed with `/dev/null`). The Docker mount pipeline already supports readonly mounts end-to-end:

- `MountSpec(readonly=True)` -> `mount_spec_to_cli_args()` appends `,readonly` -> Docker enforces at OS level
- `to_dockerpy_kwargs()` sets `"ReadOnly": True` in docker-py format

## Constraints

- **Each phase runs in a new container** (`container_spawner.py:spawn_agent_container()`), so readonly mounts can be set at container creation time without dynamic updates.
- **Docker nested mounts**: Inner writable mounts override outer readonly mounts, enabling "readonly everything except X" patterns.
- **Bind mount paths must exist**: Directories must be `mkdir -p`'d before bind mounting.
- **Worktree branches use pattern `egg/{container_id}/work`**: Set during worktree creation (`worktree_manager.py:182`), before the container starts.
- **Session already tracks phase and branch**: `Session` dataclass has `phase`, `agent_role`, and `last_branch` fields (`session_manager.py:168-264`).
- **Agent-role restriction bug (#644 comment)**: Must be fixed as part of this work since it's closely related enforcement gap.
- **Post-agent cleanup flow**: `_cleanup_session()` in `runtime.py:350-369` runs after the container exits but before worktree removal -- this is the insertion point for auto-commit.

## Options Considered

### Option A: Issue-proposed layered approach (all four layers)

**Approach**: Implement all four layers as described in the issue: Layer 0 (branch lock), Layer 1 (readonly mounts), Layer 2 (commit-time validation), Layer 3 (existing push-time), Layer 4 (post-agent auto-commit). Also wire agent-role restrictions into the push handler (from the comment).

**Pros**:
- Defense in depth: filesystem, commit, and push enforcement create overlapping safety nets
- Readonly mounts give the earliest possible enforcement (OS-level) with zero token waste
- Branch lock ensures deterministic post-agent commit/push
- Auto-commit prevents silent work loss
- Actionable error messages reduce agent token waste on recovery

**Cons**:
- Large scope: touches gateway, orchestrator, sandbox, and shared libraries across ~10 files
- Readonly mount logic needs careful per-phase configuration that mirrors phase-permissions.json patterns
- Nested Docker mount ordering must be validated empirically
- Risk of breaking existing workflows if mount configuration is wrong (agent can't write anywhere)
- `.egg-readonly` marker files add maintenance burden

### Option B: Gateway-only enforcement (branch lock + commit-time validation, no readonly mounts)

**Approach**: Implement Layer 0 (branch lock in `git_execute()`), Layer 2 (commit-time validation in `git_execute()`), and wire agent-role restrictions into push. Skip readonly mounts (Layer 1) and post-agent auto-commit (Layer 4).

**Pros**:
- Smaller scope: only `gateway.py` and `git_client.py` need significant changes
- No container/mount infrastructure changes, lower risk of breaking agent environments
- Commit-time validation catches most cases with clear error messages
- Simpler to test (gateway unit tests only, no Docker mount integration tests needed)
- Agent still gets fast feedback at commit time (before push), significantly reducing token waste

**Cons**:
- Does not close the local-only modification bypass (Gap 1): agents can still modify protected files and the orchestrator could read them
- No OS-level enforcement: agent can still write protected files locally and discover restriction only at commit time
- No auto-commit: uncommitted work is lost when agent exits

### Option C: Readonly mounts only (no branch lock or commit-time validation)

**Approach**: Implement Layer 1 (readonly mounts) and Layer 4 (post-agent auto-commit). Skip branch lock and commit-time validation since readonly mounts prevent the modifications at the OS level.

**Pros**:
- Closes the local-only modification bypass completely
- Earliest possible enforcement point (OS level)
- Simpler gateway code (no new validation logic needed in `git_execute()`)

**Cons**:
- Does not address branch switching (Gap 2)
- Readonly mounts cannot enforce fine-grained patterns (e.g., `.egg-state/drafts/*analysis*` vs `.egg-state/drafts/*plan*`): mounts operate at directory granularity, not filename patterns
- If mount configuration has bugs, agent gets cryptic OS errors with no actionable guidance
- Requires Docker mount integration testing

## Recommended Approach

**Option A (full layered approach)** is recommended, as it aligns with the issue's detailed proposal and addresses all three gaps comprehensively.

However, the implementation should be structured so that each layer is independently testable and can be shipped incrementally. The layers have clear ordering by value:

1. **Branch lock (Layer 0)** -- highest value, lowest risk. Prevents the determinism problem (Gap 2) with minimal code changes in `git_execute()`.
2. **Commit-time validation (Layer 2) + agent-role restriction wiring** -- high value, moderate risk. Catches most restriction violations early with actionable errors. The agent-role fix is a one-line addition to the push handler.
3. **Readonly mounts (Layer 1)** -- high value, higher risk. Closes the local modification bypass (Gap 1) but requires careful mount configuration and integration testing.
4. **Post-agent auto-commit (Layer 4)** -- moderate value, moderate risk. Prevents work loss but needs careful handling of edge cases (partial commits, conflicting state).

This ordering lets the plan phase structure work as incremental, independently shippable units.

Key considerations for the plan phase:

- **Fine-grained pattern enforcement**: Readonly mounts work at directory granularity, but phase restrictions include filename patterns like `.egg-state/drafts/*analysis*`. Commit-time validation is needed as a complement for patterns that mounts can't express.
- **`.egg-readonly` marker files**: Useful for agent guidance but should be generated, not manually maintained. The plan should address how and when these are created.
- **Checkout heuristics**: Distinguishing `git checkout <branch>` from `git checkout -- <file>` requires parsing git arguments. The `--` separator is the clearest signal, but there are edge cases (e.g., `git checkout HEAD file.txt` without `--`). The plan should specify the heuristic precisely.
- **Post-agent auto-commit runs on the host side**: It accesses the worktree directly (not through the gateway) and must use the same phase restriction logic. This means phase restriction patterns should be importable by both gateway and the auto-commit script.
- **Testing strategy**: Branch lock and commit-time validation can be tested with gateway unit tests. Readonly mounts require integration tests with actual Docker containers.

## Open Questions

1. **Should readonly mounts block the entire `.egg-state/` tree during implement phase, or only specific subdirectories?**

   The issue proposes blocking `contracts/`, `drafts/`, `pipelines/`, and `reviews/` individually, leaving `checkpoints/` and `agent-outputs/` writable. This matches the existing phase restrictions in `phase-permissions.json`. However, mounting 4+ individual subdirectories as readonly inside a writable parent adds mount complexity. The alternative is mounting all of `.egg-state/` readonly and overlaying writable mounts for `checkpoints/` and `agent-outputs/` only.

2. **Should Layer 4 (post-agent auto-commit) run in the gateway/orchestrator process or as a separate script?**

   The issue suggests `gateway/post_agent_commit.py` or integrating into `session_manager.py`. Running it in the orchestrator (which has direct access to worktrees and git) avoids routing through the gateway's own validation. But running through the gateway ensures the same restriction logic applies. The plan phase should decide which approach to use.

3. **How should the agent-role restriction bug (from the comment) be scoped relative to this issue?**

   The comment identifies that `check_agent_restrictions()` is never called from the push handler. This is a one-line fix but could be addressed either as part of this issue or as a separate PR. Fixing it here is logical since this issue is about comprehensive enforcement, but it increases scope.

---

*Authored-by: egg*
