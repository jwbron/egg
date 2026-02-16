# Architecture Documentation

Technical design and system architecture.

## System Overview

egg is a structurally enforced SDLC pipeline that turns GitHub issues into reviewed pull requests. The system runs as two Docker containers working together:

- **Gateway sidecar** (trusted) - Enforces SDLC phases, validates role permissions, injects credentials, proxies all external access
- **Sandbox container** (untrusted) - Where the LLM agent runs with no credentials and restricted network

The gateway acts as the enforcement engine for both process controls (SDLC phases) and security controls (credential isolation).

See the [main README](../../README.md) for the architecture diagram.

## Key Design Principles

**Structurally Enforced SDLC:**
- Work progresses through defined phases (refine → plan → implement → merge)
- Each phase has specific permitted operations enforced by the gateway
- Phase transitions require human approval
- Role-based contract mutations prevent agents from self-approving work

**Credential Isolation:**
- Gateway injects Anthropic API keys at proxy time
- Gateway injects GitHub tokens for git/gh operations
- Sandbox never sees raw credentials

**Access Control:**
- Branch ownership (agent can only push to `egg/*` branches)
- Phase-based operation restrictions (git/gh operations filtered by SDLC phase)
- File-level access restrictions (role-based blocking of sensitive files like contract files)
- Agent role-based file access (Coder, Tester, Documenter, Integrator have distinct write permissions)
- Role-based contract mutations (implementer, reviewer, human roles with field-level permissions)
- No merge capability (gateway has no merge endpoint)
- Force push and destructive operations blocked

## Components

| Component | Role | Documentation |
|-----------|------|---------------|
| **Gateway** | Credential injection, policy enforcement, HTTP proxy | [Gateway README](../../gateway/README.md) |
| **Orchestrator** | Local SDLC pipeline execution, state management, container lifecycle, worktree creation via gateway, real-time status visualization | [Orchestrator Architecture](orchestrator.md) |
| **Sandbox** | Agent execution environment, git/gh wrappers | [Sandbox README](../../sandbox/README.md) |
| **Shared Libraries** | Config, logging, git utilities, orchestrator types | [Shared README](../../shared/README.md) |
| **egg_contracts** | SDLC contract models, role-based mutation validation, multi-agent orchestration | `shared/egg_contracts/` |
| **egg_orchestrator** | Shared orchestrator types and sandbox-to-orchestrator communication | `shared/egg_orchestrator/` |
| **Multi-Agent Orchestration** | Parallel agent execution (Coder, Tester, Documenter, Integrator) | `orchestrator/multi_agent.py`, `orchestrator/container_spawner.py` |

## SDLC Contracts

Contracts are JSON documents that track issue progress through SDLC phases, tasks, decisions, and acceptance criteria. They provide structurally-verified agent checkpoints.

**Schemas**:
- `.egg/schemas/contract.schema.json` – Contract structure and role-based field ownership
- `.egg/schemas/yaml-tasks.schema.json` – Structured appendix format for plan documents (used by plan parser)
- `.egg/schemas/phase-permissions.schema.json` – Allowed git/gh operations and file restrictions per SDLC phase
- `.egg/schemas/checkpoint.schema.json` – Agent checkpoint structure (session context, transcripts, tool calls)

**Role-based ownership**: Each contract field is owned by a specific role:
- `implementer`: `tasks[].commit`, `tasks[].notes`, `tasks[].files_affected`
- `reviewer`: `tasks[].status`, `phases[].status`, `phases[].review_feedback`, `acceptance_criteria[].verified`, `current_phase`
- `human`: `decisions[].resolved`, `decisions[].resolution`, `decisions[].resolved_by`, `decisions[].resolved_at`, all other fields
- `system`: Structural fields (`issue`, `schemaVersion`)

The gateway enforces role-based mutations via the `/api/v1/contract/` endpoints. Role is determined from workflow context, preventing privilege escalation.

### Contract CLI

Agents interact with contract state via the `egg-contract` CLI (`sandbox/egg_lib/contract_cli.py`):

