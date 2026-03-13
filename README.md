# egg

A structurally enforced SDLC pipeline for autonomous LLM agents, turning tasks into reviewed pull requests with mandatory human gates.

> *Inspired by Andy Weir's short story "The Egg": a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

**Note**: this project is currently under heavy development. The core workflow is functional, but continually being refined and refactored. Expect breakages and changing behavior for the foreseeable future.

## What It Does

egg takes a GitHub issue (or a plain-text prompt) and produces a reviewed pull request autonomously. Multiple specialized agents analyze the task, plan an approach, implement code, write tests, update docs, and review each other's work. Humans stay in the loop at critical checkpoints but don't need to drive the process.

The key idea: **constraints are enforced by infrastructure, not by prompts.** The agent can't skip steps, self-approve, or steal credentials because the gateway physically blocks those operations. There's no system prompt saying "please don't merge" — the merge endpoint doesn't exist in the agent's environment.

## A Pipeline In Action

Point egg at a GitHub issue and it runs the full lifecycle. Here's what a completed pipeline looks like via `egg-pipeline-watch`:

```
    ╔═════════════════════════════════════════════╗
    │ ✓ Refine                                    │
    │   complete                                  │
    │   ✓ refiner                                 │
    │   ✓ reviewer_refine  ✓ reviewer_agent_design│
    │   [11m25s]                                  │
    ╚═════════════════════════════════════════════╝
        │
        ▼
    ╔═════════════════════════════════════════╗
    │ ✓ Plan                                  │
    │   complete                              │
    │   ✓ architect                           │
    │   ✓ task_planner  ✓ risk_analyst        │
    │   ✓ reviewer_plan                       │
    │   [23m55s]                              │
    ╚═════════════════════════════════════════╝
        │
        ▼
    ╔═══════════════════════════════════════════════╗
    │ ✓ Implement                                   │
    │   complete                                    │
    │   ✓ coder                                     │
    │   ✓ tester  ✓ documenter                      │
    │   ✓ integrator                                │
    │   ✓ checker                                   │
    │   ✓ reviewer_code  ✓ reviewer_contract        │
    │   [1h11m]                                     │
    ╚═══════════════════════════════════════════════╝
        │
        ▼
    ╔════════════╗
    │ ✓ PR       │
    │   complete │
    │   [2s]     │
    ╚════════════╝
```

Each box is a pipeline phase. Within each phase, specialized agents run in dependency-ordered waves — some sequentially, some in parallel. The orchestrator manages the entire DAG. Humans approve at the refine and plan gates; then agents implement, test, review, and the orchestrator auto-creates the PR.

## How It Works

```
    ┌──────────┐      ┌──────────┐      ┌──────────────┐      ┌───────────┐
    │  REFINE  │─────▶│   PLAN   │─────▶│  IMPLEMENT   │─────▶│    PR     │
    └────┬─────┘      └────┬─────┘      └──────────────┘      └─────┬─────┘
         │                 │                                        │
    Human gate        Human gate                              Human merge
```

1. **Refine** — Agents analyze the task, research the codebase, and produce a requirements document. Reviewers validate the analysis. Human approves before planning begins.
2. **Plan** — An architect recommends an approach, a task planner breaks it into discrete tasks with acceptance criteria, and a risk analyst flags concerns. Human approves before any code is written.
3. **Implement** — A coder writes code, a tester finds gaps and writes tests, a documenter updates docs, and an integrator runs the full test suite. Code and contract reviewers provide line-level feedback. Re-implementation cycles continue until all checks pass.
4. **PR** — The orchestrator auto-creates the PR using metadata from the plan, commit log, and diff stats. No agent is spawned. Only a human can merge via GitHub UI.

**Short-circuit mode**: Simple tasks (typos, config changes) skip the plan phase entirely — the refine phase signals `short_circuit: true` and jumps straight to implementation.

### Tiered Dispatch

The pipeline adapts its execution strategy to task complexity:

| Tier | Complexity | Strategy |
|------|-----------|----------|
| **Tier 1** | Low (typos, config) | Short-circuit: refine → implement (skip plan) |
| **Tier 2** | Medium (single features) | Full pipeline, single coder in waves |
| **Tier 3** | High (multi-phase features) | Parallel implement cycles per plan phase |

