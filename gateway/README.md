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
| `git push` | Branch ownership + Phase filter + Pipeline-push consensus | Branch has open PR authored by egg, OR branch starts with `egg-` or `egg/`, AND operation is allowed in current phase, AND in pipeline sessions push must come through consensus protocol (`consensus_push` marker) |
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

**Error messages:**
- `Push denied: Role 'X' cannot modify: <files>. <reason>` (HTTP 403) — Phase / contract / protected-file violation (non-agent-role restrictions)
- `Push denied: Could not verify file changes for security check: <error>` (HTTP 500) — Detection failure

**Agent-role restrictions reject on push.** As of [#2039](https://github.com/jwbron/egg/issues/2039), the gateway rejects any push whose diff modifies a path the pushing role cannot write. The handler attributes each commit in the unpushed range via the commit-authorship registry, partitions files into own-authored vs pulled-from-other-role, and checks the pushing role's write permissions against only the own-authored set. If any own-authored file is blocked, the push is rejected with `403 restricted_path_modified` carrying `role`, `blocked_paths`, `recommended_action`, and `doc_ref` — the agent's recovery is to drop the edits and re-propose with `--pre-merge-condition` per the conditional-ACK pattern ([#1998](https://github.com/jwbron/egg/issues/1998)). The `EGG_AGENT_RESTRICTIONS_ENFORCE=false` kill switch falls back to warn-only plain push. Pulled cross-role commits never block the push; phase / contract / protected-file / branch-ownership / private-mode / pipeline-push checks keep their existing `403` behavior. This replaced the silent-strip auto-filter from [#1882](https://github.com/jwbron/egg/issues/1882), which produced destructive deletions on the shared branch with no actionable signal to the agent. See [Gateway Auto-Filter Architecture](../docs/architecture/gateway-auto-filter.md) for the historical design and the commit-authorship registry that still backs attribution.

### Branch Lock (Pipeline Sessions)

Pipeline sessions are locked to their assigned worktree branch. The gateway blocks `git checkout` (branch-switching), `git switch`, and off-lineage `git reset` operations to prevent agents from moving off their assigned branch, which would break deterministic post-agent commit/push.

**How it works:**
- When a session is created with a `pipeline_id`, the worktree branch is recorded as `assigned_branch` on the `Session` object
- On every `git checkout`/`git switch` invocation, `is_branch_switch()` in `git_client.py` determines whether the command targets a branch (blocked) or files (allowed)
- File-level checkout (`git checkout -- file.txt`, `git checkout HEAD -- path/`) is always allowed
- Branch-creating flags (`-b`, `-B`, `--orphan`) and `git switch` (any form) are blocked
- On every `git reset <ref>` invocation (any mode), `extract_reset_target_ref()` in `git_client.py` extracts the target ref; the gateway then runs `git merge-base --is-ancestor <ref> HEAD` and blocks the reset if `<ref>` is not an ancestor of HEAD on the assigned branch. Path-mode resets (`git reset HEAD -- file`) are not affected (HEAD does not move)
- Fails closed: if the ancestry check cannot run (timeout, OS error), the reset is blocked

**Error messages:**
- `Branch switching is not allowed in pipeline sessions. You are locked to branch '<branch>'.` (HTTP 403)
- `Off-lineage 'git reset' is not allowed in pipeline sessions. Target ref '<ref>' is not an ancestor of HEAD on your assigned branch '<branch>'. To incorporate new commits from the remote, use 'git rebase origin/<branch>' instead.` (HTTP 403)

### Push-Target Enforcement (Pipeline Sessions)

Pipeline sessions must push only to their assigned branch. This prevents agents from improvising branch names when a push fails (e.g., pushing to `egg/issue-42-refine` instead of `egg/issue-42/work`), which would leave commits on an unexpected branch and break pipeline state tracking.

**How it works:**
- After session validation and before branch ownership checks, the gateway compares the push target branch against `session.assigned_branch`
- Both `pipeline_id` and `assigned_branch` must be set on the session for the check to activate
- Refspec formats like `local:remote` are supported — the remote portion is checked
- If the push target does not match the assigned branch, the push is rejected with HTTP 403

**Proactive upstream configuration:** When a pipeline worktree is created, the gateway sets `branch.<local>.remote=origin` and `branch.<local>.merge=refs/heads/<assigned_branch>` in the worktree's git config. This allows the sandbox's push client (`sandbox/egg_lib/orch_cli.py`) to build the correct `<local>:<assigned>` refspec automatically, so a naive `git push` produces a refspec that passes the push-target check without the agent constructing it by hand. Before this was added (#1809), missing upstream config caused agents to push to the per-container local branch name, which the gateway rejected.

**Killswitch:** Set `PUSH_TARGET_ENFORCEMENT=false` to disable (for emergency bypass).

**Error message:**
- `Pipeline sessions must push to their assigned branch '<assigned>'. Got '<attempted>'.` (HTTP 403)

### Pipeline Push Enforcement (BRC Sessions)

For every pipeline session, the gateway blocks direct `git push` operations. All pushes must go through the BRC consensus protocol via `mcp__brc__propose` (or the fallback CLI `egg-orch consensus propose --push`), which bundles the push with a proposal so all changes are peer-reviewed before landing on the branch. All SDLC producer phases (`refine`, `plan`, `implement`) are BRC phases ([`Pipeline.concurrent_phases`](../orchestrator/models.py)), so the enforcement is universal — there is no longer a "non-concurrent pipeline" path that allowed direct push ([#2028](https://github.com/jwbron/egg/issues/2028)).

**How it works:**
- The gateway checks pipeline-push enforcement BEFORE push-target enforcement so a pipeline agent on a per-role work branch sees the actionable "use mcp__brc__propose" error first, instead of a misleading wrong-branch error from the target check
- The check activates whenever the session has a `pipeline_id` and the push is not an infrastructure push (checkpoints, pipeline state). It no longer requires `EGG_CONCURRENT_MODE=true`
- When `mcp__brc__propose` (or `egg-orch consensus propose --push`) runs, it calls the gateway's `/api/v1/git/push` endpoint directly (bypassing the git wrapper) with `"consensus_push": true` in the JSON payload
- Pushes without the `consensus_push` marker are rejected with HTTP 403

**Why this matters:** Without this enforcement, agents can bypass the BRC review protocol by calling `git push` directly — changes land on the branch without peer review, breaking the "all changes must be reviewed" invariant. This was observed in pipeline #1570 v17, where the coder agent pushed 7 incremental commits without ever entering BRC consensus. Earlier versions of this check were gated on `EGG_CONCURRENT_MODE=true`, which left a gap: a pipeline session that didn't have that env var still hit a three-layer error cascade when it tried to push (sandbox wrapper → push-target validator → filtered-push fast-forward), thrashing the agent through wrong push variants ([#2028](https://github.com/jwbron/egg/issues/2028)). Gateway-level enforcement of the unconditional rule makes the invariant structural and gives a single, unambiguous error.

**Marker flow:**
```
mcp__brc__propose  (or  egg-orch consensus propose --push)
  └─→ calls gateway push API directly (bypasses git wrapper)
       └─→ includes "consensus_push": true in JSON payload
            └─→ gateway: allows push (marker present)

Fallback (no GATEWAY_URL, e.g. local dev):
  └─→ plain git push (no pipeline-push enforcement)
```

**Killswitch:** Set `PIPELINE_PUSH_ENFORCEMENT=false` to disable (for emergency bypass). The legacy `CONCURRENT_PUSH_ENFORCEMENT=false` still works as an alias.

**Error message:**
- `Direct git push is blocked for pipeline sessions. Publish your artifact via the mcp__brc__propose tool (which pushes to origin and sends CONSENSUS_PROPOSE in one step). Fallback CLI: \`egg-orch consensus propose --push\`.` (HTTP 403)

**Exempt scenarios:**
- Infrastructure pushes (checkpoint branches, pipeline state branches) — exempted before this check via `is_infrastructure_push`
- Non-pipeline sessions (no `pipeline_id`) — interactive/local sessions are unaffected

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

### Post-Agent Auto-Commit (Removed)

> **Removed in issue #1481.** The auto-commit-and-push behavior has been replaced by HITL recovery for uncommitted work. See [Post-Agent Commit Reference](../docs/reference/post-agent-commit.md) for details.

When a pipeline agent container exits with uncommitted changes, the orchestrator detects this and creates a HITL decision asking whether to recover or discard the work. No automatic commits are created.

**Rationale for removal:**
- Auto-commits bypassed BRC consensus — unreviewed code landed on the branch
- Auto-commits included files outside the agent's role boundaries, causing downstream push failures
- With per-agent worktree isolation, uncommitted work persists safely in the agent's worktree until cleanup

### Scoped Push File Detection

The gateway's `get_changed_files_in_push()` scopes file detection to the current agent's commits only, rather than diffing the entire branch history against `origin`. Since each agent has its own per-agent worktree, only that agent's commits exist in the worktree's history.

**Benefits:**
- Eliminates false positives where Agent A's push is rejected because Agent B committed files outside Agent A's role boundaries
- No commit attribution logic needed — worktree isolation provides structural scoping
- Simplifies the push validation pipeline by removing the need to filter other agents' commits

**Files:** `gateway/post_agent_commit.py`, `gateway/session_manager.py`

**Phase permissions** (also in `.egg/phase-permissions.json`):

| Phase | Allowed Operations | Blocked Operations | Exit Requires |
|-------|-------------------|-------------------|---------------|
| **refine** | `git push`, `egg-contract add-decision` | `gh issue comment/edit`, `gh pr create` | Human approval |
| **plan** | `git push`, `egg-contract add-decision` | `gh issue comment/edit`, `gh pr create` | Human approval |
| **implement** | `git push`, `egg-contract add-commit/update-notes` | `gh pr create` | All checks pass |
| **pr** | `gh pr create/edit`, `git push` | — | Human merge |

> **Note:** `egg-contract show *` is allowed in all phases for contract state viewing.

Phase transitions require specific roles (human, reviewer, implementer) as defined in the phase configuration.

## API Endpoints

### Session Management

```
POST /api/v1/sessions/create
  Request: {container_id, container_ip, mode, repos[]?, uid?, gid?, phase?}
  Auth: Bearer {launcher_secret}
  Description: Create a new session with optional SDLC phase tracking
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

**Worktree support:** Contract endpoints support per-agent worktree isolation. When a `container_id` is provided (via query parameter for GET, or request body for POST), the gateway maps the agent's container repo path to the correct worktree path using `map_container_path_to_worktree()` — the same mechanism used by git endpoints. When no `container_id` is provided (interactive sessions), the original path is used unchanged.

```
GET /api/v1/contract/<issue_number>
  Query: ?repo_path=<path>&include_audit_log=<bool>&container_id=<id>
  Policy: session_auth
  Description: Get contract state for an issue

POST /api/v1/contract/mutate
  Request: {issue_number, field_path, new_value, repo_path?, container_id?, actor?, reason?}
  Policy: session_auth + role_based
  Description: Apply a mutation to a contract (role determines allowed fields)

POST /api/v1/contract/validate
  Request: {field_path, new_value}
  Policy: session_auth
  Description: Validate a mutation without applying it

GET /api/v1/contract/exists/<issue_number>
  Query: ?repo_path=<path>&container_id=<id>
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
  Request: {container_id, repos, base_branch?, assigned_branch?, uid?, gid?}
  Policy: launcher_auth
  Description: Create git worktrees for isolated development.
               assigned_branch: when set, configures branch.<local>.merge so
               that naive `git push` targets this branch instead of the per-
               worktree local branch name (see Push-Target Enforcement)

POST /api/v1/worktree/delete
  Request: {container_id, force?}
  Policy: launcher_auth
  Description: Delete worktrees for a container

GET /api/v1/worktree/list
  Policy: launcher_auth
  Description: List all active worktrees
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
  Description: Proxy for Anthropic messages API with credential injection.
               Streaming responses survive upstream TCP resets — a bounded
               single pre-stream retry covers resets before any byte has
               been yielded downstream; mid-stream resets are surfaced as
               a synthetic SSE `event: error` frame so the SDK fails
               cleanly instead of dying on a truncated socket.
               See ../docs/architecture/credential-injection.md
               ("Upstream Stream Resilience") for design rationale.

POST /v1/messages/count_tokens
  Description: Proxy for Anthropic token counting API
```

### Health

```
GET /api/v1/health
  Response: {status, github_token_valid}
```

### Configuration

```
POST /api/v1/config/reload
  Auth: Bearer {launcher_secret}
  Description: Reload all cached configuration from disk/environment
  Response: {status: "ok", message: "Configuration reloaded"}
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
├── agent_restrictions.py   # Agent role-based file access restrictions (patterns for all 15+ roles, plus partition_files_by_role helper for the push restricted-path check)
├── commit_observer.py      # Inline observer for /api/v1/git/execute — registers every new commit SHA with the orchestrator commit-authorship registry (#1882)
├── commit_registry_client.py  # Shared-secret HTTP client for the orchestrator /api/v1/commit-authorship/{register,lookup} routes
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
│   ├── test_phase_worktree.py
│   ├── test_assigned_branch.py  # Push-target enforcement and branch lock tests
│   ├── test_pipeline_push_block.py  # Pipeline-push enforcement tests
│   ├── integration_test.sh
│   └── README-integration.md
└── README.md               # This file
```

## Configuration Reload

The gateway picks up changes to `repositories.yaml` without a restart via two mechanisms:

**Directory mount (automatic):** The config directory is bind-mounted (not the individual file), so inode-replacing editors (vim, nano, VS Code) are reflected immediately. The next request that reads the config will see the updated content.

**Explicit cache reload:** Some config values are cached in memory (bot identities, trusted users, checkpoint repos). To force an immediate reload:

```bash
# Via signal
docker kill -s HUP egg-gateway

# Via API (requires launcher secret)
curl -X POST -H "Authorization: Bearer $(cat ~/.config/egg/launcher-secret)" \
  http://localhost:9848/api/v1/config/reload
```

Both methods clear all in-memory config caches so the next access re-reads from disk.

**Note:** `GATEWAY_TRUSTED_USERS` is read from the process environment, which is fixed at container start time. Changing trusted users requires a container restart — SIGHUP will re-read the same environment value.

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

10. **Pipeline push enforcement**: For every pipeline session, direct `git push` is blocked — agents must use `mcp__brc__propose` (or the fallback CLI `egg-orch consensus propose --push`). This makes the "all changes must be reviewed" invariant structural rather than relying on agent compliance, and applies regardless of `EGG_CONCURRENT_MODE` since all SDLC producer phases are BRC phases ([#2028](https://github.com/jwbron/egg/issues/2028)). A `consensus_push` marker flows from the propose tool directly to the gateway API (bypassing the git wrapper), distinguishing protocol-originated pushes from direct pushes. A `PIPELINE_PUSH_ENFORCEMENT` killswitch (legacy alias: `CONCURRENT_PUSH_ENFORCEMENT`) follows the same pattern as `PUSH_TARGET_ENFORCEMENT` for emergency bypass.

11. **Upstream stream-reset resilience (Anthropic proxy)**: `proxy_anthropic_messages()` applies two asymmetric mitigations for `httpx.ReadError` / `httpx.RemoteProtocolError` on long-running SSE responses. A *pre-stream* retry (bounded to one attempt, gated on the first chunk not yet having been yielded downstream) transparently re-issues the upstream request when the reset lands before any downstream byte. A *mid-stream* synthetic SSE `event: error` frame is emitted when the reset lands after bytes have already flowed, because Anthropic exposes no resume tokens and mid-stream retry would risk double-charging and interleaving divergent generations. Full details in [credential-injection.md](../docs/architecture/credential-injection.md#upstream-stream-resilience). Distinct from gateway-pod-restart handling (#1883) and turn-1 consensus-wrapper retry (#1873).

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
- [Git Isolation](../docs/architecture/git-isolation.md) - Worktree isolation design
- [Credential Injection](../docs/architecture/credential-injection.md) - Zero-credential sandbox
- [Network Isolation](../docs/architecture/network-isolation.md) - Network modes
