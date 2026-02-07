# Architecture Documentation

Technical design and system architecture.

## System Overview

egg runs as two Docker containers working together:

- **Gateway sidecar** (trusted) - Holds credentials, enforces policies, proxies all external access
- **Sandbox container** (untrusted) - Where the LLM agent runs with no credentials and restricted network

See the [main README](../../README.md) for the architecture diagram.

## Key Design Principles

**Sandboxing:**
- Docker container isolation
- No credentials in sandbox container
- All git/GitHub operations proxied through gateway
- Network restricted via Squid proxy (private mode)

**Credential Isolation:**
- Gateway injects Anthropic API keys at proxy time
- Gateway injects GitHub tokens for git/gh operations
- Sandbox never sees raw credentials

**Access Control:**
- Branch ownership (agent can only push to `egg/*` branches)
- Phase-based operation restrictions (git/gh operations filtered by SDLC phase)
- Role-based contract mutations (implementer, reviewer, human roles with field-level permissions)
- No merge capability (gateway has no merge endpoint)
- Force push and destructive operations blocked

## Components

| Component | Role | Documentation |
|-----------|------|---------------|
| **Gateway** | Credential injection, policy enforcement, HTTP proxy | [Gateway README](../../gateway/README.md) |
| **Sandbox** | Agent execution environment, git/gh wrappers | [Sandbox README](../../sandbox/README.md) |
| **Shared Libraries** | Config, logging, git utilities | [Shared README](../../shared/README.md) |
| **egg_contracts** | SDLC contract models, role-based mutation validation | `shared/egg_contracts/` |

## SDLC Contracts

Contracts are JSON documents that track issue progress through SDLC phases, tasks, decisions, and acceptance criteria. They provide structurally-verified agent checkpoints.

**Schema**: `.egg/schemas/contract.schema.json`

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
| `egg-contract mark-task --task <id> --status <status>` | Mark task status (reviewer only) |
| `egg-contract mark-phase --phase <id> --passed <bool>` | Mark phase status (reviewer only) |
| `egg-contract add-decision --question <text>` | Create HITL decision point |

### Plan Parser

The plan parser (`shared/egg_contracts/plan_parser.py`) extracts tasks from plan documents:
- Parses `[TASK-X-Y]` patterns from markdown
- Supports YAML front matter for structured task definitions
- Generates placeholder tasks for empty phases

### SDLC Prompt Builder

The prompt builder (`action/build-sdlc-prompt.sh`) provides phase-specific context and instructions to the agent during SDLC pipeline runs, including orientation context (issue number, current phase, branch) and document templates for the refine and plan phases.

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
- [Setup Guides](../setup/) - Installation and configuration
- [Project Structure](../development/STRUCTURE.md) - Directory layout
