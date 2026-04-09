### Task Analysis

**Problem statement**: After `restart_phase` or `restart_agent`, new agent containers start with empty worktrees — all prior committed and uncommitted work from the previous iteration is gone. Agents must re-do merges and setup from scratch, wasting quota.

**Source context**: Issue #1597, observed on pipeline `issue-1553-v3` (Kubernetes migration). After 3 restart cycles, the branch had zero coder implementation commits. Related: #1577 (restart capabilities), #1589 (restart on failed pipelines), #1594 (MCP timeout).

**System context**: The spawn flow works as follows:
1. `ConcurrentPhaseExecutor` creates a `spawn_fn` via `container_spawner.create_concurrent_spawn_fn()` which captures `repo_volumes` and `repos` in a closure
2. `spawn_agent_container()` creates per-agent worktrees only when `repo_volumes and repos` is truthy (line 308)
3. The gateway's `WorktreeManager.create_worktree()` is idempotent — if a valid worktree exists at `{worktree_base}/{pipeline_id}-{role}/{repo}/`, it returns the existing one with all committed work intact
4. Worktrees on the host filesystem survive container stop/remove (they're bind-mounted, not Docker volumes)

**Technical root cause**: Both `restart_phase` (`pipelines.py:1595`) and `restart_agent` (`pipelines.py:1277`) call `spawn_agent_container()` / `restart_agent_container()` **without passing `repo_volumes`**. The per-agent worktree creation block at `container_spawner.py:308` checks `if repo_volumes and repos:` — since `repo_volumes` is `None` (the default), this entire block is skipped. The result:
- No call to `gateway.create_worktrees()` — so the existing worktree is never discovered
- `repo_volumes` stays `None` — so no repo bind-mounts are added to the container
- The agent starts with no mounted repository, losing access to all prior work

The initial spawn path works because `create_concurrent_spawn_fn()` (`container_spawner.py:1079`) captures `repo_volumes` from the pipeline setup and passes it through the closure. The restart paths bypass this closure and call `spawn_agent_container` directly, forgetting `repo_volumes`.

**Files affected**:
- `orchestrator/container_spawner.py:308` — Change the guard from `if repo_volumes and repos:` to `if repos:`, so that worktrees are always created when repos are specified. Since `create_worktree` is idempotent, existing worktrees (with committed work) are reused and their host paths populate `repo_volumes` for mounting.

**Risks / edge cases**:
- The `repo_volumes` parameter is never read inside the worktree creation block — it's always overwritten by the gateway result (`repo_volumes = wt_result.worktrees`). So the parameter only served as a truthy flag. Changing to `if repos:` is a safe behavioral change.
- The `worktree_created_this_call` flag (used for cleanup on Docker failure) will now be set even for restart calls. This is correct: during restarts, `preserve_worktree_on_failure=True` prevents cleanup regardless.
- No callers pass `repos` without intending repo access, so no unintended worktree creation.
- Uncommitted work from the previous container is still lost (this is a separate enhancement — the immediate fix preserves all *committed* work).