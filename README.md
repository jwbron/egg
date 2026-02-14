# egg

A structurally enforced SDLC pipeline for autonomous LLM agents — turning tasks into reviewed pull requests with mandatory human gates.

> *Inspired by Andy Weir's short story "The Egg" — a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

**Note**: this project is currently under heavy development. The core workflow is functional, but continually being refined and refactored. Expect breakages and changing behavior for the forseeable future.

## How It Works

Run `egg` to start an interactive session, then use the `/sdlc` skill to launch a multi-agent pipeline. The agent cannot skip steps, self-approve work, or bypass review — these constraints are enforced by the gateway infrastructure, not by prompts.

```
         ┌───────────┐     ┌───────────┐     ┌───────────────┐     ┌──────────┐
         │  REFINE   │────▶│   PLAN    │────▶│  IMPLEMENT    │────▶│  HUMAN   │
         │  task     │     │           │     │  + review     │     │  MERGE   │
         └─────┬─────┘     └─────┬─────┘     └───────────────┘     └────┬─────┘
               │                 │                                      │
               ▼                 ▼                                      ▼
          Human gate        Human gate                             GitHub UI
        (approve plan)  (approve tasks)                          (final merge)
```

1. **Refine** — Agent analyzes the task and produces a requirements document. Human approves.
2. **Plan** — Agent breaks work into tasks with acceptance criteria. Human approves before any code is written.
3. **Implement** — Agent creates a draft PR and implements tasks. CI runs, automated review provides line-level feedback. Re-implementation cycles continue until all checks pass.
4. **Merge** — Draft PR is marked ready. Only a human can merge via GitHub UI.

The `/sdlc` skill supports two modes:
- **Issue mode** (`/sdlc 123`): GitHub-issue-driven pipeline with full remote integration
- **Local mode** (`/sdlc` with no args): Prompt-driven pipeline that runs entirely locally

Both modes create a pipeline in the orchestrator, which spawns sandbox containers to execute each phase as a DAG. Pipeline state lives in a JSON contract (`.egg-state/contracts/{identifier}.json`) committed to the feature branch, giving full auditability of every phase transition.

## The Gateway

The gateway is the enforcement engine. It sits between the agent sandbox and the outside world, validating every operation against the current pipeline phase and role permissions.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                    egg                                      │
│                                                                             │
│   ┌───────────────────────────────┐      ┌───────────────────────────────┐  │
│   │       Gateway Sidecar         │      │      Sandbox Container        │  │
│   │      (Enforcement Engine)     │      │     (Untrusted Agent)         │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │ HTTP │  ┌─────────────────────────┐  │  │
│   │  │ Phase Filter            │◀─┼──────┼──│ git/gh wrappers         │  │  │
│   │  │ (block ops by phase)    │  │      │  │ (intercept all ops)     │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │      │  ┌─────────────────────────┐  │  │
│   │  │ Role Validator          │  │ API  │  │ egg-contract CLI        │  │  │
│   │  │ (enforce field ownership│◀─┼──────┼──│ (state mutations)       │  │  │
│   │  │  for contract mutations)│  │      │  │                         │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │      │  ┌─────────────────────────┐  │  │
│   │  │ Credential Injection    │  │Proxy │  │ Claude Code             │  │  │
│   │  │ (secrets never in       │◀─┼──────┼──│ (ANTHROPIC_BASE_URL)    │  │  │
│   │  │  sandbox)               │  │      │  │                         │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │HTTPS │  ┌─────────────────────────┐  │  │
│   │  │ Network Policy          │◀─┼──────┼──│ All outbound traffic    │  │  │
│   │  │ (domain allowlist)      │  │Proxy │  │ (private mode)          │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  HAS: GitHub tokens,          │      │  HAS: Workspace files only    │  │
│   │  Anthropic keys, network      │      │  NO: Credentials, .git/,      │  │
│   │                               │      │      direct network (private) │  │
│   └───────────────────────────────┘      └───────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key principle**: The agent cannot bypass controls because the capabilities don't exist in its environment. The gateway physically blocks operations — this is infrastructure enforcement, not behavioral controls.

