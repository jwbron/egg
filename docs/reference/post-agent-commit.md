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

With per-agent worktree isolation, each agent's uncommitted work persists safely in its own worktree on disk until the pipeline cleans up. There is no risk of losing work silently.

Source: `gateway/post_agent_commit.py`

## HITL Recovery Options

When the orchestrator detects uncommitted work in an agent's worktree after exit:

| Option | What happens |
|--------|-------------|
| **Recover** | A recovery agent or human manually reviews and commits the work |
| **Discard** | The worktree is cleaned up with uncommitted changes discarded |
| **Retry agent** | The agent is respawned in a new worktree; uncommitted work from the old worktree is not carried over |

## Worktree Lifecycle

Per-agent worktrees persist after container exit (until pipeline cleanup). The uncommitted work is not lost — it sits in the worktree on disk. This is a direct benefit of per-agent worktree isolation: each agent's worktree is independent, so cleanup of one agent's worktree cannot affect another agent's work.

## Migration from Auto-Commit

### What changed

| Before (auto-commit) | After (HITL recovery) |
|----------------------|----------------------|
| Uncommitted changes auto-committed on exit | Uncommitted changes remain in worktree |
| WIP commits pushed to branch automatically | No automatic push; HITL decides |
| Phase-restricted files filtered before commit | No filtering needed — nothing is auto-committed |
| Symlink filtering applied | No filtering needed |
| `egg/salvage-<id>` branches created for main | No salvage branches needed |

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
