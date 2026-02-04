# ADR: Per-Container Repository Mode Management

## Status

Proposed

## Context

The gateway sidecar currently supports two mutually exclusive repository visibility modes configured via environment variables at startup:

- **Private Repo Mode** (`PRIVATE_REPO_MODE=true`): Restrict operations to private repositories only
- **Public Repo Only Mode** (`PUBLIC_REPO_ONLY_MODE=true`): Restrict operations to public repositories only

This global configuration has limitations:

1. **No concurrent modes**: Cannot run containers with different modes simultaneously
2. **Sidecar restart required**: Changing modes requires restarting the gateway sidecar
3. **All-or-nothing mounting**: All configured repos are mounted regardless of mode; visibility is only enforced at operation time

### Requirements

The system should support:

| Mode | Private Repos | Internal Repos | Public Repos |
|------|---------------|----------------|--------------|
| `private_repo_mode=true` | Mounted + git/gh remotes work | Mounted + git/gh remotes work | NOT mounted |
| `private_repo_mode=false` | NOT mounted | NOT mounted | Mounted + git/gh remotes work |

**Note**: `internal` repositories (GitHub's third visibility type) are treated as `private` for mode enforcement purposes. Repos of the "wrong" visibility are NOT mounted at all (not just blocked at operation time). This provides defense in depth and avoids user confusion from seeing repos they cannot interact with remotely.

Both git AND gh operations must respect the same mode restrictions.

## Threat Model

Before describing the solution, we explicitly state the threats we're defending against:

### Attackers and Trust Boundaries

| Entity | Trust Level | Capabilities |
|--------|-------------|--------------|
| Launcher (host process) | Fully trusted | Registers sessions, starts containers |
| Gateway sidecar | Fully trusted | Enforces all policies |
| egg container | Partially trusted | Can execute arbitrary code, but network-isolated |
| External attacker | Untrusted | Cannot reach gateway (not exposed outside Docker network) |

### Attack Scenarios

1. **Compromised container tries to access wrong-visibility repos**
   - Defense: Session mode is set by launcher before container starts; container cannot change it
   - Defense: Network-verified container identity (source IP on Docker network)

2. **Compromised container tries to claim another session**
   - Defense: Session-container binding verified via Docker network source IP
   - Defense: Session IDs are 256-bit random, infeasible to guess

3. **Compromised container tries to register a new session**
   - Defense: Session registration requires launcher secret, not gateway secret
   - Defense: Containers only receive per-session tokens, not registration credentials

4. **Container makes request without session header**
   - Defense: **Fail closed** - requests without valid session are denied when session mode is deployed

5. **Attacker enumerates session IDs**
   - Defense: Rate limiting on session lookups (10 failures per minute per source IP)
   - Defense: 256-bit session IDs make brute force infeasible

6. **Gateway restart invalidates sessions**
   - Defense: Session persistence to disk with atomic writes
   - Defense: Containers get 401/403 on invalid session; launcher must be restarted to recover

### Out of Scope

- **Kernel escapes**: Container escaping to host is out of scope (use other defenses)
- **Supply chain attacks**: Compromised dependencies in container images
- **Side channels**: Timing attacks on session validation (mitigated by constant-time comparison)

## Decision

Implement **per-container session management** where the launcher (not the container) registers mode at startup and the gateway enforces operations on a per-session basis.

### Architecture

```
                          ┌─────────────────┐
                          │    Launcher     │
                          │  (host process) │
                          └────────┬────────┘
                                   │ 1. Register session (launcher_secret)
                                   │    Returns: session_token
                                   ▼
┌─────────────────────┐     ┌─────────────────────┐
│  egg container A    │     │  egg container B    │
│  mode: private      │     │  mode: public       │
│  token: tok_abc123  │     │  token: tok_xyz789  │
│  IP: 172.18.0.3     │     │  IP: 172.18.0.4     │
└─────────┬───────────┘     └─────────┬───────────┘
          │ 2. Request with           │
          │    session_token          │
          ▼                           ▼
┌─────────────────────────────────────────────────┐
│              Gateway Sidecar                     │
│                                                  │
│  Sessions:                                       │
│    tok_abc123 -> {mode: private, ip: 172.18.0.3}│
│    tok_xyz789 -> {mode: public, ip: 172.18.0.4} │
│                                                  │
│  On each request:                                │
│    1. Validate session_token exists             │
│    2. Verify source IP matches session          │
│    3. Enforce mode for repository operation     │
└─────────────────────────────────────────────────┘
```

### Key Components

#### 1. Session Manager (NEW: `gateway/session_manager.py`)

Thread-safe session storage with disk persistence:
- Session registration (launcher only) and unregistration
- Automatic expiry (24h TTL) with heartbeat renewal
- Container-to-session binding verified by source IP
- Disk persistence with atomic writes for gateway restart recovery

```python
@dataclass
class Session:
    session_token: str           # 256-bit random token for requests (in-memory only)
    session_token_hash: str      # sha256(session_token) - stored in persistence file
    container_id: str            # Docker container ID for audit and worktree cleanup
    container_ip: str            # Expected source IP for verification
    mode: Literal["private", "public"]
    created_at: datetime
    last_seen: datetime
    expires_at: datetime

# Persistence file: ~/.egg-gateway/sessions.json
# - File permissions: 0600 (owner read/write only)
# - Contains session_token_hash, NOT the raw token
# - Atomic writes: write to .sessions.json.tmp, then os.rename()
# - Raw tokens held only in memory; on gateway restart, containers must
#   re-authenticate (their token is validated against stored hash)
```

**Session Persistence Security**:
- Only `sha256(session_token)` is stored on disk, not the raw token
- File permissions set to `0600` immediately after creation
- If persistence file is compromised, attacker cannot reconstruct tokens
- Token validation: `sha256(presented_token) == stored_hash` (constant-time comparison)
- Gateway maintains in-memory token→session mapping for performance

#### 2. Gateway Session Endpoints

```
POST /api/v1/sessions/create
  Auth: Bearer {launcher_secret}  # NOT gateway_secret - separate credential
  Body: {
    "container_id": "jib-xxx",
    "container_ip": "172.18.0.3",  # Pre-allocated IP (see IP Allocation below)
    "mode": "private"|"public",
    "repos": ["owner/repo1", "owner/repo2"]  # All repos to consider
  }
  Response: {
    "success": true,
    "session_token": "tok_...",
    "filtered_repos": ["owner/repo1"],  # Only repos matching mode
    "worktrees": {
      "owner/repo1": "/path/to/worktree"
    }
  }
  Rate limit: 10 registrations per minute per source IP

  ATOMIC OPERATION: This endpoint performs visibility query, filtering,
  worktree creation, and session registration atomically. This prevents
  TOCTOU race conditions between visibility check and session registration.
  If any step fails, the entire operation is rolled back.

DELETE /api/v1/sessions/{session_token}
  Auth: Bearer {launcher_secret}  # Only launcher can delete sessions
  Response: {"success": true}
  Note: Containers CANNOT delete sessions - only the launcher can

  CLEANUP: When a session is deleted, associated worktrees are also cleaned up
  for the container_id associated with that session.

POST /api/v1/sessions/{session_token}/heartbeat
  Auth: Bearer {session_token}
  Response: {"success": true, "expires_at": "..."}
  Purpose: Extend session TTL for long-running containers
  Note: Heartbeats are also triggered implicitly on any successful
        session-authenticated request (see Heartbeat Implementation below)

GET /api/v1/repos/visibility
  Auth: Bearer {launcher_secret}  # Used by launcher for informational queries
  Query: ?repos=owner/repo1,owner/repo2
  Response: {"owner/repo1": "public", "owner/repo2": "private", "owner/repo3": "internal"}
  Note: For atomic session+worktree creation, use POST /api/v1/sessions/create instead
```

**Authentication hierarchy**:
- `launcher_secret`: Held by launcher only; can register/delete sessions, query visibility
- `session_token`: Per-container; can make git/gh requests, send heartbeats
- `gateway_secret`: Legacy; used for backwards-compatible non-session requests

#### 3. Per-Session Policy Enforcement

##### 3a. Session Validation in Request Handlers

All git/gh operation handlers in `gateway.py` must validate sessions before processing requests. This is implemented as a decorator that extracts and validates the session, then stores it in Flask's request context:

```python
# In gateway.py

from session_manager import validate_session_for_request, Session

def require_session_auth(f):
    """
    Decorator that validates session tokens in request handlers.

    When REQUIRE_SESSION_AUTH=true:
    - Extracts session token from Authorization header
    - Validates token and verifies source IP
    - Stores validated session in Flask's g object for handler use
    - Returns 401/403 on validation failure

    When REQUIRE_SESSION_AUTH=false (legacy mode):
    - Falls back to gateway_secret validation (existing require_auth behavior)
    - Sets g.session = None and g.session_mode = None
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if os.environ.get("REQUIRE_SESSION_AUTH", "false").lower() == "true":
            # Extract token from Authorization header
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return make_error("Missing or invalid Authorization header", status_code=401)

            token = auth_header[7:]  # Remove "Bearer " prefix
            source_ip = request.remote_addr

            # Validate session and IP binding
            session = validate_session_for_request(token, source_ip)
            if session is None:
                return make_error("Invalid or expired session token", status_code=401)

            # Store session in request context for handlers
            g.session = session
            g.session_mode = session.mode  # "private" or "public"
        else:
            # Legacy mode: use existing gateway_secret auth
            is_valid, error = check_auth()
            if not is_valid:
                return make_error(error, status_code=401)
            g.session = None
            g.session_mode = None  # Use global env vars

        return f(*args, **kwargs)
    return decorated
```

##### 3b. Handler Integration Pattern

Every git/gh handler must pass `session_mode` to `check_private_repo_access()`. Here's the pattern for the `git_push` handler as an example:

```python
@app.route("/api/v1/git/push", methods=["POST"])
@require_session_auth  # Replaces @require_auth
def git_push():
    # ... existing request parsing ...

    # Get session mode from request context (set by decorator)
    session_mode = getattr(g, 'session_mode', None)

    # Check Private Repo Mode policy with session mode
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation="push",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,  # NEW: pass session mode
        )
        if not priv_result.allowed:
            # ... existing denial handling ...

    # ... rest of handler ...
```

**All handlers requiring modification**:
- `git_push()` - pass `session_mode` to `check_private_repo_access()`
- `git_fetch()` - pass `session_mode` to `check_private_repo_access()`
- `git_execute()` - pass `session_mode` to `check_private_repo_access()` (for operations on repos)
- `gh_pr_create()` - pass `session_mode` to `check_private_repo_access()`
- `gh_pr_comment()` - pass `session_mode` to `check_private_repo_access()`
- `gh_pr_edit()` - pass `session_mode` to `check_private_repo_access()`
- `gh_pr_close()` - pass `session_mode` to `check_private_repo_access()`
- `gh_execute()` - pass `session_mode` to `check_private_repo_access()`

##### 3c. Modified check_private_repo_access()

The `check_private_repo_access()` function is modified to accept an optional `session_mode` parameter:

```python
# In private_repo_policy.py

def check_private_repo_access(
    operation: str,
    owner: str | None = None,
    repo: str | None = None,
    repo_path: str | None = None,
    url: str | None = None,
    for_write: bool = False,
    session_mode: str | None = None,  # NEW: "private", "public", or None
) -> PrivateRepoPolicyResult:
    """
    Check if access to a repository is allowed.

    Args:
        session_mode: If provided, use this mode for enforcement.
                     If None, fall back to global env vars (legacy mode).
    """
    # Determine which mode to use
    if session_mode is not None:
        # Session-based mode (per-container)
        use_private_mode = (session_mode == "private")
        use_public_only = (session_mode == "public")
    else:
        # Legacy: use global env vars
        use_private_mode = is_private_repo_mode_enabled()
        use_public_only = is_public_repo_only_mode_enabled()

    # If neither mode is enabled, allow everything
    if not use_private_mode and not use_public_only:
        return PrivateRepoPolicyResult(
            allowed=True,
            reason="Repository visibility policy is disabled",
        )

    # ... existing visibility checking logic ...
    # ... use use_private_mode/use_public_only instead of self._enabled/self._public_only ...
```

#### 3d. gh CLI Operation Handling

The `gh_execute()` handler must enforce session mode for all gh CLI operations. This requires determining the target repository for each command type:

```python
# In gateway.py - gh_execute() handler

@app.route("/api/v1/gh/execute", methods=["POST"])
@require_session_auth
def gh_execute():
    # ... existing arg parsing ...

    session_mode = getattr(g, 'session_mode', None)

    # Determine target repo based on command type
    repo = determine_gh_command_repo(args, payload_repo)

    if repo:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            priv_result = check_private_repo_access(
                operation="gh_execute",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=is_gh_write_operation(args),
                session_mode=session_mode,  # Pass session mode
            )
            if not priv_result.allowed:
                return make_error(priv_result.reason, status_code=403)

    # ... rest of handler ...
```

**Repository determination for gh commands**:

| Command Pattern | Repo Source | Example |
|-----------------|-------------|---------|
| `gh pr view 123 --repo owner/repo` | `--repo` flag | Explicit repo |
| `gh pr create` | `--repo` flag or payload `repo` | Inferred from worktree |
| `gh repo view owner/repo` | Positional argument | `args[2]` |
| `gh repo clone owner/repo` | Positional argument | `args[2]` |
| `gh api repos/owner/repo/...` | API path parsing | Extract from path |
| `gh issue list --repo owner/repo` | `--repo` flag | Explicit repo |

```python
def determine_gh_command_repo(args: list[str], payload_repo: str | None) -> str | None:
    """
    Determine target repository from gh command arguments.

    Returns:
        Repository in owner/repo format, or None if cannot be determined.
    """
    if not args:
        return payload_repo

    cmd = args[0] if args else ""

    # Check for explicit --repo or -R flag (highest priority)
    for i, arg in enumerate(args):
        if arg in ("--repo", "-R") and i + 1 < len(args):
            return args[i + 1]

    # Handle 'gh repo' commands - repo is positional
    if cmd == "repo" and len(args) >= 3:
        # gh repo view owner/repo, gh repo clone owner/repo
        subcmd = args[1]
        if subcmd in ("view", "clone", "fork", "edit", "delete"):
            candidate = args[2]
            if "/" in candidate:  # Looks like owner/repo
                return candidate

    # Handle 'gh api' commands - extract from path
    if cmd == "api" and len(args) >= 2:
        api_path = _find_api_path(args[1:])
        if api_path:
            # Match patterns like /repos/owner/repo/...
            match = re.match(r"/?repos/([^/]+/[^/]+)", api_path)
            if match:
                return match.group(1)

    # Fall back to payload repo (from container's git remote)
    return payload_repo


def is_gh_write_operation(args: list[str]) -> bool:
    """
    Determine if a gh command is a write operation.

    Write operations require stricter visibility checking (no caching).
    """
    if not args:
        return False

    cmd = " ".join(args[:2]) if len(args) >= 2 else args[0]

    WRITE_COMMANDS = {
        "pr create", "pr close", "pr merge", "pr edit", "pr comment", "pr review",
        "issue create", "issue close", "issue edit", "issue comment",
        "repo create", "repo delete", "repo edit", "repo fork",
        "release create", "release delete", "release edit",
    }

    return cmd in WRITE_COMMANDS
```

**API path handling for `gh api` commands**:

```python
def _find_api_path(args: list[str]) -> str | None:
    """
    Find the API path in gh api command arguments.

    Skips flags like -X, --method, -H, --header, etc.
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # Flags that take a value
            if arg in ("-X", "--method", "-H", "--header", "-f", "--field",
                       "-F", "--raw-field", "--jq", "-t", "--template"):
                skip_next = True
            continue
        # This is the API path
        return arg
    return None
```

**Important**: When the target repository cannot be determined and `REQUIRE_SESSION_AUTH=true`, the request should be **denied** (fail-closed). This is the conservative security default.

#### 4. Session Token in Requests

Both git and gh wrappers use the same session token for authentication:

```python
# Container receives session_token via EGG_SESSION_TOKEN environment variable
# This is set by the launcher AFTER session registration succeeds

# git-wrapper.py AND gh-wrapper.py both use:
def get_gateway_auth() -> dict[str, str]:
    session_token = os.environ.get("EGG_SESSION_TOKEN")
    if session_token:
        return {"Authorization": f"Bearer {session_token}"}
    # Legacy fallback (when REQUIRE_SESSION_AUTH=false)
    return {"Authorization": f"Bearer {get_gateway_secret()}"}

headers = get_gateway_auth()
# No X-Jib-Session header needed - token IS the session identifier
```

**Authentication hierarchy at request time**:
1. If `EGG_SESSION_TOKEN` env var is set → use session token (session-based mode)
2. Else use `gateway_secret` (legacy mode, only works when `REQUIRE_SESSION_AUTH=false`)

**Both wrappers (git and gh) use identical authentication logic.** The same `get_gateway_auth()` function is shared or duplicated in both wrappers.

**Security note**: Session token is passed via environment variable. To mitigate `/proc/*/environ` exposure:
- Container runs as non-root user (cannot read other containers' /proc)
- Gateway validates source IP matches registered container IP
- Token is per-container, limiting blast radius if exposed

#### 5. Launcher Registration and Mount Filtering

**Critical**: Session registration MUST happen before container starts. The AI cannot change mode at runtime.

Before starting a container, the launcher uses the atomic session creation endpoint:
1. Pre-allocates container IP from Docker network
2. Calls `POST /api/v1/sessions/create` with mode, repos, and pre-allocated IP
3. Gateway atomically: queries visibility → filters repos → creates worktrees → registers session
4. Launcher receives `session_token` and `filtered_repos`/`worktrees`
5. Starts container with session token and filtered mounts

```python
# Launcher sequence (atomic operation via gateway)
def start_container(mode: str, repos: list[str]):
    # 1. Generate container ID
    container_id = f"jib-{uuid.uuid4().hex[:12]}"

    # 2. Pre-allocate container IP (see IP Allocation section)
    container_ip = docker.allocate_ip(network="egg-isolated")

    # 3. Atomic session creation (visibility + filter + worktrees + session)
    # This single call prevents TOCTOU race conditions
    response = gateway.create_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
        repos=repos,
        auth=launcher_secret
    )
    session_token = response["session_token"]
    worktrees = response["worktrees"]  # Pre-created by gateway

    # 4. Start container with session token and worktree mounts
    docker.run(
        container_id=container_id,
        ip=container_ip,
        env={"EGG_SESSION_TOKEN": session_token},
        mounts=worktrees,
    )
```

**Why atomic**: The previous non-atomic flow (query visibility → filter → register) had a TOCTOU race condition where visibility could change between query and registration. The atomic endpoint ensures the visibility state used for filtering is the same state recorded in the session.

**Why registration must be pre-container**: If registration happened from within the container, a compromised AI could register with a different mode than intended. The launcher is trusted; the container is not.

#### 5a. Container IP Pre-Allocation

Docker assigns IPs dynamically when containers start, but we need the IP before session registration. Options:

**Option A (Recommended): Docker `--ip` flag with custom network**

Create a custom network with defined subnet that supports static IP assignment:
```bash
# One-time setup
docker network create --driver bridge --subnet 172.18.0.0/16 egg-isolated
```

Pre-allocate IP by tracking assigned IPs and selecting next available:
```python
def allocate_ip(network: str = "egg-isolated") -> str:
    # Get network info
    net_info = docker.network.inspect(network)
    subnet = net_info["IPAM"]["Config"][0]["Subnet"]  # e.g., "172.18.0.0/16"

    # Get assigned IPs from running containers
    assigned = {c.attrs["NetworkSettings"]["Networks"][network]["IPAddress"]
                for c in docker.containers.list() if network in c.attrs["NetworkSettings"]["Networks"]}

    # Gateway is typically .1
    assigned.add("172.18.0.1")

    # Find next available IP
    import ipaddress
    for ip in ipaddress.ip_network(subnet).hosts():
        if str(ip) not in assigned:
            return str(ip)

    raise RuntimeError("No available IPs in network")
```

Start container with assigned IP:
```python
docker.run(
    container_id=container_id,
    network="egg-isolated",
    ip=container_ip,  # Uses --ip flag
    ...
)
```

**Option B: Container name resolution (less secure)**

Use container name instead of IP for identification. Less precise but simpler:
```python
# Session stores container_name instead of container_ip
# Gateway verifies via Docker API that request comes from that container
```

We recommend **Option A** for stronger security guarantees.

#### 6. CLI Interface

```bash
# Default: public repos only (safe default)
jib

# Explicit modes
egg --public-repos
egg --private-repos

# For exec mode
egg --exec --private-repos "process this task"
```

#### 7. Session Lifecycle and Recovery

**Normal lifecycle**:
1. Launcher registers session (receives token)
2. Container starts with token
3. Container makes git/gh requests (implicit heartbeat - see below)
4. Container exits → launcher deletes session (cleans up worktrees)

**Heartbeat Implementation**:

The heartbeat mechanism is implemented as a **side-effect of any successful session-authenticated request**, not as a separate background process. This avoids the need for a daemon inside Claude Code containers:

```python
# In gateway request handler
def handle_git_request(request):
    session = validate_session(request)  # Raises if invalid
    session.last_seen = datetime.utcnow()
    session.expires_at = datetime.utcnow() + timedelta(hours=24)  # Extend TTL
    # ... handle request
```

- **No separate heartbeat process needed** - Claude Code doesn't have a background daemon
- Each successful git/gh operation extends the session TTL
- If container is idle for 24h without any git/gh operations, session expires
- The explicit `POST /api/v1/sessions/{token}/heartbeat` endpoint exists for edge cases where long-running operations need TTL extension without git/gh activity

**Gateway restart recovery**:
- Sessions persisted to `~/.egg-gateway/sessions.json` with atomic writes
- On startup, gateway loads sessions from disk (validates hashes)
- Sessions with expired TTL are pruned on load
- Running containers continue working after gateway restart
- Container's token is validated against stored hash on first request

**Container restart (without gateway restart)**:
- Container cannot re-register (no launcher_secret)
- Launcher must restart to create new session
- This is intentional: prevents compromised container from changing mode

**Rate limiting**:
- Session registration: 10/minute per source IP
- Failed session lookups: 10/minute per source IP (prevents enumeration)
- Explicit heartbeats: 100/hour per session (DoS protection on dedicated endpoint)

#### 8. Rate Limiting Implementation

Rate limiting is new infrastructure for the gateway. Implementation approach:

```python
# In gateway/rate_limiter.py (NEW FILE)
import threading
from collections import defaultdict
from datetime import datetime, timedelta

class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self.requests: dict[str, list[datetime]] = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        """Check if request is allowed for the given key (e.g., IP address)."""
        now = datetime.utcnow()
        cutoff = now - self.window

        with self.lock:
            # Prune old entries
            self.requests[key] = [t for t in self.requests[key] if t > cutoff]

            if len(self.requests[key]) >= self.max_requests:
                return False

            self.requests[key].append(now)
            return True

# Rate limiters (module-level singletons)
registration_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)
failed_lookup_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)
heartbeat_limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=3600)
```

**Design decisions**:
- In-memory rate limiting (NOT persisted) - gateway restart clears limits
- Thread-safe with fine-grained locking
- Sliding window algorithm for accurate rate tracking
- Separate limiters for different operations (registration, failed lookups, heartbeats)

**Rate limit state is NOT persisted**: This is intentional. Persisting rate limit state could allow attackers to permanently DoS legitimate users. Gateway restart provides a clean slate.

#### 9. Worktree Cleanup on Session Expiry

Sessions store `container_id` for audit purposes, which also enables worktree cleanup. Worktrees are located at `~/.egg-worktrees/{container_id}/`.

**Normal cleanup** (container exits gracefully):
1. Launcher calls `DELETE /api/v1/sessions/{token}`
2. Gateway deletes session AND cleans up worktrees for `container_id`

**Cleanup on session expiry** (container crashes, launcher doesn't clean up):
```python
# In session_manager.py, called periodically and on gateway startup
def prune_expired_sessions():
    now = datetime.utcnow()
    for token_hash, session in list(sessions.items()):
        if session.expires_at < now:
            # Log expiry event
            log_event("session_expired", session)
            # Clean up worktrees for this container
            cleanup_worktrees(session.container_id)
            # Remove session
            del sessions[token_hash]
    save_to_disk()

def cleanup_worktrees(container_id: str):
    worktree_path = Path.home() / ".egg-worktrees" / container_id
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
        log.info(f"Cleaned up worktrees for expired session: {container_id}")
```

**Cleanup schedule**:
- On gateway startup (prune expired sessions from persistence file)
- Periodically every 15 minutes (configurable via `SESSION_CLEANUP_INTERVAL`)
- On session expiry check during any session validation

**Existing behavior**: The current `startup_cleanup()` in `gateway.py` already removes orphaned worktrees. Session-based cleanup integrates with this existing mechanism by using `container_id` as the link between sessions and worktrees.

#### 10. Audit Logging

All session events are logged with the following fields:

```json
{
  "event_type": "session_*",
  "timestamp": "ISO8601",
  "session_token_hash": "sha256(token)[:16]",  // NOT the full token
  "container_id": "jib-xxx",
  "container_ip": "172.18.0.3",
  "mode": "private|public",
  "outcome": "success|denied|error",
  "reason": "..."
}
```

Events logged:
- `session_registered`: New session created
- `session_deleted`: Session removed
- `session_expired`: Session TTL expired
- `session_auth_failed`: Invalid token presented
- `session_ip_mismatch`: Source IP doesn't match registered IP
- `session_rate_limited`: Rate limit exceeded

### Files to Modify

| File | Changes |
|------|---------|
| `gateway/session_manager.py` | NEW: Session storage, persistence, worktree cleanup |
| `gateway/rate_limiter.py` | NEW: Thread-safe sliding window rate limiting |
| `gateway/gateway.py` | Add session endpoints, extract session+IP in all handlers |
| `gateway/private_repo_policy.py` | Accept session_token, verify IP, fail-closed logic |
| `jib-launcher/launcher.py` | Session registration (atomic endpoint), IP pre-allocation |
| `jib-launcher/secrets.py` | NEW: Manage launcher_secret separate from gateway_secret |
| `jib-launcher/ip_allocator.py` | NEW: Docker network IP pre-allocation |
| `sandbox/egg_lib/auth.py` | Shared `get_gateway_auth()` using EGG_SESSION_TOKEN |
| `sandbox/wrappers/git-wrapper.py` | Use session_token for auth (via shared auth module) |
| `sandbox/wrappers/gh-wrapper.py` | Use session_token for auth (via shared auth module) |
| `sandbox/egg` | Add CLI flags for mode selection (launcher interprets these) |

## Consequences

### Positive

- **Concurrent modes**: Multiple containers can run with different modes simultaneously
- **No sidecar restart**: Mode changes are per-container, sidecar stays running
- **Mount-time filtering**: Wrong-visibility repos not mounted at all (defense in depth)
- **Audit trail**: Session-container binding with IP verification enables tracking
- **Fail-closed security**: Invalid/missing sessions are denied when session mode is deployed
- **Gateway restart recovery**: Sessions persisted to disk

### Negative

- **Complexity**: Additional session management layer with persistence
- **Disk I/O**: Session persistence requires atomic file writes
- **API surface**: More endpoints to maintain
- **Launcher changes**: Significant changes to launcher for registration flow

### Neutral

- **Backwards compatible**: `REQUIRE_SESSION_AUTH=false` (default) uses legacy global env vars
- **Migration path**: Deploy with `REQUIRE_SESSION_AUTH=false` initially, enable after testing

### Migration Plan

The transition from legacy global env var mode to session-based mode follows this timeline:

1. **Phase 1: Implementation (This PR)**
   - Implement session management infrastructure
   - Add session endpoints to gateway
   - Modify handlers to support optional session mode
   - Default: `REQUIRE_SESSION_AUTH=false` (legacy mode)

2. **Phase 2: Testing (1-2 weeks after merge)**
   - Deploy with `REQUIRE_SESSION_AUTH=false`
   - Launcher begins using session registration for new containers
   - Both legacy and session-based containers coexist
   - Monitor for issues via audit logging

3. **Phase 3: Gradual Rollout (2-4 weeks after merge)**
   - Enable `REQUIRE_SESSION_AUTH=true` in staging
   - Verify all containers use session tokens
   - Log WARNING for any requests using legacy gateway_secret auth

4. **Phase 4: Enforcement (4-6 weeks after merge)**
   - Enable `REQUIRE_SESSION_AUTH=true` in production
   - Legacy mode still available via env var for rollback
   - Deprecation warning in logs when `REQUIRE_SESSION_AUTH=false`

5. **Phase 5: Removal (3-6 months after Phase 4)**
   - Remove legacy `PRIVATE_REPO_MODE` / `PUBLIC_REPO_ONLY_MODE` env vars
   - Remove backwards-compatibility code
   - Session-based mode becomes the only option

**Deprecation warnings**: When `REQUIRE_SESSION_AUTH=false`, log a WARNING on gateway startup:
```
WARNING: Legacy repository mode via env vars is deprecated.
Set REQUIRE_SESSION_AUTH=true and update launcher to use session registration.
Legacy mode will be removed in a future version.
```

## Security Considerations

1. **Session token generation**: Use `secrets.token_urlsafe(32)` - 256-bit entropy
2. **Session validation**: Constant-time comparison to prevent timing attacks
3. **Session expiry**: Auto-cleanup stale sessions (24h default, extendable via heartbeat)
4. **No session reuse**: Each container gets unique session token
5. **Session-container binding**: Token tied to container_id AND container_ip for verification
6. **Fail-closed behavior**: When `REQUIRE_SESSION_AUTH=true`, requests without valid session are DENIED
7. **IP verification**: Gateway verifies request source IP matches registered container IP
8. **Separate credentials**: Launcher uses `launcher_secret`; containers use per-session `session_token`
9. **Rate limiting**: Protects against enumeration and DoS attacks
10. **Audit logging**: All session events logged with hashed token (not plaintext)
11. **Session persistence**: Atomic writes to prevent corruption; sessions survive gateway restart

### Session Token vs Environment Variable Exposure

Session tokens are passed to containers via environment variables. Risks and mitigations:

| Risk | Mitigation |
|------|------------|
| `/proc/*/environ` readable | Containers run as non-root; can't read other containers |
| Token in error messages | Gateway never logs full tokens, only `sha256(token)[:16]` |
| Token in crash dumps | Container crashes don't expose host-level data |
| Token leaked via logging | Container wrappers don't log Authorization header |

The per-container, IP-verified token model limits blast radius: a leaked token only compromises one container's mode, and only from the expected IP.

## Testing Strategy

1. **Unit tests**:
   - Session registration, expiry, concurrent access
   - Rate limiting enforcement (sliding window behavior)
   - IP verification logic
   - Persistence (write, read, atomic corruption recovery)
   - Token hashing and constant-time comparison
   - IP allocation (network inspection, IP selection)

2. **Integration tests**:
   - Policy enforcement with session mode
   - Multiple containers with different modes simultaneously
   - Gateway restart with running containers (session persistence)
   - Fail-closed behavior when `REQUIRE_SESSION_AUTH=true`
   - Atomic session creation (visibility + filter + worktrees + session)
   - Worktree cleanup on session deletion and expiry
   - **Handler-level session enforcement**:
     - `git_push()` rejects requests without valid session when `REQUIRE_SESSION_AUTH=true`
     - `git_push()` uses session mode to filter operations (private session can't push to public repo)
     - `git_fetch()` respects session mode
     - `gh_execute()` extracts repo from various command patterns and enforces session mode
     - `gh_execute()` with `gh repo view owner/repo` extracts repo from positional arg
     - `gh_execute()` with `gh api repos/owner/repo/...` extracts repo from API path
     - `gh_execute()` with `gh pr create --repo owner/repo` extracts repo from flag
     - Session IP verification: request from wrong IP returns 401/403

3. **Security tests**:
   - Attempt to use session token from wrong IP (should fail)
   - Attempt to register session without launcher_secret (should fail)
   - Attempt to delete session from container (should fail)
   - Rate limit enforcement under load
   - Session enumeration resistance
   - IP spoofing test: request with valid token but wrong source IP

4. **Concurrency tests**:
   - Two containers registering sessions simultaneously (no interference)
   - Concurrent session validation with same token
   - Rate limiter thread safety under parallel load

5. **Cleanup tests**:
   - Session expiry triggers worktree cleanup
   - Gateway startup prunes expired sessions and orphaned worktrees
   - Container crash leaves no leaked resources after expiry

6. **Manual verification**:
   - Container A (private) and B (public) with different access
   - Gateway restart while containers running
   - Mode restrictions enforced at both mount time AND operation time
   - Container in public mode cannot `git push` to private repo (even if somehow mounted)

## Dependencies

- Gateway sidecar must be running
- Requires existing repo visibility checking infrastructure (`repo_visibility.py`)

## References

- Related PRs: #631 (CLI flags for network mode)
- Existing implementation: `private_repo_policy.py`, `repo_visibility.py`
