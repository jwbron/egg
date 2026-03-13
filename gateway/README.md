# Gateway Sidecar

Policy enforcement gateway for git/gh operations in egg containers.

## Overview

The gateway sidecar is the **trusted** component that holds credentials and validates all operations against ownership and approval rules. The sandbox container has no direct access to credentials; instead, all requests route through this gateway.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              HOST                                       │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │              Gateway Sidecar (Docker container)                    │ │
│  │  ┌─────────────┐  ┌─────────────────┐  ┌────────────────────────┐  │ │
│  │  │ REST API    │  │ Policy Engine   │  │ GitHub Client          │  │ │
│  │  │ :9848       │  │ - PR ownership  │  │ - Token holder         │  │ │
│  │  │             │  │ - Branch owner  │  │ - gh CLI executor      │  │ │
│  │  │             │  │ - Approval check│  │                        │  │ │
│  │  └──────┬──────┘  └────────┬────────┘  └────────────┬───────────┘  │ │
│  └─────────┼──────────────────┼────────────────────────┼──────────────┘ │
│            │ HTTP (Docker network)                     │                │
│  ┌─────────▼──────────────────────────────────────────────────────────┐ │
│  │                    Sandbox container(s)                            │ │
│  │  ┌─────────────┐   ┌─────────────┐                                 │ │
│  │  │ git wrapper │   │ gh wrapper  │   NO CREDENTIALS                │ │
│  │  │ calls API   │   │ calls API   │   Wrappers route to gateway     │ │
│  │  └─────────────┘   └─────────────┘                                 │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

## Policy Rules

### Ownership Policies

| Operation | Policy | Check |
|-----------|--------|-------|
| `git push` | Branch ownership + Phase filter | Branch has open PR authored by egg, OR branch starts with `egg-` or `egg/`, AND operation is allowed in current phase |
| `gh pr create` | Phase filter + mode policy | Operation is allowed in current phase (typically only in 'pr' phase)<br>In user mode, PR is forced to draft<br>Blocked in reviewer mode |
| `gh pr comment` | Allowed on any PR | PR must exist and be accessible |
| `gh pr merge` | **BLOCKED** | No merge endpoint - human must merge via GitHub UI |
| `gh pr edit` | PR ownership | PR must be authored by egg or configured user |
| `gh pr close` | PR ownership | PR must be authored by egg or configured user |
| `gh api PATCH repos/.../issues/comments/{id}` | Comment ownership | Comment must be authored by egg or configured user |
| `gh api PATCH repos/.../pulls/comments/{id}` | Comment ownership | Comment must be authored by egg or configured user |
| `gh api PATCH repos/.../comments/{id}` (commits) | Comment ownership | Comment must be authored by egg or configured user |
| `gh api POST repos/.../issues/{id}/labels` | Issue/PR ownership | Issue or PR must be authored by egg or configured user |
| `gh api POST repos/.../pulls/{id}/requested_reviewers` | PR ownership | PR must be authored by egg or configured user |
| `gh api POST repos/.../pulls/{id}/reviews` | Allowed on any PR | PR must exist and be accessible |

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
- **Tier-aware access**: Agent file restrictions accept an optional `complexity_tier` parameter. In Tier 3 (`high`), the Integrator role uses `INTEGRATOR_TIER3_PATTERNS` which grants write access to source, test, and documentation directories for fixing integration issues across phase boundaries (while still blocking `.egg-state/contracts/` and `.github/`)
- **Coordinator role**: The `coordinator` agent role is restricted to `.egg-state/agent-outputs/` only. All source code, docs, tests, contracts, drafts, reviews, and `.github/` are blocked. This ensures the coordinator operates purely as an orchestration layer without modifying pipeline artifacts directly (defined in `agent_restrictions.py` as `COORDINATOR_PATTERNS`)

**Error messages:**
- `Push denied: Role 'X' cannot modify: <files>. <reason>` (HTTP 403) - File blocked by restriction
- `Push denied: Could not verify file changes for security check: <error>` (HTTP 500) - Detection failure

### Per-Task File Restrictions (Implement Phase)

When the planner defines `files:` per task, the gateway enforces these as a push-time boundary for implement-phase coder agents. This prevents agents from accidentally modifying files outside their assigned task scope — critical for Tier 3 parallel dispatch where multiple coders work concurrently.

**How it works:**
- The orchestrator collects the union of `files_affected` from all tasks assigned to a coder agent and passes them as `allowed_files` during session creation
- For each listed file with a directory component (e.g., `src/auth/login.py`), the directory is auto-expanded to `src/auth/*`, granting recursive sibling access via `fnmatch` matching
- Glob patterns in the file list (e.g., `tests/**`) pass through unchanged
- On push, changed files are checked against the session's `allowed_files` using `PhaseFileRestriction.is_file_allowed()`

