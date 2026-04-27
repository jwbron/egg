# Git Isolation Architecture

This document describes how we safely allow multiple AI agent containers to work on the same git repositories simultaneously, without risking cross-contamination or unauthorized access.

**Scope:** This document focuses on **agent isolation** (gateway-managed worktrees for sandbox containers). The orchestrator also uses git worktrees for pipeline state persistence, but that is a separate concern managed independently. See [Orchestrator Architecture](orchestrator.md#pipeline-state-persistence) for details on orchestrator state storage.

**The core security guarantee**: An AI agent can only see and modify its own isolated workspace. It cannot access other agents' work, cannot directly push to remote repositories, and cannot see credentials. All git operations that touch the network or affect shared state go through a trusted gateway that enforces access policies.

**Key properties:**
- **Complete isolation**: Each agent gets its own branch and working directory
- **No credential exposure**: Agents never see GitHub tokens or SSH keys
- **Enforced code review**: Agents cannot merge their own PRs---humans must review and merge
- **Crash-safe**: System recovers cleanly if an agent container crashes
- **Private Repo Mode** (optional): Restricts agent to private repositories only

---

This architecture implements the git-specific aspects of [network isolation](network-isolation.md), which establishes the core principle: **behavioral controls are insufficient for AI agent security**. Instructions can be bypassed by prompt injection, model drift, or adversarial inputs. Security must be enforced at the infrastructure level.

For git operations, this means:
- **Credential isolation**: Agents cannot push directly---no credentials exist in the container
- **Filesystem isolation**: Agents cannot access other agents' workspaces---they don't exist in the container's view
- **Gateway enforcement**: All git operations go through a policy-enforcing gateway

This document focuses on the specific challenge of **multi-agent git isolation**: how multiple agents can work on the same repository simultaneously without cross-contamination.

---

## Threat Model

### Threats Addressed

| Threat | Mitigation |
|--------|------------|
| Agent accesses another agent's workspace | Filesystem isolation---other workspaces don't exist in container's view |
| Agent pushes to unauthorized branches | Gateway enforces branch ownership policy |
| Agent pushes malicious code directly to main | Gateway blocks direct pushes to protected branches; PRs require human review |
| Agent bypasses BRC consensus in pipeline session | Gateway blocks direct `git push` for pipeline sessions; requires `consensus_push` marker from `mcp__brc__propose` (or fallback `egg-orch consensus propose --push`) |
| Agent discovers or exfiltrates credentials | Credentials only exist in gateway; container never sees them |
| Agent modifies git config to bypass security | Container has no access to git metadata; config is gateway-controlled |
| Agent escapes via git hooks or filters | Hooks universally disabled via `core.hooksPath=/dev/null` in gateway and orchestrator; filters mitigated in containers by metadata isolation (no `.gitattributes` processing); gateway protected by branch ownership policy (agents cannot push to main) and required human review of all commits |
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
6. **Merge prevention**: Containers cannot merge PRs---humans must review and merge
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
   │  Check for uncommitted changes                                   │
   │  - If uncommitted: create HITL decision for recovery/discard     │
   │  - No auto-commit (removed — see post-agent-commit reference)    │
   │                              │                                   │
   │                              ▼                                   │
   │  git worktree remove /worktrees/abc123/my-repo                  │
   └──────────────────────────────────────────────────────────────────┘

5. Agent Restart (preserves worktree)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Orchestrator calls spawn_agent_container() for replacement      │
   │                              │                                   │
   │                              ▼                                   │
   │  Gateway create_worktrees() is idempotent — if the worktree     │
   │  at /worktrees/{pipeline}-{role}/{repo}/ already exists, it is  │
   │  reused with all committed work intact.                          │
   │                              │                                   │
   │                              ▼                                   │
   │  New container launched with the existing worktree mounted       │
   │  Agent resumes with full commit history from prior container     │
   └──────────────────────────────────────────────────────────────────┘
```

### Multi-Agent Isolation

Each agent works on its own isolated worktree with its own staging area. This applies to **all** agents, including concurrent pipeline agents (coder, tester, documenter, reviewers) that share a pipeline branch:

| Agent | Working Directory | Branch | Index (Staging) |
|-------|-------------------|--------|-----------------|
| abc123 | `/worktrees/abc123/my-repo/` | `agent/abc123/work` | Isolated |
| def456 | `/worktrees/def456/my-repo/` | `agent/def456/work` | Isolated |

**Guarantees:**
- Agents cannot see each other's uncommitted changes
- Agents can work on different branches simultaneously
- All agents share commit history and git objects (efficient storage)
- Gateway manages worktree metadata---agents never touch it

**Pipeline agents:** In concurrent pipeline execution, all agents push to the same shared branch (e.g., `egg/issue-{N}`) but each agent has its own worktree. Since each role has mutually exclusive file write permissions (coder → source code, tester → tests, documenter → docs), push rebases cannot conflict. Reviewer agents sync their worktrees before reviewing by fetching and merging the pipeline branch, ensuring they evaluate up-to-date code from producers. See [Concurrent Execution Guide](../guides/concurrent-execution.md#per-agent-worktree-isolation) for details.

**Pipeline-session push enforcement:** For all pipeline sessions (refine/plan/implement), the gateway blocks direct `git push` — all pushes must go through `mcp__brc__propose` (which pushes to origin and sends CONSENSUS_PROPOSE in one step; push is on by default). The fallback CLI is `egg-orch consensus propose --push`. This structurally enforces the "all changes must be reviewed" invariant rather than relying on agent compliance. See [Gateway README — Pipeline Push Enforcement](../../gateway/README.md#pipeline-push-enforcement-brc-sessions) for details.

**Worktree-aware APIs:** All gateway APIs that access the filesystem use `map_container_path_to_worktree()` to resolve container repo paths to worktree paths. This includes git operations, contract operations (`egg-contract show`, `add-commit`, `add-decision`, etc.), and checkpoint operations. The mapping is transparent to agents --- they use their normal repo path and the gateway resolves it to the correct worktree.

**Per-agent git identity:** Each agent commits with a role-scoped author (e.g., `egg (coder) <coder@egg.local>`) for auditability. Combined with per-agent worktrees, this provides structural commit attribution without requiring post-hoc analysis.

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
| Recovery | `update-ref` | Pipeline sessions only; scoped to `refs/heads/<assigned_branch>`; detached-HEAD recovery primitive (issue #2162) |

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
| `--no-verify` | Blocked in agent-facing API (hooks already disabled via `core.hooksPath=/dev/null`) |
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

### Git Hook Protection

**Defense-in-depth**: The gateway and orchestrator universally disable all git hooks when executing git commands on user repositories via `core.hooksPath=/dev/null`. This prevents hook-based attacks where malicious repositories execute arbitrary code in the trusted gateway/orchestrator environment.

**Implementation**:
- **Gateway checkpoint handler** (`gateway/checkpoint_handler.py`): All git commands include `-c core.hooksPath=/dev/null`
- **Orchestrator state store** (`orchestrator/state_store.py`): All git commands include `-c core.hooksPath=/dev/null`
- **Usage CLI** (`shared/egg_contracts/usage_cli.py`): All git commands include `-c core.hooksPath=/dev/null`

**Rationale**: Pre-commit hooks, commit-msg hooks, and other git hooks can execute arbitrary code when git commands are run. Even though hooks shouldn't affect the checkpoint branch or internal state branches, allowing them to execute in the gateway or orchestrator would violate the security boundary - user repository code must never run in trusted contexts.

**Additional hardening**: The `--no-verify` flag is also blocked in the agent-facing API as an additional safeguard, though hooks are already disabled globally.

See issue #58 for context on hook-based attacks and the security implications.

---

## Deployment Scenarios

The architecture works identically across deployment environments:

| Aspect | Local (k3s) | Cloud (GKE / Cloud Run) |
|--------|-------------|------------------------|
| Shared storage | hostPath volumes | PVCs with ReadWriteMany or GCS FUSE |
| Gateway communication | k8s Service DNS | k8s Service DNS / localhost (sidecar) |
| Container startup | Gateway creates worktree, mounted as hostPath | Same |
| Credential storage | Local files / k8s Secrets | Secret Manager |
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

**Restart recovery:** When an agent is restarted (via `restart_agent` or `restart_phase`), the gateway's idempotent `create_worktrees` API detects the existing worktree keyed by `{pipeline_id}-{role}` and returns its host paths. The respawned container mounts the same worktree with all committed work intact. Uncommitted changes from the previous container are lost — agents should commit work incrementally to maximize recovery. See [Agent Recovery Reference](../reference/agent-recovery.md#worktree-preservation) for details.

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

**Concern:** All git operations go over HTTP---is this slow?

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

Private Repo Mode restricts egg to only interact with **private** GitHub repositories, preventing any interaction with public repositories. This is an optional policy layer built on top of the existing gateway infrastructure.

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
| `git clone` | Blocked | Allowed |
| `git fetch` | Blocked | Allowed |
| `git push` | Blocked | Allowed |
| `gh pr create` | Blocked | Allowed |
| `gh issue view` | Blocked | Allowed |
| `gh repo fork` | Blocked (either direction) | Allowed (to private only) |

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
- Fork from private to private: Allowed
- Fork from private to public: Blocked
- Fork from public to anywhere: Blocked

**Upstream references:**
- If a private repo has a public upstream, fetch from upstream is blocked
- Agent must work only with the private fork

**Organization visibility:**
- GitHub "internal" repositories (visible within org) are treated as private
- Only "public" visibility is blocked

### Repository Visibility Checking

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

### Repository URL Parsing

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

### Policy Enforcement

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

### Gateway Integration Points

The private repo policy integrates with each gateway endpoint that interacts with repositories:

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
| `POST /api/v1/gh/execute` | Custom parsing | Extract repo from command args |
| `POST /api/v1/gh/api` | `validate_gh_api_path` | Extract repo from API path |
| `GET /api/v1/health` | None | Health check, no repo access |

Issue operations (`gh issue create`, `gh issue comment`, etc.) are routed through the generic `/api/v1/gh/execute` endpoint rather than dedicated issue endpoints. The visibility check is applied within the execute handler by extracting the repository from command arguments or the current working directory context.

### Fork Handling

Fork commands via `gh repo fork` go through the `/api/v1/gh/execute` endpoint. The gateway intercepts and validates fork operations:

**Rules:**
- Cannot fork FROM a public repo
- Cannot fork TO a public destination
- Private to private: Allowed
- Private to internal: Allowed
- Internal to private: Allowed
- Internal to internal: Allowed

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

### gh API Path Validation

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

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_REPO_MODE` | `false` | Enable private repository mode |
| `VISIBILITY_CACHE_TTL` | `60` | Seconds to cache visibility lookups |
| `VISIBILITY_FAIL_OPEN` | `false` | If true, allow on API errors (less secure) |

---

## References

- [Git Worktrees Documentation](https://git-scm.com/docs/git-worktree)
- [Docker Bind Mounts](https://docs.docker.com/storage/bind-mounts/)
- [Cloud Run Multi-Container](https://cloud.google.com/run/docs/deploying#sidecars)

---

## Related Documentation

- [Credential Injection](credential-injection.md) -- how the gateway injects credentials for network operations
- [Network Isolation](network-isolation.md) -- the broader network security model that this architecture implements for git
- [Architecture Documentation Index](README.md)
