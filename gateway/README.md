# Gateway Sidecar

Policy enforcement gateway for git/gh operations in egg containers.

## Overview

The gateway sidecar is the **trusted** component that holds credentials and validates all operations against ownership and approval rules. The sandbox container has no direct access to credentials; instead, all requests route through this gateway.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              HOST                                        │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │              Gateway Sidecar (Docker container)                     │ │
│  │  ┌─────────────┐  ┌─────────────────┐  ┌────────────────────────┐ │ │
│  │  │ REST API    │  │ Policy Engine   │  │ GitHub Client          │ │ │
│  │  │ :9848       │  │ - PR ownership  │  │ - Token holder         │ │ │
│  │  │             │  │ - Branch owner  │  │ - gh CLI executor      │ │ │
│  │  │             │  │ - Approval check│  │                        │ │ │
│  │  └──────┬──────┘  └────────┬────────┘  └────────────┬───────────┘ │ │
│  └─────────┼──────────────────┼────────────────────────┼──────────────┘ │
│            │ HTTP (Docker network)                     │                │
│  ┌─────────▼──────────────────────────────────────────────────────────┐ │
│  │                    Sandbox container(s)                             │ │
│  │  ┌─────────────┐   ┌─────────────┐                                 │ │
│  │  │ git wrapper │   │ gh wrapper  │   NO CREDENTIALS                │ │
│  │  │ calls API   │   │ calls API   │   Wrappers route to gateway     │ │
│  │  └─────────────┘   └─────────────┘                                 │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

## Policy Rules

### Ownership Policies

| Operation | Policy | Check |
|-----------|--------|-------|
| `git push` | Branch ownership + Phase filter | Branch has open PR authored by egg, OR branch starts with `egg-` or `egg/`, AND operation is allowed in current phase |
| `gh pr create` | Phase filter | Operation is allowed in current phase (typically only in 'pr' phase) |
| `gh pr comment` | PR ownership | PR must be authored by egg |
| `gh pr merge` | **BLOCKED** | No merge endpoint - human must merge via GitHub UI |
| `gh pr edit` | PR ownership | PR must be authored by egg |
| `gh pr close` | PR ownership | PR must be authored by egg |

**Bot variants for ownership check**: `egg`, `egg[bot]`, `app/egg`, `apps/egg`

**Branch ownership definition**:
- Branch has an open PR where author is an egg variant, OR
- Branch name starts with `egg-` or `egg/` (allows new branches before PR exists)

### Phase-Based Operation Restrictions

The gateway enforces phase-specific operation restrictions based on the current SDLC pipeline phase. This prevents operations like pushing code during planning or creating PRs during implementation.

**Implementation**: Each session tracks the current SDLC phase via the `phase` field. The phase is set during session creation from the `EGG_PIPELINE_PHASE` environment variable and can be updated via the `PATCH /api/v1/sessions/{token}/phase` endpoint. When operations like `gh pr create` are invoked, the gateway checks the session's phase against the allowed operations in `.egg/phase-permissions.json` and returns HTTP 403 if the operation is blocked.

**Phase permissions** (configured in `.egg/phase-permissions.json`):

### File-Level Access Restrictions

The gateway enforces file-level access restrictions to prevent certain roles from modifying protected files via `git push`. This protects sensitive files like SDLC contracts that should only be modified through dedicated APIs.

**Configuration** (in `.egg/phase-permissions.json`):

```json
{
  "file_restrictions": [
    {
      "role": "implementer",
      "blocked_patterns": [".egg-state/contracts/"],
      "blocked_reason": "Contract files can only be modified through the contract API"
    }
  ]
}
```

**Key behaviors:**
- Restrictions are role-based: each entry specifies a role and its blocked file patterns
- Pattern matching uses prefix matching on normalized file paths
- Path normalization prevents bypass via `./`, `../`, or `//` manipulation
- Fail-closed security: if file detection fails, push is blocked with HTTP 500
- Backwards compatibility: when session role is unavailable, file restrictions are skipped to support legacy sessions

**Error messages:**
- `Push denied: Role 'X' cannot modify: <files>. <reason>` (HTTP 403) - File blocked by restriction
- `Push denied: Could not verify file changes for security check: <error>` (HTTP 500) - Detection failure

