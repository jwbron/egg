# Post-Agent Commit Reference

> **Breaking change (issue #1481):** The auto-commit-and-push behavior has been removed. Uncommitted work now remains in the agent's per-agent worktree and is handled via HITL recovery. See [Migration from auto-commit](#migration-from-auto-commit) below.

## Current Behavior

When an agent container exits, the gateway **no longer** automatically commits and pushes uncommitted changes. Instead:

1. The orchestrator checks if the agent's worktree has uncommitted changes
2. If uncommitted changes exist, a **HITL decision** is created: "Agent `<role>` exited with uncommitted changes in `<N>` files. Recover or discard?"
3. The human decides whether to salvage the work (manually or via a recovery agent) or discard it
4. No unreviewed code ever reaches the branch automatically

**Rationale:** The previous auto-commit behavior caused cascading failures:
- It bypassed BRC consensus — unreviewed work landed on the branch
- It committed files outside the agent's role boundaries, blocking downstream agents' pushes
- WIP commits broke CI and confused reviewers

With per-agent worktree isolation, each agent's uncommitted work persists safely in its own worktree on disk until the pipeline cleans up. For committed-but-unpushed work (pushes wedged by gateway rejection or infra failure), `cleanup_pipeline`, `restart_phase`, and `restart_agent_job` all auto-salvage to `egg/recovered/…` refs before deleting or hard-resetting worktrees — see [Committed but Unpushed: Auto-Salvage](#committed-but-unpushed-auto-salvage) below.

Source: `gateway/post_agent_commit.py`

## HITL Recovery Options

When the orchestrator detects uncommitted work in an agent's worktree after exit:

| Option | What happens |
|--------|-------------|
| **Recover** | A recovery agent or human manually reviews and commits the work |
| **Discard** | The worktree is cleaned up with uncommitted changes discarded |
| **Retry agent** | The agent is respawned and the gateway rediscovers the existing worktree (including all committed work on the branch); uncommitted changes from the old container are not carried over |

## Worktree Lifecycle

Per-agent worktrees persist after container exit (until pipeline cleanup). Uncommitted work is not lost — it sits in the worktree on disk. This is a direct benefit of per-agent worktree isolation: each agent's worktree is independent, so cleanup of one agent's worktree cannot affect another agent's work.

## Committed but Unpushed: Auto-Salvage

A different failure class exists when an agent **commits** work locally but its **pushes** to the remote are wedged — gateway branch-allowlist rejection from a wrong-branch spawn-time env var, transient infra failure, or restart-reconciliation marking a still-running pipeline `failed`. In these cases the commits sit on the local `egg/{worktree_id}/work` branch and are lost when `cleanup_pipeline` deletes the worktree.

Since [#2438](https://github.com/jwbron/egg/pull/2438), `cleanup_pipeline` automatically calls `auto_salvage_pipeline` before deleting any worktree. Since [#2526](https://github.com/jwbron/egg/pull/2526), `restart_phase` does the same — making phase restart (the scenario most likely to produce wedged pushes) a safe worktree-deletion path. Since [#2855](https://github.com/jwbron/egg/pull/2855), **agent restart** (`restart_agent_job`) also calls `auto_salvage_pipeline`, with `salvage_uncommitted=True` — so uncommitted edits present at restart time are committed to a synthetic `[salvage] pre-crash working-tree state` snapshot before the recovery push, extending salvage coverage to the mid-`Edit` crash window where work exists only in the dirty working tree. Every per-agent worktree with local commits not reachable from `origin/<assigned_branch>` is pushed to a recovery ref:

```
egg/recovered/<pipeline_id>/<scope>/<short_sha>
```

`<scope>` is the worktree's stable scope label: `pipeline` for pipeline-scoped worktrees, `<agent_role>` for role-scoped worktrees (e.g. `coder`), or `<slice_id>-<agent_role>` for slice-scoped worktrees (e.g. `slice-2-coder`). The `<short_sha>` is the first 12 chars of the HEAD SHA at salvage time, so re-running salvage produces a fresh ref when the agent has new commits, rather than force-overwriting the earlier one (a re-run with an unchanged HEAD pushes to the same ref name as a no-op fast-forward). Recovery refs are never deleted by the orchestrator — they outlive the pipeline.

> **Note on assigned-branch reachability.** When `origin/<assigned_branch>` is not reachable (e.g. the branch was never pushed), the enumeration falls back to `origin/<base_branch>`, then to a HEAD-only cap of 200 commits — so the salvage report is best-effort, not strictly the diff against the assigned branch.

**To locate all salvaged commits for a pipeline:**

```bash
git ls-remote origin 'egg/recovered/<pipeline>/*'
```

**To replay onto a recovery branch:**

```bash
git fetch origin egg/recovered/<pipeline>/<scope>/<sha>
git cherry-pick <sha>
```

### Operator MCP Tools

Two MCP tools let operators triage and trigger salvage manually, before or instead of waiting for cleanup:

| Tool | Mutates? | Purpose |
|------|----------|---------|
| `list_agent_local_commits` | No | List unpushed commits in every per-agent worktree for a pipeline. Scoped by `agent_role` and/or `slice_id`. |
| `salvage_agent_commits` | Yes (pushes to origin) | Push unpushed commits to `egg/recovered/...` refs using orchestrator launcher auth, which bypasses the agent-targeted allowlist that rejected the original push. |

**Example — triage before cleanup** (called from a sandboxed agent or operator MCP client; the MCP transport handles tool registration):

```python
# Check what would be lost
commits = await mcp.call_tool("list_agent_local_commits", {"task_id": "issue-2261-v9"})
# commits["worktrees"] — per-worktree breakdown with commit list

# Salvage all worktrees for the pipeline
result = await mcp.call_tool("salvage_agent_commits", {"task_id": "issue-2261-v9"})
# result["recovery_refs"] — list of pushed egg/recovered/... refs
```

Scope to a single role:

```python
await mcp.call_tool("list_agent_local_commits", {
    "task_id": "issue-2261-v9",
    "agent_role": "coder",
    "slice_id": "slice-2",
})
```

## Migration from Auto-Commit

### What changed

| Before (auto-commit) | After (HITL recovery) |
|----------------------|----------------------|
| Uncommitted changes auto-committed on exit | Uncommitted changes remain in worktree |
| WIP commits pushed to branch automatically | No automatic push; HITL decides |
| Phase-restricted files filtered before commit | No filtering needed — nothing is auto-committed |
| Symlink filtering applied | No filtering needed |
| `egg/salvage-<id>` branches created for main | `egg/recovered/<pipeline>/...` refs auto-created by `cleanup_pipeline` or `restart_phase` for committed-but-unpushed work |

### What operators should know

- **No more WIP commits on branches.** If you previously relied on WIP commits to inspect what an agent was working on before it exited, check the agent's worktree directly instead.
- **HITL decisions for uncommitted work.** You will see new HITL decisions when agents exit with uncommitted changes. These require human action to resolve.
- **No impact on normal flow.** Agents that commit and push their own work before exiting (the intended pattern) are unaffected.

## Legacy Behavior Reference

The previous `auto_commit_worktree()` function (removed) performed:

1. `git status --porcelain` to detect uncommitted changes
2. Phase-restricted file identification and restoration via `check_phase_file_restrictions()`
3. Symlink filtering (excluding container-local `CLAUDE.md` symlinks)
4. Staging allowed files with `git add -- <files>`
5. Creating a commit: `WIP: auto-commit uncommitted work (<role>) [<pipeline_id>]`
6. Optional push via gateway API with protected branch safeguard

This behavior was removed because per-agent worktree isolation and the BRC consensus protocol make it unnecessary and harmful.

## Related Documentation

- [Git Isolation Architecture](../architecture/git-isolation.md) — Per-agent worktree isolation
- [Concurrent Execution Guide](../guides/concurrent-execution.md) — BRC consensus and per-agent worktrees
- [Architecture Overview](../architecture/README.md) — Post-agent commit in the access control overview
