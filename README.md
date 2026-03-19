# egg

**Autonomous software engineering with structural guarantees.**

Turn GitHub issues into reviewed pull requests. Not by asking agents to follow rules, but by making rule-breaking physically impossible.

> *Inspired by Andy Weir's short story "The Egg": a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

**Note**: egg is under heavy development. The core workflow is functional, but expect breakages and changing behavior.

## The Problem

LLM agents are capable enough to write real code. They are not reliable enough to be trusted with real credentials, real branches, and real merge buttons. The standard approach (system prompts that say "please don't merge" or "please run tests first") fails because:

- Prompts are suggestions, not constraints. Agents ignore them under pressure.
- Agents self-approve their own work. They hallucinate that tests pass.
- Concurrent agents produce sycophantic reviews ("looks good!") with no actual evaluation.
- A single agent writing and reviewing its own code is a conflict of interest, not a workflow.

egg solves this by moving enforcement out of the prompt and into infrastructure.

## What Makes egg Different

### 1. The Gateway: Infrastructure Over Prompts

The gateway is a trusted sidecar that sits between every agent and the outside world. Agents use standard tools (`git`, `gh`, `curl`) but transparent wrappers route every operation through the gateway for policy enforcement.

**What the gateway enforces:**

- **No credentials in the sandbox.** The agent environment has zero tokens, zero keys. The gateway holds all credentials and injects them into proxied requests. Agents never see or handle secrets.
- **No merging.** The merge endpoint doesn't exist. There's no prompt saying "don't merge"; the capability is absent from the agent's world.
- **Phase-locked operations.** An agent in the "plan" phase physically cannot push code. An agent in the "implement" phase cannot modify the contract. The gateway validates every git operation against the current SDLC phase.
- **Branch ownership.** Agents can only push to `egg/`-prefixed branches. They can only edit PRs they created. Role-based file restrictions prevent agents from modifying protected state.
- **Network isolation.** In private mode, the sandbox can reach the Anthropic API and nothing else. In public mode, all external access is proxied through the gateway.

This is zero-trust architecture applied to AI agents. The agent doesn't need to be trustworthy because the environment is structurally safe.

### 2. Agent Teams: Deliberative Consensus, Not Vote Counting

When multiple agents work concurrently, they need to agree that their combined output is coherent. The naive approach (each agent tells a central orchestrator "I'm ready") fails because agents are unreliable self-assessors.

egg replaces orchestrator-decreed consensus with **Deliberative Consensus**: agents review each other's actual work, cite specific evidence, and individually confirm agreement through the **Broadcast-Review-Converge (BRC)** protocol.

**How BRC works:**

```
Phase 1: Broadcast     Each producer (coder, tester, documenter) completes work
                       and proposes it with structured attestations: commit SHAs,
                       files changed, tests run, risks considered.

Phase 2: Review        Reviewers evaluate proposals from assigned producers.
                       ACKs must cite specific file paths, line numbers, commit SHAs.
                       NACKs must include specific, actionable objections.
                       Generic "looks good" is rejected by schema validation.

Phase 3: Converge      When all reviewers have ACKed all assigned producers,
                       each agent independently confirms. The orchestrator
                       observes consensus; it doesn't decide it.
```

**Anti-sycophancy by design:**

- **Delphi-style ordering.** Reviewers form independent judgments from git artifacts before seeing the producer's self-assessment. The server holds back producer metadata until the reviewer submits their own evaluation.
- **Costly signals.** Proposals and reviews require structured attestations tied to real artifacts (commit SHAs, file paths, test counts). These are mechanically hard to produce without doing the work (game theory: costly signaling over cheap talk).
- **Commitment devices.** Proposals have cooldown periods. Retracting a proposal requires citing specific new information. After 3 flip-flops, the agent is locked out and escalated to a human.

The review topology is asymmetric and sparse: reviewers evaluate producers, not each other. This keeps overhead at ~5 review edges instead of ~20 for full pairwise review across 5 agents.

See [Agent Teams and Deliberative Consensus](docs/guides/agent-teams.md) for the full protocol design, research foundations, and failure mode analysis.

### 3. The Overseer: AI Monitoring AI

The overseer is a lightweight agent that watches all other agents in real-time. It runs on every pipeline automatically, has no code access (it can file GitHub issues but cannot read or modify repository contents), and follows a corrective action ladder:

```
Detect anomaly (stall, loop, error, off-track behavior)
    │
    ├─→ Auto-nudge: send corrective message to the stuck agent
    ├─→ Redirect: send targeted instructions to change approach
    ├─→ HITL escalation: queue decision for human review
    ├─→ File diagnostic GitHub issue with full context
    └─→ Slack notification to the team
```

The overseer uses a lightweight model (Haiku) for continuous classification (is this agent stalled? looping? producing errors?) and escalates to a stronger model (Sonnet/Opus) only when corrective decisions are needed. It cannot restart agents on its own; restart requests go through the human-in-the-loop decision queue.

If the overseer itself crashes, the orchestrator automatically respawns it (up to 3 times).

### 4. The SDLC Pipeline: Humans at the Right Moments

egg structures work into phases with mandatory human gates:

```
┌──────────┐      ┌──────────┐      ┌──────────────┐      ┌──────────┐
│  REFINE  │─────▶│   PLAN   │─────▶│  IMPLEMENT   │─────▶│    PR    │
└────┬─────┘      └────┬─────┘      └──────────────┘      └────┬─────┘
     │                 │                                        │
Human gate        Human gate                              Human merge
```

