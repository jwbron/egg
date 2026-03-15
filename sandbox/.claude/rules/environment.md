# Sandboxed Environment

Sandboxed Docker container. No SSH keys, cloud creds, or production access.

## Network Modes

- `egg --public` (default): Full internet + public repos only. Can access PyPI, npm, web.
- `egg --private`: Anthropic API only + private repos only. No PyPI/npm/web access — dependencies are pre-installed.

GitHub access MUST go through the gateway sidecar (not the proxy) for policy enforcement.

## Capabilities

**CAN**: Read/edit `~/repos/`, run tests, `git push` (HTTPS), `gh` CLI, PostgreSQL, Redis, Python, Node.js, Go, Java

**CANNOT**: Merge PRs, SSH push, deploy, access production, access GitHub tokens directly

## Gateway Sidecar

All git/gh operations routed through gateway. Key restrictions:
- `git push`: Only to `egg/`-prefixed branches (or branches with your open PR)
- `git worktree add/remove`: **Unsupported** — use `git checkout -b` instead
- `git checkout/switch` (branch): **Blocked in pipeline mode**
- `git commit`: **Phase-validated** — staged files must comply with phase restrictions
- `gh pr merge`: **Blocked** — human must merge via GitHub UI

If push fails: check `git remote -v` is HTTPS, check `curl http://egg-gateway:9848/api/v1/health`, verify branch is egg-owned.

## File System

| Path | Purpose |
|------|---------|
| `~/repos/` | Code workspace (RW) — NOT a git repo itself |
| `~/repos/<repo>/.egg-state/` | SDLC pipeline state (may be readonly in implement phase) |
| `~/context-sync/` | Confluence/JIRA (RO) |
| `~/sharing/` | Persistent data, notifications, context |

**Pipeline readonly directories:** During the implement phase, `.egg-state/drafts/`, `.egg-state/contracts/`, `.egg-state/pipelines/`, and `.egg-state/reviews/` are mounted readonly. Check for `.egg-readonly` marker files to understand restrictions. Attempting to write to these directories will produce an EROFS (read-only filesystem) error.

**Post-agent auto-commit**: Uncommitted changes are auto-committed on container exit. Phase-restricted files are restored.

## Services

- PostgreSQL and Redis start automatically

## Shell Command Safety

**Scope all filesystem operations to `~/repos/` or `$EGG_REPO_PATH`.** Never search from `/` — it will be killed by timeout.

**DO**:
```bash
grep -rn "pattern" ~/repos/
find ~/repos/ -name "*.py" -exec grep -l "pattern" {} \;
```

**DON'T**:
```bash
grep -rn "pattern" /          # Scans entire filesystem — will be killed after 120s
find / -name "*.py"           # Same problem — unbounded search
```

**On push failure**: Report via `egg-orch signal error --error "Push failed: <msg>" --recoverable`. Do NOT push to a different branch name.

If a tool returns 403 Forbidden, you are likely in private mode. Proceed with local resources.
