# Sandboxed Environment

You run in a sandboxed Docker container with network lockdown. No SSH keys, cloud creds, or production access.

## Network Modes

Network traffic is routed through a filtering proxy. The mode is selected via CLI flags when starting egg:
- `egg` or `egg --public` → Public mode (default)
- `egg --private` → Private mode

### Public Mode (`PRIVATE_MODE=false`, default)
Full internet access + public repos only.

In this mode:
- Web search and fetch work normally
- You CAN access PyPI, npm, and package registries
- You CAN access arbitrary URLs
- You CANNOT access private repositories (only public repos allowed)

### Private Mode (`PRIVATE_MODE=true`)
Network locked down (Anthropic API only) + private repos only.

In this mode:
- Only `api.anthropic.com` (Claude API) is allowed through the proxy
- You CANNOT access PyPI, npm, or any package registry (dependencies are pre-installed)
- You CANNOT use web search or fetch arbitrary URLs
- You CAN access private repositories
- You CANNOT access public repositories

**GitHub access** MUST go through the gateway sidecar's git/gh wrappers (not through the proxy). This ensures policy enforcement (branch ownership, merge blocking, etc.) cannot be bypassed.

## Capabilities

**CAN**: Read/edit `~/repos/`, run tests, `git push` (HTTPS), `gh` CLI (PRs, issues), PostgreSQL, Redis, Python, Node.js, Go, Java

**CANNOT**: Merge PRs, SSH push, deploy to GCP/AWS, access production, access GitHub tokens directly

## Gateway Sidecar

All git/gh operations are routed through the gateway sidecar. You do NOT have direct access to GitHub tokens — credentials are held by the gateway.

Key restrictions enforced by the gateway:
- `git push`: Only to branches you own (`egg/` or `egg-` prefixed, or has your open PR)
- `git worktree add/remove`: **Unsupported** — use `git checkout -b` instead
- `git checkout/switch` (branch): **Blocked in pipeline mode** — you are locked to your worktree branch
- `git commit`: **Phase-validated** — staged files must comply with phase restrictions
- `gh pr merge`: **Blocked** — human must merge via GitHub UI

## Git Push

Use `git push origin <branch>` (HTTPS). Operations are authenticated by the gateway sidecar.

If push fails:
- Check `git remote -v` is HTTPS
- Check gateway sidecar is running: `curl http://egg-gateway:9848/api/v1/health`
  (Port 9848 is defined in shared/egg_config/constants.py)
- Ensure branch is egg-owned (egg-prefixed or has your open PR)

## File System

| Path | Purpose |
|------|---------|
| `~/repos/` | Code workspace (RW) - mounted repositories |
| `~/repos/<repo>/.egg-state/` | SDLC pipeline state (may be readonly in implement phase) |
| `~/context-sync/` | Confluence/JIRA (RO) |
| `~/sharing/` | Persistent data, notifications, context |

**Pipeline readonly directories:** During the implement phase, `.egg-state/drafts/`, `.egg-state/contracts/`, `.egg-state/pipelines/`, and `.egg-state/reviews/` are mounted readonly. Check for `.egg-readonly` marker files to understand restrictions. Attempting to write to these directories will produce an EROFS (read-only filesystem) error.

**Post-agent auto-commit:** When your container exits, any uncommitted changes are automatically committed and pushed by the gateway. Phase-restricted files are restored (not committed). You do not need to worry about losing work if you time out.

## Services

- PostgreSQL and Redis start automatically

## Network Lockdown Notes

If a tool returns 403 Forbidden, you are likely in private mode. Acknowledge the limitation and proceed with local resources. Package installation and web access are unavailable in private mode — all common dependencies are pre-installed.
