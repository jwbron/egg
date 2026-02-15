# Analysis: Enforce phase file restrictions via readonly mounts and commit-time validation

> Issue: #644 | Phase: refine

## Problem Statement

The phase file restriction system (`gateway/phase_filter.py`) currently enforces file restrictions **only at `git push` time** via the gateway. An agent can modify protected files locally in its worktree, and if any orchestrator component reads from the worktree (rather than committed/pushed state), those unauthorized modifications take effect. This is a trust boundary violation.

The immediate motivating scenario is #641 (Docker-in-Docker deployment validation), where the orchestrator would run `docker-compose up` against the worktree. An agent in the `refine` phase could modify `docker-compose.yml` to add `privileged: true`, mount the Docker socket, or change entrypoints -- without ever pushing. The orchestrator would trust the worktree state.

More broadly, any orchestrator operation that reads from an agent's worktree is vulnerable to local-only modifications that bypass phase restrictions.

## Current Behavior

### Push-time enforcement (existing, working)

When an agent runs `git push`, the gateway:
1. Calls `get_changed_files_in_push()` to enumerate files being pushed (`gateway/git_client.py:957-1059`)
2. Checks role-based restrictions via `check_file_restrictions()` (`gateway/gateway.py:660-684`)
3. Checks phase-based restrictions via `check_phase_file_restrictions()` (`gateway/gateway.py:720-754`)
4. Blocks the push if any file violates restrictions

### Phase file restriction definitions (`phase_filter.py:480-525`)

| Phase | Policy |
|-------|--------|
| refine | `allowed_patterns`: `.egg-state/contracts/*`, `.egg-state/drafts/*analysis*`, `.egg-state/checkpoints/*`, `.egg-state/agent-outputs/*`, `.egg-state/reviews/*` |
| plan | `allowed_patterns`: `.egg-state/contracts/*`, `.egg-state/drafts/*plan*`, `.egg-state/checkpoints/*`, `.egg-state/agent-outputs/*`, `.egg-state/reviews/*` |
| implement | `blocked_patterns`: `.egg-state/contracts/*`, `.egg-state/drafts/*`, `.egg-state/pipelines/*`, `.egg-state/reviews/*` |
| pr | `allowed_patterns`: `*` (everything) |

### Gap: no enforcement for local worktree modifications

The enforcement chain is: `agent modifies locally -> agent pushes -> gateway blocks`. If the agent never pushes but the orchestrator reads from the same worktree, the chain breaks.

### Container spawning and mounts (`orchestrator/container_spawner.py:265-287`)

The mount assembly in `spawn_agent_container()` currently creates:
- Bind mounts for repo volumes at `/home/egg/repos/<name>`
- `.git` shadow mounts (via `git_shadow_mounts()` in `shared/egg_container/__init__.py:71-125`)
- Optional certs volume mount

The `phase` parameter is already passed to the spawner but only forwarded to the gateway session registration. It is **not** used for mount configuration.

### Commit-time behavior (`gateway/gateway.py:944-1119`)

The `git_execute()` endpoint handles `commit` operations. It validates the operation against `GIT_ALLOWED_COMMANDS`, validates args, maps the container path to the worktree path, and executes. There are **no file restriction checks** at commit time -- only push time gets file checks.

## Constraints

- **Docker mount ordering**: Docker supports nested mounts where inner (more specific) mounts override outer mounts. This is required for the "readonly repo + writable overlays" strategy in refine/plan phases.
- **Directory existence**: Docker bind mounts require the source path to exist on the host. `.egg-state/` subdirectories must be created before container spawn.
- **Phase immutability**: Each phase runs in a new container (`container_spawner.py`), so mounts set at container creation time are sufficient -- no dynamic remounting needed.
- **Performance**: Readonly mounts are a kernel-level enforcement with zero runtime cost. Commit-time validation adds a `git diff --cached --name-only` call per commit.
- **Backwards compatibility**: The `PhaseFileRestriction` data is already defined in `phase_filter.py` and would be reused for mount generation. No new restriction definitions needed.
- **Security posture**: The system follows a fail-closed pattern (see `phase_filter.py:622-628` for unknown phases, `git_client.py:1043-1054` for diff failures). New enforcement layers must maintain this.

## Options Considered

### Option A: Readonly mounts + commit-time validation (issue proposal)

**Approach**: Three-layer defense:
1. **Readonly filesystem mounts** (primary) -- mount protected paths as readonly in the container at spawn time
2. **Commit-time gateway validation** (secondary) -- validate staged files against phase restrictions before allowing `git commit`
3. **Push-time validation** (existing) -- keep existing push-time enforcement as final layer

Mount strategy:
- **refine/plan**: Mount entire repo readonly, overlay writable mounts for allowed `.egg-state/` subdirs
- **implement**: Mount specific `.egg-state/` subdirs (contracts, drafts, pipelines, reviews) readonly; source code stays writable
- **pr**: No readonly mounts

For commit-time validation, add a pre-commit check in `git_execute()` that runs `git diff --cached --name-only` in the worktree and applies `PhaseFileRestriction` checks before executing `git commit`.

**Pros**:
- Defense in depth: OS-level enforcement (mounts) + application-level (gateway commit check) + existing push check
- Readonly mounts are the strongest possible enforcement -- agents cannot bypass them from userspace
- Reuses existing `PhaseFileRestriction` definitions for both mount generation and commit validation
- Phase is already available in the container spawner (`phase` parameter)
- `MountSpec` already supports `readonly=True` and `to_dockerpy_kwargs` already handles `ReadOnly` in mount dicts

