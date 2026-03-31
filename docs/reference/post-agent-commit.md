# Post-Agent Auto-Commit Reference

When an agent container exits — whether normally, on timeout, or due to a crash — the gateway checks the worktree for uncommitted changes and creates a WIP commit so that work is never silently lost.

Source: `gateway/post_agent_commit.py`

## What It Does

`auto_commit_worktree()` is called from the session cleanup flow in `gateway/session_manager.py`. It:

1. Checks `git status --porcelain` for uncommitted changes
2. If changes exist:
   a. Identifies files to commit vs. files to restore (phase restrictions)
   b. Restores phase-restricted files to their committed state
   c. Filters out container-local symlinks (e.g., the `CLAUDE.md` symlink)
   d. Stages the allowed files with `git add -- <files>`
   e. Creates a commit with message `WIP: auto-commit uncommitted work (<role>) [<pipeline_id>]`
   f. Optionally pushes via the gateway API

If no changes are present, or all changes are filtered out, no commit is created.

## Phase-Restricted File Identification and Restoration

When a `phase` argument is provided, the auto-commit uses `check_phase_file_restrictions()` from `gateway/phase_filter.py` — the same function used at push time — to identify blocked files.

Blocked files are restored via `git checkout -- <file>` before staging. This prevents phase-violating changes (e.g., a coder modifying `.egg-state/contracts/` during the implement phase) from being persisted in the auto-commit.

If the `phase_filter` import fails (e.g., import error), all changed files are treated as allowed (fail-open). Push-time validation remains the authoritative enforcement gate.

Allowed files (those not blocked by phase restrictions) are staged individually (`git add -- <files>`) rather than with `git add -A`, so no unintended files are picked up.

## Symlink Filtering

The sandbox creates a `CLAUDE.md` symlink inside the repository pointing to `~/.claude/CLAUDE.md`. This is a container-local artifact — the symlink target doesn't exist on the host, and committing it would add a broken symlink to the user's repository.

Any symlinks in the changed file list (detected via `os.path.islink()`) are excluded from the auto-commit. This filtering only applies in the auto-commit path. If an agent explicitly stages and commits a symlink via `git commit`, that operation goes through push-time validation, which does not block symlinks.

## Commit Message Format

```
WIP: auto-commit uncommitted work (coder) [issue-123]

Container abc1234567890 exited with uncommitted changes.
This commit preserves the agent's work-in-progress.

Authored-by: egg
```

The author is always `egg <egg@localhost>`.

## Gateway Push

If `session_token` and `gateway_url` are provided, the commit is pushed to the remote branch via the gateway's `/api/v1/git/push` endpoint:

```json
POST /api/v1/git/push
Authorization: Bearer <session_token>
{
  "repo_path": "<worktree_path>",
  "remote": "origin",
  "refspec": "<branch>"
}
```

This routes the push through the gateway so that branch ownership policies and phase restrictions are enforced. If the push fails, the commit remains local-only and a warning is logged. Push failure does not cause the auto-commit itself to fail.

### Protected Branch Safeguard

Auto-commits are never pushed to `main` or `master`. If the worktree's current branch is one of these protected branches (which can happen if worktree setup failed to create an `egg/` branch), the auto-commit is moved to a salvage branch first:

1. A new branch `egg/salvage-<container_id>` is created from the current HEAD.
2. The push proceeds on the salvage branch instead.
3. If creating the salvage branch fails, the push is skipped and the commit remains local-only.

This ensures WIP work is never lost but also can never inadvertently land on main.

## Error Handling

| Error | Behavior |
|-------|----------|
| Worktree path doesn't exist | Skip (logged at DEBUG) |
| `git status` fails | Skip |
| No uncommitted changes | Skip |
| All files filtered by phase restrictions | Skip (logged at INFO) |
| `git add` fails | Skip (logged at WARNING) |
| `git commit` fails | Skip (logged at WARNING) |
| Push fails | Log warning; commit stays local |
| Branch is `main` or `master` | Create `egg/salvage-<container_id>` branch and push there instead |
| Salvage branch creation fails | Skip push; commit stays local |
| Subprocess timeout (30s for git, 30s for push) | Skip (logged at WARNING) |
| Unexpected exception | Skip (logged at WARNING) |

All error cases are non-fatal — the auto-commit is best-effort. The session cleanup flow continues regardless.

## Interaction with Branch Lock and Checkpoints

The auto-commit bypasses git hooks (`--no-verify`, `core.hooksPath=/dev/null`). It does not go through the gateway's commit-time validation; it runs directly in the worktree as the gateway process. The gateway's own branch lock (enforced at push time) is respected when pushing via the gateway API.

The auto-commit creates a standard git commit in the worktree. The checkpoint system (`egg/checkpoints/v2` branch) is independent — it captures session context at the session-end trigger, which may happen around the same time but is a separate process.

## Related: Push-Time Auto-Filtering

The gateway also auto-filters agent pushes at push time (separate from post-agent auto-commit). When an agent pushes commits containing files outside its role scope, the gateway rewrites the commit to exclude disallowed files rather than rejecting the entire push. This uses a similar pattern — `filter_allowed_files()` in `agent_restrictions.py` partitions files, and `_execute_filtered_push()` in `gateway.py` handles the git rewrite. See the [Gateway README](../../gateway/README.md#agent-role-push-auto-filtering) for details.

## Log Events

| Event | Log Level | Fields |
|-------|-----------|--------|
| `post_agent_auto_commit` | INFO | `worktree_path`, `container_id`, `agent_role`, `pipeline_id`, `phase`, `commit_sha`, `allowed_files`, `blocked_files` |
| `post_agent_phase_filter` | INFO | `phase`, `blocked_files`, `allowed_count`, `container_id` |
| `post_agent_symlink_filter` | INFO | `symlink_files`, `container_id` |
| `post_agent_auto_commit_skipped` | INFO | `container_id`, `phase`, `blocked_files`, `symlink_files` |
| `post_agent_auto_push` | INFO | `commit_sha`, `branch`, `container_id` |
| `post_agent_salvage_branch` | INFO | `original_branch`, `salvage_branch`, `container_id` |
| Push via gateway failed | WARNING | `worktree_path`, `error` |
| `git add` failed | WARNING | `worktree_path`, `stderr` |
| Auto-commit failed | WARNING | `worktree_path`, `stderr` |
| Auto-commit timeout | WARNING | `worktree_path`, `container_id` |

## Related Documentation

- [Architecture Overview](../architecture/README.md) — Post-agent auto-commit in the access control overview
- [Concurrent Execution Guide](../guides/concurrent-execution.md) — Agent failure and work preservation
