# ADR: Context Sync Strategy - Custom Connectors vs MCP Servers

**Driver:** James Wiesebron
**Approver:** TBD
**Contributors:** James Wiesebron, Claude (AI Pair Programming)
**Informed:** Engineering teams
**Proposed:** November 2025
**Implemented:** November 2025 (Partial - GitHub MCP only)
**Status:** Partially Implemented (GitHub MCP ✅, JIRA MCP ❌)

## Table of Contents

- [Current Implementation Status](#current-implementation-status)
- [Context](#context)
- [Decision](#decision)
- [Decision Matrix](#decision-matrix)
- [Implementation Details](#implementation-details)
- [Migration Strategy](#migration-strategy)
- [Consequences](#consequences)
- [Decision Permanence](#decision-permanence)
- [Alternatives Considered](#alternatives-considered)

## Current Implementation Status

**MCP Integration (Partially Implemented):**
- ✅ GitHub MCP Server: Configured via `api.githubcopilot.com` (mcp-token-watcher.py) - Real-time PR/issue/repo access
- ✅ Bi-directional GitHub operations: Create PRs, add comments, manage issues via MCP
- ✅ Claude Code natively uses GitHub MCP for all GitHub interactions
- ❌ JIRA MCP Server: **Not yet implemented** - still using custom sync
- ❌ Atlassian MCP Server: **Not yet implemented** - still using custom sync

**Custom Sync System (Still Active for JIRA and Confluence):**
- Confluence Connector: ~1,134 lines of Python - Bulk ADR/docs sync (**retained per decision**)
- JIRA Connector: ~636 lines of Python - **Still in use** (MCP migration pending)
- Custom GitHub sync: **Removed** (replaced by GitHub MCP)

**Sync Frequencies:**
- Confluence: Hourly (systemd timer) - retained
- JIRA: Hourly (systemd timer) - **pending MCP migration**
- GitHub: Real-time via MCP (custom sync removed)

**Note:** As of January 2026, automated analysis features (github-watcher, pr-reviewer, etc.) have been removed to refocus on guided autonomous development. GitHub operations are now on-demand via Slack commands.

## Context

### Background

The egg project currently uses **custom file-based context synchronization** to provide Claude Code with access to external data sources (Confluence, JIRA, GitHub). This system was built as a fast path to MVP and has served well for Phase 1.

**Current Architecture:**
```
External APIs → Custom Python Sync → ~/context-sync/ (markdown/JSON) → Container (read-only)
```

The **Model Context Protocol (MCP)** has emerged as an industry standard for connecting LLMs to external data sources, with official support from Anthropic, adoption by OpenAI, and production MCP servers from major platforms including Atlassian.

### What We're Deciding

This ADR evaluates whether to:
1. **Continue** with custom context sync connectors
2. **Migrate** to off-the-shelf MCP servers
3. **Hybrid** approach using MCP for some integrations, custom for others

### Key Requirements

1. **Real-time Access:** Reduce latency from hourly sync to on-demand
2. **Bi-directional Operations:** Enable agent to update tickets, comment on PRs
3. **Maintainability:** Reduce custom code burden as APIs evolve
4. **Security:** Maintain permission boundaries and audit trails
5. **Compatibility:** Support current watcher/analyzer workflows
6. **GCP Deployment:** Work in Cloud Run (Phase 3 goal)

### Current Pain Points

1. **Stale Data:** Hourly sync means up to 60-minute lag for Confluence/JIRA changes
2. **One-Way Only:** Agent cannot update tickets or comment on PRs directly
3. **Maintenance Burden:** ~3,300 LOC to maintain as APIs change
4. **No Real-Time Triggers:** Cannot react to events as they happen
5. **Duplicate Effort:** Building what MCP servers already provide

### MCP Ecosystem Maturity

**Official/Production MCP Servers:**

| Platform | Server | Status | Capabilities |
|----------|--------|--------|--------------|
| **Atlassian** | [atlassian-mcp-server](https://github.com/atlassian/atlassian-mcp-server) | Beta (Official) | Jira + Confluence read/write, OAuth 2.0 |
| **GitHub** | [github-mcp-server](https://github.com/github/github-mcp-server) | Public Preview | Repos, issues, PRs, search |
| **Anthropic** | [Reference servers](https://github.com/modelcontextprotocol/servers) | Production | Git, Filesystem, Fetch, Memory |

**Community MCP Servers:**

| Server | Stars | Capabilities |
|--------|-------|--------------|
| [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian) | 500+ | Confluence + Jira, Cloud & Server/DC |
| [xuanxt/atlassian-mcp](https://github.com/xuanxt/atlassian-mcp) | 100+ | 51 tools, sprints, boards, backlogs |

## Decision

**We will adopt a hybrid approach: MCP servers for real-time operations, retain custom sync for bulk documentation access.**

### Rationale

1. **MCP for Jira:** Real-time ticket access + bi-directional updates (create, comment, transition)
2. **MCP for GitHub:** Real-time PR access + bi-directional operations (comment, review)
3. **Custom Sync for Confluence:** Bulk documentation better served by pre-synced files (ADRs, runbooks)
4. **Gradual Migration:** Add MCP alongside existing sync, validate, then deprecate

### Why Not Full MCP Migration?

Confluence documentation has different access patterns than JIRA/GitHub:
- **Volume:** Hundreds of pages vs. dozens of active tickets/PRs
- **Freshness:** ADRs change rarely; tickets change frequently
- **Access Pattern:** LLM benefits from having full docs in context vs. fetching on-demand
- **Token Efficiency:** Pre-synced markdown is more token-efficient than repeated API calls

## Decision Matrix

| Decision Area | Chosen Approach | Key Rationale | Rejected Alternatives |
|---------------|-----------------|---------------|----------------------|
| **Jira Integration** | Official Atlassian MCP Server | Real-time, bi-directional, maintained by Atlassian | Custom sync (one-way, stale) |
| **GitHub Integration** | GitHub MCP Server | Real-time PR/issue access, official support | Custom sync (15-min lag) |
| **Confluence Integration** | Keep Custom Sync | Bulk docs, token-efficient, stable content | MCP (too chatty for docs) |
| **Watcher Workflows** | Hybrid triggers | MCP events + scheduled analysis | Pure MCP (loses batch analysis) |

## Implementation Details

### 1. MCP Server Integration with Claude Code

Claude Code natively supports MCP servers via configuration:

```json
// ~/.claude/settings.json
{
  "mcpServers": {
    "atlassian": {
      "type": "remote",
      "url": "https://mcp.atlassian.com/v1/sse",
      "auth": {
        "type": "oauth2",
        "clientId": "${ATLASSIAN_CLIENT_ID}",
        "scopes": ["read:jira-work", "write:jira-work", "read:confluence-content.all"]
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

### 2. Atlassian MCP Server Capabilities

**Jira Tools (via official server):**
- `jira_search` - JQL-based issue search
- `jira_get_issue` - Get full issue details
- `jira_create_issue` - Create new issues
- `jira_update_issue` - Update existing issues
- `jira_add_comment` - Add comments to issues
- `jira_transition_issue` - Change issue status

**Confluence Tools:**
- `confluence_search` - CQL-based content search
- `confluence_get_page` - Get page content
- `confluence_create_page` - Create new pages
- `confluence_update_page` - Update existing pages

### 3. GitHub MCP Server Capabilities

**Repository Tools:**
- `search_repositories` - Search GitHub repos
- `get_file_contents` - Read file content
- `create_or_update_file` - Write files
- `push_files` - Batch file operations

**Issue/PR Tools:**
- `search_issues` - Search issues and PRs
- `get_issue` - Get issue/PR details
- `create_issue` - Create new issues
- `create_pull_request` - Create PRs
- `add_issue_comment` - Comment on issues/PRs

**⚠️ Checks API Limitation:**

Fine-grained Personal Access Tokens (PATs) **cannot access the Checks API** (workflow run status, check runs, annotations). This is a known GitHub limitation confirmed by GitHub Support (November 2025):

> "It isn't possible to assign Checks permissions to a Fine-grained PAT—only GitHub Apps can access this API. We had that functionality initially, but due to some edge cases we disabled it."

| Token Type | Checks API Access |
|------------|-------------------|
| Fine-grained PAT | ❌ Not supported |
| Classic PAT | ⚠️ Requires broad `repo` scope |
| GitHub App | ✅ Granular `checks:read` permission |

**Implication:** To query GitHub Actions workflow status (e.g., "did the CI pass on this PR?"), you need either:
1. A **Classic PAT** with `repo` scope (grants broad access, not ideal)
2. A **GitHub App** with `checks:read` permission (recommended)

**⚠️ GitHub MCP Server Tool Gap:**

The GitHub MCP server (api.githubcopilot.com) does **not** expose tools for the Checks API. Even with a GitHub App that has `checks:read` permission, the MCP server provides no way to query:
- Check runs (individual CI job results)
- Check suites (aggregated workflow status)
- Workflow run details (logs, annotations)

| GitHub API | MCP Server Support |
|------------|-------------------|
| Repos, Files, Branches | ✅ Full support |
| Issues and Pull Requests | ✅ Full support |
| Commits | ✅ Full support |
| Checks API | ❌ Not exposed |

**Solution:** Build a custom MCP tool that wraps the GitHub Checks API directly. This tool will:
1. Use the GitHub App installation token for authentication
2. Query check runs and workflow status via REST API
3. Expose results through the MCP protocol

See [PR #56](https://github.com/jwbron/egg/pull/56) for related security and lockdown considerations when building custom MCP extensions.

### 4. Preserving Watcher Workflows

Current watchers run post-sync to analyze changes. With MCP, we shift to:

**Option A: Event-Driven Analysis**
```
MCP Event (new comment on PR)
    ↓
Webhook → Cloud Function / Cloud Run
    ↓
Trigger egg analysis container
    ↓
Generate response via MCP (comment back)
```

**Option B: Scheduled Analysis with MCP**
```
Scheduled job (every 15 min)
    ↓
Query MCP for recent changes
    ↓
Analyze changes in egg container
    ↓
Take action via MCP (comment, update)
```

**Recommendation:** Start with Option B (scheduled + MCP query) as it's simpler and preserves current patterns. Evolve to Option A for latency-sensitive workflows.

### 5. Handling Rapid-Fire PR Comments

When reviewers leave multiple comments in quick succession (common during thorough PR reviews), responding to each comment individually creates noise and may lead to context fragmentation. We implement a **debounce strategy** to batch related comments.

**Problem:**
- Reviewer leaves 5-6 comments within 2 minutes during a review
- Without batching, egg responds 5-6 times with potentially conflicting or redundant responses
- Each response uses a separate Claude session without context from sibling comments

**Solution: 60-Second Debounce Window**

```
PR Comment Received
    ↓
Start/Reset 60-second timer
    ↓
Collect comments during window
    ↓
Timer expires
    ↓
Process all collected comments in single Claude session
    ↓
Generate unified response addressing all points
```

**Implementation:**

```python
# comment-responder.py (debounce logic)
import time
from collections import defaultdict

# Buffer: {pr_number: [(timestamp, comment), ...]}
comment_buffer = defaultdict(list)
DEBOUNCE_SECONDS = 60

def on_pr_comment_webhook(pr_number: int, comment: dict):
    """Handle incoming PR comment with debounce."""
    comment_buffer[pr_number].append((time.time(), comment))

    # Schedule processing after debounce window
    schedule_delayed_processing(pr_number, delay=DEBOUNCE_SECONDS)

def process_buffered_comments(pr_number: int):
    """Process all buffered comments for a PR."""
    comments = comment_buffer.pop(pr_number, [])

    if not comments:
        return

    # Check if more comments arrived during processing
    if any(time.time() - ts < DEBOUNCE_SECONDS for ts, _ in comments):
        # Re-buffer and wait longer
        comment_buffer[pr_number] = comments
        schedule_delayed_processing(pr_number, delay=DEBOUNCE_SECONDS)
        return

    # Build context for Claude
    context = f"These {len(comments)} comments were left in the past {DEBOUNCE_SECONDS} seconds:\n\n"
    for i, (ts, comment) in enumerate(comments, 1):
        context += f"**Comment {i}** (by {comment['author']}):\n{comment['body']}\n\n"

    # Single Claude session addresses all comments
    response = generate_unified_response(pr_number, context)
    post_pr_comment(pr_number, response)
```

**Benefits:**
- Single cohesive response addressing all reviewer points
- Full context available in one Claude session
- Reduces notification noise for reviewers
- More efficient token usage

**Trade-offs:**
- 60-second delay before first response (acceptable for non-urgent reviews)
- If reviewer is waiting for a response before continuing, they'll need to wait

**Alternative Considered: Respond Immediately**
We considered responding to each comment as it arrives, but rejected this because:
- Fragmentary responses confuse reviewers
- Later comments may contradict or supersede earlier ones
- Higher total token usage across multiple sessions

### 5.1. GitHub App Webhooks for Real-Time Events

With a GitHub App, we can receive **real-time webhooks** instead of polling every 5-15 minutes. This eliminates `github-watcher.py` entirely and enables instant responses.

**Available Webhook Events:**

| Event | Trigger | Use Case |
|-------|---------|----------|
| `issue_comment` | Comment on issue/PR | Respond to reviewer questions |
| `pull_request_review_comment` | Line-level PR comment | Address specific code feedback |
| `pull_request_review` | Review submitted | React to approvals/rejections |
| `pull_request` | PR opened/updated | Auto-review new PRs |
| `check_run` | CI check completes | React to failures |
| `check_suite` | All checks complete | Notify on CI completion |

**Architecture with Webhooks:**

```
GitHub App Webhook
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│                 Host (Webhook Receiver)                      │
│                                                              │
│  Option A: Cloudflare Tunnel / ngrok                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  webhook-receiver.py (Flask/FastAPI)                  │   │
│  │    - Receives GitHub events                           │   │
│  │    - Validates webhook signature                      │   │
│  │    - Writes to ~/sharing/incoming/                    │   │
│  │    - OR triggers egg container directly               │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Option B: smee.io (Development)                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  smee -u https://smee.io/xxx --path /webhook          │   │
│  │    - GitHub → smee.io → local client                  │   │
│  │    - No port exposure needed                          │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
        │
        ▼ (file or direct trigger)
┌──────────────────────────────────────────────────────────────┐
│                    Container (egg)                            │
│                                                               │
│  Process event → Generate response → Post via GitHub MCP     │
└──────────────────────────────────────────────────────────────┘
```

**Benefits over Polling:**
- **Instant response:** Events processed in seconds, not 5-15 minutes
- **No wasted cycles:** Only process when events occur
- **Richer context:** Webhook payload includes full event details
- **Check status access:** GitHub App can query Checks API

**Implementation Considerations:**
- Webhook receiver runs on host (container cannot receive inbound)
- Use Cloudflare Tunnel or smee.io to expose endpoint securely
- Debounce logic (Section 5) still applies for rapid-fire comments
- Combine with GitHub App installation token for API calls

**Migration Path:**
1. Create GitHub App with webhook URL
2. Configure events: `issue_comment`, `pull_request_review`, `check_run`
3. Implement webhook receiver on host
4. Test alongside existing `github-watcher.py`
5. Deprecate polling-based watcher

### 6. Example: Migrated JIRA Watcher

**Before (Custom Sync):**
```python
# jira-processor.py
def analyze_tickets():
    # Read from pre-synced files
    tickets = glob.glob("~/context-sync/jira/ASSIGNED/*.md")
    for ticket_file in tickets:
        content = Path(ticket_file).read_text()
        # Analyze with Claude...
```

**After (MCP):**
```python
# jira-processor.py (MCP version)
def analyze_tickets():
    # Query Jira directly via MCP
    # Claude Code handles this natively - no custom code needed
    # Agent prompt: "Search Jira for my assigned tickets updated in last hour"

    # Agent can now also:
    # - Add comments directly
    # - Update ticket status
    # - Create subtasks
```

### 7. Architecture Comparison

**Current (Custom Sync):**
```
┌─────────────────────────────────────────────────────────────┐
│                    Host (Systemd Timers)                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │ Confluence  │  │   JIRA      │  │  GitHub     │          │
│  │   Sync      │  │   Sync      │  │   Sync      │          │
│  │  (hourly)   │  │  (hourly)   │  │  (15 min)   │          │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘          │
│         └────────────────┼────────────────┘                  │
│                          ▼                                   │
│              ~/context-sync/ (files)                         │
└──────────────────────────┬───────────────────────────────────┘
                           │ read-only mount
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    Container (egg)                            │
│                                                               │
│  Claude Code reads files, analyzes, generates notifications   │
│  ❌ Cannot update Jira/GitHub directly                        │
└──────────────────────────────────────────────────────────────┘
```

**Proposed (Hybrid MCP + Sync):**
```
┌──────────────────────────────────────────────────────────────┐
│                    Container (egg)                            │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │                    Claude Code                           │ │
│  │                                                          │ │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │ │
│  │  │ Atlassian   │  │  GitHub     │  │ ~/context-sync/ │  │ │
│  │  │ MCP Server  │  │ MCP Server  │  │ (Confluence)    │  │ │
│  │  │ (real-time) │  │ (real-time) │  │ (bulk docs)     │  │ │
│  │  └──────┬──────┘  └──────┬──────┘  └────────┬────────┘  │ │
│  │         │                │                   │           │ │
│  │         ▼                ▼                   ▼           │ │
│  │  ✅ Read/Write    ✅ Read/Write      ✅ Read-only       │ │
│  │  ✅ Real-time     ✅ Real-time       ✅ Token-efficient │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
    Jira Cloud           GitHub API
    Confluence Cloud
```

### 8. Security Model

**MCP Authentication:**
- **Atlassian:** OAuth 2.0 with user-scoped tokens (respects Jira/Confluence permissions)
- **GitHub:** Personal Access Token or GitHub App (scoped to repos)

**GitHub Authentication Strategy:**

Due to the Checks API limitation (see Section 3), we recommend a **hybrid authentication approach**:

| Use Case | Auth Method | Rationale |
|----------|-------------|-----------|
| PR/Issue operations | Fine-grained PAT | Minimal permissions, secure |
| Checks/Workflow status | GitHub App | Only option for Checks API |
| Real-time webhooks | GitHub App | Apps can receive webhooks |

**GitHub App Benefits:**
- Granular permissions (can request only `checks:read`)
- Webhook support for real-time event processing
- Higher rate limits than PATs
- Installation tokens auto-expire (1 hour)

**GitHub App Trade-offs:**
- More complex setup (private key, JWT generation)
- Short-lived tokens require refresh logic
- Requires webhook endpoint for real-time events

**Permission Boundaries:**
- Agent actions limited by authenticated user's permissions
- Audit trail via Atlassian/GitHub activity logs
- No elevation of privilege possible

**Credential Management:**
- OAuth tokens stored in Claude Code config (encrypted)
- Refresh handled automatically by MCP protocol
- GitHub App private keys stored securely on host
- Installation tokens generated on-demand (1-hour expiry)

## Migration Strategy

### Phase 1: Full MCP Migration (Local)

**Goal:** Complete migration from custom syncs to MCP servers on local development environment

1. Configure Atlassian MCP server in Claude Code settings
2. Configure GitHub MCP server
3. Test MCP queries alongside existing sync
4. Compare data quality and latency
5. Enable write scopes in MCP OAuth config
6. Update CLAUDE.md with new capabilities
7. Test bi-directional workflows:
   - Agent comments on PR after review
   - Agent updates ticket status after completing work
   - Agent creates subtasks for complex tickets
8. Update jira-watcher to query via MCP instead of files
9. Update GitHub analyzers to use MCP
10. Keep Confluence sync (bulk docs still valuable)
11. Disable JIRA connector in sync_all.py
12. Disable GitHub sync service
13. Remove associated systemd timers
14. Archive removed code (don't delete)

**Success Criteria:** System runs locally with MCP for JIRA/GitHub, custom sync removed (~1,000 LOC reduction)
**Rollback:** Re-enable custom sync services

### Phase 2: Bi-Directional Sync

**Goal:** Enable bi-directional sync capabilities beyond what custom sync provided

1. Validate agent can create/update JIRA tickets in real workflows
2. Validate agent can comment on PRs and create reviews
3. Implement event-driven triggers for latency-sensitive workflows
4. Add MCP-based notifications for real-time updates
5. Document new capabilities in CLAUDE.md

**Success Criteria:** Agent performs 50+ bi-directional operations successfully; latency-sensitive workflows use event-driven triggers

### Phase 3: GCP Migration

**Goal:** Ensure MCP works in Cloud Run environment (see [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md))

1. MCP servers work over network (no local file dependencies)
2. OAuth token refresh works in stateless containers
3. Confluence sync works with Cloud Storage mount
4. Scheduled sync jobs (sync-confluence, sync-jira, sync-github) work via scheduled-job module
5. Manual sync via `/sync` slash commands (see [ADR-Slack-Bot-GCP-Integration](../not-implemented/ADR-Slack-Bot-GCP-Integration.md))

**Success Criteria:** Full MCP functionality in Cloud Run environment

## Consequences

### Benefits

1. **Real-Time Data:** Sub-second access vs. hourly sync
2. **Bi-Directional:** Agent can update tickets, comment on PRs
3. **Reduced Maintenance:** ~1,000 lines of custom code removed
4. **Industry Standard:** MCP adopted by Anthropic, OpenAI, Microsoft
5. **Better Security:** OAuth 2.0 with user-scoped permissions
6. **GCP Ready:** MCP works over network, no file mounts needed
7. **Richer Capabilities:** Access to features not in custom sync (sprints, boards, etc.)

### Drawbacks

1. **Dependency on External Services:** MCP servers must be available
2. **Token Usage:** Real-time queries may use more tokens than pre-synced files
3. **Learning Curve:** Team needs to understand MCP configuration
4. **Beta Status:** Atlassian MCP is still in beta
5. **Rate Limits:** MCP servers have request limits (1000/hour for Atlassian Premium)

### Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| MCP server unavailable | Keep Confluence sync as fallback; MCP failures are recoverable |
| Higher token usage | Monitor usage; Confluence bulk docs stay file-based |
| Rate limiting | Batch queries; use caching where possible |
| OAuth token expiry | MCP protocol handles refresh automatically |
| Breaking changes in MCP | Pin MCP server versions; test before upgrading |

### Neutral

1. **Confluence Stays File-Based:** Neither better nor worse; fits use case
2. **Watcher Refactoring:** Same effort whether custom or MCP-based
3. **Testing Approach:** Different but equivalent complexity

## Decision Permanence

**Medium permanence.**

The choice of MCP vs. custom sync is reversible - we can always fall back to custom sync if MCP proves problematic. However, investing in MCP integration aligns with industry direction and would be expensive to reverse after Phase 4.

The decision to keep Confluence file-based is **low permanence** - we can migrate to MCP later if real-time Confluence access becomes valuable.

## Alternatives Considered

### Alternative 1: Continue with Custom Sync Only

**Description:** Maintain and enhance current custom sync system.

**Pros:**
- No migration effort
- Full control over implementation
- No external dependencies

**Cons:**
- One-way sync only (cannot update tickets/PRs)
- Hourly latency for changes
- ~3,300 LOC to maintain
- Reinventing what MCP provides

**Rejected because:** Bi-directional operations and real-time access are high-value capabilities that custom sync cannot provide without significant additional investment.

### Alternative 2: Full MCP Migration (No Custom Sync)

**Description:** Replace all custom sync with MCP servers.

**Pros:**
- Maximum code reduction
- Consistent access pattern
- Simplest architecture

**Cons:**
- Confluence bulk docs less efficient via MCP
- Higher token usage for documentation access
- All-or-nothing dependency on MCP availability

**Rejected because:** Confluence documentation benefits from bulk pre-sync; MCP is better suited for transactional data (tickets, PRs).

### Alternative 3: Build Custom MCP Servers

**Description:** Implement our own MCP servers wrapping existing sync.

**Pros:**
- Full control
- Can optimize for our use case
- MCP-compatible interface

**Cons:**
- More code to maintain, not less
- Duplicates work Atlassian/GitHub already did
- No community support

**Rejected because:** Official MCP servers are maintained by platform owners with better API access and resources.

### Alternative 4: Webhook-Based Real-Time Sync

**Description:** Replace polling with webhooks for real-time event processing.

**Pros:**
- Real-time updates (instant vs. 5-15 minute polling)
- Richer event context in webhook payload
- No wasted polling cycles
- Eliminates `github-watcher.py` entirely

**Cons:**
- Requires inbound network access to host (not container)
- More complex infrastructure (tunnel or public endpoint)
- Webhook secret management

**Original Rejection Reconsidered:**

Initially rejected because "sandbox cannot receive inbound connections." However, this concern is mitigated:

1. **Webhooks go to host, not container** - The webhook receiver runs on the host machine, not inside the sandboxed container. The host can receive inbound connections.

2. **Secure tunnel options exist:**
   - Cloudflare Tunnel (free, no port exposure)
   - smee.io (GitHub's recommended dev solution)
   - ngrok (easy setup)

3. **GitHub App enables bi-directional operations** - Unlike the original file-sync approach, a GitHub App provides both webhook reception AND API access for write operations.

**Revised Status:** Now **recommended** as part of the hybrid approach (see Section 5.1). The GitHub App requirement for Checks API access makes webhooks a natural addition.

### Alternative 5: Database Replication

**Description:** Replicate Jira/Confluence data to local database.

**Pros:**
- Fast queries
- Offline access
- Rich query capabilities

**Cons:**
- Complex sync logic
- Storage overhead
- Still one-way
- Over-engineered for use case

**Rejected because:** Adds significant complexity without enabling bi-directional operations.

## Related ADRs

This ADR is part of a series defining the egg GCP deployment architecture:

| ADR | Relationship to This ADR |
|-----|-------------------------|
| [ADR-Message-Queue-Slack-Integration](../not-implemented/ADR-Message-Queue-Slack-Integration.md) | Sync jobs use Pub/Sub to send notifications about sync status and errors |
| [ADR-Slack-Integration-Strategy-MCP-vs-Custom](../not-implemented/ADR-Slack-Integration-Strategy-MCP-vs-Custom.md) | Parallel decision - MCP for Slack reading, similar hybrid approach |
| [ADR-Slack-Bot-GCP-Integration](../not-implemented/ADR-Slack-Bot-GCP-Integration.md) | Defines `/sync` slash commands that trigger sync operations |
| [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md) | Defines scheduled sync jobs (sync-confluence, sync-jira, sync-github) using scheduled-job module |

## References

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Atlassian MCP Server](https://github.com/atlassian/atlassian-mcp-server)
- [GitHub MCP Server](https://github.com/github/github-mcp-server)
- [Anthropic Reference Servers](https://github.com/modelcontextprotocol/servers)
- [Atlassian MCP Announcement](https://www.atlassian.com/blog/announcements/remote-mcp-server)

---

**Last Updated:** 2025-11-30
**Next Review:** 2025-12-28 (Monthly)
**Status:** Partially Implemented
- ✅ GitHub MCP: Active via api.githubcopilot.com (mcp-token-watcher.py)
- ✅ Confluence sync: Retained per decision (custom sync)
- ❌ JIRA MCP: Pending implementation (still using custom sync)
