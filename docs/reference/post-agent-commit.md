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

With per-agent worktree isolation, each agent's uncommitted work survives the container's exit — it sits in its own worktree on disk. It does **not** survive the next respawn: worktree re-attach hard-resets the tree (see [Worktree Lifecycle](#worktree-lifecycle)). Since [#3639](https://github.com/jwbron/egg/issues/3639) that reset snapshots the dirty tree to a recovery ref first, so the work is recoverable rather than lost — best-effort, and never at the cost of blocking the reset; see [Unpushed Work: Auto-Salvage](#unpushed-work-auto-salvage) below for the cases where the snapshot yields nothing. For committed-but-unpushed work (pushes wedged by gateway rejection or infra failure), `cleanup_pipeline`, `restart_phase`, the refine-redirect restart (`_restart_refine_phase`), and worktree re-attach's dirty-discard reset all auto-salvage to `egg/recovered/…` refs before deleting or hard-resetting worktrees.

Source: `gateway/post_agent_commit.py`

## HITL Recovery Options

When the orchestrator detects uncommitted work in an agent's worktree after exit:

| Option | What happens |
|--------|-------------|
| **Recover** | A recovery agent or human manually reviews and commits the work |
| **Discard** | The worktree is cleaned up with uncommitted changes discarded |
| **Retry agent** | The agent is respawned and the gateway rediscovers the existing worktree (including all committed work on the branch); uncommitted changes from the old container are not carried over into the respawned worktree — but since [#3639](https://github.com/jwbron/egg/issues/3639) they are no longer discarded outright either: the respawn's worktree re-attach snapshots the dirty tree to an `egg/recovered/…` ref before its hard reset. That is the only mechanism on this path — since [#3164](https://github.com/jwbron/egg/issues/3164) the `restart_agent` route spawns no pod itself, so the [#2855](https://github.com/jwbron/egg/pull/2855) restart-time snapshot never runs here. Best-effort, with preconditions — see [Unpushed Work: Auto-Salvage](#unpushed-work-auto-salvage) |

## Worktree Lifecycle

Per-agent worktrees persist after container exit (until pipeline cleanup). Uncommitted work survives the container's exit — it sits in the worktree on disk — but it does **not** survive the next respawn: under the event-pump one-shot model respawn is the normal lifecycle, and worktree re-attach's R6 dirty-discard reset (`_clean_reused_worktree`) hard-resets the tree. Since [#3639](https://github.com/jwbron/egg/issues/3639) that reset snapshots the dirty tree to a `[salvage] pre-reset working-tree state` commit first, so the work is recoverable from an `egg/recovered/…` ref rather than lost — best-effort, see [Unpushed Work: Auto-Salvage](#unpushed-work-auto-salvage) below for when it doesn't fire.

Worktree isolation still holds independently of that: each agent's worktree is independent, so cleanup of one agent's worktree cannot affect another agent's work.

## Unpushed Work: Auto-Salvage

A different failure class exists when an agent **commits** work locally but its **pushes** to the remote are wedged — gateway branch-allowlist rejection from a wrong-branch spawn-time env var, transient infra failure, or restart-reconciliation marking a still-running pipeline `failed`. In these cases the commits sit on the local `egg/{worktree_id}/work` branch and are lost when `cleanup_pipeline` deletes the worktree.

Since [#2438](https://github.com/jwbron/egg/pull/2438), `cleanup_pipeline` automatically calls `auto_salvage_pipeline` before deleting any worktree. Since [#2526](https://github.com/jwbron/egg/pull/2526), `restart_phase` does the same — making phase restart (the scenario most likely to produce wedged pushes) a safe worktree-deletion path. `_restart_refine_phase` (the in-process refine re-run an operator triggers by adopting a first-principles redirect) carries the same hook before deleting the refine worktrees. [#2855](https://github.com/jwbron/egg/pull/2855) added it to **agent restart** (`restart_agent_job`) as well, there with `salvage_uncommitted=True` — uncommitted edits present at restart time are committed to a synthetic `[salvage] pre-crash working-tree state` snapshot before the recovery push. **That path has no live caller today:** since [#3164](https://github.com/jwbron/egg/issues/3164) moved respawn ownership to the orchestrator event loop, the `restart_agent` route only kills the role's one-shot Job and resets consensus, and `restart_agent_job` is reached from tests alone. In production the uncommitted half of an operator-triggered agent restart is therefore covered by the worktree re-attach path below, not by `restart_agent_job`. Since [#3509](https://github.com/jwbron/egg/issues/3509), **worktree re-attach**'s dirty-discard reset (the R6 `_clean_reused_worktree` path) also auto-salvages: any commits its `git reset --hard` is about to discard are pushed to a recovery ref first via `salvage_discarded_tip`, and the discarded tip + recovery ref are durably recorded as a message-bus system message so a resuming agent can find and resume its prior work instead of re-deriving it. Since [#3639](https://github.com/jwbron/egg/issues/3639), that path also snapshots the dirty working tree itself *before* the reset — `_clean_reused_worktree` stages and commits it as `[salvage] pre-reset working-tree state (#3639)` — so a session that worked for hours without committing is salvageable too, not just its prior commits; without that snapshot the orphan-commit detector above had nothing to find and the uncommitted work was lost outright. The snapshot is best-effort and never blocks the reset, so it is not a guarantee. Among the ways it yields nothing: it is taken only when the worktree has an assigned `branch` (with no branch there is no origin tip to sync to and no salvage target, so the dirty tree is discarded and only a WARNING with the file count is emitted); it only reaches an `egg/recovered/…` ref when the re-attach has pipeline context — without `pipeline_id` the salvage records `no pipeline context (legacy caller)` and the snapshot commit survives only in the local object store until gc; the staging-and-commit step itself can fail, in which case a WARNING says the hard reset *will* discard the work and the reset proceeds; and the recovery-ref push can fail, leaving `salvage_error` set and no ref — including on a gateway-mode mismatch, which the code calls out as the exact silent-loss class the hook exists to prevent. A snapshot that *is* taken may also be **truncated**: when `git add -A` did not complete cleanly the commit message body ends with an ``INCOMPLETE: `git add -A` `` paragraph (the token leads that paragraph, not the subject line) — see [Agent Recovery → Recovery Workflow](agent-recovery.md#recovery-workflow) for the triage query that finds those across both snapshot paths. Every per-agent worktree with local commits not reachable from `origin/<assigned_branch>` is pushed to a recovery ref:

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
| Uncommitted changes auto-committed on exit | Uncommitted changes remain in the worktree until the next respawn, whose worktree re-attach salvages them before hard-resetting — see above |
| WIP commits pushed to branch automatically | No automatic push; HITL decides |
| Phase-restricted files filtered before commit | No filtering needed — nothing is auto-committed |
| Symlink filtering applied | No filtering needed |
| `egg/salvage-<id>` branches created for main | `egg/recovered/<pipeline>/...` refs auto-created by `cleanup_pipeline`, `restart_phase`, `_restart_refine_phase`, or worktree re-attach's dirty-discard reset for committed-but-unpushed work. **Uncommitted** work is additionally captured on **worktree re-attach only** ([#3639](https://github.com/jwbron/egg/issues/3639)); the other three deliberately leave it — they use `salvage_worktree`'s `salvage_uncommitted=False` default, to avoid turning routine untracked build artifacts into recovery refs. (`restart_agent_job` also passes `salvage_uncommitted=True` per [#2855](https://github.com/jwbron/egg/pull/2855), but has had no live caller since [#3164](https://github.com/jwbron/egg/issues/3164) — see above) |

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