## What's Enforced

The gateway enforces both process controls (SDLC phases) and security controls (credential isolation) as aspects of the same system:

| What | How It's Prevented |
|------|-------------------|
| Agent skips to implementation | Gateway blocks `git push` during refine and plan phases |
| Agent self-approves work | Role-based validation: implementer cannot modify task status |
| Agent merges its own PR | Gateway has no merge endpoint — humans must merge via GitHub UI |
| Agent steals credentials | Credentials never enter sandbox; gateway injects them at request time |
| Agent pushes to main | Gateway enforces branch policies; agent can only push to `egg/*` branches |
| Agent tampers with contracts | File-level restrictions block implementers from modifying contract files via `git push` |
| Agent exfiltrates code | Private mode restricts network to Anthropic API + private GitHub repos only |
| Agent accesses other workspaces | Each agent gets isolated git worktree; `.git/` is shadowed |

### Phase Permissions

Each pipeline phase has a defined set of permitted operations:

| Phase | Allowed Operations | Exit Requires |
|-------|-------------------|---------------|
| **Refine** | `gh issue comment/edit` | Human approval |
| **Plan** | `gh issue comment/edit`, `egg-contract add-decision` | Human approval |
| **Implement** | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
| **Merge** | `gh pr edit`, `git push` | Human merge |

### How Isolation Works

- **Zero credential exposure**: Anthropic API requests route through gateway; GitHub operations use wrappers that call gateway API. Container environment is sanitized.
- **Git isolation**: Each agent gets an isolated worktree. The `.git/` directory is shadowed (tmpfs mount) — the agent cannot access git metadata directly.
- **Network modes**: Public mode allows full internet + credential-injected API calls. Private mode restricts to Anthropic API + private GitHub repos only.

## Multi-Agent Orchestration

The orchestrator (`orchestrator/`) manages parallel execution of specialized agent roles during implementation. Each role runs in its own sandbox container with scoped permissions:

| Role | Responsibility |
|------|----------------|
| **Coder** | Write code, create commits, push branches |
| **Tester** | Run tests, validate acceptance criteria |
| **Documenter** | Update docs, generate changelogs |
| **Integrator** | Coordinate roles, manage PR lifecycle |

Roles are enforced by the gateway — each agent can only perform operations allowed for its role. The orchestrator handles wave-based execution, dependency tracking, container lifecycle, and result collection.

## Starting a Pipeline

```bash
# Start egg
egg

# Inside the session, launch the SDLC pipeline:
/sdlc 123              # Issue mode — drives from GitHub issue #123
/sdlc                  # Local mode — prompt-driven, no GitHub interaction
```

The pipeline creates a draft PR automatically when entering the implement phase. Once all checks pass, the PR is marked ready for human review and merge.

### Human-in-the-Loop Checkpoints

At each phase boundary (refine and plan), the pipeline pauses for human approval before proceeding. Interaction happens through checkbox-based UI in GitHub comments (issue mode) or terminal prompts (local mode). In local mode, the orchestrator also supports requesting changes to re-run a phase with feedback (limited by `max_review_cycles`, default 3).

- **Guidance**: Provide additional context, adjust acceptance criteria, break into subtasks
- **Override**: Mark complete, skip tasks, cancel pipeline
- **Manual**: Complete manually, reassign

## Quick Start

```bash
# Clone and install
git clone https://github.com/jwbron/egg.git
cd egg
pip install -e ./sandbox

# Run egg — auto-setup prompts on first run
egg
```

Running `egg` starts the gateway and sandbox automatically. On first run it will prompt you to configure repositories and credentials via `egg --setup`. By default it launches in public mode (full internet access); use `egg --private` for network-locked private repo mode.

