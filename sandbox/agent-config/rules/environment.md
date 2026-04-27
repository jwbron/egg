# Sandboxed Environment

Sandboxed Docker container. No SSH keys, cloud creds, or production access.

## Network Modes

- **Public mode** (default): Full internet + public repos only. Can access PyPI, npm, web.
- **Private mode**: Anthropic API only + private repos only. No PyPI/npm/web access — dependencies are pre-installed.

GitHub access MUST go through the gateway sidecar (not the proxy) for policy enforcement.

## Environment Flags

| Variable | Default | Purpose |
|----------|---------|---------|
| `EGG_MCP_TOOLS` | unset (on) | **On by default since #1942.** Registers the in-process SDK MCP tool surface (30 verbs across 6 namespaces: `mcp__sdlc__*`, `mcp__brc__*`, `mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`, `mcp__checkpoint__*`) on `ClaudeAgentOptions.mcp_servers` and appends a bootstrap paragraph to the system prompt. Prefer these tools over Bash-ing `egg-contract` / `egg-orch` / `egg-checkpoint`. Set to `false` / `0` / `no` / `off` to opt out; the code path is then byte-identical to pre-#1765 behaviour. See `../../../docs/reference/agent-tools.md`. Currently `claude_agent_sdk`-only; `EGG_HARNESS=egg` is not yet covered. |

## Capabilities

**CAN**: Read/edit `~/repos/`, run tests, `git push` (HTTPS), `gh` CLI, PostgreSQL, Redis, Python, Node.js, Go, Java

**CANNOT**: Merge PRs, SSH push, deploy, access production, access GitHub tokens directly

## Gateway Sidecar

All git/gh operations routed through gateway. Key restrictions:
- `git push`: Only to `egg/`-prefixed branches (or branches with your open PR)
- `git worktree add/remove`: **Unsupported** — use `git checkout -b` instead
- `git checkout/switch` (branch): **Blocked in pipeline mode**
- `git commit`: **Phase-validated** — staged files must comply with phase restrictions
- `gh pr merge`: **Blocked** — human must merge via GitHub UI

If push fails: check `git remote -v` is HTTPS, check `curl http://egg-gateway:9848/api/v1/health`, verify branch is egg-owned.

### Jira Wrapper (`jira`)

The `sandbox/scripts/jira` wrapper is the only way for the sandbox to reach Jira — it POSTs to the gateway's `/api/v1/jira/*` routes with `Authorization: Bearer $EGG_SESSION_TOKEN` and Atlassian credentials never enter the sandbox. **Private network mode only**: in public mode every Jira call returns 403 `private_mode_required` before any upstream request.

| Verb | Gateway route |
|------|---------------|
| `jira ticket get <KEY> [--fields f1,f2]` | `POST /api/v1/jira/ticket/get` |
| `jira search '<JQL>' [--fields ...] [--max-results N] [--next-page-token TOK]` | `POST /api/v1/jira/search` |
| `jira ticket comments <KEY>` | `POST /api/v1/jira/ticket/comments` |
| `jira execute <METHOD> <PATH> [--query k=v,...] [--body-file path]` | `POST /api/v1/jira/execute` (GET-only) |

**Environment variables** (advisory, set by the orchestrator):

| Variable | Meaning |
|----------|---------|
| `EGG_JIRA_TICKET` | The ticket the pipeline is scoped to (e.g. `ENG-1234`); empty if the pipeline has no Jira ticket |
| `EGG_JIRA_PROJECT` | Optional project hint; empty if absent |

Both are **advisory only** — the project allowlist (`config/context-filters.yaml` → `jira.projects:`) is the only hard boundary enforced by the gateway.

**Example:**
```bash
# Read the ticket the pipeline is scoped to
jira ticket get "$EGG_JIRA_TICKET"

# Search within an allowlisted project (JQL must statically scope to allowlisted projects)
jira search 'project = ENG AND status = "Open"'

# Read comments
jira ticket comments "$EGG_JIRA_TICKET"

# This WILL be rejected with 403 jira_search_rejected — the JQL scope extractor
# denies on ambiguity, so any `OR` clause containing `project` is refused even
# when every candidate is allowlisted.
jira search 'project = ENG OR project = SEC'
```

**Hard limits (always denied):** `transitions`, `worklog`, `attachments`, `watchers`, HTTP `DELETE` / `PUT` / `PATCH`, path traversal (`..`), duplicate slashes, non-ASCII keys. Non-GET `execute` calls return 403 regardless of the path. See [Jira Wrapper Reference](../../../docs/reference/jira-wrapper.md) for the full endpoint surface, JQL scope extractor rules, and the `not_found` response envelope.

### Confluence Wrapper (`confluence`)

The `sandbox/scripts/confluence` wrapper is the only way for the sandbox to reach Confluence — it POSTs to the gateway's `/api/v1/confluence/*` routes with `Authorization: Bearer $EGG_SESSION_TOKEN` and Atlassian credentials never enter the sandbox. **Private network mode only**: in public mode every Confluence call returns 403 `private_mode_required` before any upstream request.