Tier 3 decomposes large features into independent plan phases that run as parallel implement cycles (coder → tester → documenter → checker → code reviewer), each scoped to its own file boundaries. After all phases complete, an integrator merges the results and runs the full test suite.

### Concurrent Execution Mode

An optional execution mode where all implement-phase agents (coder, tester, documenter, checker, reviewers) start simultaneously, each in their own worktree branch. Agents communicate via the orchestrator message bus and signal readiness via a consensus protocol. Phase completion requires all agents to reach `READY` state. Enable with `concurrent_execution: true` in pipeline config.

See [Concurrent Execution Guide](docs/guides/concurrent-execution.md) for the full protocol.

### Coordinator Agent

An optional dynamic orchestration mode where a `coordinator` agent — rather than fixed-phase dispatch — analyzes each task and determines the appropriate workflow: skipping unnecessary phases, spawning agents on demand, and adapting based on results. The coordinator interacts with a human via a Claude Code session connected to the MCP server (port 9850).

See [Coordinator Agent Guide](docs/guides/coordinator.md) for full documentation.

### Two Modes

- **Issue mode** (`egg-sdlc -r <repo> -i <issue>`): Pulls context from GitHub issues, HITL via terminal prompts
- **Local mode** (`egg-sdlc` or `egg-sdlc -r <repo> -p "prompt"`): Prompt-driven, HITL via terminal prompts

## Architecture

egg is a multi-container system: a **gateway** (trusted) that holds credentials and enforces policy, a **sandbox** (untrusted) where agent containers run, and an **orchestrator** that manages pipeline state, container lifecycle, and multi-agent coordination. The agent uses standard tools (`git`, `gh`, `curl`) — transparent wrappers intercept operations and route them through the gateway for validation.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                                    egg                                         │
│                                                                                │
│  ┌───────────────────┐   ┌──────────────────────┐   ┌───────────────────────┐ │
│  │   Orchestrator    │   │   Gateway Sidecar     │   │  Sandbox Containers   │ │
│  │   :9849           │   │   (Trusted) :9848     │   │  (Untrusted Agents)   │ │
│  │                   │   │                       │   │                       │ │
│  │  • Pipeline state │◀──│  • Phase enforcement  │──▶│  • Claude Code        │ │
│  │  • Container mgmt │   │  • Role validation    │   │  • git/gh wrappers    │ │
│  │  • HITL decisions │   │  • Credential inject  │   │  • egg-contract CLI   │ │
│  │  • Health checks  │   │  • Network policy     │   │  • No credentials     │ │
│  │  • MCP server     │   │  • Branch policies    │   │  • No direct network  │ │
│  │  • Message bus    │   │  • Squid proxy        │   │  • Isolated worktree  │ │
│  └───────────────────┘   └──────────────────────┘   └───────────────────────┘ │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

**Key principle**: The agent cannot bypass controls because the capabilities don't exist in its environment. The gateway physically blocks operations — this is infrastructure enforcement, not behavioral controls.

### Components

| Component | Directory | Description |
|-----------|-----------|-------------|
| **Gateway Sidecar** | `gateway/` | Policy enforcement, credential injection, Squid HTTP proxy, branch ownership, phase filtering, post-agent auto-commit |
| **Orchestrator** | `orchestrator/` | Pipeline state (git-backed), container lifecycle, multi-agent wave dispatch, HITL decision queue, SSE status streams, DAG visualization, MCP server, health checks |
| **Sandbox** | `sandbox/` | Agent execution environment, git/gh wrappers routing to gateway, Claude Code runner, CLI tools (`egg-contract`, `egg-orch`, `egg-checkpoint`) |
| **Shared Libraries** | `shared/` | `egg_config` (config framework), `egg_logging` (structured JSON logs), `egg_contracts` (SDLC models, plan parser, checkpoints, resilience), `egg_container` (container config builder), `egg_orchestrator` (orchestrator client), `egg_git` (git utilities) |
| **Configuration** | `config/` | Repository config, host config templates, secrets management |
| **CLI Entry Points** | `bin/` | `egg`, `egg-sdlc`, `egg-deploy`, `egg-status`, `egg-pipeline-watch`, `egg-orch`, `egg-contract`, `egg-checkpoint` |
| **GitHub Action** | `action/` | Composite action for CI/CD: PR review, autofixing, conflict resolution, doc updates |