**Warn-then-block escalation:**
- First push containing an out-of-scope file: **warning** logged (push allowed)
- Second push with the same file: **blocked** (HTTP 403)
- Violation counts are tracked per-file on the `Session._warned_files` dict (transient, not persisted)

**Strict mode:** Set `EGG_TASK_FILE_RESTRICTIONS_ENFORCE=true` to block immediately on first violation (no warning phase). The warning threshold is configurable via `EGG_TASK_FILE_WARN_THRESHOLD` (default: `1`).

**Graceful fallback:** When `allowed_files` is `None` or empty (non-pipeline sessions, non-coder roles, or tasks without `files_affected`), no per-task restriction is applied — only phase-level enforcement.

**Post-agent auto-commit:** The auto-commit path (`post_agent_commit.py`) applies the same per-task filtering. Files outside the task's allowlist are restored (not committed) with clear logging via `post_agent_task_filter` events.

**Files:** `gateway/gateway.py` (push validation), `gateway/session_manager.py` (`allowed_files` field), `gateway/post_agent_commit.py` (auto-commit filtering)

### Branch Lock (Pipeline Sessions)

Pipeline sessions are locked to their assigned worktree branch. The gateway blocks `git checkout` (branch-switching) and `git switch` operations to prevent agents from moving off their assigned branch, which would break deterministic post-agent commit/push.

**How it works:**
- When a session is created with a `pipeline_id`, the worktree branch is recorded as `assigned_branch` on the `Session` object
- On every `git checkout`/`git switch` invocation, `is_branch_switch()` in `git_client.py` determines whether the command targets a branch (blocked) or files (allowed)
- File-level checkout (`git checkout -- file.txt`, `git checkout HEAD -- path/`) is always allowed
- Branch-creating flags (`-b`, `-B`, `--orphan`) and `git switch` (any form) are blocked

**Error message:**
- `Branch switching is not allowed in pipeline sessions. You are locked to branch '<branch>'.` (HTTP 403)

### Push-Target Enforcement (Pipeline Sessions)

Pipeline sessions must push only to their assigned branch. This prevents agents from improvising branch names when a push fails (e.g., pushing to `egg/issue-42-refine` instead of `egg/issue-42/work`), which would leave commits on an unexpected branch and break pipeline state tracking.

**How it works:**
- After session validation and before branch ownership checks, the gateway compares the push target branch against `session.assigned_branch`
- Both `pipeline_id` and `assigned_branch` must be set on the session for the check to activate
- Refspec formats like `local:remote` are supported — the remote portion is checked
- If the push target does not match the assigned branch, the push is rejected with HTTP 403

**Killswitch:** Set `PUSH_TARGET_ENFORCEMENT=false` to disable (for emergency bypass).

**Error message:**
- `Pipeline sessions must push to their assigned branch '<assigned>'. Got '<attempted>'.` (HTTP 403)

**Pipeline-aware push error messages:** When a push is rejected due to phase file restrictions (e.g., branch history contains files from prior phases), pipeline sessions receive a targeted error message directing the agent to signal the error via `egg-orch signal error` rather than attempting workarounds. Non-pipeline sessions continue to see the original generic hint about creating a clean branch.

### Commit-Time Validation (Pipeline Sessions)

In addition to push-time file restriction enforcement, the gateway validates staged files at `git commit` time for pipeline sessions. This catches violations early—before the agent spends tokens on building up commits that would be rejected at push.

**How it works:**
- When a pipeline session runs `git commit`, the gateway inspects `git diff --cached --name-only` for staged files
- Staged files are checked against `PhaseFileRestriction.blocked_patterns` for the session's current phase
- If any staged files violate phase restrictions, the commit is rejected with a list of blocked files and guidance on how to unstage them

**Error message:**
- `Commit blocked: <phase restriction message>. Unstage the blocked files with 'git reset HEAD <file>'.` (HTTP 403)

**Defense-in-depth:** Commit-time validation is a complement to push-time validation, not a replacement. If the commit-time check encounters an error, it fails open—the push-time check remains the authoritative gate.

### Post-Agent Auto-Commit

When a pipeline agent container exits (normally or via timeout), the gateway automatically commits any uncommitted changes in the agent's worktree so that work-in-progress is never lost.

