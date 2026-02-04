# Sandboxed Environment

You run in a sandboxed Docker container with network lockdown. No SSH keys, cloud creds, or production access.

## Network Modes

Network traffic is routed through a filtering proxy. The mode is selected via CLI flags when starting jib:
- `jib` or `jib --public` → Public mode (default)
- `jib --private` → Private mode

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

All git/gh operations are routed through the gateway sidecar (runs as `egg-gateway` container on the egg-isolated network). The container does NOT have direct access to GitHub tokens - credentials are held by the gateway.

**Policy enforcement:**
- `git push`: Only to branches you own (jib-prefixed or has your open PR)
- `git fetch/pull/ls-remote`: Routed through gateway for authentication
- `git remote update`: Converted to `fetch --all` via gateway
- `gh pr merge`: **Blocked** - human must merge via GitHub UI
- `gh pr comment/edit/close`: Only on PRs you authored

## Git/Gh Binary Redirection

Both `/usr/bin/git` and `/usr/bin/gh` are symlinked to the gateway wrappers. All invocations (whether `git` or `/usr/bin/git`) route through the gateway sidecar for policy enforcement. The real binaries are relocated to a hidden path.

## Git Push

Use `git push origin <branch>` (HTTPS). Operations are authenticated by the gateway sidecar.

If push fails:
- Check `git remote -v` is HTTPS
- Check gateway sidecar is running: `curl http://egg-gateway:9847/api/v1/health`
- Ensure branch is jib-owned (jib-prefixed or has your open PR)

## File System

| Path | Purpose |
|------|---------|
| `~/repos/` | Code workspace (RW) - mounted repositories |
| `~/context-sync/` | Confluence/JIRA (RO) |
| `~/sharing/` | Persistent data, notifications, context |
| `~/beads/` | Task memory |

## Available Commands

- `discover-tests`, `@load-context`, `@save-context`, `@create-pr`
- PostgreSQL and Redis start automatically

## Working with Network Lockdown

1. **Web search/fetch will fail in private mode** - Use local codebase search instead
2. **Package installation fails in private mode** - All common dependencies are pre-installed; if you need a package that's missing, note it in your PR description
3. **External URLs blocked in private mode** - Claude API works; GitHub access goes through gateway; everything else returns HTTP 403

If a tool returns 403 Forbidden, acknowledge the limitation and proceed with local resources.
