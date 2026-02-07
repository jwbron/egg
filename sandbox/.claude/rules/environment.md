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
- `gh pr merge`: **Blocked** — human must merge via GitHub UI
- **Protected files**: Certain files or lines may be protected from modification (see below)

## Protected Files

Some files or specific line ranges may be protected from modification. The gateway blocks pushes that touch protected files.

Protected files are configured in `repositories.yaml` under `protected_files`. Common protections include:
- Test coverage configuration (`.coveragerc`, coverage thresholds in `pyproject.toml`)
- CI workflow files (`.github/workflows/*.yml`)
- Critical security code (e.g., merge blocking logic)

If your push is blocked due to protected files:
1. The error message will list which files/lines are protected
2. Do NOT attempt to modify protected files or their thresholds
3. Find an alternative solution that doesn't require modifying protected content
4. If the protection seems incorrect, escalate to human review

## Git Push

Use `git push origin <branch>` (HTTPS). Operations are authenticated by the gateway sidecar.

If push fails:
- Check `git remote -v` is HTTPS
- Check gateway sidecar is running: `curl http://egg-gateway:9847/api/v1/health`
- Ensure branch is egg-owned (egg-prefixed or has your open PR)

## File System

| Path | Purpose |
|------|---------|
| `~/repos/` | Code workspace (RW) - mounted repositories |
| `~/context-sync/` | Confluence/JIRA (RO) |
| `~/sharing/` | Persistent data, notifications, context |

## Services

- PostgreSQL and Redis start automatically

## Network Lockdown Notes

If a tool returns 403 Forbidden, you are likely in private mode. Acknowledge the limitation and proceed with local resources. Package installation and web access are unavailable in private mode — all common dependencies are pre-installed.