**How it works:**
- Triggered during session cleanup in `session_manager.py`, before checkpoint capture
- Only runs for pipeline sessions (`pipeline_id` is set)
- Uses `post_agent_commit.auto_commit_worktree()` which:
  1. Detects uncommitted changes via `git status --porcelain`
  2. When a phase is set, imports `check_phase_file_restrictions` from `phase_filter` to classify files as allowed or blocked
  3. Restores blocked files to their committed state via `git checkout -- <file>`
  4. Stages only allowed files via `git add -- <file1> <file2> ...` (not `git add -A`)
  5. Commits with a descriptive WIP message and `egg <egg@localhost>` author
  6. If a session token and gateway URL are available, pushes via the gateway API (`/api/v1/git/push`) so push policy is enforced
- Commit message format: `WIP: auto-commit uncommitted work (<role>) [<pipeline_id>]`
- Errors during auto-commit do not block session cleanup (fail-safe)

**Phase filtering defense-in-depth:** The auto-commit reuses `check_phase_file_restrictions()` from `phase_filter.py` (the same function used by push-time validation) rather than reimplementing restriction logic. This ensures consistent enforcement across all code paths.

**Files:** `gateway/post_agent_commit.py`, `gateway/session_manager.py`

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
  Request: {container_id, container_ip, mode, repos[]?, uid?, gid?, phase?, allowed_files?}
  Auth: Bearer {launcher_secret}
  Description: Create a new session with optional SDLC phase tracking and per-task file restrictions
  Note: repos[] is required for private/public modes (visibility filtering + worktree creation)
        but optional for local mode (orchestrator-internal temp sessions for git auth only)

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
  Request: {repo, title, body, base, head, draft? (bool)}
  Policy: phase_filter + mode_policy (user mode forces draft=true; reviewer mode blocked)

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

### Worktree Operations

```
POST /api/v1/worktree/create
  Request: {repo_path, branch, base_branch?}
  Policy: session_auth
  Description: Create a new git worktree for isolated development

POST /api/v1/worktree/delete
  Request: {worktree_path}
  Policy: session_auth
  Description: Delete a worktree

GET /api/v1/worktree/list
  Policy: session_auth
  Description: List active worktrees
```

### Session Management (Extended)

```
DELETE /api/v1/sessions/<session_token>
  Auth: Bearer {launcher_secret}
  Description: Delete a session

DELETE /api/v1/sessions/by-container/<container_id>
  Auth: Bearer {launcher_secret}
  Description: Delete sessions for a specific container

GET /api/v1/sessions/<session_token>
  Auth: Bearer {launcher_secret}
  Description: Get session details

POST /api/v1/sessions/<session_token>/heartbeat
  Auth: Bearer {launcher_secret}
  Description: Send session heartbeat

PATCH /api/v1/sessions/<session_token>
  Auth: Bearer {launcher_secret}
  Description: Update session metadata

GET /api/v1/sessions
  Auth: Bearer {launcher_secret}
  Description: List all active sessions
```

### Repository Operations

```
GET /api/v1/repos/visibility
  Policy: session_auth
  Description: Get repository visibility information (public/private)
```

### Checkpoint Operations

The checkpoint API provides read access to agent session checkpoints stored on the `egg/checkpoints/v2` branch. These endpoints enable checkpoint access in the sandbox when checkpoints are stored in an external repository. The `repo_path` query parameter is inferred from the environment if omitted.

**Inter-agent message capture:** When `EGG_CONCURRENT_MODE=true`, the checkpoint handler fetches inter-agent messages from the orchestrator message bus (`/api/v1/pipelines/{id}/messages`) during both commit-triggered and session-end checkpoint creation. Messages are stored in the checkpoint's `inter_agent_messages` field with direction (`sent`/`received`) relative to the checkpointed agent. This enables post-hoc analysis of agent collaboration patterns. See `checkpoint_handler.py:_fetch_inter_agent_messages()`.

```
GET /api/v1/checkpoints
  Query: ?repo_path=<path>&issue=<n>&pr=<n>&branch=<name>&session=<id>
         &trigger=<type>&status=<status>&agent_type=<type>&phase=<phase>
         &pipeline=<id>&repo=<owner/repo>&limit=<n>
  Policy: session_auth
  Description: List checkpoint summaries with optional filters (default limit: 50)

GET /api/v1/checkpoints/cost
  Query: ?repo_path=<path>&pipeline=<id>&issue=<n>&pr=<n>&limit=<n>
  Policy: session_auth
  Description: Get cost breakdown (token usage and USD) for matching checkpoints (default limit: 500)
  Response: {checkpoint_count, total_input_tokens, total_output_tokens, total_cost_usd, breakdown[]}

GET /api/v1/checkpoints/<identifier>
  Path: identifier (checkpoint ID or commit SHA)
  Query: ?repo_path=<path>
  Policy: session_auth
  Description: Get full checkpoint details by ID (ckpt-...) or commit SHA
```

