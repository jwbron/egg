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

| Operation | Policy | Check |
|-----------|--------|-------|
| `git push` | Branch ownership | Branch has open PR authored by egg, OR branch starts with `egg-` or `egg/` |
| `gh pr create` | Always allowed | egg can create PRs on any branch it can push to |
| `gh pr comment` | PR ownership | PR must be authored by egg |
| `gh pr merge` | **BLOCKED** | No merge endpoint - human must merge via GitHub UI |
| `gh pr edit` | PR ownership | PR must be authored by egg |
| `gh pr close` | PR ownership | PR must be authored by egg |

**Bot variants for ownership check**: `egg`, `egg[bot]`, `app/egg`, `apps/egg`

**Branch ownership definition**:
- Branch has an open PR where author is an egg variant, OR
- Branch name starts with `egg-` or `egg/` (allows new branches before PR exists)

## API Endpoints

```
POST /api/v1/git/push
  Request: {repo_path, remote, refspec, force}
  Policy: branch_ownership

POST /api/v1/git/fetch
  Request: {repo_path, remote, operation, args[]}
  Policy: none (read operations)
  Operations: "fetch", "ls-remote"

POST /api/v1/gh/pr/create
  Request: {repo, title, body, base, head}
  Policy: none (always allowed)

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
│   ├── integration_test.sh
│   └── README-integration.md
└── README.md               # This file
```

## Design Decisions

1. **No merge capability**: Gateway does not expose a merge endpoint. Human must merge via GitHub UI. This maintains the existing safety model.

2. **Branch ownership**: Branch has an open egg-authored PR OR starts with `egg-` or `egg/`. This allows pushing to new branches before a PR exists.

3. **Token source**: In-memory token refresh via `token_refresher.py`. Tokens are refreshed automatically 15 minutes before expiry.

4. **Dual network modes**: Squid proxy controls outbound access. Private mode restricts to Anthropic API only; public mode allows all traffic.

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
- [Troubleshooting: GitHub Auth](../docs/troubleshooting/github-auth-in-long-running-containers.md) - Token refresh issues
