# Gateway Sidecar

Policy enforcement gateway for git/gh operations in egg containers.

## Overview

The gateway sidecar holds GitHub credentials and validates all GitHub operations against ownership and approval rules. Containers no longer have direct access to `GITHUB_TOKEN`; instead, they route requests through this gateway.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              HOST                                        │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │              Gateway Sidecar (systemd service)                      │ │
│  │  ┌─────────────┐  ┌─────────────────┐  ┌────────────────────────┐ │ │
│  │  │ REST API    │  │ Policy Engine   │  │ GitHub Client          │ │ │
│  │  │ :9848       │  │ - PR ownership  │  │ - GITHUB_TOKEN holder  │ │ │
│  │  │             │  │ - Branch owner  │  │ - gh CLI executor      │ │ │
│  │  │             │  │ - Approval check│  │                        │ │ │
│  │  └──────┬──────┘  └────────┬────────┘  └────────────┬───────────┘ │ │
│  └─────────┼──────────────────┼────────────────────────┼──────────────┘ │
│            │ HTTP (Docker network)                     │                │
│  ┌─────────▼──────────────────────────────────────────────────────────┐ │
│  │                    egg container(s)                                 │ │
│  │  ┌─────────────┐   ┌─────────────┐                                 │ │
│  │  │ git wrapper │   │ gh wrapper  │   NO GITHUB_TOKEN               │ │
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

**Bot identity for ownership check**: The agent's GitHub username (configured via `GATEWAY_BOT_NAME`)

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
host-services/gateway/
├── gateway.py              # Flask REST API server
├── policy.py               # Policy enforcement logic
├── github_client.py        # Wraps gh CLI with token management
├── git_client.py           # Git path/arg validation, credential helpers
├── setup.sh                # Installation script
├── gateway.service # Systemd unit file
├── tests/                  # Unit tests
│   ├── test_policy.py
│   └── test_gateway.py
└── README.md               # This file
```

## Implementation Phases

### Phase 1: Gateway Service (Foundation)
- [x] Create directory structure
- [x] Implement `gateway.py` - Flask app with REST endpoints
- [x] Implement `github_client.py` - wraps `gh` CLI with token management
- [x] Create systemd service file
- [x] Add health check endpoint

### Phase 2: Policy Engine
- [x] Implement `policy.py` with:
  - `check_pr_ownership(repo, pr_number)` - verify egg is author
  - `check_branch_ownership(repo, branch)` - verify branch tied to egg's PR or bot-prefixed
- [x] Add PR info caching to reduce GitHub API calls
- [x] Write tests for policy logic

### Phase 3: Wrapper Modifications
- [x] Modify `sandbox/scripts/git` to call gateway for push
- [x] Modify `sandbox/scripts/gh` to route commands through gateway
- [x] Update `sandbox/egg` to:
  - Add `--add-host=host.docker.internal:host-gateway` for Linux
  - Set `GATEWAY_URL` environment variable

### Phase 4: Integration
- [x] Test full workflow: container -> gateway -> GitHub
- [x] Add audit logging for all policy decisions
- [x] Update CLAUDE.md rules about merge capability

## Design Decisions

1. **No merge capability**: Gateway does not expose a merge endpoint. Human must merge via GitHub UI. This maintains the existing safety model.

2. **Branch ownership**: Branch has an open egg-authored PR OR starts with `egg-` or `egg/`. This allows pushing to new branches before a PR exists.

3. **Token source**: PAT from environment variable (`GITHUB_TOKEN`). Set up for the agent's dedicated GitHub user account.

## Testing

```bash
# Unit tests
pytest host-services/gateway/tests/

# Manual test - push (should succeed for egg's branch)
git push origin egg-test-branch

# Manual test - push (should fail for main)
git push origin main  # ERROR: branch not owned by egg

# Manual test - PR comment (should succeed for egg's PR)
gh pr comment 123 --body "test"

# Manual test - merge blocked
gh pr merge 123  # ERROR: merge not supported
```

## Installation

```bash
./host-services/gateway/setup.sh
systemctl --user enable --now gateway
```