1. **Refine**: Agents analyze the task, research the codebase, produce requirements. Reviewers validate. Human approves before planning.
2. **Plan**: Architect recommends approach, task planner breaks it into discrete tasks with acceptance criteria, risk analyst flags concerns. Human approves before any code is written.
3. **Implement**: Coder writes code, tester writes tests and runs linters/type-checkers, documenter updates docs. Code and contract reviewers provide line-level feedback. Cycles continue until all checks pass and BRC consensus is reached.
4. **PR**: Orchestrator auto-creates the PR from plan metadata, commit log, and diff stats. Only a human can merge via GitHub UI.

Within each phase, specialized agents run concurrently via BRC (enabled by default for refine, plan, and implement). Here's what a completed pipeline looks like:

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

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                       egg                                           │
│                                                                                     │
│  ┌──────────────────────┐  ┌───────────────────────────┐  ┌───────────────────────┐ │
│  │    Orchestrator      │  │    Gateway Sidecar        │  │  Sandbox Containers   │ │
│  │                      │  │    (Trusted)              │  │  (Untrusted)          │ │
│  │  • Pipeline state    │  │                           │  │                       │ │
│  │  • Container mgmt    │  │  • Zero-trust credential  │  │  • Claude Code agent  │ │
│  │  • BRC consensus     │◀─│    injection              │──│  • Standard git/gh    │ │
│  │  • Overseer          │  │  • Phase-locked ops       │  │  • egg-orch/contract  │ │
│  │  • Health monitoring │  │  • Branch ownership       │  │  • No credentials     │ │
│  │  • HITL decisions    │  │  • Role-based file gates  │  │  • No merge endpoint  │ │
│  │  • MCP server        │  │  • Network isolation      │  │  • Proxied network    │ │
│  │  • Message bus       │  │  • Post-agent auto-commit │  │                       │ │
│  └──────────────────────┘  └───────────────────────────┘  └───────────────────────┘ │
│                                                                                     │
│  ┌──────────────────────┐                                                           │
│  │    Overseer Agent    │  Lightweight model classifies anomalies continuously.     │
│  │    (Monitoring-only) │  No code access. Corrective action ladder.                │
│  │                      │  Auto-respawned on crash (up to 3x).                      │
│  └──────────────────────┘                                                           │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**Key principle**: The agent cannot bypass controls because the capabilities don't exist in its environment. This is infrastructure enforcement, not behavioral controls.

## Integration Points

### MCP Server

The orchestrator exposes an MCP server (port 9850) for controlling pipelines from Claude Code or any MCP-compatible client:

- `submit_task` / `cancel_task`: pipeline lifecycle
- `get_status` / `get_phase` / `get_pipeline_snapshot`: monitoring
- `provide_input`: resolve HITL decisions programmatically
- `list_containers` / `get_container_logs`: debugging
- `send_message` / `get_consensus_status`: agent coordination

## Quick Start

```bash
# Clone and install
git clone https://github.com/jwbron/egg.git
cd egg
pip install -e ./sandbox

# Run egg (auto-setup prompts on first run)
egg
```

`egg` starts the gateway and sandbox automatically. First run prompts for repository and credential configuration via `egg --setup`. Default is public mode (full internet); use `egg --private` for network-locked mode.

```bash
# From inside an egg session, launch a full SDLC pipeline
/sdlc 123                        # From a GitHub issue number
/sdlc Add auth middleware         # From a description
/sdlc --short Fix flaky test      # Lightweight coder+reviewer mode
/sdlc                             # Interactive — browse issues or describe a task
```

Or use the MCP server directly from any MCP-compatible client (see [MCP Server](#mcp-server)).

See [Local Quickstart](docs/guides/local-quickstart.md) for detailed setup and [Deployment Guide](docs/guides/deployment.md) for Docker Compose and production options.

## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **Linux** (x86_64, arm64) | Supported | Primary development platform |
| **macOS** (Apple Silicon, Intel) | Supported | Requires Docker Desktop |

## Documentation

| Topic | Link |
|-------|------|
| **Full docs index** | [docs/index.md](docs/index.md) |
| **Architecture & security model** | [Architecture Overview](docs/architecture/README.md) |
| **Gateway enforcement** | [Gateway README](gateway/README.md) |
| **Agent teams & deliberative consensus** | [Agent Teams Guide](docs/guides/agent-teams.md) |
| **Concurrent execution** | [Concurrent Execution Guide](docs/guides/concurrent-execution.md) |
| **SDLC pipeline** | [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) |
| **Orchestrator & overseer** | [Orchestrator Architecture](docs/architecture/orchestrator.md) |
| **Agent roles & permissions** | [Agent Roles Reference](docs/reference/agent-roles.md) |
| **GitHub automation** | [GitHub Automation Guide](docs/guides/github-automation.md) |
| **Health monitoring** | [Health Monitoring Guide](docs/guides/pipeline-health-monitoring.md) |
| **Sandbox environment** | [Sandbox README](sandbox/README.md) |
| **Contributing** | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Development

```bash
make setup             # Install dev dependencies
make lint              # Run all linters
make test              # Run all tests
make test-integration  # Run integration tests (Docker required)
make lint-fix          # Auto-fix lint issues
make security          # Run security scans
make build             # Build Docker images
```

Requires Python >= 3.11. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.