| Command | Purpose |
|---------|---------|
| `egg-contract show` | Display current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract add-decision --question <text> [--options ...] [--format {json,markdown}]` | Create HITL decision point with optional predefined choices and markdown output format for GitHub comments |
| `egg-contract add-feedback --question <text> [--question <text>...] [--format {json,markdown}]` | Create feedback comment for open-ended questions |

### Checkpoint System

Checkpoints capture agent session context as first-class versioned data in Git. The v2 checkpoint system captures **all agent sessions** (not just commits) with rich multi-dimensional querying.

**Triggers**: Checkpoints are captured on two events:
- **Commit**: When agents push commits during implementation
- **Session-end**: When agent containers terminate (completed, expired, or failed)

**Captured data**:
- **Transcript**: Full conversation history with timestamps and message roles
- **Tool calls**: All tool invocations with parameters, results, and durations
- **Files touched**: All file operations (read, write, edit, glob, grep)
- **Token usage**: Input/output tokens and estimated costs
- **Session metadata**: Session ID, agent role, model, duration
- **Workflow context**: Issue number, PR number, pipeline phase, agent type, session status

Checkpoints are stored in the `egg/checkpoints/v2` branch with a multi-dimensional index supporting rich queries. This provides full traceability from requirements to implementation, including sessions that didn't produce commits.

**Checkpoint CLI**

Browse and query checkpoints via the `egg-checkpoint` CLI:

| Command | Purpose |
|---------|---------|
| `egg-checkpoint list [filters] [--limit <n>]` | List checkpoints with metadata |
| `egg-checkpoint show <id-or-commit>` | Display full checkpoint details |
| `egg-checkpoint browse --issue <number>` | Filter checkpoints by issue number |
| `egg-checkpoint context [filters]` | Cross-agent context summary grouped by phase and agent type |
| `egg-checkpoint cost [filters]` | Show cost breakdown (token usage and USD) by phase and agent type |

**Supported filters**:
- `--branch <name>` — Filter by git branch
- `--issue <n>` — Filter by issue number
- `--pr <n>` — Filter by PR number
- `--pipeline <id>` — Filter by pipeline run ID (for multi-agent workflows)
- `--repo <owner/repo>` — Filter by source repository
- `--session <id>` — Filter by session ID
- `--trigger <commit|session_end>` — Filter by trigger type
- `--status <completed|expired|failed>` — Filter by session status
- `--agent-type <coder|tester|documenter|integrator|reviewer|unknown>` — Filter by agent type
- `--phase <refine|plan|implement|pr>` — Filter by pipeline phase

Checkpoints enable post-hoc analysis of agent behavior, debugging failed sessions, auditing agent decisions, and tracking token usage across issues and PRs.

### Plan Parser

The plan parser (`shared/egg_contracts/plan_parser.py`) extracts tasks and PR metadata from plan documents using three extraction modes in priority order:

1. **YAML code fence** (preferred): A `yaml` code block marked with `# yaml-tasks` header, structured according to `.egg/schemas/yaml-tasks.schema.json`. Provides machine-readable task data and PR metadata while allowing human-readable prose above it.
2. **YAML front matter** (legacy): A `---`-delimited YAML block at the document start. Supported for backwards compatibility.
3. **Markdown regex** (fallback): Parses `[TASK-X-Y]` patterns from markdown. Fragile and may miss tasks if LLM output format drifts.

The parser also extracts optional PR metadata (title and description) from the `pr:` field in the YAML data. If provided, this metadata is used when creating the pull request during the implement phase.

The parser generates placeholder tasks for empty phases and includes warnings for human review when parsing issues occur.

### Phase Checks

Each SDLC phase can have configurable automated checks that run before completion. The check system (`shared/egg_contracts/phase_defaults.py`) provides:

**Built-in checks:**
- `lint`: Runs project linter (via `make lint`)
- `test`: Runs project tests (via `make test` or pytest)
- `merge-conflict`: Detects merge conflicts with base branch
- `draft-validation`: Validates refine phase draft documents
- `plan-yaml`: Validates plan phase YAML appendix
- `deployment`: Validates changes against locally running devserver (DinD)
- `fixer`: Auto-fixes certain check failures when possible

**Phase defaults:**
- Refine: draft validation
- Plan: YAML validation
- Implement: merge conflict, lint (auto-retry), tests, auto-fixer, deployment (optional, auto-retry)
- PR: none

Contracts can override phase defaults via the `phase_configs` field, allowing per-issue check customization.

### SDLC Pipeline

The SDLC pipeline orchestrates agent-based development with structurally enforced checkpoints through the local orchestrator:

**Core components:**
- `orchestrator/dispatch.py` - Pipeline phase dispatch and management
- `orchestrator/container_spawner.py` - Agent container lifecycle management
- `orchestrator/decision_queue.py` - Human-in-the-loop decision handling with debounce
- `orchestrator/state_store.py` - Git-backed pipeline state management
- `orchestrator/routes/pipelines.py` - Pipeline API, prompt building, and visualization
- `.github/workflows/reusable-review.yml` - PR-based code review (invoked for draft PRs during implement phase)

**Resilience features:**
- HITL escalation: Generates checkbox-based decision UI with 30-second debounce
- Rate limiting: GitHub API rate limit tracking and automatic retry backoff
- Container monitoring: Health checks and automatic recovery

## Key Architectural Decisions

See [ADR Overview](../adr/README.md) for the full list. Key decisions:

- [Git Isolation Architecture](../adr/implemented/ADR-Git-Isolation-Architecture.md) - Worktree isolation via gateway
- [Gateway Credential Injection](../adr/implemented/ADR-Gateway-Credential-Injection.md) - Zero-credential sandbox
- [Anthropic API Credential Injection](../adr/implemented/ADR-Anthropic-API-Credential-Injection.md) - API key proxy
- [Declarative Setup Architecture](../adr/implemented/ADR-Declarative-Setup-Architecture.md) - Python-based setup
- [Standardized Logging Interface](../adr/implemented/ADR-Standardized-Logging-Interface.md) - Structured JSON logging
- [Internet Tool Access Lockdown](../adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) - Public/private network modes

## Design Guidelines

- [Agent Mode Design](../guides/agent-mode-design.md) - When to let the agent operate freely vs. when constraints are appropriate

## See Also

- [ADR: Autonomous Software Engineer](../adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full system architecture
- [Local Quickstart](../guides/local-quickstart.md) - Installation and configuration
- [Project Structure](../development/STRUCTURE.md) - Directory layout