**Cons**:
- Readonly mounts can't express fine-grained glob patterns (e.g., `.egg-state/drafts/*analysis*` requires the entire `drafts/` dir to be writable or readonly, not pattern-matched subsets)
- Adds complexity to mount assembly -- nested mounts require careful ordering
- `.egg-state/` subdirectories must exist before container creation (requires a pre-spawn `mkdir -p`)
- Need `.egg-readonly` marker files for agent UX when they hit "Read-only file system" errors

### Option B: Commit-time validation only (no readonly mounts)

**Approach**: Add staged-file validation in `git_execute()` for commit operations. Skip readonly mounts entirely. Rely on gateway interception at commit and push time.

**Pros**:
- Simpler implementation -- one new code path in `git_execute()` instead of mount generation + commit validation
- No mount ordering complexity
- No pre-spawn directory creation needed
- Fine-grained pattern matching works naturally (reuses `PhaseFileRestriction.is_file_allowed()`)

**Cons**:
- Does not prevent local file modification, only prevents committing. Agent can still modify files and the orchestrator may read them.
- Does not close the trust boundary gap described in the issue -- the core problem is orchestrator reading from the worktree, not committing
- A second gateway vulnerability (e.g., bypass of git_execute) would leave no enforcement

### Option C: Orchestrator reads only from committed state (no worktree reads)

**Approach**: Instead of hardening the worktree, change orchestrator operations to only read from committed/pushed state (e.g., `git show HEAD:path` or `git archive`). The orchestrator would never read directly from the filesystem.

**Pros**:
- Eliminates the trust boundary violation at its root -- if the orchestrator never reads the worktree, local modifications don't matter
- No mount changes needed
- No commit-time validation needed

**Cons**:
- Not always feasible -- some orchestrator operations (e.g., #641 Docker-in-Docker `docker-compose up`) inherently need filesystem paths
- Requires auditing and changing every orchestrator component that reads from worktrees
- Future orchestrator features would need to maintain this discipline, creating an ongoing maintenance burden
- Does not protect against non-orchestrator worktree readers (e.g., shared volume mounts)

### Option D: Readonly mounts only (no commit-time validation)

**Approach**: Implement readonly mounts (same as Option A Layer 1) but skip commit-time validation. Rely on readonly mounts + existing push-time validation.

**Pros**:
- Simpler than Option A (two layers instead of three)
- OS-level enforcement is the strongest protection
- Less gateway code to maintain

**Cons**:
- Readonly mounts can't enforce fine-grained patterns like `.egg-state/drafts/*analysis*` -- the entire `drafts/` dir must be writable or readonly for a given phase
- Without commit-time validation, agents can commit files to allowed directories that should be pattern-restricted (e.g., committing a plan file during refine phase when only analysis files are allowed in drafts)
- The fine-grained pattern enforcement only kicks in at push time, and the gap between commit and push is a window where invalid state exists

## Recommended Approach

**Option A (Readonly mounts + commit-time validation)** is recommended. The issue author's three-layer proposal is well-designed and matches the codebase's existing defense-in-depth patterns:

1. **Readonly mounts** close the primary trust boundary gap -- the orchestrator can safely read from the worktree because the OS prevents unauthorized modifications. This is the only option that fully addresses the motivating scenario (#641).

2. **Commit-time validation** fills the granularity gap that readonly mounts can't cover. Since mounts operate at the directory level but restrictions use glob patterns (e.g., `*analysis*`), commit-time checks catch violations that mounts can't express.

3. **Existing push-time validation** remains as the final safety net.

The implementation touches five files as identified in the issue, with the mount generation in `shared/egg_container/__init__.py` being the most architecturally significant change (new `phase_readonly_mounts()` alongside existing `git_shadow_mounts()`).

Key implementation considerations:
- The `MountSpec` dataclass and `to_dockerpy_kwargs` already support readonly mounts, reducing new code
- Pre-spawn `mkdir -p` for `.egg-state/` subdirectories should be added to worktree setup (gateway worktree creation) rather than container_spawner to keep the spawner stateless
- `.egg-readonly` marker files should be brief and include the `EGG_PHASE` env var reference so agents can self-diagnose

## Open Questions

1. **Granularity trade-off for readonly mounts in refine/plan**: The refine phase allows `drafts/*analysis*` but blocks other drafts. Readonly mounts can only make the entire `drafts/` directory writable or readonly. Should we:
   - Make `drafts/` writable in refine/plan (rely on commit-time + push-time for pattern enforcement)?
   - Make `drafts/` readonly and have the agent use the contract API to write drafts (requires new API)?

2. **Orchestrator worktree reads before #641**: Are there any current orchestrator operations that read from agent worktrees? If not, this is a preventive measure for #641 and the urgency is lower. If there are existing worktree reads, those are currently vulnerable.

3. **`add` operation validation**: Should we also validate at `git add` time (not just commit)? This would give earlier feedback but the gateway `git_execute` handler would need `git diff --cached --name-only` after the add operation completes to see what was staged. Commit-time is more natural since that's when the change is recorded, but add-time gives earlier UX feedback.

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity: high
```
