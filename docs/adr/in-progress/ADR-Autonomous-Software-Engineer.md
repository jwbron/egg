# ADR: LLM-Powered Autonomous Software Engineering Agent

**Driver:** Engineering Leadership
**Approver:** TBD
**Contributors:** James Wiesebron, Claude (AI Pair Programming)
**Informed:** Engineering teams
**Proposed:** November 2025
**Status:** In Progress

## Table of Contents

- [Current Implementation Status](#current-implementation-status)
- [Context](#context)
- [Decision](#decision)
- [High-Level Design](#high-level-design)
- [User Interaction Model](#user-interaction-model)
- [Security Considerations](#security-considerations)
- [Continuous Improvement Strategy](#continuous-improvement-strategy)
- [Migration & Adoption](#migration--adoption)
- [Consequences](#consequences)
- [Decision Permanence](#decision-permanence)
- [Future Enhancements](#future-enhancements)
- [Alternatives Considered](#alternatives-considered)
- [Related ADRs](#related-adrs)

## Current Implementation Status

**Phase 1 Complete:** Core infrastructure established
- ✅ Docker-based Claude Code sandbox (egg)
- ✅ Slack integration for bidirectional communication
- ✅ Context syncing (Confluence, JIRA, GitHub → local markdown/JSON)
  - ✅ Confluence sync (hourly) - ADRs, runbooks, docs
  - ✅ JIRA sync (hourly) - All open INFRA tickets + epics
  - ✅ GitHub sync (15 min) - PR data, checks, comments
- ✅ Active context monitoring and analysis
  - ✅ JIRA watcher (triggered after sync) - Analyzes tickets, creates Beads tasks
  - ✅ Confluence watcher (triggered after sync) - Monitors ADRs, identifies impact
  - ✅ GitHub watcher (triggered after sync) - Monitors PR checks, suggests fixes
- ✅ Sprint analysis tool - On-demand ticket analysis and recommendations
- ✅ Persistent task memory (Beads - git-backed task tracking)
- ✅ Automated code and conversation analyzers
  - ✅ Codebase analyzer with self-improvement tracking
- ✅ Systemd service management
  - ✅ Setup script with update/reload support
  - ✅ Host setup verification in egg command
- ✅ File-based notification system
- ✅ Mobile-accessible Slack interface
- ✅ Git worktree isolation for concurrent containers
- ✅ **Deployment:** Local laptop (development/pilot)

**Planned:**
- Cloud Run deployment via Terraform (production)

**In Progress:**
- Production hardening
- MCP server evaluation for context integration (see [ADR-Context-Sync-Strategy](../implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md))
- GCP deployment planning (see [Related ADRs](#related-adrs) for detailed specifications):
  - [ADR-Message-Queue-Slack-Integration](../not-implemented/ADR-Message-Queue-Slack-Integration.md) - Pub/Sub messaging
  - [ADR-Slack-Bot-GCP-Integration](../not-implemented/ADR-Slack-Bot-GCP-Integration.md) - Slash command interface
  - [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md) - Terraform infrastructure

**Recently Completed:**
- ✅ **Automated PR creation** - Agent creates PRs via `gh` CLI after task completion
- ✅ **PR auto-review** - Agent reviews PRs from others automatically after github-sync
- ✅ **Comment response** - Agent detects and suggests responses to PR comments
- ✅ **Full repo PR sync** - Sync all PRs in repo (not just @me) for cross-team visibility

## Context

### Background

**Problem Statement:**

Engineering teams face several interconnected challenges:
1. **Limited Capacity:** Engineers spend significant time on routine tasks (code reviews, refactoring, documentation, test writing)
2. **Context Switching:** Frequent interruptions reduce deep work time
3. **Code Quality:** Maintaining consistent standards across growing codebase
4. **Developer Experience:** Engineers want to focus on strategic, creative work
5. **Knowledge Transfer:** Institutional knowledge locked in documentation and tickets
6. **Mobile Productivity:** Engineers often unavailable at desk but want to stay productive

**Opportunity:**

Large Language Models (LLMs) like Claude have demonstrated strong capabilities in:
- Code generation and refactoring
- Documentation writing
- Test creation
- Code review and analysis
- Following complex instructions
- Learning from context and examples

### What We're Deciding

This ADR establishes the architecture, security model, and operational patterns for deploying an **LLM-powered autonomous software engineering agent** that can:

1. Work independently on well-defined tasks
2. Generate production-quality code
3. Create comprehensive documentation
4. Perform code analysis and suggest improvements
5. Interact naturally with engineers via Slack (including from mobile devices)
6. Create pull requests ready for review and merge
7. Continuously learn and improve

### Goals

**Primary Goals:**
1. **Increase Engineering Capacity:** Handle routine tasks autonomously
2. **Improve Code Quality:** Consistent standards, comprehensive tests, better documentation
3. **Enable Mobile Productivity:** Engineers can manage agent and review output from phone
4. **Enhance Developer Experience:** Let engineers focus on strategic, creative work
5. **Reduce Cognitive Load:** Agent handles context gathering, boilerplate, repetitive work
6. **Accelerate Delivery:** Faster iteration on well-defined features

**Non-Goals:**
- Replace human engineers
- Make architectural decisions without human input
- Deploy to production without human review
- Handle ambiguous or under-specified requirements alone
- Bypass security controls or code review processes

### Key Requirements

**Functional:**
1. **Autonomous Operation:** Work independently for hours on clear tasks
2. **Quality Output:** Production-ready code, tests, and documentation
3. **Context Awareness:** Access to JIRA, Confluence, codebase, recent changes
4. **Natural Interaction:** Conversational interface via Slack
5. **Reviewable Output:** All work easily reviewable by humans
6. **Mobile-First Review:** Engineer can review, approve, and manage from phone

**Non-Functional:**
1. **Security:** Zero access to credentials or production systems
2. **Safety:** Cannot deploy, push, or modify production without human approval
3. **Reliability:** Consistent, predictable behavior
4. **Observability:** Full visibility into what the agent is doing
5. **Maintainability:** System easy to understand and modify

## Decision

**We will build a Docker-sandboxed Claude Code agent ("egg") with mobile-first interaction and safe automated PR creation.**

### Core Architecture

**1. Isolation Strategy: Docker Sandbox**
- Agent runs in isolated Docker container
- **No credentials:** SSH keys, cloud tokens, secrets excluded
- **Code access:** Read-write mount of codebase (agent commits locally)
- **Network isolation:** Outbound HTTP only (Claude API, packages)
- **Human-gated operations:** Deploy requires human action; PR creation safe-guarded

**Rationale:** Maximum security with operational flexibility. Agent can work freely on code but cannot affect production.

**2. Integration Strategy: Slack as Primary Interface (Mobile-First)**
- **Bidirectional Slack messaging** for task assignment and updates
- **Mobile-optimized notifications** with inline previews
- **PR creation from mobile:** Agent creates PR via GitHub API, engineer reviews and merges from phone
- **Thread-based conversations** for context maintenance
- **Quick action buttons** for approve/reject/defer decisions

**Rationale:** Slack is where engineers already work. Low friction, immediate adoption, excellent mobile support. Engineer can be fully productive from phone.

**3. Context Strategy: Multi-Source with Evolution Path**
- **Current (Phase 1):** Confluence docs and JIRA tickets synced to local markdown
- **Markdown format:** LLM-friendly, version-controllable
- **Scheduled sync:** Every 15-30 minutes (cron)
- **Near-term evolution (Phase 2-3):** Expand to real-time and cloud-based context sources

**Rationale:** File-based sync is fastest path to MVP and sufficient for most workflows. However, we acknowledge limitations (not real-time, one-way sync). **Recommendation:** Assess hybrid approach using:
- **MCP servers** for real-time context access (JIRA updates, GitHub activity)
- **API calls** for bi-directional updates (comment on PRs, update ticket status)
- **GCP read-only access** for production context (logs, metrics, monitoring data)
- **File-based sync** retained for bulk Confluence documentation

**GCP Integration (Phase 2-3):**
- **Limited-scope service accounts:** Read-only access to specific GCP resources
- **Cloud Logging:** Error patterns, application logs for debugging context
- **Cloud Monitoring:** Performance metrics, system health indicators
- **Cloud Storage:** Configuration snapshots (non-sensitive)
- **BigQuery:** Usage analytics, performance data
- **Security:** No Secret Manager access, no write permissions, audit all reads

**4. Continuous Improvement: Automated Analyzers**
- **Codebase Analyzer:** Weekly analysis for code quality, security, structure
- **Conversation Analyzer:** Daily analysis of agent interactions for prompt improvement
- Output guides refinements to system prompts and agent behavior

**Rationale:** System gets better over time without manual intervention. Data-driven improvement.

**5. Mobile-First PR Workflow (Core Requirement)**
- **Agent prepares:** Complete PR with description, tests, documentation
- **Agent creates PR:** Uses GitHub API with PR-creation-scoped token, tags engineer
- **Mobile review:** Engineer reviews on phone via Slack notification
- **Quick actions:** Approve, request changes, or defer from Slack
- **Human merges:** Final merge approval remains with human

**Rationale:** Engineers are often away from desk. Mobile productivity is essential for adoption and engineer satisfaction.

**6. Deployment Strategy: Local-First, Cloud-Native Future**
- **Current (Phase 1):** Runs on engineer's laptop for pilot/development
- **Rationale:** Fastest iteration, easiest debugging, no cloud infrastructure needed for MVP
- **Future (Phase 3):** Deploy to Cloud Run using Terraform
  - **Stateless containers:** Agent instances are ephemeral
  - **Persistent storage:** Code mounts via Cloud Storage FUSE or NFS
  - **Service orchestration:** Systemd → Cloud Run jobs/services
  - **IAM integration:** Workload Identity (no service account key files)
  - **Scaling:** Multi-engineer support via container instances
  - **Terraform managed:** Infrastructure as code, same deploy process as other services

**Rationale:** Start on laptop to validate quickly, evolve to cloud-native infrastructure for production scale and multi-engineer support.

**7. Agent Demeanor and Cultural Alignment: Engineering Standards**
- **Current approach:** Agent behavior defined by `sandbox/.claude/rules/` configuration files
- **Core principle:** Agent should embody team engineering values and expectations
- **Implementation:**
  - Load engineering guidelines from Confluence docs (if available)
  - Software Engineer level expectations (L3, L4, L5 criteria)
  - Code quality standards and best practices
  - Communication style (clarity, precision, collaboration)
  - Problem-solving approach (systematic, data-driven, user-focused)
- **Continuous refinement:** Conversation analyzer evaluates tone, collaboration quality, technical communication
- **Configuration location:** `sandbox/.claude/rules/code-standards.md` and referenced documentation

**Rationale:** Agent effectiveness depends on cultural fit, not just technical capability. Organizations have specific expectations for engineers (thoroughness, clarity, user empathy, collaborative problem-solving). Agent should demonstrate these values in all interactions. Using team-documented standards ensures alignment with actual organizational expectations.

**Example Integration:**
```markdown
# sandbox/.claude/rules/engineering-culture.md (example)

## Engineering Values (from team documentation)

### Technical Excellence
- Write clear, maintainable code (not clever code)
- Comprehensive testing (unit, integration, edge cases)
- Documentation that explains "why" not just "what"
- Performance-conscious but not prematurely optimized

### Communication Standards
- Concise technical writing (clarity over verbosity)
- Data-driven recommendations (metrics, evidence)
- Proactive progress updates (don't wait to be asked)
- Honest about uncertainty ("I don't know" over guessing)

### Problem-Solving Approach
- User impact first (how does this help end users?)
- Systematic debugging (reproduce, isolate, fix, test)
- Consider edge cases and failure modes
- Security and accessibility by default

### Collaboration Style
- Respectful code review feedback (constructive, specific)
- Ask clarifying questions when requirements unclear
- Escalate blockers early (don't thrash alone)
- Share knowledge (document learnings for team)

### Software Engineer Level Expectations
Reference: Team career framework documentation

L3 (Mid-level): Execute well-defined projects, ask good questions
L4 (Senior): Lead projects, mentor, architectural input
L5 (Staff): Cross-team impact, technical strategy

Agent should demonstrate L3-L4 behaviors:
- Break down complex tasks independently (L3)
- Identify risks and trade-offs proactively (L4)
- Produce production-ready code with minimal iteration (L4)
- Document decisions and rationale (L3-L4)
```

**Benefits:**
- Agent behavior aligns with team norms
- Output quality matches organizational expectations
- Communication style familiar to engineers
- Cultural consistency across human and agent work
- Easier adoption (feels like working with a team engineer)

### Decision Matrix

| Area | Chosen Approach | Key Rationale | Rejected Alternatives |
|------|----------------|---------------|----------------------|
| **Execution Environment** | Docker sandbox with no credentials | Security, isolation, safe experimentation | VM isolation, cloud workstation, direct host access |
| **LLM Provider** | Anthropic Claude (Code-optimized) | Code quality, context window, tool use, safety | OpenAI GPT-4, self-hosted Llama, multiple providers |
| **User Interface** | Slack bidirectional messaging (mobile-first) | Where engineers are, mobile support, low friction | Web UI, CLI only, email, dedicated app |
| **Context Integration** | File-based sync (Phase 1), evolving to API/MCP/GCP (Phase 2-3) | Fastest to implement initially, scalable to real-time and cloud data | Pure API integration, database replication, webhook-only |
| **Code Management** | Git commits in sandbox, human approves PR | Safety, review-before-merge, audit trail | Agent pushes directly, squash commits, no git |
| **PR Creation** | Agent creates PR, human merges (mobile-friendly) | Balance automation and safety, mobile productivity | Fully autonomous merge, PR description only, no automation |
| **Deployment Model** | Human reviews and approves all changes | Safety, learning opportunity, regulatory compliance | Fully autonomous deployment, staged rollout, feature flags |
| **Improvement Mechanism** | Automated analyzers with prompt refinement | Data-driven, continuous improvement, low overhead | Manual tuning, A/B testing, user feedback only |
| **Infrastructure Deployment** | Laptop (Phase 1), Cloud Run via Terraform (Phase 3) | Fast MVP iteration, scales to cloud-native production | Direct cloud deployment, VM-based, Kubernetes |
| **Cultural Alignment** | Team-documented engineering standards in sandbox/.claude/rules/ | Behavior aligns with org values, feels like team engineer | Generic LLM behavior, manual prompt tuning, no cultural context |

## High-Level Design

### System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Engineer (Human) - Mobile First               │
│              (Reviews on Phone, Approves, Merges PRs)            │
└──────────┬──────────────────────────────────────┬────────────────┘
           │                                      │
           │ Slack Messages (Mobile)              │ PR Merge (GitHub)
           │                                      │
┌──────────▼──────────────────┐        ┌─────────▼─────────────────┐
│                              │        │                            │
│    Slack Integration         │        │    GitHub                  │
│  (Bidirectional Messaging)   │◄───────│  (PR Creation & Review)    │
│  - Mobile-optimized cards    │        │  - Agent creates PRs       │
│  - Quick action buttons      │        │  - Human merges            │
│                              │        │                            │
└──────────┬───────────────────┘        └────────────────────────────┘
           │
           │ File-based Notifications
           │
┌──────────▼──────────────────────────────────────────────────────┐
│                                                                   │
│                  egg (Docker Sandbox)                 │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                                                           │   │
│  │              Claude Code CLI Agent                        │   │
│  │         (Autonomous Software Engineer)                    │   │
│  │                                                           │   │
│  └──┬──────────────┬───────────────┬───────────────────┬───┘   │
│     │              │               │                   │        │
│     │              │               │                   │        │
│  ┌──▼──────┐   ┌──▼──────┐   ┌───▼──────┐   ┌────▼─────┐   ┌────▼─────┐  │
│  │ Code    │   │ Context │   │ Prompts  │   │ Memory   │   │ Analysis │  │
│  │ (R/W)   │   │ (R/O)   │   │ (R/O)    │   │ (R/W)    │   │ (Output) │  │
│  │         │   │         │   │          │   │          │   │          │  │
│  │~/repos │   │ Confl.  │   │ egg-     │   │ ~/beads  │   │ ~/sharing│  │
│  │         │   │ JIRA    │   │ container│   │ (tasks   │   │ /notif.  │  │
│  │         │   │         │   │ /.claude │   │  state)  │   │          │  │
│  └─────────┘   └─────────┘   └──────────┘   └──────────┘   └──────────┘  │
│                                                                   │
│  Security Boundary: No credentials, no direct push, no deploy    │
└───────────────────────────────────────────────────────────────────┘
```

### Component Descriptions

**1. egg (Docker Container)**
- Isolated execution environment
- Claude Code CLI with custom commands and rules
- Mounts: code (RW), context (RO), beads (RW), sharing (RW)
- Persistent memory: Beads git repository for task state across restarts
- No SSH keys, cloud credentials, or production access
- Bridge networking: outbound HTTP only

**2. Slack Integration (Mobile-First)**
- **Incoming:** Tasks from engineers ("implement feature X")
- **Outgoing:** Notifications with mobile-optimized formatting
- **PR Notifications:** Rich cards with diff summaries, quick actions
- **Threading:** Conversations maintain context
- **Mobile UI:** Large buttons, concise summaries, inline previews

**3. GitHub Integration (Safe PR Creation)**
- **PR-creation-scoped token:** Agent can create PRs and comment, but cannot merge or modify settings
- **Automatic tagging:** Engineer assigned on PR creation
- **Detailed descriptions:** Agent generates comprehensive PR details
- **Linked context:** JIRA tickets, design docs automatically referenced

**4. Context Sync (Phase 1: File-Based) - Implementation Details**

**Architecture: Host Syncs Data → Container Analyzes**
- **Security Boundary:** Host has credentials, container is credential-free
- **Sync Components:** Run as systemd user services on host
- **Analysis Components:** Run inside container, analyze synced data
- **Communication:** File-based via `~/context-sync/` (read-only in container)

**Implemented Sync Systems:**

**Confluence Sync** (`host-services/context-sync/connectors/confluence/`)
- **Host Service:** `context-sync.timer` - Runs hourly
- **Source:** Confluence API (ADRs, runbooks, engineering docs)
- **Output:** `~/context-sync/confluence/` - Markdown files
- **Format:** Atlassian Document Format (ADF) → Markdown
- **Connector:** Python-based with incremental sync
- **Container Analysis:** Triggered via `egg --exec --worktree` after sync completes

**JIRA Sync** (`host-services/context-sync/connectors/jira/`)
- **Host Service:** `context-sync.timer` - Runs hourly (same timer as Confluence)
- **Source:** JIRA API - All open INFRA project tickets
- **JQL Query:** `project = INFRA AND resolution = Unresolved ORDER BY updated DESC`
- **Output:** `~/context-sync/jira/` - Markdown files per ticket
- **Format:** `{KEY}_{SUMMARY}.md` with metadata, description, comments
- **Includes:** All issue types (stories, tasks, bugs, epics)
- **Incremental Sync:** Tracks ticket hashes to avoid re-fetching unchanged tickets
- **Container Analysis:** `jira-processor.py` triggered via `egg --exec` after sync

**GitHub Sync** (`host-services/github-sync/`)
- **Host Service:** `github-sync.timer` - Runs every 15 minutes
- **Source:** GitHub API (PR data, checks, comments)
- **Output:** `~/context-sync/github/` - JSON files
- **Structure:**
  - `prs/{repo}-PR-{num}.md` - PR metadata, description, comments
  - `prs/{repo}-PR-{num}.diff` - Full diff
  - `checks/{repo}-PR-{num}-checks.json` - Check status and logs
  - `comments/{repo}-PR-{num}-comments.json` - PR comments for response tracking
- **Scope:** All open PRs in configured repository (`--repo` and `--all-prs` flags)
- **GitHub Operations:** On-demand via Slack commands to `egg --exec`

**Container-Side Task Processors:**

**GitHub Processor** (`sandbox/egg-tasks/github/`)
- **Architecture:** Exec-based pattern triggered by Slack commands via `egg --exec`
- **PR Review:** On-demand PR reviews triggered via Slack ("review PR 123")
- **Command Handler:** Routes GitHub commands to appropriate handlers

**JIRA Processor** (`sandbox/egg-tasks/jira/`)
- **Architecture:** Exec-based pattern triggered via `egg --exec`
- **Ticket Analysis:** Analyzes JIRA tickets, extracts action items
- **Sprint Analysis:** Provides actionable recommendations for sprint work

**PR Creation via GitHub MCP**
- **Purpose:** Enables egg to create PRs after completing tasks
- **Capabilities:**
  - Auto-generates PR title/body from git commits
  - Pushes branch to remote via GitHub MCP
  - Creates PR via GitHub MCP `create_pull_request`
  - Requests review from configured reviewer
  - Creates Slack notification with PR URL
- **Integration:** Uses GitHub MCP server directly

**Sprint Analysis** (`sandbox/scripts/analyze-sprint.py`)
- **On-Demand:** Execute via `bin/egg --exec`
- **Analyzes:** Currently assigned JIRA tickets
- **Groups:** By status (In Progress, In Review, Blocked, To Do)
- **Suggests:** Next steps for each ticket, backlog tickets to pull in
- **Scoring:** Prioritizes by urgency, clarity, and recent activity
- **Output:** Slack notification with actionable recommendations

**5. Context Evolution (Phase 2-3: MCP + APIs + GCP)**
- **MCP Servers:** Real-time context access for JIRA, GitHub, monitoring
- **API Calls:** Bi-directional updates (comment on PRs, update tickets)
- **GCP Read-Only Access:** Production logs, metrics, analytics via service accounts
- **Hybrid approach:** File-based for bulk docs, real-time for critical updates, cloud data for operational context

**6. Automated Analyzers**
- **Codebase Analyzer:** Weekly code quality analysis
- **Conversation Analyzer:** Daily interaction analysis
- **Output:** Recommendations for prompt/system improvements
- **Runs:** Systemd timers, containerized via `egg --exec`

**7. File Watchers**
- **Notifications:** `~/sharing/notifications/` → Slack
- **Incoming Tasks:** `~/sharing/incoming/` → Agent pickup
- **Tracking:** State management for async workflows

### Persistent Memory & State Management

**Challenge:** LLMs are stateless - each conversation starts fresh. Without persistent memory:
- Work interruptions mean lost context
- Container restarts lose all progress
- Multiple concurrent containers can duplicate work
- No knowledge of what's already been done
- Cannot resume interrupted tasks

**Solution: Beads (Git-Backed Task Memory)**

Beads provides automatic persistent memory for the agent, solving the "LLM amnesia" problem:

**Architecture:**
```
~/.egg-sharing/beads/          # Host storage (persists across rebuilds)
├── issues.jsonl               # Task database (source of truth)
├── .git/                      # Version history
└── .beads.sqlite             # SQLite cache (disposable, auto-rebuilt)
       ↑
       │ Git sync
       │
Container 1: ~/beads/          # Symlink, shares same git repo
Container 2: ~/beads/          # Another container, same repo
Container N: ~/beads/          # All containers coordinate
```

**Automatic Workflow Integration:**

Agent automatically (without being asked):
1. **On startup:** Check for in-progress tasks (`bd --allow-stale list --status in_progress`)
2. **On new task:** Create Beads entry (`bd --allow-stale create "Implement OAuth2 for JIRA-1234"`)
3. **During work:** Update status and notes (`bd --allow-stale update bd-a3f8 --status in_progress`)
4. **Multi-step tasks:** Break into subtasks with dependencies
5. **On completion:** Mark closed, update dependent tasks

**Key Features:**
- **Git-backed:** All changes versioned, can review history
- **Multi-container safe:** Hash-based IDs (bd-a3f8) prevent conflicts
- **Dependency tracking:** Tasks can block other tasks
- **Parent-child relationships:** Complex features broken into subtasks
- **Automatic resumption:** Agent picks up where it left off after restarts
- **Cross-session memory:** Context preserved indefinitely

**Example Workflow:**

```bash
# Engineer sends: "Implement OAuth2 authentication for JIRA-1234"
# Agent automatically:

cd ~/beads
bd --allow-stale search "OAuth2 JIRA-1234"  # Check if already exists
bd --allow-stale create "Implement OAuth2 for JIRA-1234" --labels feature,jira-1234,slack
bd --allow-stale update bd-a3f8 --status in_progress

# Break into subtasks
bd --allow-stale create "Design auth schema" --parent bd-a3f8
bd --allow-stale create "Implement OAuth2 endpoints" --parent bd-a3f8 --deps blocks:bd-b1
bd --allow-stale create "Write integration tests" --parent bd-a3f8 --deps blocks:bd-b2,blocks:bd-b3

# Work on tasks, update progress
bd --allow-stale update bd-b1 --status closed
bd --allow-stale update bd-b1 --notes "Schema designed per ADR-042, using httpOnly cookies"
bd --allow-stale dep remove bd-b2 bd-b1  # Remove dependency to unblock

# Container crashes/restarts...

# On resume:
bd --allow-stale list --status in_progress
# Shows: bd-a3f8 "Implement OAuth2..." and remaining subtasks
bd --allow-stale show bd-a3f8  # Read all previous notes and context
# Continue work seamlessly
```

**Benefits:**
- **No lost work:** Container crashes don't lose context
- **Parallel work:** Multiple containers coordinate via shared git repo
- **Progress visibility:** Can check status of all tasks across all sessions
- **Knowledge accumulation:** Notes on decisions, blockers, approaches persist
- **Automatic resumption:** Agent knows exactly where to pick up

**Implementation:**
- **Storage:** `~/.egg-sharing/beads/` (git repository on host)
- **Access:** `~/beads/` symlink in container
- **CLI:** `beads` command (beads-cli package)
- **Format:** JSONL (human-readable, git-friendly)
- **Cache:** SQLite for fast queries (auto-rebuilt each session)

This solves the fundamental "LLM amnesia" problem while enabling true autonomous operation across sessions and containers.

### Exec-Based Analysis Architecture

**Pattern:** Event-driven analysis triggered by host services via `egg --exec`, using Claude Code for intelligent analysis

**Architecture:**
```
Host systemd service syncs data → Triggers analysis via egg --exec
  ↓
Spawn new ephemeral container (docker run --rm)
  ↓
Analysis script collects data → Constructs prompt → Calls claude --print
  ↓
Claude Code analyzes, creates Beads tasks, generates notifications
  ↓
Container automatically removed (--rm flag)
```

**Intelligence Layer: Claude Code Integration**

All watchers use `claude --print` for intelligent analysis instead of simple pattern matching:

- **GitHub Check Monitor:** Analyzes PR check failures, understands logs, suggests and attempts automatic fixes
- **JIRA Watcher:** Parses tickets, assesses scope/complexity, extracts requirements and action items
- **Confluence Watcher:** Analyzes ADRs and runbooks, identifies architectural decisions and impacts
- **Slack Processor:** Processes incoming tasks and responses with full context awareness

Each watcher constructs a comprehensive prompt including:
- Data to analyze (check failures, tickets, documents, messages)
- Workflow instructions per ADR (analyze → track in Beads → act → notify)
- Context about the ephemeral container environment
- Expected output format and actions

**Benefits:**
- **Event-driven:** Analysis only when data changes (after sync completes)
- **Intelligent analysis:** Claude Code understands context, not just pattern matching
- **No background processes:** Container has no continuous watchers
- **Total isolation:** Each execution in separate container, can't affect interactive sessions
- **Concurrent safe:** Multiple analyses can run simultaneously without conflicts
- **Automatic cleanup:** Containers removed immediately after exit (--rm flag)
- **Lower resource usage:** No polling loops, scripts exit after completion
- **Simpler debugging:** One-shot executions easier to trace than continuous loops
- **No git complexity:** Mounts main repos directly, no worktree management needed
- **Autonomous action:** Can attempt automatic fixes, create PRs, update code

**Implementation:**
- **github-sync.service:** `ExecStartPost` triggers `check-monitor.py` via `egg --exec`
  - Collects all PR check failures with logs
  - Calls `claude --print` with analysis workflow
  - Claude analyzes logs, identifies root causes, attempts automatic fixes
- **context-sync.service:** `ExecStartPost` triggers `jira-processor.py` and `confluence-processor.py`
  - JIRA: Collects new/updated tickets, calls Claude to analyze requirements and scope
  - Confluence: Collects new/updated ADRs/runbooks, calls Claude to identify impacts
- **slack-receiver:** Triggers `incoming-processor.py` when message arrives
  - Parses incoming tasks or responses
  - Calls Claude with full context to execute work
- **Containers:** Each execution gets unique ID (`egg-exec-{timestamp}-{pid}`)
- **Cleanup:** Automatic via Docker `--rm` flag (no manual cleanup needed)
- **Timeout:** All Claude invocations have 10-15 minute timeouts to prevent hangs

**Why ephemeral containers over docker exec:**
1. **Isolation:** Cannot affect running Claude Code sessions or other jobs
2. **Simplicity:** No worktree creation/cleanup logic needed
3. **Resource efficiency:** No CPU cycles wasted on polling empty directories
4. **Clearer architecture:** Analysis explicitly coupled to data availability
5. **Better error handling:** Failed analysis doesn't affect container lifecycle
6. **Easier testing:** Can manually trigger analysis via `egg --exec`
7. **Scalability:** Adding new analysis scripts doesn't increase baseline load
8. **Concurrent Claude:** Multiple Claude instances can run in parallel safely

## User Interaction Model

### Mobile-First Task Assignment & Review

**Primary Flow: Slack DM to Bot (Mobile-Optimized)**

```
Engineer (on phone) → Slack DM: "Implement user auth for project X"
           ↓
Slack Bot → File: ~/sharing/incoming/task-TIMESTAMP.md
           ↓
File Watcher → Agent: New task detected
           ↓
Agent → Reads: JIRA tickets, Confluence docs, codebase
           ↓
Agent → Works: Generates code, tests, documentation
           ↓
Agent → Commits: Local git commits with clear messages
           ↓
Agent → Creates PR: Via GitHub API (read-only token)
           ↓
Agent → Notifies: Slack with mobile-optimized card
           ↓
Engineer (on phone) → Reviews: Inline diff summary, test results
           ↓
Engineer (on phone) → Approves: Click "✅ Looks Good"
           ↓
PR → Auto-labeled: "approved-for-merge"
           ↓
Engineer (at desk or phone) → Merges: Final merge via GitHub mobile
```

### Mobile Review Interface

**Slack Notification Format:**
```
🤖 egg

PR Ready: Add OAuth2 authentication (#1234)
─────────────────────────
📊 Changes: +234 / -45 lines
✅ Tests: 12 passing
📝 Docs: Updated

Summary:
• Added OAuth2 middleware
• Configured Google provider
• Updated user model
• Added integration tests

View: github.com/org/repo/pull/1234

Quick Actions:
[✅ Approve] [❌ Request Changes] [👀 Review Later]
```

**Mobile-Optimized Features:**
- Large touch targets (buttons min 44px)
- Inline diff summaries (key changes only, not full diff)
- Test status front-and-center
- Direct links to full PR for detailed review
- Voice-to-text for feedback/comments

### Review Process

**All agent output requires human review before merge:**

1. **Code Changes:** Git commits with clear messages
2. **Pull Request:** Agent creates, human reviews and merges
3. **Documentation:** Included in PR for review
4. **Analysis:** Recommendations with rationale

**Review Interfaces:**
- **Slack (Primary):** Mobile-optimized notifications with summaries
- **GitHub Mobile:** Full PR review on phone when needed
- **GitHub Desktop:** Detailed review for complex changes
- **Git:** Standard diff/review tools

### Continuous Interaction

**Async Communication:**
- Agent posts updates as it works
- Engineer can intervene at any time (from phone or desktop)
- Questions automatically posted to Slack
- Context maintained in Slack threads

**Sync Communication:**
- Engineer can chat with agent in real-time
- Agent responds to clarifications
- Collaborative problem-solving

## Security Considerations

### Isolation Layers

**Layer 1: Container Isolation**
- Docker container with minimal privileges
- No host network access (bridge mode only)
- No sensitive mounts (SSH keys, cloud credentials excluded)
- Non-root user inside container

**Layer 2: Credential Isolation**
- **No SSH keys:** Cannot push to GitHub directly (uses API for PRs)
- **No deployment credentials:** Cannot deploy to GCP/AWS
- **No production DB access:** No connection strings
- **PR-creation-scoped GitHub token:** Can create PRs and comment, but cannot merge or modify settings
- **Limited GCP service accounts (Phase 2-3):** Read-only, scoped to specific resources only
  - Cloud Logging: Read-only to application logs
  - Cloud Monitoring: Read-only to metrics and dashboards
  - Cloud Storage: Read-only to non-sensitive buckets
  - BigQuery: Read-only to analytics datasets
  - **Excluded:** Secret Manager, write permissions, production data modification
  - **Authentication:** Service account keys (Phase 2-3 laptop), Workload Identity (Phase 3+ Cloud Run)
- **Claude API key:** Scoped to code generation only

**Layer 3: Network Isolation**
- **Outbound only:** HTTP/HTTPS for Claude API, GitHub API, and packages
- **No inbound ports:** Cannot accept connections
- **Bridge networking:** Isolated from host services
- **No VPN:** Cannot access internal networks

**Layer 4: Filesystem Isolation**
- **Code (RW):** Agent can modify, but changes stay local until PR approved
- **Context (RO):** Agent reads but cannot modify source
- **Sharing (RW):** Agent writes output for review
- **Host isolation:** Cannot access host filesystem

### Human-in-the-Loop Controls

**Required Human Approval:**
1. **PR Merge:** Agent creates PR, human must merge
2. **Deployment:** All deployments manual
3. **Credential Use:** Agent never has push credentials
4. **Production Access:** Agent never touches production
5. **Breaking Changes:** Flagged for mandatory detailed review

**Audit Trail:**
- All agent actions logged to systemd journal
- Git history shows agent authorship with Co-Authored-By tag
- Slack threads maintain conversation history
- File system tracks all output with timestamps
- GitHub PR history shows creation by agent, merge by human

### Safe-by-Default Design

**What Agent CAN Do:**
- Read code and documentation
- Generate and test code locally
- Create local git commits
- Create pull requests via GitHub API (PR-creation-scoped token)
- Write analysis and recommendations
- Ask questions via Slack

**What Agent CANNOT Do:**
- Merge pull requests (requires human)
- Push code directly to repositories
- Deploy to any environment
- Access production systems
- Modify credentials or secrets
- Accept inbound connections
- Bypass code review

### Data Exfiltration Concerns

**Critical Security Consideration:** Agent has read access to potentially confidential information (Confluence docs, JIRA tickets, internal code, GCP logs). We must prevent exfiltration of sensitive data.

**Exfiltration Vectors:**

1. **Claude API calls** - Context sent to Anthropic's API
2. **GitHub PR descriptions** - Public or semi-public PRs could leak internal info
3. **Slack notifications** - Posted to Slack channels (potentially broad audience)
4. **Git commit messages** - Commits could reference confidential strategies
5. **File outputs** - Writes to `~/sharing/` could contain sensitive data

**Data at Risk:**

- **Confluence**: Internal ADRs, business strategies, customer information, security practices
- **JIRA**: Customer names, feature roadmaps, revenue data, support tickets
- **Code**: Architecture details, algorithms, API keys in comments, business logic
- **GCP Logs**: Production errors with user data, system internals, performance characteristics
- **Conversations**: Discussions about vulnerabilities, internal processes, unreleased features

**Layered Mitigations:**

**Layer 1: Human Review (Current - Phase 1)**
- ✅ **All PR descriptions reviewed** before human opens PR
- ✅ **All commit messages reviewed** before push
- ✅ **All Slack notifications visible** to engineer before posting
- ✅ **Engineer responsible** for identifying sensitive content
- ⚠️ **Limitation:** Relies on human vigilance, no automated detection

**Layer 2: Context Source Filtering (Phase 2)**
- [ ] **Confluence space allowlist:** Only sync approved public/internal spaces
- [ ] **JIRA project allowlist:** Only sync approved projects (no customer support tickets)
- [ ] **Code repo allowlist:** Only sync approved repositories
- [ ] **GCP resource scoping:** Only logs/metrics from approved services
- [ ] **Exclude patterns:** `.env` files, `/secrets/` directories, customer data repos

**Layer 3: Content Classification (Phase 2-3)**
- [ ] **Document tagging:** Mark Confluence docs as Public/Internal/Confidential
- [ ] **JIRA field classification:** Identify fields with sensitive data
- [ ] **Code scanning:** Detect API keys, tokens, PII in comments
- [ ] **Metadata filtering:** Remove customer identifiers before sync

**Layer 4: DLP Scanning (Phase 3-4)**
- [ ] **Cloud DLP integration:** Scan all context before Claude API calls
- [ ] **Pattern matching:** Detect SSN, credit cards, API keys, customer names
- [ ] **Redaction:** Automatically redact detected sensitive patterns
- [ ] **Alerting:** Flag attempts to reference highly sensitive content
- [ ] **Blocking:** Prevent Claude API calls with unredacted sensitive data

**Layer 5: Output Monitoring (Phase 3-4)**
- [ ] **PR description scanning:** Check for leaked confidential info before creation
- [ ] **Commit message scanning:** Detect sensitive references
- [ ] **Notification scanning:** Check Slack messages for leaks
- [ ] **Audit logging:** Track what data was sent to Claude API
- [ ] **Anomaly detection:** Flag unusual data volumes or patterns

**Current Risk Assessment:**

**Risk Level: MEDIUM (Phase 1)**
- ✅ Human review provides basic protection
- ✅ Network isolation prevents direct exfiltration
- ✅ No direct production access limits exposure
- ⚠️ No automated detection of sensitive content
- ⚠️ Relies on engineer identifying confidential info in PR/commit reviews
- ⚠️ Claude API receives full unfiltered context

**Risk Level: LOW (Phase 3-4 with DLP)**
- ✅ Automated DLP scanning
- ✅ Context source filtering and classification
- ✅ Redaction of known sensitive patterns
- ✅ Output monitoring and blocking
- ✅ Audit trail of data sent externally

**Recommended Immediate Actions (Phase 1):**

1. **Document acceptable context sources**
   - Which Confluence spaces are safe? (Engineering ADRs: YES, Customer contracts: NO)
   - Which JIRA projects? (Internal engineering: YES, Customer support: NO)
   - Which code repos? (Open source + internal tools: YES, Payment processing: NO)

2. **Engineer training**
   - How to identify confidential content in PR descriptions
   - What to look for in commit messages
   - When to reject agent output

3. **Monitoring baseline**
   - Log all Claude API calls (prompt sizes, response sizes)
   - Track what context was included in each session
   - Manual review of logs weekly

4. **Incident response plan**
   - If sensitive data leaked to PR: Close PR, notify security team
   - If sensitive data sent to Claude API: Document incident, assess exposure
   - If pattern detected: Update filters, retrain engineer

**Future Enhancements Timeline:**

**Phase 2:** Source filtering and classification
**Phase 3:** DLP scanning with Cloud DLP, output monitoring, automated redaction

**Anthropic Security Considerations:**

- **Claude API data handling:** Anthropic states they don't train on API data (verify current policy before Phase 2)
- **Data retention:** Anthropic retains API logs for 30 days (verify current policy before Phase 2)
- **Encryption:** All API calls over HTTPS
- **Access control:** API key scoped to our account only
- **Audit:** Anthropic provides usage logs

**Action Required:** Review and document Anthropic's current data handling policies before Phase 2 rollout.

**Trade-off Accepted (Phase 1):**
We accept MEDIUM risk of confidential data exfiltration during pilot phase because:
1. Human reviews all outputs before they become public
2. Agent has no direct production access
3. Network isolation limits exfiltration vectors
4. Pilot scope limited to approved engineers and low-sensitivity repos
5. Benefits outweigh risks for internal engineering work

**Escalation to LOW risk required before:**
- Expanding to customer-facing repos
- Including customer support JIRA projects
- Accessing production GCP logs with user data
- Multi-engineer rollout beyond pilot

**Escalation Approval Process:**
- Security team reviews DLP implementation
- Engineering leadership approves multi-engineer rollout
- Data exfiltration risk assessment documents LOW risk achievement
- Requires: Context filtering + DLP scanning + output monitoring all operational

### Security Risks & Mitigations

**Risk 1: Data exfiltration of confidential information (PRIMARY CONCERN)**

See comprehensive analysis in [Data Exfiltration Concerns](#data-exfiltration-concerns) section above.

**Summary:**
- **Threat:** Agent reads confidential Confluence/JIRA/code/GCP data, could leak via PR descriptions, commits, Slack, or Claude API
- **Impact:** HIGH - Business strategies, customer data, security practices exposed
- **Current Risk:** MEDIUM (Phase 1) - Human review only, no automated detection
- **Target Risk:** LOW (Phase 3) - DLP scanning, source filtering, output monitoring
- **5-Layer Defense:** Human review → Source filtering → Classification → DLP scanning → Output monitoring
- **Escalation:** DLP required before customer-facing repos or multi-engineer rollout

Detailed remediation plan in Future Enhancements section.

**Risk 2: Agent-generated code contains security vulnerability**
- **Mitigation:** All code reviewed by human before merge
- **Mitigation:** Automated security scanning in CI/CD pipeline
- **Mitigation:** Codebase analyzer runs weekly security checks
- **Mitigation:** Agent cannot deploy, only prepare code for review

**Risk 3: Prompt injection via malicious context (e.g., crafted JIRA ticket)**
- **Mitigation:** Agent has no credentials to abuse
- **Mitigation:** Cannot access production or push directly
- **Mitigation:** All outputs reviewed by human
- **Mitigation:** Network isolation prevents lateral movement
- **Future:** Content filtering on context sources

**Risk 4: GitHub API token compromise**
- **Mitigation:** Token scoped to PR creation and commenting only
- **Mitigation:** Cannot merge, modify settings, or access secrets
- **Mitigation:** Token has minimal required permissions (repo:write for PRs only)
- **Mitigation:** Token rotation via systemd on schedule
- **Mitigation:** Monitoring for unusual API activity

**Risk 5: Claude API compromise or malicious responses**
- **Mitigation:** Agent sandboxed, cannot affect production
- **Mitigation:** All outputs reviewed before production
- **Mitigation:** Audit logs track all agent actions
- **Mitigation:** Network isolation limits blast radius

**Risk 6: Context sync pulls malicious content**
- **Mitigation:** Context mount is read-only to agent
- **Mitigation:** Sync runs on host, not in container
- **Mitigation:** Can validate/sanitize during sync process
- **Future:** Content scanning before sync

**Risk 7: Supply chain attack via dependencies**
- **Mitigation:** Agent installs packages in isolated container
- **Mitigation:** Package installs logged and reviewable
- **Mitigation:** Dockerfile controls base image
- **Mitigation:** Can add package verification to workflow

**Risk 8: GCP service account credential compromise (Phase 2-3)**
- **Mitigation:** Service accounts scoped to read-only permissions
- **Mitigation:** No Secret Manager or production write access
- **Mitigation:** IAM policies limit access to specific resources only
- **Mitigation (Phase 2-3 laptop):** Service account key rotation on schedule (weekly/monthly)
- **Mitigation (Phase 3+ Cloud Run):** Workload Identity eliminates service account keys entirely
- **Mitigation:** Audit logging tracks all GCP API calls
- **Mitigation:** Anomaly detection on unusual access patterns
- **Mitigation:** Can revoke service account instantly if compromised

**Risk 9: Engineer approves PR without proper review**
- **Mitigation:** Mobile UI shows key changes prominently
- **Mitigation:** Test status front-and-center
- **Mitigation:** Large/complex changes flagged for detailed review
- **Mitigation:** PR descriptions comprehensive
- **Future:** Automated review checklist

## Continuous Improvement Strategy

### Automated Analysis System

**1. Codebase Analyzer (Weekly)**

**Purpose:** Identify code quality, security, and structural issues

**Process:**
1. Runs Monday 11 AM via systemd timer
2. Analyzes entire codebase with Claude
3. Categorizes issues (HIGH/MEDIUM/LOW priority)
4. Generates improvement recommendations
5. Posts to Slack for review

**Output Guides:**
- Security vulnerability fixes
- Code quality improvements
- Documentation gaps
- Structural refactoring
- Best practice adoption
- Directory organization issues
- Dependency updates

**2. Conversation Analyzer (Daily)**

**Purpose:** Improve agent prompts and behavior based on interactions

**Process:**
1. Runs daily at 2 AM via systemd timer
2. Analyzes last 7 days of agent conversations
3. Identifies patterns in errors, successful interactions
4. Recommends prompt refinements
5. Suggests new slash commands or capabilities

**Output Guides:**
- System prompt improvements
- New slash command creation
- Error handling enhancements
- Workflow optimizations
- Context gathering improvements
- **Demeanor and culture alignment** (tone, collaboration quality, KA values adherence)
- Communication style refinements (clarity, precision, helpfulness)
- Problem-solving approach adjustments (systematic vs. ad-hoc)

**Cultural Alignment Evaluation:**
The conversation analyzer specifically assesses:
- **Technical communication:** Clear, concise, data-driven
- **Collaboration quality:** Respectful, constructive, proactive (L3-L4 behavior)
- **Problem-solving approach:** Systematic debugging, edge case consideration
- **User empathy:** End user impact mentioned in decisions
- **Honesty:** Admitting uncertainty vs. guessing
- **Escalation timing:** Asking for help appropriately (not too early, not thrashing)

If agent drifts from team engineering culture, analyzer recommends:
- Specific prompt adjustments to agent rules
- Examples of desired vs. observed behavior
- Training data from team guidelines to reinforce standards

### Feedback Loops

**1. Engineer Feedback**
- Slack reactions (👍/👎) on agent output
- Explicit feedback via `/feedback` command
- Code review comments on agent PRs
- Feature requests and bug reports

**2. Automated Metrics**
- Task completion rate
- PR acceptance rate (merged vs. closed without merge)
- Time to completion
- Code quality metrics (tests passing, linter scores)
- Security scan results
- Mobile review adoption rate

**3. Iterative Refinement**
- Weekly review of analyzer recommendations
- Monthly prompt engineering sessions
- Quarterly capability assessments
- Annual architecture reviews

### Learning Mechanisms

**Short-term (Session Memory):**
- `/save-context` preserves session learnings
- `/load-context` recalls previous work
- Context documents accumulate knowledge

**Medium-term (Prompt Evolution):**
- Analyzer recommendations update system prompts
- New patterns added to agent rules
- Anti-patterns documented and avoided

**Long-term (Capability Expansion):**
- New slash commands for common workflows
- Additional connectors (MCP servers, APIs)
- Enhanced automation safe-guards
- Expanded autonomous capabilities

## Migration & Adoption

### Phase 1: MVP (Current)
✅ Core sandbox infrastructure
✅ Slack integration
✅ Basic context (Confluence/JIRA file-based sync)
✅ Manual task assignment via Slack
✅ Automated analyzers
✅ Mobile-accessible Slack interface
🔄 Safe PR creation (in progress)

### Phase 2: Enhanced Mobile & Context
- [ ] Automated PR creation fully operational
- [ ] Mobile review workflow optimized (quick actions, inline diffs)
- [ ] **Cultural alignment implementation** (sandbox/.claude/rules/company-culture.md from Confluence guidelines)
- [ ] **Conversation analyzer demeanor evaluation** (assess cultural fit)
- [ ] **Assess MCP servers** for real-time context access
- [ ] **Evaluate API integration** for JIRA/GitHub bi-directional updates
- [ ] **Design GCP service account strategy** (scoping, IAM policies, rotation)
- [ ] **Context source filtering** (Confluence space allowlist, JIRA project allowlist)
- [ ] **Document classification** policy (what's safe to send to Claude API)
- [ ] **Data exfiltration monitoring** baseline (log all Claude API calls)
- [ ] Enhanced Slack slash commands for task management
- [ ] Team-based task routing

### Phase 3: Production Hardening & Cloud Migration

**Detailed specifications in companion ADRs:**
- [ADR-Message-Queue-Slack-Integration](../not-implemented/ADR-Message-Queue-Slack-Integration.md) - Cloud Pub/Sub for Slack messaging
- [ADR-Context-Sync-Strategy-Custom-vs-MCP](../implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) - MCP for Jira/GitHub, custom for Confluence
- [ADR-Slack-Integration-Strategy-MCP-vs-Custom](../not-implemented/ADR-Slack-Integration-Strategy-MCP-vs-Custom.md) - MCP for reading, Pub/Sub for sending
- [ADR-Slack-Bot-GCP-Integration](../not-implemented/ADR-Slack-Bot-GCP-Integration.md) - Full slash command interface
- [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md) - Complete Terraform infrastructure

**Infrastructure:**
- [ ] **Cloud Run deployment** via Terraform (Pattern A from ADR #889)
- [ ] Cloud Run services: egg-bot, slack-worker, slack-receiver
- [ ] Cloud Run jobs: egg-task, egg-sync, egg-analyze
- [ ] Cloud Storage for context sync and artifacts
- [ ] Native GCP IAM (Workload Identity, no service account key files)
- [ ] Multi-engineer support (container instances per engineer)

**Messaging & State:**
- [ ] Cloud Pub/Sub for Slack messaging (replace file-based)
- [ ] Firestore for job tracking, thread state, user auth
- [ ] Cloud Tasks for job queue management

**Context Evolution:**
- [ ] **Hybrid context strategy:** MCP for real-time (Jira/GitHub), files for bulk (Confluence)
- [ ] **GCP read-only integration:** Cloud Logging, Cloud Monitoring, BigQuery (non-sensitive only)
- [ ] Service account key rotation automation (or Workload Identity)
- [ ] GCP access audit logging and alerting

**Security:**
- [ ] **DLP integration** (Cloud DLP scanning before Claude API calls)
- [ ] **Automated redaction** of PII, API keys, sensitive patterns
- [ ] **Output monitoring** (scan PR descriptions, commits, Slack for leaks)
- [ ] User allowlist and rate limiting in Firestore
- [ ] Advanced security scanning before PR creation

**Operations:**
- [ ] Automated test generation and execution
- [ ] Performance monitoring integration
- [ ] Disaster recovery and backup
- [ ] Budget alerts (~$114/month estimate per ADR-GCP-Deployment-Terraform)

### Phase 4: Scale & Optimize
- [ ] Cross-repo context awareness
- [ ] Advanced autonomous capabilities (with safe-guards)
- [ ] Integration with CI/CD pipelines for automated testing
- [ ] A/B testing framework for prompts
- [ ] Self-service onboarding for new engineers
- [ ] Voice interface for mobile task assignment

### Adoption Strategy

**1. Pilot Program (Phase 1)**
- Single engineer (early adopter)
- Well-defined task types (documentation, tests, refactoring)
- Mobile workflow emphasis
- Weekly feedback sessions
- Rapid iteration on pain points

**2. Early Adopters (Phase 2)**
- 3-5 engineers
- Broader task types
- Mobile-first PR reviews validated
- Team-level workflows
- Process documentation

**3. Team Rollout (Phase 3)**
- Entire team (10-15 engineers)
- Full feature set including MCP/API context
- Training and onboarding materials
- Support channels established

**4. Organization-wide (Phase 4+)**
- All engineering teams
- Specialized configurations per team
- Best practices shared
- Center of excellence established

## Consequences

### Positive

**For Engineers:**
- ✅ More time for strategic, creative work
- ✅ Less time on boilerplate, documentation, repetitive tasks
- ✅ Can stay productive from mobile device
- ✅ Faster turnaround on well-defined features
- ✅ Improved code quality and consistency
- ✅ Better documentation coverage
- ✅ Work-life balance improved (can review on-the-go)

**For Engineering Organization:**
- ✅ Increased capacity without headcount growth
- ✅ Faster delivery on routine work
- ✅ Consistent code standards
- ✅ Reduced technical debt
- ✅ Better knowledge capture and transfer
- ✅ More flexible work arrangements

**For Product:**
- ✅ Faster feature delivery
- ✅ Higher quality output
- ✅ More engineering time for innovation
- ✅ Reduced time to market

### Negative / Trade-offs

**Initial Costs:**
- ⚠️ Setup time and learning curve
- ⚠️ Infrastructure maintenance overhead
- ⚠️ Prompt engineering investment
- ⚠️ Review process adaptation
- ⚠️ Mobile workflow training

**Ongoing Considerations:**
- ⚠️ Claude API costs (offset by time saved)
- ⚠️ Context sync maintenance (file-based initially)
- ⚠️ Security monitoring requirements
- ⚠️ Potential over-reliance on agent
- ⚠️ GitHub API rate limits

**Risks:**
- ⚠️ Agent-generated code may have subtle bugs (mitigated by human review)
- ⚠️ Over-automation could reduce engineer skill growth (agent handles routine, not complex work)
- ⚠️ Dependency on third-party LLM provider (architecture supports swapping)
- ⚠️ Context drift if syncing breaks (monitoring and alerting)
- ⚠️ Mobile review may be less thorough (large changes flagged for desktop review)
- ⚠️ File-based context has lag (near-term: assess MCP/API integration)

**Mitigations:**
- All code reviewed before merge (mobile or desktop)
- Agent handles routine work, engineers handle complex/creative work
- Architecture supports swapping LLM providers (minimal coupling)
- Monitoring and alerting on sync failures
- Large/complex PRs flagged for detailed desktop review
- Phase 2-3: Evaluate hybrid context approach (MCP + APIs)

## Decision Permanence

**Reversible Decisions (Low Cost to Change):**
- LLM provider (Claude vs. others) - Architecture supports swapping
- Context sync mechanism (file-based vs. API vs. MCP) - Planned evolution
- Slack bot implementation details
- Analyzer configurations
- Mobile UI layout and quick actions
- GitHub token permissions

**Semi-Permanent (Moderate Cost to Change):**
- Docker-based isolation approach (could move to VMs, cloud workstations)
- Slack as primary interface (could add web UI, but Slack likely remains)
- Git-based code management (fundamental to workflow)
- Human review requirements (core safety principle)
- PR creation workflow (could evolve to more automation)

**Permanent (High Cost to Change):**
- Sandboxed execution model (fundamental security principle)
- Human-in-the-loop for production changes (regulatory, safety requirement)
- Security-first architecture (credential isolation, network isolation)
- Audit logging requirements (compliance, debugging essential)
- Mobile-first design philosophy (affects all UX decisions)

**Review Cadence:**
- **Weekly:** Analyzer output, quick improvements
- **Monthly:** Operational metrics, prompt refinements, mobile UX feedback
- **Quarterly:** Feature priorities, capability additions, context strategy assessment (MCP/API evaluation)
- **Annually:** Architecture review, provider evaluation, security audit

## Future Enhancements

### Near-term

**1. Data Exfiltration Risk Remediation (PRIORITY - Phase 2-3)**
```
Reduce data exfiltration risk from MEDIUM to LOW:

Phase 2 (Early):
✓ Document acceptable context sources
  - Create Confluence space allowlist (Engineering, Product ADRs only)
  - Create JIRA project allowlist (Internal engineering projects only)
  - Exclude: Customer support, sales, finance, HR spaces/projects

✓ Implement context source filtering
  - Update sync scripts to respect allowlists
  - Add configuration for allowed/blocked patterns
  - Test filtering with sensitive test documents

✓ Engineer training program
  - Document what constitutes confidential information
  - Review process for PR descriptions (checklist)
  - Examples of sensitive content to watch for
  - Regular review sessions

✓ Monitoring baseline
  - Log all Claude API calls (timestamp, prompt size, context sources)
  - Regular manual review of logs
  - Track what Confluence/JIRA docs were included

Phase 2 (Later):
✓ Content classification system
  - Tag Confluence docs (Public/Internal/Confidential)
  - JIRA custom field for sensitivity level
  - Automated detection of classification tags
  - Block unclassified documents from high-risk sources

✓ Basic pattern detection
  - Regex patterns for common sensitive data (SSN, credit cards)
  - API key detection in code comments
  - Customer name matching against known list
  - Warning (not blocking) on pattern detection

Phase 3:
✓ Cloud DLP integration
  - Deploy Cloud DLP API for scanning
  - Configure infotypes (PII, credentials, custom patterns)
  - Scan context before Claude API calls
  - Redact detected sensitive data automatically
  - Block Claude API call if critical sensitivity detected

✓ Output monitoring
  - Scan PR descriptions before GitHub API call
  - Scan commit messages for sensitive references
  - Scan Slack notifications before posting
  - Alert + block on detected leaks

✓ Anomaly detection
  - Baseline normal data volumes to Claude API
  - Alert on unusual prompt sizes or frequency
  - Flag sudden inclusion of new context sources
  - Monitor for API key or credential patterns

Success Metrics:
- Zero confidential data leaks to public PRs
- >95% of sensitive patterns caught by DLP
- <5% false positive rate on DLP blocking
- 100% of context sources classified
- Weekly audit log review showing no policy violations

Testing Plan:
- Create test Confluence docs with fake sensitive data
- Verify filtering blocks excluded spaces
- Verify DLP catches planted PII/credentials
- Verify output monitoring catches leaks in PR descriptions
- Simulate breach scenario and validate incident response
```

**2. MCP Server Integration (Context Evolution)**
```
Evaluate Model Context Protocol servers for:
- Real-time JIRA ticket updates
- GitHub PR/issue activity
- Monitoring/observability data
- Code review comments
- Live system metrics

Benefits:
- No sync lag for critical updates
- Bi-directional context flow
- Richer context awareness
- Reduced file-based sync load
```

**3. GCP Read-Only Integration (Operational Context)**
```
Limited-scope service accounts for production insights:

Cloud Logging:
- Recent error patterns for debugging
- Application log context for bug fixes
- Performance issues and warnings
- User-reported error correlation

Cloud Monitoring:
- System health indicators
- Performance metrics and trends
- Resource utilization patterns
- Alerting history and patterns

Cloud Storage:
- Configuration file snapshots
- Public documentation assets
- Non-sensitive data exports

BigQuery:
- Usage analytics and patterns
- Performance benchmarking data
- Feature adoption metrics
- A/B test results

Security Controls:
- Read-only IAM roles (no write, no Secret Manager)
- Resource-level IAM policies (specific buckets/datasets)
- Service account key rotation (automated, monthly)
- Audit logging (all GCP API calls tracked)
- Anomaly detection (unusual access patterns flagged)
- Break-glass revocation (instant disable if compromised)

Benefits:
- Agent understands production context
- Better debugging recommendations
- Performance-aware code suggestions
- Real-world usage informs decisions
```

**4. API-Based Bi-Directional Updates**
- Agent comments on PRs (via GitHub API)
- Agent updates JIRA ticket status
- Agent posts to Slack threads (not just notifications)
- Agent requests additional context when needed

**5. Enhanced Mobile Workflow**
- Voice-to-text for task assignment
- Offline support for reading (review when back online)
- Progressive web app for richer mobile experience
- Notification preferences (critical only, all updates, etc.)

**6. Cultural Alignment Self-Assessment**
```
Agent self-evaluates adherence to team engineering standards:

Automated Assessment:
- After each task completion, agent scores itself on team values
- Technical excellence (code quality, testing, documentation)
- Communication clarity (PR descriptions, commit messages)
- Problem-solving approach (systematic, user-focused)
- Collaboration quality (respectful, constructive feedback)

Reference Standards:
- Load Software Engineer level expectations from documentation
- Compare output quality to L3/L4 criteria
- Identify gaps between expected and actual behavior

Self-Improvement Loop:
- Flag areas for improvement to conversation analyzer
- Request additional guidance when uncertain about cultural fit
- Propose prompt refinements to better align with team standards
- Track cultural alignment metrics over time

Examples:
Good: "I wrote comprehensive tests covering edge cases (L4 behavior)"
Gap: "I didn't consider user impact in this API design (missing user focus)"
Correction: "Let me revise to prioritize user workflow efficiency"

Success Metrics:
- >90% of agent outputs match L3-L4 communication standards
- User empathy (end user impact) mentioned in >75% of feature decisions
- Cultural alignment score >85% (conversation analyzer evaluation)
- Zero complaints about agent demeanor or collaboration style
- Engineer feedback: "Feels like working with a team engineer"

Benefits:
- Continuous cultural alignment without manual monitoring
- Agent learns team standards through self-reflection
- Earlier detection of cultural drift
- Data for conversation analyzer to refine prompts
```

### Medium-term

**7. Cloud Run Production Deployment**

> **Detailed specification:** See [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md) for complete architecture.

```
Deploy egg to Cloud Run using Terraform (Pattern A from ADR #889):

Cloud Run Services:
- egg-bot: Slack slash command handler, job orchestration
- slack-worker: Outbound message processing with rate limiting
- slack-receiver: Socket Mode event ingestion

Cloud Run Jobs:
- egg-task: Task execution (Claude Code container)
- egg-sync: Context synchronization (Confluence, Jira, GitHub)
- egg-analyze: Codebase and conversation analysis

Supporting Infrastructure:
- Cloud Pub/Sub: slack-outgoing, slack-incoming, slack-outgoing-dlq
- Firestore: jobs, threads, contexts, users, beads collections
- Cloud Tasks: egg-tasks, egg-sync, egg-analyze queues
- Cloud Storage: context-sync, artifacts buckets
- Secret Manager: Slack tokens, API keys
- Cloud Scheduler: Periodic sync jobs (via scheduled-job module)

Security:
- Workload Identity Federation (keyless auth)
- Dual service accounts (RW main, RO PRs per ADR #889)
- User allowlist in Firestore
- Per-command rate limiting

Cost Estimate: ~$114/month (see ADR for breakdown)
```

**8. Multi-Repository Awareness**
- Cross-repo context understanding
- Dependency impact analysis
- Coordinated multi-repo changes
- Shared library updates

**9. Advanced Test Generation**
- Automated unit test creation
- Integration test scaffolding
- Test coverage analysis
- Property-based test generation
- Visual regression tests

**10. Performance Monitoring Integration**
- Before/after performance analysis
- Automated benchmarking
- Regression detection
- Optimization recommendations

### Long-term

**11. Proactive Improvements**
- Agent suggests refactorings based on codebase analysis
- Security vulnerability scanning and automated fixes
- Dependency updates with comprehensive testing
- Dead code identification and removal
- Technical debt reduction campaigns

**12. Team Coordination**
- Multi-agent collaboration (multiple engineers, multiple agents)
- Workload balancing across agents
- Conflict resolution (when agents work on overlapping code)
- Shared context management

**13. Self-Service Capabilities**
- Engineer-defined workflows
- Custom slash commands per engineer/team
- Personalized agent behavior
- Team-specific configurations

### Safe Autonomous Actions

**Expanded with Safe-Guards:**

**Automated PR Creation (Current Phase):**
- ✅ Safe: Creates PR with tag to engineer
- ✅ Safe: Cannot merge without approval
- ✅ Safe: Read-only GitHub token
- ✅ Safe: All PRs require human review

**Test Execution:**
- ✅ Safe: Run tests in isolated environment
- ✅ Safe: Report results via Slack and in PR
- ✅ Safe: No deployment on success
- ✅ Safe: Failures block "approved-for-merge" label

**Documentation Updates:**
- ✅ Safe: Update docs in PR (requires review)
- ✅ Safe: Link to source code changes
- ✅ Safe: Version control maintained
- ✅ Safe: Review before merge

**Dependency Updates (with constraints):**
- ⚠️ Risky: Requires extensive testing
- ✅ Safe if: Minor/patch versions only
- ✅ Safe if: Full test suite passes
- ✅ Safe if: Human reviews breaking changes
- ✅ Safe if: Rollback plan documented

## Alternatives Considered

### Alternative 1: Cloud-Based IDE (e.g., GitHub Codespaces, Replit Agent)

**Approach:** Use hosted development environment with LLM integration

**Pros:**
- Managed infrastructure
- Built-in security
- No local setup required
- Integrated with existing tools

**Cons:**
- Less control over environment
- Vendor lock-in
- Limited customization
- Cost at scale
- Credential management still complex
- Mobile experience limited

**Rejected Because:** Need full control for security requirements and custom workflows. Codespaces don't provide credential isolation model we need. Mobile experience not as flexible as Slack.

### Alternative 2: VM-Based Isolation

**Approach:** Run agent in dedicated VM per engineer

**Pros:**
- Stronger isolation than containers
- Can simulate production environment
- More flexibility in networking

**Cons:**
- Higher resource usage
- Slower startup/teardown
- More complex management
- Higher costs

**Rejected Because:** Docker provides sufficient isolation with better resource efficiency and faster iteration.

### Alternative 3: Pure API Integration (Confluence, JIRA, GitHub) - No File Sync

**Approach:** Real-time API calls instead of file-based sync

**Pros:**
- Always up-to-date data
- No sync lag
- Richer data access (comments, history)
- Bi-directional updates possible

**Cons:**
- Complex authentication management
- API rate limits
- More failure modes
- Longer implementation time
- Need error handling for each API
- Higher initial development cost

**Rejected for Phase 1 Because:** File-based sync is faster to implement and sufficient for most use cases. **However, we will assess hybrid approach (MCP servers + APIs) in Phase 2-3** for high-value real-time integrations while keeping file-based for bulk documentation.

### Alternative 4: Self-Hosted Open Source LLM

**Approach:** Deploy Llama 3, CodeLlama, or similar locally

**Pros:**
- No API costs
- Full data control
- No external dependencies
- Customizable model

**Cons:**
- Significant GPU infrastructure required
- Lower code quality than Claude/GPT-4
- Model management overhead
- Fine-tuning complexity
- Limited context window
- Higher operational complexity

**Rejected Because:** Quality gap is significant. API costs are offset by time saved. Can reconsider if costs become prohibitive or open-source capabilities improve significantly.

### Alternative 5: Fully Autonomous (No Human Review)

**Approach:** Agent merges PRs directly to production after tests pass

**Pros:**
- Maximum automation
- Fastest delivery
- No review bottleneck

**Cons:**
- Unacceptable risk level
- Regulatory concerns (code review requirements)
- Debugging difficulty (harder to trace issues)
- Loss of learning opportunity
- Trust building required first
- Security vulnerabilities could reach production

**Rejected Because:** Safety and quality requirements mandate human review. May revisit for specific low-risk scenarios (documentation-only updates, automated dependency patches) after establishing trust and track record.

### Alternative 6: Web UI Instead of Slack

**Approach:** Custom web application for task assignment and review

**Pros:**
- More control over UX
- Richer interfaces possible
- Better for complex reviews (side-by-side diffs)
- Custom workflows easier

**Cons:**
- Another tool to maintain
- Not where engineers already are
- Poor mobile experience without custom app development
- Higher development cost
- Slower adoption (new tool to learn)
- Notification fatigue (separate from Slack)

**Rejected Because:** Slack is where engineers work. Mobile support is critical. Low friction is key to adoption. Can add web UI later for specific advanced workflows (complex code reviews, configuration management), but Slack remains primary interface.

### Alternative 7: PR Description Only (No PR Creation)

**Approach:** Agent generates PR description, human creates PR manually

**Pros:**
- Simpler implementation
- No GitHub API token needed
- Human fully controls PR creation
- Lower security risk

**Cons:**
- Requires engineer at computer (can't do from phone easily)
- More friction in workflow
- Less automation benefit
- Slower iteration

**Rejected Because:** Mobile productivity is core requirement. Creating PR from phone with manual copy-paste is cumbersome. Read-only GitHub token is low-risk. Safe PR creation (agent creates, human merges) provides best balance of automation and safety.

---

## Implementation Checklist

### Phase 1 (Complete)
- [x] Docker sandbox infrastructure
- [x] Claude Code integration
- [x] Slack bidirectional messaging
- [x] Confluence/JIRA markdown sync (file-based)
- [x] Automated analyzers (codebase, conversation)
- [x] File-based notification system
- [x] Systemd service management
- [x] Security isolation (no credentials)
- [x] Mobile-accessible Slack interface

### Phase 2 (In Progress)
- [x] Safe automated PR creation (agent creates, human merges)
- [x] PR auto-review for others' PRs (pattern-based analysis)
- [x] Comment response suggestions (contextual response generation)
- [x] Full repo PR sync (--repo and --all-prs flags)
- [ ] Mobile-optimized review workflow (quick actions, inline diffs)
- [ ] **Team cultural alignment** (`.claude/rules/code-standards.md` and team documentation)
- [ ] **Conversation analyzer demeanor evaluation** (cultural fit assessment)
- [ ] **Context source filtering** (Confluence space allowlist, JIRA project allowlist)
- [ ] **Document classification policy** (what's safe to send to Claude API)
- [ ] **Data exfiltration monitoring baseline** (log all Claude API calls)
- [ ] **Engineer training program** (identify confidential content in PR/commit reviews)
- [ ] **MCP server evaluation** for real-time context
- [ ] **API integration assessment** for bi-directional updates
- [ ] **GCP service account design** (IAM policies, scoping, rotation)
- [ ] Enhanced error handling and retry logic
- [ ] GitHub context integration (PR/issue data)
- [ ] Comprehensive production documentation

### Phase 3 (Planned)
- [ ] **Cloud Run deployment** via Terraform
- [ ] **Hybrid context strategy** implementation (MCP + files + GCP)
- [ ] **GCP read-only integration** (Cloud Logging, Monitoring, BigQuery)
- [ ] Workload Identity integration (no service account keys)
- [ ] GCP access audit logging and anomaly detection
- [ ] Multi-engineer workspaces (Cloud Run instances)
- [ ] Advanced test generation
- [ ] Performance monitoring integration
- [ ] Team coordination features
- [ ] Self-service configuration
- [ ] Voice interface for mobile task assignment

---

## Related ADRs

This ADR is the foundational document for egg. The following companion ADRs provide detailed specifications for Phase 3 GCP deployment:

| ADR | Purpose |
|-----|---------|
| [ADR-Message-Queue-Slack-Integration](../not-implemented/ADR-Message-Queue-Slack-Integration.md) | Cloud Pub/Sub messaging to replace file-based Slack notifications |
| [ADR-Context-Sync-Strategy-Custom-vs-MCP](../implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) | Hybrid approach: MCP for Jira/GitHub, keep custom sync for Confluence |
| [ADR-Slack-Integration-Strategy-MCP-vs-Custom](../not-implemented/ADR-Slack-Integration-Strategy-MCP-vs-Custom.md) | MCP for reading Slack, Pub/Sub for sending messages |
| [ADR-Slack-Bot-GCP-Integration](../not-implemented/ADR-Slack-Bot-GCP-Integration.md) | Full slash command interface replacing host-based commands |
| [ADR-GCP-Deployment-Terraform](../not-implemented/ADR-GCP-Deployment-Terraform.md) | Complete Terraform infrastructure following ADR #889 patterns |

**Architecture Evolution:**
```
Phase 1 (Current - Laptop)          Phase 3 (Target - GCP)
─────────────────────────────       ─────────────────────────────
Host Services:                      Cloud Run Services:
├── slack-notifier (file-based) →   ├── slack-worker (Pub/Sub)
├── slack-receiver (Socket Mode) →  ├── slack-receiver (Socket Mode)
├── context-sync (cron) ────────→   ├── egg-sync (Cloud Scheduler)
├── github-sync (cron) ─────────→   │   └── via scheduled-job module
├── codebase-analyzer (timer) ──→   ├── egg-analyze (Cloud Scheduler)
└── conversation-analyzer ──────→   └── egg-analyze (Cloud Scheduler)

Container (Docker):                 Cloud Run Jobs:
└── sandbox ──────────────→   ├── egg-task (Claude Code)
                                    ├── egg-sync (context sync)
                                    └── egg-analyze (analyzers)

Host Commands:                      Slack Slash Commands:
├── egg --exec ─────────────────→   ├── /egg task|status|cancel|logs
├── remote-control.sh ──────────→   ├── /sync confluence|jira|github
└── systemctl (services) ───────→   ├── /analyze codebase|conversation
                                    ├── /pr create|status|review
                                    └── /context save|load|list
```

## References

- [Claude Code Documentation](https://docs.anthropic.com/claude-code)
- [Model Context Protocol (MCP) Specification](https://modelcontextprotocol.io/)
- [Docker Security Best Practices](https://docs.docker.com/engine/security/)
- [Slack API Documentation](https://api.slack.com/)
- [GitHub API Documentation](https://docs.github.com/en/rest)
- [GitHub Mobile Review Workflow](https://github.com/mobile)
- [ADR #889: Terraform CI/CD Standards](https://khanacademy.atlassian.net/wiki/pages/viewpage.action?pageId=4385800306)
- [Khan Academy scheduled-job module](https://github.com/Khan/terraform-modules/tree/main/terraform/modules/scheduled-job)

---

**Last Updated:** 2025-11-28
**Next Review:** 2025-12-28 (Monthly)
**Status:** In Progress