**Phase permissions** (also in `.egg/phase-permissions.json`):

| Phase | Allowed Operations | Blocked Operations | Exit Requires |
|-------|-------------------|-------------------|---------------|
| **refine** | `gh issue comment/edit`, `egg-contract add-decision` | `git push`, `gh pr create` | Human approval |
| **plan** | `gh issue comment/edit`, `egg-contract add-decision` | `git push`, `gh pr create` | Human approval |
| **implement** | `git push`, `egg-contract add-commit/update-notes` | `gh pr create` | All checks pass |
| **pr** | `gh pr create/edit`, `git push` | — | Human merge |

> **Note:** `egg-contract show *` is allowed in all phases for contract state viewing.

Phase transitions require specific roles (human, reviewer, implementer) as defined in the phase configuration.

## API Endpoints

### Session Management

```
POST /api/v1/sessions/create
  Request: {container_id, container_ip, mode, repos[], uid?, gid?, phase?}
  Auth: Bearer {launcher_secret}
  Description: Create a new session with optional SDLC phase tracking

PATCH /api/v1/sessions/<session_token>/phase
  Request: {phase: "refine"|"plan"|"implement"|"pr"}
  Auth: Bearer {launcher_secret}
  Description: Update the SDLC pipeline phase for a session
```

### Git Operations

```
POST /api/v1/git/push
  Request: {repo_path, remote, refspec, force}
  Policy: branch_ownership + phase_filter

POST /api/v1/git/fetch
  Request: {repo_path, remote, operation, args[]}
  Policy: none (read operations)
  Operations: "fetch", "ls-remote"
```

### GitHub Operations

```
POST /api/v1/gh/pr/create
  Request: {repo, title, body, base, head}
  Policy: phase_filter

POST /api/v1/gh/pr/comment
  Request: {repo, pr_number, body}
  Policy: pr_ownership

POST /api/v1/gh/pr/edit
  Request: {repo, pr_number, title?, body?}
  Policy: pr_ownership

POST /api/v1/gh/pr/close
  Request: {repo, pr_number}
  Policy: pr_ownership

POST /api/v1/gh/execute
  Request: {args[], require_auth}
  Policy: filtered passthrough for read operations
  Note: For 'gh pr review' commands, automatically switches to reviewer
        token if available (separate GitHub App identity for posting
        approve/request-changes on bot-authored PRs)
```

### Phase Operations

```
POST /api/v1/phase/advance
  Request: {issue_number, repo_path?, reason?, actor?}
  Policy: session_auth
  Description: Advance pipeline to next phase

POST /api/v1/phase/filter
  Request: {issue_number, operation_type, command, repo_path?}
  Policy: session_auth
  Description: Check if operation is allowed in current phase

GET /api/v1/phase/current/<issue_number>
  Query: ?repo_path=<path>
  Policy: session_auth
  Description: Get current phase for an issue

GET /api/v1/phase/permissions/<phase>
  Policy: session_auth
  Description: Get allowed/blocked operations for a phase
```

### Contract Operations

The contract API provides role-based access to SDLC contracts that track issue progress through phases, tasks, and decisions.

```
GET /api/v1/contract/<issue_number>
  Query: ?repo_path=<path>&include_audit_log=<bool>
  Policy: session_auth
  Description: Get contract state for an issue

POST /api/v1/contract/mutate
  Request: {issue_number, field_path, new_value, repo_path?, actor?, reason?}
  Policy: session_auth + role_based
  Description: Apply a mutation to a contract (role determines allowed fields)

POST /api/v1/contract/validate
  Request: {field_path, new_value}
  Policy: session_auth
  Description: Validate a mutation without applying it

GET /api/v1/contract/exists/<issue_number>
  Query: ?repo_path=<path>
  Policy: session_auth
  Description: Check if a contract exists for an issue
```

**Role-based field ownership**: Mutations are validated against the caller's role:
- `implementer`: Can modify `tasks[].commit`, `tasks[].notes`, `tasks[].files_affected`
- `reviewer`: Can modify `tasks[].status`, `phases[].status`, `phases[].review_feedback`, `acceptance_criteria[].verified`, `current_phase`
- `human`: Can modify `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, `decisions[].resolved_at`, and all other fields

Role is determined from workflow context (session metadata), not request body, preventing privilege escalation.

### Health

```
GET /api/v1/health
  Response: {status, github_token_valid}
