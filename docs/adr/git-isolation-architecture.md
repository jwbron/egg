# ADR: Git Isolation Architecture

**Status:** Implemented
**Origin:** Extracted from james-in-a-box

## Summary

This document describes how egg safely allows multiple AI agent containers to work on the same git repositories simultaneously, without risking cross-contamination or unauthorized access.

**Core security guarantee**: An AI agent can only see and modify its own isolated workspace. It cannot access other agents' work, cannot directly push to remote repositories, and cannot see credentials. All git operations that touch the network or affect shared state go through a trusted gateway that enforces access policies.

## Key Properties

- **Complete isolation**: Each agent gets its own branch and working directory
- **No credential exposure**: Agents never see GitHub tokens or SSH keys
- **Enforced code review**: Agents cannot merge their own PRs - humans must review and merge
- **Crash-safe**: System recovers cleanly if an agent container crashes

## Architecture

### Complete Metadata Isolation

The fundamental security property is that **agents never touch git metadata**. The container mounts only the working directory (source files), with the `.git` path shadowed by an empty tmpfs:

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

```
┌─────────────────────────────────────────────────────────────────┐
│                     Agent Container (Untrusted)                  │
│                                                                 │
│   The agent runs 'git status', which invokes the git wrapper    │
│                              │                                   │
│                              ▼                                   │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │  Git Wrapper Script                                     │   │
│   │  - Intercepts all git commands                          │   │
│   │  - Cannot bypass (no git metadata = native git fails)   │   │
│   └─────────────────────────────────────────────────────────┘   │
│                              │                                   │
└──────────────────────────────│───────────────────────────────────┘
                               │ HTTP API call
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Gateway Sidecar (Trusted)                    │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │  Request Validation                                     │   │
│   │  - Verify container identity                            │   │
│   │  - Check operation against allowlist                    │   │
│   │  - Validate flags (block dangerous options)             │   │
│   └─────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │  Policy Enforcement                                     │   │
│   │  - Branch ownership: only push to agent's own branches  │   │
│   │  - Protected branches: block direct push to main        │   │
│   │  - Merge blocking: agents cannot merge PRs              │   │
│   └─────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │  Git Execution                                          │   │
│   │  - Execute in correct worktree context                  │   │
│   │  - Inject credentials for network operations            │   │
│   │  - Return sanitized output to container                 │   │
│   └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Multi-Agent Isolation

Each agent works on its own isolated branch with its own staging area:

| Agent | Working Directory | Branch | Index (Staging) |
|-------|-------------------|--------|-----------------|
| abc123 | `/worktrees/abc123/my-repo/` | `egg/abc123/work` | Isolated |
| def456 | `/worktrees/def456/my-repo/` | `egg/def456/work` | Isolated |

**Guarantees:**
- Agents cannot see each other's uncommitted changes
- Agents can work on different branches simultaneously
- All agents share commit history and git objects (efficient storage)
- Gateway manages worktree metadata - agents never touch it

## Security Properties

1. **Filesystem isolation**: Containers cannot see other containers' working directories
2. **Metadata isolation**: Containers have no access to git metadata (`.git/` contents)
3. **Credential isolation**: GitHub tokens exist only in the gateway, never in containers
4. **Operation allowlist**: Gateway only permits known-safe git operations and flags
5. **Branch ownership**: Containers can only push to branches they created
6. **Merge prevention**: Containers cannot merge PRs - humans must review and merge
7. **Audit trail**: All git operations are logged through the gateway

## Crash Recovery

If an agent container crashes without cleanup:

1. **On next gateway startup**: Gateway scans for orphaned worktrees
2. **Orphan detection**: Compare worktree list against active containers
3. **Cleanup**: Remove worktrees for containers that no longer exist
4. **Branch preservation**: Committed work is preserved; only working directory removed

## Why This Design?

### Alternatives Considered

**Behavioral controls only**
- Rely on instructions telling agents not to access other workspaces
- **Rejected:** Insufficient for security-critical applications

**Full clone per container**
- Each container gets complete independent clone
- **Rejected:** Wasteful storage; complex sync requirements

### Why Gateway-Managed Worktrees?

The chosen approach provides:
- **Uniform architecture** across deployment environments
- **Simple security model** (no git metadata in container = no git-based attacks)
- **Efficient storage** (worktrees share git objects)
- **Fast workspace creation** (O(1) via git worktree)
- **Clean crash recovery** (gateway manages all state)
