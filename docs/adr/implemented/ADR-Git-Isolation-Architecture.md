# ADR: Git Isolation Architecture for Autonomous AI Agents

**Status:** Implemented (core), Private Repo Mode proposed
**Date:** 2026-01-27 (core), 2026-01-28 (Private Repo Mode)
**Supersedes:** ADR-Container-Worktree-Isolation

---

## Executive Summary

This document describes how we safely allow multiple AI agent containers to work on the same git repositories simultaneously, without risking cross-contamination or unauthorized access.

**Scope:** This ADR focuses on **agent isolation** (gateway-managed worktrees for sandbox containers). The orchestrator also uses git worktrees for pipeline state persistence, but that is a separate concern managed independently. See [Orchestrator Architecture](../../architecture/orchestrator.md#pipeline-state-persistence) for details on orchestrator state storage.

**The core security guarantee**: An AI agent can only see and modify its own isolated workspace. It cannot access other agents' work, cannot directly push to remote repositories, and cannot see credentials. All git operations that touch the network or affect shared state go through a trusted gateway that enforces access policies.

**Key properties:**
- **Complete isolation**: Each agent gets its own branch and working directory
- **No credential exposure**: Agents never see GitHub tokens or SSH keys
- **Enforced code review**: Agents cannot merge their own PRs—humans must review and merge
- **Crash-safe**: System recovers cleanly if an agent container crashes
- **Private Repo Mode** (optional): Restricts agent to private repositories only

---

## Motivation

This document implements the git-specific aspects of [ADR-Internet-Tool-Access-Lockdown](../in-progress/ADR-Internet-Tool-Access-Lockdown.md), which establishes the core principle: **behavioral controls are insufficient for AI agent security**. Instructions can be bypassed by prompt injection, model drift, or adversarial inputs. Security must be enforced at the infrastructure level.

For git operations, this means:
- **Credential isolation**: Agents cannot push directly—no credentials exist in the container
- **Filesystem isolation**: Agents cannot access other agents' workspaces—they don't exist in the container's view
- **Gateway enforcement**: All git operations go through a policy-enforcing gateway

This document focuses on the specific challenge of **multi-agent git isolation**: how multiple agents can work on the same repository simultaneously without cross-contamination.

---

## Threat Model

### Threats Addressed

| Threat | Mitigation |
|--------|------------|
| Agent accesses another agent's workspace | Filesystem isolation—other workspaces don't exist in container's view |
| Agent pushes to unauthorized branches | Gateway enforces branch ownership policy |
| Agent pushes malicious code directly to main | Gateway blocks direct pushes to protected branches; PRs require human review |
| Agent discovers or exfiltrates credentials | Credentials only exist in gateway; container never sees them |
| Agent modifies git config to bypass security | Container has no access to git metadata; config is gateway-controlled |
| Agent escapes via git hooks or filters | No hooks/filters in container; only gateway executes git commands |
| Crashed container corrupts shared state | Gateway cleans up orphaned workspaces on startup |

### Explicit Non-Goals

This architecture does **not** protect against:
- Malicious agents with root access to the container (defense in depth via container sandboxing)
- Network-level attacks between containers (addressed by network policies)
- Compromise of the gateway itself (gateway runs with minimal attack surface)

---

## Security Model

### Principle: Complete Metadata Isolation

The fundamental security property is that **agents never touch git metadata**. The container mounts only the working directory (source files), with the `.git` path shadowed by an empty tmpfs:

```
Container filesystem view:
/home/egg/repos/my-repo/
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

### Principle: Gateway as Security Boundary

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

### Security Properties

1. **Filesystem isolation**: Containers cannot see other containers' working directories
2. **Metadata isolation**: Containers have no access to git metadata (`.git/` contents)
3. **Credential isolation**: GitHub tokens exist only in the gateway, never in containers
4. **Operation allowlist**: Gateway only permits known-safe git operations and flags
5. **Branch ownership**: Containers can only push to branches they created
6. **Merge prevention**: Containers cannot merge PRs—humans must review and merge
7. **Audit trail**: All git operations are logged through the gateway

---

## Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            Shared Storage                                │
│                                                                          │
│  /repos/                              ← Main repositories                │
│  └── my-repo/                         ← Standard git repo                │
│      └── .git/                                                           │
│          ├── objects/                 ← Shared objects (all worktrees)   │
│          ├── refs/                    ← Shared refs                      │
│          └── worktrees/               ← Worktree metadata (gateway only) │
│              ├── agent-abc123/        ← Metadata for agent abc123        │
│              └── agent-def456/        ← Metadata for agent def456        │
│                                                                          │
│  /worktrees/                          ← Working directories              │
│      ├── agent-abc123/                                                   │
│      │   └── my-repo/                 ← Agent abc123's working dir       │
│      └── agent-def456/                                                   │
│          └── my-repo/                 ← Agent def456's working dir       │
└─────────────────────────────────────────────────────────────────────────┘
                    │                              │
                    ▼                              ▼
     ┌──────────────────────────┐    ┌──────────────────────────┐
     │   Agent Container        │    │   Gateway Sidecar        │
     │                          │    │                          │
     │  Mounts ONLY:            │    │  Has access to:          │
     │  /worktrees/abc123/      │    │  /repos/                 │
     │    my-repo/              │    │  /worktrees/             │
     │  → /home/egg/repos/      │    │                          │
     │                          │    │  Manages:                │
     │  Can do:                 │    │  - Worktree lifecycle    │
     │  - Edit source files     │    │  - All git operations    │
     │                          │    │  - Push/fetch to GitHub  │
     │  Cannot do:              │    │  - Policy enforcement    │
     │  - See other worktrees   │    │                          │
     │  - Access .git metadata  │    │                          │
     │  - See credentials       │    │                          │
     └──────────────────────────┘    └──────────────────────────┘
                    │                              ▲
                    │           HTTP API           │
                    └──────────────────────────────┘
```

### Container Lifecycle

```
1. Agent Startup
   ┌──────────────────────────────────────────────────────────────────┐
   │  Gateway receives request to create workspace for agent-abc123   │
   │                              │                                   │
   │                              ▼                                   │
   │  git worktree add /worktrees/abc123/my-repo -b agent/abc123/work │
   │                              │                                   │
   │                              ▼                                   │
   │  Return worktree path to orchestrator                            │
   └──────────────────────────────────────────────────────────────────┘

2. Container Launch
   ┌──────────────────────────────────────────────────────────────────┐
   │  docker run                                                      │
   │    -v /worktrees/abc123/my-repo:/home/egg/repos/my-repo:rw      │
   │    --mount type=tmpfs,destination=/home/egg/repos/my-repo/.git   │
   │    -e CONTAINER_ID=abc123                                        │
   │    agent-image                                                   │
   │                                                                  │
   │  The tmpfs mount shadows .git, giving agent no git metadata      │
   └──────────────────────────────────────────────────────────────────┘

3. Normal Operation
   ┌──────────────────────────────────────────────────────────────────┐
   │  Agent edits files directly in /home/egg/repos/my-repo/         │
   │  Agent runs 'git add', 'git commit' → routed through gateway    │
   │  Agent runs 'git push' → gateway authenticates and pushes       │
   │  Agent runs 'gh pr create' → gateway handles API call           │
   └──────────────────────────────────────────────────────────────────┘

4. Container Shutdown
   ┌──────────────────────────────────────────────────────────────────┐
   │  Gateway receives cleanup request                                │
   │                              │                                   │
   │                              ▼                                   │
   │  Check for uncommitted changes (warn if present)                 │
   │                              │                                   │
   │                              ▼                                   │
   │  git worktree remove /worktrees/abc123/my-repo                  │
   └──────────────────────────────────────────────────────────────────┘
```

### Multi-Agent Isolation

Each agent works on its own isolated branch with its own staging area:

| Agent | Working Directory | Branch | Index (Staging) |
|-------|-------------------|--------|-----------------|
| abc123 | `/worktrees/abc123/my-repo/` | `agent/abc123/work` | Isolated |
| def456 | `/worktrees/def456/my-repo/` | `agent/def456/work` | Isolated |

**Guarantees:**
- Agents cannot see each other's uncommitted changes
- Agents can work on different branches simultaneously
- All agents share commit history and git objects (efficient storage)
- Gateway manages worktree metadata—agents never touch it

---

## Gateway Operations

### Allowed Git Operations

The gateway implements an explicit allowlist. Operations not on this list are rejected.

| Category | Operations | Notes |
|----------|------------|-------|
| Read | `status`, `diff`, `log`, `show`, `blame`, `branch --list` | Informational only |
| Stage | `add`, `reset` (non-destructive modes) | Modify staging area |
| Commit | `commit` | Create commits |
| Branch | `checkout`, `switch`, `branch` (create/delete own branches) | Branch management |
| Network | `push`, `fetch`, `pull` | Credentials injected by gateway |
| GitHub | `gh pr create`, `gh pr comment`, `gh issue` | API calls via gateway |

### Blocked Operations

| Operation | Why Blocked |
|-----------|-------------|
| `git merge` to protected branches | Must go through PR review |
| `gh pr merge` | Human must review and merge |
| `git push --force` to others' branches | Could destroy others' work |
| `git config --global` | Could affect other agents |
| `git remote add/remove` | Could redirect pushes |

### Blocked Flags

The gateway blocks dangerous flags across all operations:

| Flag | Risk |
|------|------|
| `--exec`, `-c` | Command injection via config/scripts |
| `--upload-pack`, `--receive-pack` | Arbitrary command execution on fetch/push |
| `--config`, `-c` | Runtime config override |
| `--no-verify` | Skip hooks (defense in depth) |
| `--git-dir`, `--work-tree` | Path traversal outside sandbox |

### Flag Validation

Each operation has an explicit allowlist of permitted flags. Unknown flags are rejected:

```python
# Example: 'git commit' allowed flags
"commit": {
    "allowed_flags": [
        "--message", "-m",
        "--amend",          # Only own recent commits
        "--allow-empty",
        "--author",
        "--signoff", "-s",
        "--verbose", "-v",
        "--quiet", "-q",
    ],
}
```

---

## Deployment Scenarios

The architecture works identically across deployment environments:

| Aspect | Local (Docker) | Cloud (Cloud Run) |
|--------|----------------|-------------------|
| Shared storage | Docker bind mounts | emptyDir or GCS FUSE |
| Gateway communication | Docker network | localhost (sidecar) |
| Container startup | Gateway creates worktree | Same |
| Credential storage | Local files | Secret Manager |
| Persistence | Host filesystem | GCS checkpoint (optional) |

### Cloud Run Specifics

On Cloud Run, containers are stateless and can be preempted. Additional considerations:

1. **Git state checkpointing**: Periodically save git bundles to Cloud Storage
2. **Session affinity**: Route requests for one session to the same instance
3. **Startup recovery**: Restore from checkpoint if container was preempted

---

## Crash Recovery

If an agent container crashes without cleanup:

1. **On next gateway startup**: Gateway scans for orphaned worktrees
2. **Orphan detection**: Compare worktree list against active containers
3. **Cleanup**: Remove worktrees for containers that no longer exist
4. **Branch preservation**: Committed work is preserved; only working directory removed

```python
def cleanup_orphaned_worktrees():
    """Remove worktrees for containers that no longer exist."""
    worktrees = list_all_worktrees()
    active_containers = get_active_containers()

    for worktree in worktrees:
        container_id = extract_container_id(worktree.path)
        if container_id not in active_containers:
            # Log warning if uncommitted changes
            if has_uncommitted_changes(worktree.path):
                log.warning(f"Removing worktree with uncommitted changes: {container_id}")
            remove_worktree(worktree.path, force=True)
```

---

## Performance Considerations

**Concern:** All git operations go over HTTP—is this slow?

**Analysis:**
- Local HTTP latency: ~0.1-1ms per request
- Typical `git status`: 10-100ms (I/O bound)
- HTTP overhead: <10% for most operations

**Benchmarks (estimated):**

| Operation | Direct Git | Via Gateway | Overhead |
|-----------|-----------|-------------|----------|
| git status | 50ms | 55ms | ~10% |
| git diff | 30ms | 35ms | ~17% |
| git commit | 100ms | 110ms | ~10% |

The overhead is acceptable given the security benefits. Optimizations (batching, caching) can be added if needed.

---

## Why This Design?

### Alternatives Considered

**1. Behavioral controls only**
- Rely on instructions telling agents not to access other workspaces
- **Rejected:** The security incident proved this insufficient

**2. Mount restriction isolation (previous approach)**
- Each container mounts only its own worktree admin directory
- Local git operations run in container
- **Rejected:** Required complex path rewriting; host git broken while containers run

**3. Overlayfs isolation**
- Each container gets copy-on-write view via overlayfs
- **Rejected:** Doesn't work on Cloud Run; different architecture per environment

**4. Full clone per container**
- Each container gets complete independent clone
- **Rejected:** Wasteful storage; complex sync requirements

### Why Gateway-Managed Worktrees?

The chosen approach provides:
- **Uniform architecture** across local and cloud deployments
- **Simple security model** (no git metadata in container = no git-based attacks)
- **Efficient storage** (worktrees share git objects)
- **Fast workspace creation** (O(1) via git worktree)
- **Clean crash recovery** (gateway manages all state)

---

## Private Repo Mode

**Status:** Proposed extension

> **Note on ADR location:** This section describes a proposed extension to the implemented Git Isolation architecture. It's included here rather than as a separate ADR because it builds directly on the existing gateway infrastructure and shares the same threat model. The core architecture (worktrees, gateway routing, credential isolation) is implemented; Private Repo Mode adds an optional policy layer on top.

Private Repo Mode restricts egg to only interact with **private** GitHub repositories, preventing any interaction with public repositories.

### Motivation

When operating on sensitive codebases, there's risk of:
1. **Accidental code sharing:** Agent might reference or copy code to a public repository
2. **Data leakage via forks:** Agent could fork a private repo to a public destination
3. **Cross-contamination:** Agent might mix private code with public dependencies

Private Repo Mode addresses these risks by ensuring egg can only see and modify private repositories.

### Design

The gateway enforces repository visibility at the policy layer:

```python
# Gateway policy check for all git operations
def validate_repository_access(repo: str, operation: str) -> bool:
    """
    In Private Repo Mode, only allow access to private repositories.
    """
    if not PRIVATE_REPO_MODE_ENABLED:
        return True  # Standard mode: all repos allowed

    visibility = get_repo_visibility(repo)  # GitHub API call, cached

    if visibility == "public":
        log.warning(f"Blocked {operation} on public repo: {repo}")
        return False

    return True  # private or internal repos allowed
```

### Visibility Cache Policy

Repository visibility is cached to avoid excessive GitHub API calls. A **two-tier caching strategy** balances security and performance:

| Operation Type | TTL | Rationale |
|----------------|-----|-----------|
| **Read operations** (fetch, clone, ls-remote) | 60 seconds | Lower risk; brief window acceptable |
| **Write operations** (push, pr create) | 0 seconds | Higher risk; always verify before writes |

| Property | Value | Rationale |
|----------|-------|-----------|
| **Read TTL** | 60 seconds | Short enough to catch visibility changes; long enough to avoid API rate limits |
| **Write TTL** | 0 seconds (always check) | Critical operations should never use stale visibility data |
| **Refresh** | On cache miss or expiry | No background refresh; checked synchronously on each operation |
| **Invalidation** | Manual or restart | Can force refresh via gateway API if needed |

**Security consideration:** A repository changing from private to public mid-session could theoretically allow one read operation before the cache expires. The 60-second read TTL limits this window. Write operations always verify visibility in real-time, eliminating this risk for the most critical operations.

```python
# Cache configuration
VISIBILITY_CACHE_TTL_READ = int(os.getenv("VISIBILITY_CACHE_TTL_READ", "60"))
VISIBILITY_CACHE_TTL_WRITE = int(os.getenv("VISIBILITY_CACHE_TTL_WRITE", "0"))

def get_repo_visibility(owner: str, repo: str, for_write: bool = False) -> str:
    """Get repository visibility with tiered caching.

    Args:
        owner: Repository owner
        repo: Repository name
        for_write: If True, use write TTL (stricter caching)

    Returns:
        'public', 'private', or 'internal'
    """
    ttl = VISIBILITY_CACHE_TTL_WRITE if for_write else VISIBILITY_CACHE_TTL_READ
    # ... caching logic with appropriate TTL ...
```

### Enforced Restrictions

| Operation | Public Repo | Private Repo |
|-----------|-------------|--------------|
| `git clone` | ❌ Blocked | ✓ Allowed |
| `git fetch` | ❌ Blocked | ✓ Allowed |
| `git push` | ❌ Blocked | ✓ Allowed |
| `gh pr create` | ❌ Blocked | ✓ Allowed |
| `gh issue view` | ❌ Blocked | ✓ Allowed |
| `gh repo fork` | ❌ Blocked (either direction) | ✓ Allowed (to private only) |

### Configuration

Private Repo Mode is enabled via environment variable in the gateway:

```yaml
# docker-compose.yml
services:
  gateway:
    environment:
      - PRIVATE_REPO_MODE=true
```

### Edge Cases

**Forking:**
- Fork from private → private: ✓ Allowed
- Fork from private → public: ❌ Blocked
- Fork from public → anywhere: ❌ Blocked

**Upstream references:**
- If a private repo has a public upstream, fetch from upstream is blocked
- Agent must work only with the private fork

**Organization visibility:**
- GitHub "internal" repositories (visible within org) are treated as private
- Only "public" visibility is blocked

### Private Repo Mode: Detailed Implementation Plan

#### 1. Overview

Private Repo Mode adds a policy layer to the gateway that restricts all git/gh operations to private repositories only. This prevents accidental interaction with public repositories and reduces the risk of code being shared publicly.

#### 2. GitHub API Integration

##### 2.1 Repository Visibility Check

```python
# gateway/repo_visibility.py
"""Repository visibility checking with caching."""

import os
import time
import threading
from functools import lru_cache
from typing import Literal, Optional

import requests

# Configuration
GITHUB_API_BASE = "https://api.github.com"
VISIBILITY_CACHE_TTL = int(os.getenv("VISIBILITY_CACHE_TTL", "60"))
PRIVATE_REPO_MODE = os.getenv("PRIVATE_REPO_MODE", "false").lower() == "true"

# Cache for visibility lookups
_visibility_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def get_github_token() -> str:
    """Get GitHub token from secrets."""
    token_path = "/secrets/.github-token"
    if os.path.exists(token_path):
        with open(token_path) as f:
            return f.read().strip()
    return os.getenv("GITHUB_TOKEN", "")


def _fetch_repo_visibility(owner: str, repo: str) -> Optional[str]:
    """Fetch repository visibility from GitHub API.

    Returns:
        'public', 'private', 'internal', or None if not found/error
    """
    token = get_github_token()
    if not token:
        # No token - assume private (fail closed)
        return "private"

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            return data.get("visibility", "private")

        elif response.status_code == 404:
            # Repo not found - could be private and we don't have access
            # Or could be truly not found
            # Fail closed: treat as private
            return "private"

        elif response.status_code == 403:
            # Rate limited or forbidden - fail closed
            return "private"

        else:
            # Unknown error - fail closed
            return "private"

    except requests.RequestException:
        # Network error - fail closed
        return "private"


def get_repo_visibility(owner: str, repo: str) -> str:
    """Get repository visibility with caching.

    Args:
        owner: Repository owner (user or org)
        repo: Repository name

    Returns:
        'public', 'private', or 'internal'
    """
    cache_key = f"{owner}/{repo}"
    now = time.time()

    with _cache_lock:
        if cache_key in _visibility_cache:
            visibility, timestamp = _visibility_cache[cache_key]
            if now - timestamp < VISIBILITY_CACHE_TTL:
                return visibility

    # Cache miss or expired - fetch from API
    visibility = _fetch_repo_visibility(owner, repo) or "private"

    with _cache_lock:
        _visibility_cache[cache_key] = (visibility, now)

    return visibility


def clear_visibility_cache(owner: Optional[str] = None, repo: Optional[str] = None):
    """Clear visibility cache.

    Args:
        owner: If provided with repo, clear specific entry
        repo: If provided with owner, clear specific entry
        If neither provided, clear entire cache
    """
    with _cache_lock:
        if owner and repo:
            cache_key = f"{owner}/{repo}"
            _visibility_cache.pop(cache_key, None)
        else:
            _visibility_cache.clear()


def is_private_repo_mode_enabled() -> bool:
    """Check if private repo mode is enabled."""
    return PRIVATE_REPO_MODE
```

##### 2.2 Repository URL Parsing

```python
# gateway/repo_parser.py
"""Parse repository identifiers from various formats."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass
class RepoIdentifier:
    """Parsed repository identifier."""
    owner: str
    repo: str
    full_name: str  # owner/repo


def parse_repo_url(url: str) -> Optional[RepoIdentifier]:
    """Parse owner/repo from a GitHub URL.

    Handles:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    - github.com/owner/repo
    - owner/repo
    """
    # SSH format: git@github.com:owner/repo.git
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if ssh_match:
        owner, repo = ssh_match.groups()
        return RepoIdentifier(owner=owner, repo=repo, full_name=f"{owner}/{repo}")

    # HTTPS format: https://github.com/owner/repo
    https_match = re.match(
        r"(?:https?://)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$", url
    )
    if https_match:
        owner, repo = https_match.groups()
        return RepoIdentifier(owner=owner, repo=repo, full_name=f"{owner}/{repo}")

    # Short format: owner/repo
    short_match = re.match(r"^([^/]+)/([^/]+)$", url)
    if short_match:
        owner, repo = short_match.groups()
        return RepoIdentifier(owner=owner, repo=repo, full_name=f"{owner}/{repo}")

    return None


def parse_repo_from_path(repo_path: str) -> Optional[RepoIdentifier]:
    """Extract owner/repo from a local repository path by reading git config.

    Args:
        repo_path: Local filesystem path to repository

    Returns:
        RepoIdentifier if remote origin found, None otherwise
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return parse_repo_url(result.stdout.strip())
    except subprocess.SubprocessError:
        pass

    return None
```

#### 3. Policy Integration

##### 3.1 Policy Checker

```python
# gateway/private_repo_policy.py
"""Private repository mode policy enforcement."""

import logging
from typing import Optional, Tuple

from repo_parser import RepoIdentifier, parse_repo_url, parse_repo_from_path
from repo_visibility import (
    get_repo_visibility,
    is_private_repo_mode_enabled,
)

log = logging.getLogger(__name__)


class PrivateRepoPolicyError(Exception):
    """Raised when an operation violates private repo mode policy."""
    pass


def check_repo_access(
    repo_identifier: RepoIdentifier,
    operation: str,
) -> Tuple[bool, Optional[str]]:
    """Check if repository access is allowed under private repo mode.

    Args:
        repo_identifier: Parsed repository identifier
        operation: Name of operation being performed (for logging)

    Returns:
        Tuple of (allowed, error_message)
    """
    if not is_private_repo_mode_enabled():
        return True, None

    visibility = get_repo_visibility(repo_identifier.owner, repo_identifier.repo)

    if visibility == "public":
        error_msg = (
            f"Private Repo Mode: Blocked {operation} on public repository "
            f"'{repo_identifier.full_name}'. Only private repositories are allowed."
        )
        log.warning(
            "private_repo_policy_violation",
            extra={
                "operation": operation,
                "repository": repo_identifier.full_name,
                "visibility": visibility,
            },
        )
        return False, error_msg

    # private or internal - allowed
    log.debug(
        "private_repo_policy_allowed",
        extra={
            "operation": operation,
            "repository": repo_identifier.full_name,
            "visibility": visibility,
        },
    )
    return True, None


def validate_repo_url_access(url: str, operation: str) -> Tuple[bool, Optional[str]]:
    """Validate repository URL access under private repo mode.

    Args:
        url: Repository URL (HTTPS or SSH format)
        operation: Name of operation being performed

    Returns:
        Tuple of (allowed, error_message)
    """
    if not is_private_repo_mode_enabled():
        return True, None

    repo = parse_repo_url(url)
    if not repo:
        # Can't parse URL - allow (might not be GitHub)
        log.warning(f"Could not parse repo URL for visibility check: {url}")
        return True, None

    return check_repo_access(repo, operation)


def validate_repo_path_access(path: str, operation: str) -> Tuple[bool, Optional[str]]:
    """Validate local repository path access under private repo mode.

    Args:
        path: Local filesystem path to repository
        operation: Name of operation being performed

    Returns:
        Tuple of (allowed, error_message)
    """
    if not is_private_repo_mode_enabled():
        return True, None

    repo = parse_repo_from_path(path)
    if not repo:
        # Can't determine remote - allow (might be local-only)
        log.warning(f"Could not determine remote for visibility check: {path}")
        return True, None

    return check_repo_access(repo, operation)
```

##### 3.2 Gateway Integration Points

Update `gateway/gateway.py` to integrate private repo checks:

```python
# In gateway.py - Add to each endpoint that interacts with repositories

from private_repo_policy import (
    validate_repo_url_access,
    validate_repo_path_access,
    PrivateRepoPolicyError,
)


@app.route("/api/v1/git/push", methods=["POST"])
def git_push():
    """Handle git push requests."""
    # ... existing validation ...

    repo_path = data.get("repo_path")

    # NEW: Private repo mode check
    allowed, error = validate_repo_path_access(repo_path, "git push")
    if not allowed:
        return jsonify({"error": error, "policy": "private_repo_mode"}), 403

    # ... rest of existing code ...


@app.route("/api/v1/git/fetch", methods=["POST"])
def git_fetch():
    """Handle git fetch requests."""
    # ... existing validation ...

    repo_path = data.get("repo_path")

    # NEW: Private repo mode check
    allowed, error = validate_repo_path_access(repo_path, "git fetch")
    if not allowed:
        return jsonify({"error": error, "policy": "private_repo_mode"}), 403

    # ... rest of existing code ...


@app.route("/api/v1/git/clone", methods=["POST"])
def git_clone():
    """Handle git clone requests."""
    # ... existing validation ...

    remote_url = data.get("url")

    # NEW: Private repo mode check
    allowed, error = validate_repo_url_access(remote_url, "git clone")
    if not allowed:
        return jsonify({"error": error, "policy": "private_repo_mode"}), 403

    # ... rest of existing code ...


@app.route("/api/v1/gh/pr/create", methods=["POST"])
def gh_pr_create():
    """Handle PR creation requests."""
    # ... existing validation ...

    repo_path = data.get("repo_path")

    # NEW: Private repo mode check
    allowed, error = validate_repo_path_access(repo_path, "gh pr create")
    if not allowed:
        return jsonify({"error": error, "policy": "private_repo_mode"}), 403

    # ... rest of existing code ...


# Add similar checks to all repository-touching endpoints:
# - /api/v1/gh/pr/comment
# - /api/v1/gh/pr/edit
# - /api/v1/gh/pr/close
# - /api/v1/gh/issue/create
# - /api/v1/gh/issue/comment
# - /api/v1/gh/execute (for generic gh commands)
```

#### 4. Operations Coverage Matrix

| Endpoint | Policy Check | Notes |
|----------|--------------|-------|
| `POST /api/v1/git/push` | `validate_repo_path_access` | Checks origin remote |
| `POST /api/v1/git/fetch` | `validate_repo_path_access` | Checks origin remote |
| `POST /api/v1/git/pull` | `validate_repo_path_access` | Checks origin remote |
| `POST /api/v1/git/clone` | `validate_repo_url_access` | Checks clone URL directly |
| `POST /api/v1/git/ls-remote` | `validate_repo_url_access` | Checks remote URL |
| `POST /api/v1/gh/pr/create` | `validate_repo_path_access` | Checks target repo |
| `POST /api/v1/gh/pr/comment` | `validate_repo_path_access` | Checks target repo |
| `POST /api/v1/gh/pr/edit` | `validate_repo_path_access` | Checks target repo |
| `POST /api/v1/gh/pr/close` | `validate_repo_path_access` | Checks target repo |
| `POST /api/v1/gh/issue/create` | `validate_repo_path_access` | Via gh/execute (see Note) |
| `POST /api/v1/gh/issue/comment` | `validate_repo_path_access` | Via gh/execute (see Note) |

**Note on Issue Endpoints:** Issue operations (`gh issue create`, `gh issue comment`, etc.) are routed through the generic `/api/v1/gh/execute` endpoint rather than dedicated issue endpoints. The visibility check is applied within the execute handler by extracting the repository from command arguments or the current working directory context. This design keeps the gateway API surface minimal while supporting the full range of `gh` CLI operations.
| `POST /api/v1/gh/execute` | Custom parsing | Extract repo from command args (see 4.1, 4.2) |
| `POST /api/v1/gh/api` | `validate_gh_api_path` | Extract repo from API path (see 4.3) |
| `GET /api/v1/health` | None | Health check, no repo access |

##### 4.1 URL Argument Parsing for gh Commands

Commands like `gh pr view https://github.com/owner/repo/pull/123` include repository URLs as arguments. The gateway parses these to extract and validate the target repository:

```python
# gateway/url_argument_parser.py
"""Parse repository URLs from gh command arguments."""

import re
from typing import Optional
from repo_parser import RepoIdentifier


# GitHub URL patterns that may appear in command arguments
GITHUB_URL_PATTERNS = [
    # PR/Issue URLs: https://github.com/owner/repo/pull/123
    re.compile(r"https?://github\.com/([^/]+)/([^/]+)/(?:pull|issues)/\d+"),
    # Repo URLs: https://github.com/owner/repo
    re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$"),
]


def extract_repo_from_args(args: list[str]) -> Optional[RepoIdentifier]:
    """Extract repository identifier from command arguments.

    Scans arguments for GitHub URLs and extracts owner/repo.

    Args:
        args: Command arguments to scan

    Returns:
        RepoIdentifier if found, None otherwise
    """
    for arg in args:
        for pattern in GITHUB_URL_PATTERNS:
            match = pattern.match(arg)
            if match:
                owner, repo = match.groups()
                # Clean up repo name (remove trailing slashes, etc.)
                repo = repo.rstrip("/")
                return RepoIdentifier(owner=owner, repo=repo, full_name=f"{owner}/{repo}")

    return None
```

##### 4.2 gh/execute Visibility Check Integration

```python
# In gateway.py - Updated gh/execute handler

@app.route("/api/v1/gh/execute", methods=["POST"])
def gh_execute():
    """Handle generic gh CLI commands with visibility checks."""
    data = request.get_json()
    command = data.get("command", "")
    args = data.get("args", [])

    # Extract repo from URL arguments (e.g., gh pr view https://...)
    repo_from_url = extract_repo_from_args(args)
    if repo_from_url:
        allowed, error = check_repo_access(repo_from_url, f"gh {command}")
        if not allowed:
            return jsonify({"error": error, "policy": "private_repo_mode"}), 403

    # ... rest of existing code ...
```

##### 4.3 gh api Path Validation

Raw `gh api` calls to repository endpoints require visibility validation:

```python
# gateway/gh_api_validator.py
"""Validate gh api paths for Private Repo Mode."""

import re
from typing import Optional, Tuple
from repo_parser import RepoIdentifier
from private_repo_policy import check_repo_access


# Patterns for API paths that reference repositories
REPO_API_PATTERNS = [
    # /repos/{owner}/{repo}[/...]
    re.compile(r"^/?repos/([^/]+)/([^/]+)(?:/.*)?$"),
    # /orgs/{org}/repos - lists repos, not specific to one
    # /user/repos - lists user's repos
]


def validate_gh_api_path(path: str, method: str = "GET") -> Tuple[bool, Optional[str]]:
    """Validate gh api path against Private Repo Mode policy.

    Args:
        path: API path (e.g., 'repos/owner/repo/pulls')
        method: HTTP method (GET, POST, etc.)

    Returns:
        Tuple of (allowed, error_message)
    """
    for pattern in REPO_API_PATTERNS:
        match = pattern.match(path)
        if match:
            owner, repo = match.groups()
            repo_id = RepoIdentifier(owner=owner, repo=repo, full_name=f"{owner}/{repo}")
            return check_repo_access(repo_id, f"gh api {method} {path}")

    # Non-repo paths (e.g., /user, /orgs) - allow
    return True, None
```

**Note:** This validation is applied when Private Repo Mode is enabled. Without it, all API paths are allowed (subject to the existing allowlist).

#### 5. Fork Handling

##### 5.1 Integration with gh/execute Endpoint

Fork commands via `gh repo fork` go through the `/api/v1/gh/execute` endpoint. The gateway intercepts and validates fork operations by parsing the command arguments:

```python
# gateway/gh_execute_handler.py (excerpt)
"""Handler for gh/execute that intercepts fork commands."""

import shlex
from typing import Optional, Tuple

from fork_policy import validate_fork_operation
from repo_parser import parse_repo_url


def handle_gh_execute(command: str, args: list[str]) -> Tuple[bool, Optional[str]]:
    """Handle gh execute requests, intercepting fork commands.

    Fork commands are identified and validated through fork_policy.
    Other commands pass through with standard visibility checks.
    """
    # Detect fork command: gh repo fork [<repository>] [flags]
    if command == "repo" and args and args[0] == "fork":
        return _handle_fork_command(args[1:])

    # Detect fork via full command string parsing
    full_args = [command] + args
    if "repo" in full_args and "fork" in full_args:
        fork_idx = full_args.index("fork")
        return _handle_fork_command(full_args[fork_idx + 1:])

    # Non-fork commands: apply standard visibility checks
    return True, None


def _handle_fork_command(fork_args: list[str]) -> Tuple[bool, Optional[str]]:
    """Parse and validate gh repo fork arguments.

    Usage: gh repo fork [<repository>] [-- <gitflags>...]
    Flags:
        --clone              Clone the fork
        --org <string>       Create fork in organization
        --fork-name <name>   Rename the fork
        --remote             Add remote for fork

    Args:
        fork_args: Arguments after 'gh repo fork'

    Returns:
        Tuple of (allowed, error_message)
    """
    source_repo = None
    target_org = None

    i = 0
    while i < len(fork_args):
        arg = fork_args[i]

        # Stop at git flags separator
        if arg == "--":
            break

        # Parse --org flag
        if arg == "--org" and i + 1 < len(fork_args):
            target_org = fork_args[i + 1]
            i += 2
            continue

        # Skip other flags
        if arg.startswith("-"):
            # Handle flags with values
            if arg in ("--fork-name", "--remote-name") and i + 1 < len(fork_args):
                i += 2
            else:
                i += 1
            continue

        # Positional argument is the source repo
        if source_repo is None:
            source_repo = arg

        i += 1

    # If no source repo specified, gh uses current directory's repo
    # The caller should provide this context
    if source_repo is None:
        # Will be filled in by caller from current repo context
        return True, None

    return validate_fork_operation(source_repo, target_org=target_org)
```

##### 5.2 Fork Policy Implementation

```python
# gateway/fork_policy.py
"""Special handling for fork operations under private repo mode."""

from typing import Optional, Tuple
from repo_visibility import get_repo_visibility, is_private_repo_mode_enabled
from repo_parser import parse_repo_url


def validate_fork_operation(
    source_repo: str,
    target_org: Optional[str] = None,
    target_visibility: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate fork operation under private repo mode.

    Rules:
    - Cannot fork FROM a public repo
    - Cannot fork TO a public destination
    - Private -> Private: Allowed
    - Private -> Internal: Allowed
    - Internal -> Private: Allowed
    - Internal -> Internal: Allowed

    Args:
        source_repo: Repository being forked (owner/repo format)
        target_org: Target organization (if specified)
        target_visibility: Explicitly requested visibility (if any)

    Returns:
        Tuple of (allowed, error_message)
    """
    if not is_private_repo_mode_enabled():
        return True, None

    # Parse source repo
    source = parse_repo_url(source_repo)
    if not source:
        return True, None  # Can't parse, allow

    source_visibility = get_repo_visibility(source.owner, source.repo)

    # Rule 1: Cannot fork FROM public repo
    if source_visibility == "public":
        return False, (
            f"Private Repo Mode: Cannot fork from public repository "
            f"'{source.full_name}'. Only private repositories can be forked."
        )

    # Rule 2: Cannot fork TO public visibility
    if target_visibility == "public":
        return False, (
            f"Private Repo Mode: Cannot create public fork. "
            f"Forks must be private or internal."
        )

    return True, None
```

#### 6. Testing Plan

##### 6.1 Unit Tests

```python
# gateway/test_private_repo_policy.py
"""Tests for private repository mode policy."""

import os
import pytest
from unittest.mock import patch, MagicMock

from repo_visibility import get_repo_visibility, clear_visibility_cache
from repo_parser import parse_repo_url, RepoIdentifier
from private_repo_policy import (
    check_repo_access,
    validate_repo_url_access,
    validate_repo_path_access,
)


class TestRepoParser:
    """Tests for repository URL parsing."""

    def test_parse_https_url(self):
        result = parse_repo_url("https://github.com/owner/repo")
        assert result == RepoIdentifier("owner", "repo", "owner/repo")

    def test_parse_https_url_with_git_suffix(self):
        result = parse_repo_url("https://github.com/owner/repo.git")
        assert result == RepoIdentifier("owner", "repo", "owner/repo")

    def test_parse_ssh_url(self):
        result = parse_repo_url("git@github.com:owner/repo.git")
        assert result == RepoIdentifier("owner", "repo", "owner/repo")

    def test_parse_short_format(self):
        result = parse_repo_url("owner/repo")
        assert result == RepoIdentifier("owner", "repo", "owner/repo")

    def test_parse_invalid_url(self):
        result = parse_repo_url("not-a-repo-url")
        assert result is None


class TestRepoVisibility:
    """Tests for repository visibility checking."""

    def setup_method(self):
        clear_visibility_cache()

    @patch("repo_visibility.requests.get")
    def test_public_repo(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"visibility": "public"},
        )
        assert get_repo_visibility("owner", "repo") == "public"

    @patch("repo_visibility.requests.get")
    def test_private_repo(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"visibility": "private"},
        )
        assert get_repo_visibility("owner", "repo") == "private"

    @patch("repo_visibility.requests.get")
    def test_not_found_defaults_private(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        assert get_repo_visibility("owner", "repo") == "private"

    @patch("repo_visibility.requests.get")
    def test_cache_hit(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"visibility": "private"},
        )

        # First call
        get_repo_visibility("owner", "repo")
        # Second call - should use cache
        get_repo_visibility("owner", "repo")

        assert mock_get.call_count == 1


class TestPrivateRepoPolicy:
    """Tests for private repo mode policy enforcement."""

    def setup_method(self):
        clear_visibility_cache()

    @patch.dict(os.environ, {"PRIVATE_REPO_MODE": "true"})
    @patch("private_repo_policy.get_repo_visibility")
    def test_blocks_public_repo(self, mock_visibility):
        mock_visibility.return_value = "public"

        repo = RepoIdentifier("owner", "public-repo", "owner/public-repo")
        allowed, error = check_repo_access(repo, "git push")

        assert allowed is False
        assert "public repository" in error

    @patch.dict(os.environ, {"PRIVATE_REPO_MODE": "true"})
    @patch("private_repo_policy.get_repo_visibility")
    def test_allows_private_repo(self, mock_visibility):
        mock_visibility.return_value = "private"

        repo = RepoIdentifier("owner", "private-repo", "owner/private-repo")
        allowed, error = check_repo_access(repo, "git push")

        assert allowed is True
        assert error is None

    @patch.dict(os.environ, {"PRIVATE_REPO_MODE": "true"})
    @patch("private_repo_policy.get_repo_visibility")
    def test_allows_internal_repo(self, mock_visibility):
        mock_visibility.return_value = "internal"

        repo = RepoIdentifier("org", "internal-repo", "org/internal-repo")
        allowed, error = check_repo_access(repo, "git push")

        assert allowed is True
        assert error is None

    @patch.dict(os.environ, {"PRIVATE_REPO_MODE": "false"})
    @patch("private_repo_policy.get_repo_visibility")
    def test_disabled_allows_all(self, mock_visibility):
        mock_visibility.return_value = "public"

        repo = RepoIdentifier("owner", "public-repo", "owner/public-repo")
        allowed, error = check_repo_access(repo, "git push")

        assert allowed is True
        assert error is None
```

##### 6.2 Integration Tests

```bash
#!/bin/bash
# gateway/test_private_repo_integration.sh

set -e

echo "=== Private Repo Mode Integration Tests ==="

# Setup: Enable private repo mode
export PRIVATE_REPO_MODE=true

# Start gateway in test mode
python gateway.py &
GATEWAY_PID=$!
sleep 3

# Test 1: Fetch from private repo (should succeed)
echo "Test 1: Fetch from private repo"
curl -X POST http://localhost:9848/api/v1/git/fetch \
    -H "Authorization: Bearer $GATEWAY_SECRET" \
    -H "Content-Type: application/json" \
    -d '{"repo_path": "/home/egg/repos/private-repo"}'
echo "✓ Private repo fetch allowed"

# Test 2: Clone public repo (should fail)
echo "Test 2: Clone public repo"
RESPONSE=$(curl -s -w "%{http_code}" -X POST http://localhost:9848/api/v1/git/clone \
    -H "Authorization: Bearer $GATEWAY_SECRET" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://github.com/octocat/Hello-World"}')

HTTP_CODE=${RESPONSE: -3}
if [ "$HTTP_CODE" = "403" ]; then
    echo "✓ Public repo clone blocked (403)"
else
    echo "✗ Expected 403, got $HTTP_CODE"
    exit 1
fi

# Test 3: Create PR on private repo (should succeed)
echo "Test 3: Create PR on private repo"
# ... similar test ...

# Cleanup
kill $GATEWAY_PID

echo "=== All tests passed ==="
```

#### 7. Configuration

##### 7.1 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_REPO_MODE` | `false` | Enable private repository mode |
| `VISIBILITY_CACHE_TTL` | `60` | Seconds to cache visibility lookups |
| `VISIBILITY_FAIL_OPEN` | `false` | If true, allow on API errors (less secure) |

##### 7.2 Docker Compose Configuration

```yaml
# docker-compose.yml
services:
  gateway:
    environment:
      - PRIVATE_REPO_MODE=${PRIVATE_REPO_MODE:-false}
      - VISIBILITY_CACHE_TTL=${VISIBILITY_CACHE_TTL:-60}
```

##### 7.3 Setup Script Addition

```bash
# gateway/setup.sh - Add to configuration section

# Private Repo Mode
read -p "Enable Private Repo Mode? (y/N): " ENABLE_PRIVATE
if [[ "$ENABLE_PRIVATE" =~ ^[Yy]$ ]]; then
    echo "PRIVATE_REPO_MODE=true" >> "$CONFIG_DIR/gateway.env"
    echo "Private Repo Mode enabled - only private repositories allowed"
else
    echo "PRIVATE_REPO_MODE=false" >> "$CONFIG_DIR/gateway.env"
fi
```

#### 8. Error Messages

```python
# gateway/error_messages.py
"""User-friendly error messages for private repo mode."""

PRIVATE_REPO_MODE_ERRORS = {
    "clone_public": (
        "Cannot clone public repository '{repo}'. "
        "Private Repo Mode only allows interaction with private repositories. "
        "If you need to clone a public repo, ask your administrator to add it "
        "to the allowed repositories list or disable Private Repo Mode."
    ),
    "push_public": (
        "Cannot push to public repository '{repo}'. "
        "Private Repo Mode restricts operations to private repositories only."
    ),
    "fetch_public": (
        "Cannot fetch from public repository '{repo}'. "
        "Private Repo Mode restricts operations to private repositories only."
    ),
    "fork_from_public": (
        "Cannot fork from public repository '{repo}'. "
        "In Private Repo Mode, you can only fork from private repositories."
    ),
    "fork_to_public": (
        "Cannot create public fork. "
        "In Private Repo Mode, all forks must be private or internal."
    ),
}
```

#### 9. Audit Logging

```python
# Add to gateway audit logging

from datetime import datetime


def log_private_repo_policy_event(
    operation: str,
    repository: str,
    visibility: str,
    allowed: bool,
    source_ip: str,
):
    """Log private repo mode policy decision."""
    log.info(
        "private_repo_policy",
        extra={
            "event_type": "policy_check",
            "policy": "private_repo_mode",
            "operation": operation,
            "repository": repository,
            "visibility": visibility,
            "decision": "allowed" if allowed else "denied",
            "source_ip": source_ip,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )
```

#### 10. Implementation Checklist

##### Phase 1: Core Infrastructure
- [ ] Create `repo_visibility.py` with GitHub API integration
- [ ] Create `repo_parser.py` with URL parsing utilities
- [ ] Create `private_repo_policy.py` with policy logic
- [ ] Add `PRIVATE_REPO_MODE` environment variable
- [ ] Add `VISIBILITY_CACHE_TTL` environment variable

##### Phase 2: Gateway Integration
- [ ] Add policy check to `POST /api/v1/git/push`
- [ ] Add policy check to `POST /api/v1/git/fetch`
- [ ] Add policy check to `POST /api/v1/git/pull`
- [ ] Add policy check to `POST /api/v1/git/clone`
- [ ] Add policy check to `POST /api/v1/git/ls-remote`
- [ ] Add policy check to `POST /api/v1/gh/pr/*` endpoints
- [ ] Add policy check to `POST /api/v1/gh/issue/*` endpoints
- [ ] Add policy check to `POST /api/v1/gh/execute`

##### Phase 3: Fork Handling
- [ ] Create `fork_policy.py` with fork-specific rules
- [ ] Add fork source validation
- [ ] Add fork destination validation
- [ ] Block `gh repo fork` to public destinations

##### Phase 4: Testing
- [ ] Unit tests for `repo_visibility.py`
- [ ] Unit tests for `repo_parser.py`
- [ ] Unit tests for `private_repo_policy.py`
- [ ] Integration tests for gateway endpoints
- [ ] End-to-end tests with real repositories

##### Phase 5: Documentation & Rollout
- [ ] Update gateway setup documentation
- [ ] Add Private Repo Mode to CLAUDE.md
- [ ] Create runbook for enabling/disabling
- [ ] Add monitoring for policy violations

#### 11. Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `gateway/repo_visibility.py` | CREATE | Visibility checking with caching |
| `gateway/repo_parser.py` | CREATE | URL/path parsing utilities |
| `gateway/private_repo_policy.py` | CREATE | Policy enforcement logic |
| `gateway/fork_policy.py` | CREATE | Fork-specific rules |
| `gateway/error_messages.py` | CREATE | User-friendly error messages |
| `gateway/gateway.py` | MODIFY | Add policy checks to endpoints |
| `gateway/setup.sh` | MODIFY | Add configuration prompts |
| `gateway/test_private_repo_policy.py` | CREATE | Unit tests |
| `gateway/test_private_repo_integration.sh` | CREATE | Integration tests |

---

## Implementation Status

- [x] Gateway-managed worktree creation/deletion
- [x] Git command routing through gateway API
- [x] Operation and flag allowlist validation
- [x] Branch ownership policy enforcement
- [x] Orphaned worktree cleanup on startup
- [x] tmpfs mount shadowing for .git
- [ ] Batch operation endpoint (optimization)
- [ ] Cloud Run checkpoint/restore

---

## References

- [Git Worktrees Documentation](https://git-scm.com/docs/git-worktree)
- [Docker Bind Mounts](https://docs.docker.com/storage/bind-mounts/)
- [Cloud Run Multi-Container](https://cloud.google.com/run/docs/deploying#sidecars)

---

*This document describes the git isolation architecture for the egg autonomous agent system. For questions or contributions, see the project repository.*