```

## Files

```
gateway/
├── gateway.py              # Flask REST API server
├── git_client.py           # Git operation handler
├── github_client.py        # GitHub API handler
├── policy.py               # Branch ownership, push policies
├── fork_policy.py          # Fork access policies
├── private_repo_policy.py  # Private/public repo access control
├── phase_filter.py         # Phase-based operation filtering
├── phase_transition.py     # Phase transition validation
├── phase_api.py            # Phase API endpoints
├── contract_api.py         # Contract API endpoints
├── auth.py                 # Session authentication
├── token_refresher.py      # GitHub App token management
├── anthropic_credentials.py # Anthropic API key injection
├── worktree_manager.py     # Git worktree lifecycle
├── session_manager.py      # Agent session management
├── repo_parser.py          # Repository config parsing
├── repo_visibility.py      # Repository visibility logic
├── proxy_monitor.py        # Squid proxy monitoring
├── rate_limiter.py         # Rate limiting
├── config_validator.py     # Configuration validation
├── error_messages.py       # Error message formatting
├── Dockerfile              # Gateway container image
├── entrypoint.sh           # Gateway startup script
├── squid.conf              # Squid proxy config (private mode)
├── squid-allow-all.conf    # Squid proxy config (public mode)
├── scripts/                # Helper scripts
├── tests/                  # Unit and integration tests
│   ├── test_gateway.py
│   ├── test_gateway_integration.py
│   ├── test_git_client.py
│   ├── test_git_validation.py
│   ├── test_policy.py
│   ├── test_private_repo_policy.py
│   ├── test_proxy_security.py
│   ├── test_rate_limiter.py
│   ├── test_repo_parser.py
│   ├── test_repo_visibility.py
│   ├── test_session_manager.py
│   ├── test_token_refresher.py
│   ├── test_worktree_manager.py
│   ├── test_phase_filter.py
│   ├── test_phase_transition.py
│   ├── test_phase_api.py
│   ├── test_contract_api.py
│   ├── integration_test.sh
│   └── README-integration.md
└── README.md               # This file
```

## Design Decisions

1. **No merge capability**: Gateway does not expose a merge endpoint. Human must merge via GitHub UI. This maintains the existing safety model.

2. **Branch ownership**: Branch has an open egg-authored PR OR starts with `egg-` or `egg/`. This allows pushing to new branches before a PR exists.

3. **Phase-based filtering**: Operations are filtered based on the current SDLC pipeline phase. Configuration is loaded from `.egg/phase-permissions.json` with schema validation. This prevents incidents like pushing code during planning phases.

4. **Token source**: In-memory token refresh via `token_refresher.py`. Tokens are refreshed automatically 15 minutes before expiry. The gateway supports an optional reviewer token (separate GitHub App) for posting approve/request-changes reviews on bot-authored PRs—GitHub blocks self-approval, so a second identity is required for full review capabilities.

5. **Dual network modes**: Squid proxy controls outbound access. Private mode restricts to Anthropic API only; public mode allows all traffic.

6. **Role-based contract mutations**: Contract field ownership is tied to roles (implementer, reviewer, human). Role is determined from workflow context via session metadata, not request body, preventing privilege escalation. The `egg_contracts` shared library provides Pydantic models and validation.

## Testing

```bash
# Run gateway tests (via act, CI parity)
make test

# Run gateway tests directly
.venv/bin/pytest gateway/tests/ -v

# Run specific test
.venv/bin/pytest gateway/tests/test_policy.py -v
```

## Related Documentation

- [Architecture Overview](../docs/architecture/README.md) - System design
- [ADR: Git Isolation](../docs/adr/implemented/ADR-Git-Isolation-Architecture.md) - Worktree isolation design
- [ADR: Credential Injection](../docs/adr/implemented/ADR-Gateway-Credential-Injection.md) - Zero-credential sandbox
- [ADR: Internet Lockdown](../docs/adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) - Network modes
