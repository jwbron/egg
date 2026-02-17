# egg

A structurally enforced SDLC pipeline for autonomous LLM agents — turning tasks into reviewed pull requests with mandatory human gates.

> *Inspired by Andy Weir's short story "The Egg" — a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

**Note**: this project is currently under heavy development. The core workflow is functional, but continually being refined and refactored. Expect breakages and changing behavior for the foreseeable future.

## What It Does

egg takes a GitHub issue (or a plain-text prompt) and produces a reviewed pull request — autonomously. Multiple specialized agents analyze the task, plan an approach, implement code, write tests, update docs, and review each other's work. Humans stay in the loop at critical checkpoints but don't need to drive the process.

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
    │   ✓ coder  │
    │   [2m27s]  │
    ╚════════════╝
```

Each box is a pipeline phase. Within each phase, specialized agents run in dependency-ordered waves — some sequentially, some in parallel. The orchestrator manages the entire DAG. Humans approve at the refine and plan gates; then agents implement, test, review, and open the PR.

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
3. **Implement** — A coder writes code, a tester validates it, a documenter updates docs, and an integrator runs the full test suite. Code and contract reviewers provide line-level feedback. Re-implementation cycles continue until all checks pass.
4. **PR** — Agents create the PR and push the branch. Only a human can merge via GitHub UI.

**Short-circuit mode**: Simple tasks (typos, config changes) skip the plan phase entirely — the refine phase signals `short_circuit: true` and jumps straight to implementation.

### Tiered Dispatch

The pipeline adapts its execution strategy to task complexity:

| Tier | Complexity | Strategy |
|------|-----------|----------|
| **Tier 1** | Low (typos, config) | Short-circuit: refine → implement (skip plan) |
| **Tier 2** | Medium (single features) | Full pipeline, single coder in waves |
| **Tier 3** | High (multi-phase features) | Parallel implement cycles per plan phase |

Tier 3 decomposes large features into independent plan phases that run as parallel implement cycles (coder → tester → agentic review), each scoped to its own file boundaries and sub-branch. An integrator merges the results and runs the full test suite.

### Two Modes

- **Issue mode** (`egg-sdlc -r <repo> -i <issue>`): GitHub-issue-driven with full remote integration, HITL via GitHub comments
- **Local mode** (`egg-sdlc` or `egg-sdlc -r <repo> -p "prompt"`): Prompt-driven, HITL via terminal prompts

## Architecture

egg is a two-container system: a **gateway** (trusted) that holds credentials and enforces policy, and a **sandbox** (untrusted) where the agent runs. The agent uses standard tools (`git`, `gh`, `curl`) — transparent wrappers intercept operations and route them through the gateway for validation.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                 egg                                     │
│                                                                         │
│   ┌─────────────────────────┐      ┌─────────────────────────────────┐  │
│   │    Gateway Sidecar      │      │    Sandbox Container            │  │
│   │    (Trusted)            │      │    (Untrusted Agent)            │  │
│   │                         │      │                                 │  │
│   │  • Phase enforcement    │◀────▶│  • git/gh wrappers             │  │
│   │  • Role validation      │      │  • Claude Code                 │  │
│   │  • Credential injection │      │  • egg-contract CLI            │  │
│   │  • Network policy       │      │  • Workspace files only        │  │
│   │  • Branch policies      │      │  • No credentials, no .git/    │  │
│   │                         │      │                                 │  │
│   │  HAS: tokens, keys,    │      │  HAS: code, tools              │  │
│   │       network access    │      │  NO:  secrets, direct network  │  │
│   └─────────────────────────┘      └─────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key principle**: The agent cannot bypass controls because the capabilities don't exist in its environment. The gateway physically blocks operations — this is infrastructure enforcement, not behavioral controls.

For details on what's enforced and how, see the [Architecture Overview](docs/architecture/README.md) and [Gateway README](gateway/README.md).

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

# Run egg — auto-setup prompts on first run
egg
```

Running `egg` starts the gateway and sandbox automatically. On first run it will prompt you to configure repositories and credentials via `egg --setup`. By default it launches in public mode (full internet access); use `egg --private` for network-locked private repo mode.

```bash
# Launch a full SDLC pipeline
egg-sdlc -r myrepo -i 123        # From a GitHub issue
egg-sdlc -r myrepo -p "Add auth" # From a prompt
egg-sdlc                          # Interactive local mode

# Or from inside an egg session
/sdlc -r myrepo -i 123
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

## Documentation

| Topic | Start Here |
|-------|-----------|
| **Full docs index** | [docs/index.md](docs/index.md) |
| **Architecture & security model** | [Architecture Overview](docs/architecture/README.md) |
| **SDLC pipeline details** | [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) |
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
make setup           # Set up development environment
make lint            # Run all linters
make test            # Run all tests
make test-integration # Run integration tests
make lint-fix        # Auto-fix lint issues
make build           # Build Docker images
```

Requires Python >= 3.11. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [RELEASING.md](RELEASING.md) for the release process.

## License

MIT License — see [LICENSE](LICENSE) for details.