Once inside a session, use `/sdlc` to launch the full pipeline, or work interactively with individual agent modes (`/coder-mode`, `/tester-mode`, etc.).

See the [Local Quickstart Guide](docs/guides/local-quickstart.md) for detailed setup instructions including PAT-based authentication.

### Docker Compose (Advanced)

For managing the gateway stack separately:

```bash
# Initialize configuration
bin/egg-deploy init

# Edit .env with your credentials (GITHUB_USER_TOKEN, etc.)
vim .env

# Start the gateway
bin/egg-deploy up

# Start a sandbox session against the running gateway
egg --compose
```

See the [Deployment Guide](docs/guides/deployment.md) for deployment options.

## CLI Reference

### egg CLI

| Command | Description |
|---------|-------------|
| `egg` | Start interactive sandbox session (public mode, auto-setup on first run) |
| `egg --public` | Explicit public mode (full internet access, default) |
| `egg --private` | Private mode (Anthropic API only, network lockdown) |
| `egg --setup` | Run interactive setup wizard |
| `egg --reset` | Reset configuration and start over |
| `egg --exec <cmd>` | Execute command in ephemeral container |
| `egg --compose` | Start gateway via Docker Compose |
| `egg --compose --down` | Stop the Docker Compose stack (gateway + orchestrator) |
| `egg --compose --build` | Rebuild compose images before starting |

### egg-deploy CLI

For production/advanced deployments using Docker Compose:

| Command | Description |
|---------|-------------|
| `bin/egg-deploy init` | Initialize configuration files |
| `bin/egg-deploy up` | Start the gateway stack |
| `bin/egg-deploy down` | Stop the gateway stack |
| `bin/egg-deploy status` | Show container status and health |
| `bin/egg-deploy logs` | Follow gateway logs |
| `bin/egg-deploy build` | Rebuild Docker images |

### egg-status CLI

For monitoring all active SDLC pipelines in real-time:

| Command | Description |
|---------|-------------|
| `bin/egg-status` | Stream real-time status for all active pipelines |
| `bin/egg-status --once` | Show current snapshot and exit |
| `bin/egg-status --all` | Include completed/failed pipelines |
| `bin/egg-status --verbose` | Show full DAG instead of compact status |
| `bin/egg-status --ascii` | Use ASCII-only characters (no Unicode) |
| `bin/egg-status --port <port>` | Specify orchestrator port (default: 9849) |

### egg-pipeline-watch CLI

For watching a specific pipeline's progress with DAG visualization:

| Command | Description |
|---------|-------------|
| `bin/egg-pipeline-watch <pipeline-id>` | Stream DAG visualization for a specific pipeline |
| `bin/egg-pipeline-watch <pipeline-id> --compact` | Show compact single-line status instead of full DAG |
| `bin/egg-pipeline-watch <pipeline-id> --once` | Show current state and exit (no streaming) |
| `bin/egg-pipeline-watch <pipeline-id> --ascii` | Use ASCII-only characters (no Unicode) |

### egg-orch CLI

For programmatic interaction with the orchestrator API (usable by both agents and humans):