### Git Execute

```
POST /api/v1/git/execute
  Request: {args[], repo_path?}
  Policy: session_auth
  Description: Execute arbitrary git commands (read operations)
```

### Anthropic Proxy

```
POST /v1/messages
  Description: Proxy for Anthropic messages API with credential injection

POST /v1/messages/count_tokens
  Description: Proxy for Anthropic token counting API
```

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
├── session_manager.py      # Agent session management (includes post-agent auto-commit trigger)
├── post_agent_commit.py    # Post-agent auto-commit for uncommitted worktree changes
├── repo_parser.py          # Repository config parsing
├── repo_visibility.py      # Repository visibility logic
├── proxy_monitor.py        # Squid proxy monitoring
├── rate_limiter.py         # Rate limiting
├── config_validator.py     # Configuration validation
├── error_messages.py       # Error message formatting
├── agent_restrictions.py   # Agent role-based file access restrictions
├── checkpoint_handler.py   # Session checkpoint handling
├── transcript_buffer.py    # Transcript buffering for agent sessions
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
│   ├── test_proxy_monitor.py
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
│   ├── test_checkpoint_handler.py
│   ├── test_checkpoint_inter_agent.py  # Inter-agent message capture in concurrent mode
│   ├── test_concurrency.py
│   ├── test_config_validator.py
│   ├── test_edge_cases.py
│   ├── test_error_paths.py
│   ├── test_fork_policy.py
│   ├── test_transcript_buffer.py
│   ├── test_integrator_tier3.py
│   ├── test_phase_worktree.py
│   ├── test_assigned_branch.py  # Push-target enforcement and branch lock tests
│   ├── test_session_file_restrictions.py  # Per-task file restriction enforcement tests
│   ├── test_post_agent_commit.py  # Post-agent auto-commit with task filtering tests
│   ├── integration_test.sh
│   └── README-integration.md
└── README.md               # This file
```

## Design Decisions

1. **No merge capability**: Gateway does not expose a merge endpoint. Human must merge via GitHub UI. This maintains the existing safety model.

2. **Branch ownership**: Branch has an open egg-authored PR OR starts with `egg-` or `egg/`. This allows pushing to new branches before a PR exists.

3. **Phase-based filtering**: Operations are filtered based on the current SDLC pipeline phase. Configuration is loaded from `.egg/phase-permissions.json` with schema validation. This prevents incidents like pushing code during planning phases.

4. **Token source**: In-memory token refresh via `token_refresher.py`. Tokens are refreshed automatically 15 minutes before expiry. The gateway supports an optional reviewer token (separate GitHub App) for posting approve/request-changes reviews on bot-authored PRs—GitHub blocks self-approval, so a second identity is required for full review capabilities.

5. **Dual network modes**: Squid proxy controls outbound access. Private mode restricts to Anthropic API only; public mode allows all traffic. Checkpoint operations (repositories configured via `checkpoint_repo` and pushes to the `egg/checkpoints/v2` branch) are exempt from private mode restrictions and always allowed.

6. **Role-based contract mutations**: Contract field ownership is tied to roles (implementer, reviewer, human). Role is determined from workflow context via session metadata, not request body, preventing privilege escalation. The `egg_contracts` shared library provides Pydantic models and validation.

7. **Defense-in-depth enforcement**: Phase file restrictions are enforced at multiple layers—readonly filesystem mounts (OS level), commit-time validation (gateway), and push-time validation (gateway). Each layer catches violations earlier, reducing wasted agent tokens and preventing bypass vectors.

8. **Branch lock for pipeline sessions**: Pipeline agents are locked to their worktree branch to ensure deterministic post-agent commit/push. The `Session.assigned_branch` field is set during session creation when a pipeline ID is present.

9. **Push-target enforcement**: Pipeline agents must push to their assigned branch only. When a push to the assigned branch fails (e.g., due to phase file restrictions from branch history contamination), agents must signal an error rather than improvise a new branch name. This prevents commits from landing on unexpected branches where the pipeline cannot find them.

10. **Per-task file restrictions**: In Tier 3 parallel dispatch, multiple coder agents work on different plan phases concurrently. The planner's `files:` field per task is enforced as a push-time boundary (not just informational context). The warn-then-block escalation prevents accidental cross-contamination while avoiding hard blocks on first attempt. Directory-sibling expansion (listing `dir/foo.py` grants access to `dir/*`) ensures agents aren't caged by overly precise file lists.

## Testing

```bash
# Run gateway tests (same checks as GitHub Actions)
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