### Security Model

- **Credential isolation**: Gateway injects GitHub tokens and Anthropic API keys at proxy time. The sandbox never sees raw credentials.
- **Branch ownership**: Agents can only push to `egg/` or `egg-` prefixed branches.
- **Phase enforcement**: Git/gh operations are filtered per SDLC phase (e.g., no `git push` during planning).
- **Branch lock**: Pipeline agents are locked to their assigned worktree branch.
- **File-level access**: Role-based file restrictions prevent agents from modifying protected files (e.g., contracts).
- **Commit-time validation**: Staged files checked against phase restrictions before commit.
- **No merge capability**: The gateway has no merge endpoint. Only humans can merge via GitHub UI.
- **Post-agent auto-commit**: Uncommitted work is automatically committed when agent containers exit, with phase-restricted files excluded.
- **Network modes**: Public mode allows filtered internet access via Squid proxy. Private mode restricts to Anthropic API only.

For details, see the [Architecture Overview](docs/architecture/README.md) and [Gateway README](gateway/README.md).

## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **Linux** (x86_64, arm64) | Supported | Primary development platform |
| **macOS** (Apple Silicon, Intel) | Supported | Requires Docker Desktop |

Both platforms use the same Docker-based architecture. The `egg` CLI detects the host platform and passes the appropriate UID/GID to the container. On macOS, UID/GID conflicts (e.g., GID 20 "staff" colliding with Ubuntu's "dialout") are resolved automatically at container startup.

## Quick Start

```bash
# Clone and install
git clone https://github.com/jwbron/egg.git
cd egg
pip install -e ./sandbox
```

Running `egg` starts the gateway and sandbox automatically. On first run it will prompt you to configure repositories and credentials via `egg --setup`. By default it launches in public mode (full internet access); use `egg --private` for network-locked private repo mode.

```bash
# Start an interactive session
egg                              # Public mode (default)
egg --private                    # Private mode (Anthropic API only)

# Launch a full SDLC pipeline
egg-sdlc -r myrepo -i 123       # From a GitHub issue
egg-sdlc -r myrepo -p "Add auth" # From a prompt
egg-sdlc                         # Interactive local mode
egg-sdlc -r myrepo -i 42 --concurrent  # Concurrent agent execution

# Or from inside an egg session
/sdlc -r myrepo -i 123

# Deploy with Docker Compose (production)
egg --compose                    # Start gateway + orchestrator stack
egg --compose --down             # Stop the stack
```

See the [Local Quickstart Guide](docs/guides/local-quickstart.md) for detailed setup including PAT-based authentication, and the [Deployment Guide](docs/guides/deployment.md) for Docker Compose and production options.

## GitHub Action

egg ships as a [GitHub Action](action/) for CI/CD integration — automated PR review, auto-fixing failing checks, merge conflict resolution, and review feedback addressing.

```yaml
- uses: jwbron/egg@main
  with:
    prompt: "Review this pull request"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

See [action/README.md](action/README.md) for full documentation and [GitHub Automation Guide](docs/guides/github-automation.md) for built-in workflow examples.

## Directory Structure

```
egg/
├── gateway/              # Gateway sidecar (Flask API, policy engine, Squid proxy)
├── orchestrator/         # Pipeline orchestrator (state, dispatch, containers, HITL, MCP)
├── sandbox/              # Sandbox container (entrypoint, Claude Code, git/gh wrappers)
│   ├── egg_lib/          # CLI and container utility libraries
│   ├── llm/              # LLM integration (Claude Code runner)
│   ├── bin/              # CLI tools and symlinks (added to PATH)
│   ├── scripts/          # git/gh wrapper scripts routing to gateway
│   └── .claude/          # Claude Code rules and slash commands
├── shared/               # Shared Python libraries
│   ├── egg_config/       # Configuration framework
│   ├── egg_logging/      # Structured JSON logging
│   ├── egg_contracts/    # SDLC contracts, plan parser, checkpoints, resilience
│   ├── egg_container/    # Container config builder
│   ├── egg_orchestrator/ # Orchestrator client and types
│   ├── egg_git/          # Git utilities
│   └── prompts/          # Shared review criteria for Actions and orchestrator
├── config/               # Configuration templates and repo config loader
├── bin/                  # CLI entry points (egg, egg-sdlc, egg-deploy, etc.)
├── action/               # GitHub Action (composite action, prompt builders)
├── docs/                 # Documentation
│   ├── architecture/     # System design and orchestrator architecture
│   ├── adr/              # Architecture decision records
│   ├── guides/           # Operational guides (deployment, SDLC, GitHub automation, etc.)
│   ├── reference/        # Agent roles, recovery, post-agent commit, redaction
│   ├── development/      # Project structure
│   └── templates/        # SDLC phase templates
├── tests/                # Unit tests (shared libraries)
├── integration_tests/    # Integration tests (require Docker)
├── scripts/              # Development scripts (linting checks)
├── metrics/              # Metrics collection
├── .egg/                 # SDLC schemas and phase permissions
├── .github/              # GitHub Actions workflows and release scripts
├── docker-compose.yml    # Production deployment (gateway + orchestrator)
├── pyproject.toml        # Python project configuration
├── Makefile              # Development commands (lint, test, build)
└── uv.lock               # Dependency lock file
```

## Documentation

| Topic | Start Here |
|-------|-----------|
| **Full docs index** | [docs/index.md](docs/index.md) |
| **Architecture & security model** | [Architecture Overview](docs/architecture/README.md) |
| **SDLC pipeline details** | [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) |
| **Concurrent execution mode** | [Concurrent Execution Guide](docs/guides/concurrent-execution.md) |
| **Tier 3 / phase-level dispatch** | [Tier 3 Dispatch Guide](docs/guides/tier3-dispatch.md) |
| **Coordinator agent** | [Coordinator Agent Guide](docs/guides/coordinator.md) |
| **Agent roles & permissions** | [Agent Roles Reference](docs/reference/agent-roles.md) |
| **Agent recovery & circuit breaker** | [Agent Recovery Reference](docs/reference/agent-recovery.md) |
| **Post-agent auto-commit** | [Post-Agent Commit Reference](docs/reference/post-agent-commit.md) |
| **Checkpoint redaction** | [Redaction Reference](docs/reference/redaction.md) |
| **Gateway enforcement** | [Gateway README](gateway/README.md) |
| **Sandbox environment** | [Sandbox README](sandbox/README.md) |
| **Multi-agent orchestration** | [Orchestrator Architecture](docs/architecture/orchestrator.md) |
| **Architecture decisions** | [ADR Index](docs/adr/README.md) |
| **CLI reference** | [CLI Entry Points](bin/README.md) |
| **GitHub automation** | [GitHub Automation Guide](docs/guides/github-automation.md) |
| **Agent design patterns** | [Agent Mode Design](docs/guides/agent-mode-design.md) |
| **Project structure** | [STRUCTURE.md](docs/development/STRUCTURE.md) |
| **Contributing** | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Development

```bash
make setup            # Set up dev environment (venv via uv, pre-commit hooks)
make lint             # Run all linters (ruff, mypy, shellcheck, yamllint, hadolint)
make test             # Run all unit tests
make security         # Run security scan (bandit)
make test-integration # Run integration tests (requires Docker)
make lint-fix         # Auto-fix lint issues
make build            # Build Docker images (gateway + sandbox)
```

Requires Python >= 3.13 and [uv](https://docs.astral.sh/uv/) for dependency management. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [RELEASING.md](RELEASING.md) for the release process.

## Configuration

egg stores configuration in XDG-compliant directories:

| Directory | Purpose |
|-----------|---------|
| `~/.config/egg/` | User configuration (`config.yaml`, `secrets.env`, `repositories.yaml`, GitHub App keys) |
| `~/.cache/egg/` | Docker build staging and cache |
| `~/.egg-sharing/` | Runtime data shared with containers (notifications, context) |
| `~/.egg-worktrees/` | Git worktrees for isolated agent development |

Key configuration files:

- **`config.yaml`** — Non-secret settings: ports, Docker Compose project name, git identity, auth method
- **`secrets.env`** — Secrets: GitHub tokens, Slack webhook, launcher secret
- **`repositories.yaml`** — Repository access config: writable/readonly repos, per-repo check commands, build commands, checkpoint repo

Run `egg --setup` for guided configuration. See [config/README.md](config/README.md) for full documentation.

## License

MIT License — see [LICENSE](LICENSE) for details.