| Command Group | Description |
|---------------|-------------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch pipeline list/get/create/status/delete` | Pipeline management |
| `egg-orch signal complete/progress/error/heartbeat` | Send agent signals |
| `egg-orch phase get/advance/start/complete` | Phase transitions |
| `egg-orch decision list/create/resolve/status` | HITL decision queue |
| `egg-orch container list/spawn/get/stop/logs` | Container operations |
| `egg-orch gateway health/phase/permissions` | Gateway operations |
| `egg-orch env` | Show orchestrator environment variables |

All commands support `--json` for machine-readable output. Run `egg-orch <command> --help` for detailed usage.

### Flags

| Flag | Description |
|------|-------------|
| `--private` | Enable private mode (Anthropic API + private GitHub repos only) |
| `--public` | Enable public mode (full internet access, default) |
| `--compose` | Use Docker Compose to manage the gateway stack |
| `--down` | Stop the Docker Compose stack (use with `--compose`) |
| `--build` | Rebuild compose images before starting (use with `--compose`) |
| `--exec <cmd>` | Execute command in new ephemeral container |
| `--timeout <min>` | Timeout for --exec commands (default: 30) |
| `--auth <method>` | Anthropic auth method for --exec: `oauth-token` (default) or `api-key` |
| `--rebuild` | Force rebuild Docker image |
| `--time` | Show startup timing breakdown for debugging |
| `-v, --verbose` | Show detailed output instead of progress bar |

## Documentation

### SDLC Pipeline

- [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) — Operational guide, CLI commands, triggering
- [ADR: SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) — Architecture, threat model, security properties

### Architecture

- [Documentation Index](docs/index.md) — Navigation hub for all docs
- [Architecture Overview](docs/architecture/README.md) — System design and components
- [Gateway Sidecar](gateway/README.md) — Policy enforcement, API endpoints, credential injection
- [Sandbox Container](sandbox/README.md) — Agent environment, tools, wrappers
- [Project Structure](docs/development/STRUCTURE.md) — Directory layout and component map

### Architecture Decision Records

- [SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) — Structurally enforced agent checkpoints
- [Git Isolation](docs/adr/implemented/ADR-Git-Isolation-Architecture.md) — Worktree isolation design
- [Credential Injection](docs/adr/implemented/ADR-Gateway-Credential-Injection.md) — Zero-credential sandbox design
- [Declarative Setup](docs/adr/implemented/ADR-Declarative-Setup-Architecture.md) — Setup wizard architecture
- [Standardized Logging](docs/adr/implemented/ADR-Standardized-Logging-Interface.md) — Structured logging interface
- [All ADRs](docs/adr/README.md) — Complete index (7 implemented, 3 in-progress)

### Component Documentation

- [Shared Libraries](shared/README.md) — Config, logging, and git utilities
- [Configuration](config/README.md) — Repository and host configuration

### Guides

- [Local Quickstart](docs/guides/local-quickstart.md) — Get running locally with PAT authentication
- [Deployment Guide](docs/guides/deployment.md) — Deployment options
- [Deploy Migration](docs/guides/deploy-migration.md) — Migrating from legacy deployments
- [Agent Development](docs/guides/agent-development.md) — Developing agent strategies
- [Agent Mode Design](docs/guides/agent-mode-design.md) — When to use constraints vs. freedom

### Other

- [Human-in-the-Loop Decisions](docs/hitl-decisions.md) — Decision workflow and checkbox UI
- [Agentic Feedback Loop](docs/agentic-feedback-loop.md) — The foundational feedback loop
- [Why egg Works](docs/collaboration-effectiveness.md) — Safety, quality, and collaboration
- [Contributing](CONTRIBUTING.md) — Development setup and workflow

## Versioning

egg uses [semantic versioning](https://semver.org/) for Docker images.

### Docker Images

```bash
# Latest stable (updated on every release)
docker pull ghcr.io/jwbron/egg-sandbox:latest

# Major version (updated on v0.x.y releases)
docker pull ghcr.io/jwbron/egg-sandbox:v0

# Exact version
docker pull ghcr.io/jwbron/egg-sandbox:v0.1.0
```

### Breaking Changes

- **v0.x.y**: Pre-stable releases. Minor versions may contain breaking changes.
- **v1.x.y and later**: Stable releases. Breaking changes only in major version bumps.

See [RELEASING.md](RELEASING.md) for the release process.

## Development

```bash
make setup           # Set up development environment
make lint            # Run all linters
make test            # Run all tests
make test-integration # Run integration tests
make test-e2e        # Run end-to-end tests
make lint-fix        # Auto-fix lint issues
make build           # Build Docker images
```

Requires Python >= 3.11. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT License — see [LICENSE](LICENSE) for details.