| Verb | Gateway route |
|------|---------------|
| `confluence page get <PAGE_ID> [--body-format storage,atlas_doc_format] [--expand ...]` | `POST /api/v1/confluence/page/get` |
| `confluence page descendants <PAGE_ID> [--depth N] [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/page/descendants` |
| `confluence page footer-comments <PAGE_ID> [--include-replies] [--body-format ...] [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/page/footer-comments` |
| `confluence page inline-comments <PAGE_ID> [--body-format ...] [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/page/inline-comments` |
| `confluence space pages <SPACE_KEY> [--body-format ...] [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/space/pages` |
| `confluence space list [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/space/list` |
| `confluence search '<CQL>' [--limit N] [--cursor TOK]` | `POST /api/v1/confluence/search` |
| `confluence execute <METHOD> <PATH> [--query k=v,...] [--body-file path]` | `POST /api/v1/confluence/execute` (GET-only) |

**No per-pipeline env var.** Unlike Jira (`EGG_JIRA_TICKET`), Confluence has **no orchestrator-exported env var** — agents pass `pageId` / `spaceKey` directly in each call. Confluence is reference material, not a primary unit of work; the gateway recovers `pageId` / `spaceKey` from each request body or response for audit purposes.

**Default body format.** Reads default to `body-format=storage` (smallest payload, Confluence's internal XHTML-like format). Pass `--body-format atlas_doc_format` for the ADF JSON tree, or `--body-format view` for rendered HTML.

**Example:**
```bash
# Read a referenced page from a Jira ticket — pass the numeric pageId
confluence page get 12345

# Search within an allowlisted space (CQL must statically scope to allowlisted spaces)
confluence search 'space = ENG AND text ~ "RFC"'

# List spaces visible to the agent (response is filtered to the allowlist)
confluence space list

# Read inline comments — wrapper transparently retries against v1 if v2 returns 404
confluence page inline-comments 12345

# This WILL be rejected with 403 confluence_search_rejected — the CQL scope extractor
# denies on ambiguity, so any `OR` clause containing `space` is refused even when
# every candidate is allowlisted.
confluence search 'space = ENG OR space = SEC'
```

**Hard limits (always denied):** `restrictions`, `permissions`, `space.admin`, `users`, `attachments`, HTTP `DELETE` / `PUT` / `PATCH`, path traversal (`..`), duplicate slashes, non-ASCII keys, URL-encoded smuggling of denied terms (e.g., `%61ttachments`), and any `/execute` path that a narrow route already covers (anti-bypass). Non-GET `execute` calls return 403 regardless of the path. See [Confluence Wrapper Reference](../../../docs/reference/confluence-wrapper.md) for the full endpoint surface, CQL scope extractor rules, the `not_found` response envelope, response redaction (`accountId` / `emailAddress` / user-profile `_links.webui`), and the `used_fallback` flag emitted when the v1 inline-comment fallback fires.

> **Note on `~/context-sync/confluence/`.** The sandbox may also have a read-only `~/context-sync/confluence/` cache mounted (legacy syncer). That cache is independent of the new gateway wrapper — `confluence ...` calls always go through the gateway and never touch the syncer cache.

## File System

| Path | Purpose |
|------|---------|
| `~/repos/` | Code workspace (RW) — NOT a git repo itself |
| `~/repos/<repo>/.egg-state/` | SDLC pipeline state (may be readonly in implement phase) |
| `~/context-sync/` | Confluence/JIRA cache (RO, may not be mounted) |
| `~/sharing/` | Persistent data, notifications, context |

**Pipeline readonly directories:** During the implement phase, `.egg-state/drafts/`, `.egg-state/contracts/`, `.egg-state/pipelines/`, and `.egg-state/reviews/` are mounted readonly. Check for `.egg-readonly` marker files to understand restrictions. Attempting to write to these directories will produce an EROFS (read-only filesystem) error.

**Post-agent worktree preservation**: Uncommitted changes are preserved in the agent's worktree on container exit. The orchestrator detects uncommitted work and creates a HITL decision for recovery.

## Services

- PostgreSQL and Redis start automatically

## Shell Command Safety

**Scope all filesystem operations to `~/repos/` or `$EGG_REPO_PATH`.** Never search from `/` — it will be killed by timeout.

**DO**:
```bash
grep -rn "pattern" ~/repos/
find ~/repos/ -name "*.py" -exec grep -l "pattern" {} \;
```

**DON'T**:
```bash
grep -rn "pattern" /          # Scans entire filesystem — will be killed after 120s
find / -name "*.py"           # Same problem — unbounded search
```

**On push failure**: Report via `egg-orch signal error --error "Push failed: <msg>" --recoverable`. Do NOT push to a different branch name.

If a tool returns 403 Forbidden, you are likely in private mode. Proceed with local resources.
