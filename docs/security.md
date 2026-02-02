# Security Model

This document describes the security architecture of the egg sandbox environment.

## Core Principle

**Security through infrastructure, not instructions.**

An LLM cannot bypass controls that don't exist in its environment. Behavioral controls (instructions telling an agent what not to do) can be bypassed through prompt injection, model drift, or adversarial inputs. Security must be enforced at the infrastructure level.

## Threat Model

### Threats Addressed

| Threat | Mitigation |
|--------|------------|
| Agent accesses another agent's workspace | Filesystem isolation - other workspaces don't exist in container's view |
| Agent pushes to unauthorized branches | Gateway enforces branch ownership policy |
| Agent pushes malicious code directly to main | Gateway blocks direct pushes to protected branches; PRs require human review |
| Agent discovers or exfiltrates credentials | Credentials only exist in gateway; container never sees them |
| Agent modifies git config to bypass security | Container has no access to git metadata; config is gateway-controlled |
| Agent escapes via git hooks or filters | No hooks/filters in container; only gateway executes git commands |
| Crashed container corrupts shared state | Gateway cleans up orphaned workspaces on startup |
| Credential theft from container | Credentials never enter container |
| Data exfiltration via web tools | WebSearch/WebFetch filtered at gateway in private mode |

### Explicit Non-Goals

This architecture does NOT protect against:
- Malicious agents with root access to the container (defense in depth via container sandboxing)
- Network-level attacks between containers (addressed by network policies)
- Compromise of the gateway itself (gateway runs with minimal attack surface)

## Security Architecture

### Two-Container Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              egg                                            │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        Gateway Container                             │   │
│  │                                                                      │   │
│  │  - REST API server           - Credential storage                   │   │
│  │  - Policy enforcement        - Git command execution                │   │
│  │  - Session management        - HTTP proxy (Squid)                   │   │
│  │  - Audit logging             - Anthropic API proxy                  │   │
│  │                                                                      │   │
│  │  HAS: GitHub tokens, API keys, full network access                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                      ▲                                      │
│                                      │ HTTP API                             │
│                                      │                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                       Sandbox Container                              │   │
│  │                                                                      │   │
│  │  - git/gh wrappers           - Workspace files                      │   │
│  │  - LLM CLI (Claude)          - Development tools                    │   │
│  │                                                                      │   │
│  │  NO: GitHub tokens, SSH keys, API keys, direct network access       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Credential Isolation

The sandbox container **never** has direct access to credentials:

1. **Git credentials**: All git/gh operations go through the gateway, which injects authentication
2. **API credentials**: Anthropic API calls route through gateway via `ANTHROPIC_BASE_URL`
3. **No environment leakage**: Container environment is sanitized of any credential variables

#### Anthropic API Credential Flow

```
┌─────────────────┐   ANTHROPIC_BASE_URL    ┌─────────────────────┐
│    Sandbox      │ ───────────────────────▶│     Gateway         │
│   Container     │   http://gateway:8080   │   Auth Proxy        │
│                 │   (no credentials)      │                     │
│  Claude Code    │                         │  1. Receive request │
│  (no API key)   │                         │  2. Inject creds    │
│                 │                         │  3. Forward to API  │──▶ api.anthropic.com
└─────────────────┘                         └─────────────────────┘
```

### Git Metadata Isolation

Containers have no access to git metadata. The `.git` directory is shadowed by an empty tmpfs:

```
Container filesystem view:
/home/sandbox/repos/my-repo/
├── src/                 ← Agent can edit these files
├── tests/               ← Agent can edit these files
├── README.md            ← Agent can edit this file
└── .git/                ← Empty directory (tmpfs shadow)
```

Without git metadata, the agent cannot:
- Discover where the repository came from
- See commit history directly
- Modify the staging area directly
- Change branch pointers
- Execute git hooks
- Access other worktrees

### Gateway as Security Boundary

All git operations that require metadata access go through the gateway:

1. Agent runs `git status` → invokes git wrapper
2. Git wrapper sends HTTP request to gateway
3. Gateway validates the request against policies
4. Gateway executes git command with credentials
5. Gateway returns sanitized output to container

## Policy Enforcement

### Branch Ownership

Agents can only push to branches they own:
- Branches prefixed with `egg/` (configurable)
- Branches with an open PR authored by the agent

### Protected Branches

Direct pushes to protected branches (main, master) are blocked. Changes must go through pull requests.

### Merge Blocking

**The gateway has no merge endpoint.** Agents cannot merge PRs - humans must review and merge via the GitHub UI. This is enforced at the infrastructure level, not by instructions.

### Allowed Operations

| Category | Operations | Notes |
|----------|------------|-------|
| Read | `status`, `diff`, `log`, `show`, `blame` | Informational only |
| Stage | `add`, `reset` (non-destructive modes) | Modify staging area |
| Commit | `commit` | Create commits |
| Branch | `checkout`, `switch`, `branch` | Branch management |
| Network | `push`, `fetch`, `pull` | Credentials injected by gateway |
| GitHub | `gh pr create`, `gh pr comment` | API calls via gateway |

### Blocked Operations

| Operation | Why Blocked |
|-----------|-------------|
| `git merge` to protected branches | Must go through PR review |
| `gh pr merge` | Human must review and merge |
| `git push --force` to others' branches | Could destroy others' work |
| `git config --global` | Could affect other agents |
| `git remote add/remove` | Could redirect pushes |

## Network Isolation

### Public Mode

- Full internet access for the sandbox container
- Anthropic API calls still route through gateway for credential injection
- Domain allowlist not enforced

### Private Mode

- All network traffic routes through gateway proxy
- Only allowed domains accessible (configurable allowlist)
- WebSearch and WebFetch tools blocked at gateway level
- Prevents data exfiltration through Anthropic's API infrastructure

## Audit Logging

All operations through the gateway are logged with:
- Operation type
- Repository and branch
- Session/container ID
- Timestamp
- Success/failure status
- Policy violations (if any)

## Recovery

### Crash Recovery

If a container crashes without cleanup:
1. Gateway scans for orphaned worktrees on startup
2. Compares worktree list against active containers
3. Removes worktrees for containers that no longer exist
4. Committed work is preserved; only working directory removed

### Session Recovery

Sessions are stored in `~/.egg/sessions.json` (host-side), surviving gateway restarts.

## Related ADRs

- [ADR: Git Isolation Architecture](adr/git-isolation-architecture.md)
- [ADR: Credential Injection](adr/credential-injection.md)
- [ADR: Network Isolation](adr/network-isolation.md)
