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
    ├─→ Restart agent: stop and respawn the stuck agent (preserves worktree, up to 2×)
    ├─→ HITL escalation: queue decision for human review
    ├─→ Restart phase (HITL): restart all phase agents after human approval
    ├─→ File diagnostic GitHub issue with full context
    └─→ Slack notification to the team
```

The overseer uses a lightweight model (Haiku) for continuous classification (is this agent stalled? looping? producing errors?) and escalates to a stronger model (Sonnet/Opus) only when corrective decisions are needed. It can restart individual agents autonomously (up to 2 times per phase, preserving committed work); phase-level restarts require human-in-the-loop approval.

The overseer is phase-scoped: it is spawned at the start of each pipeline phase and torn down when that phase completes, advances, or fails — giving each phase a fresh instance with no accumulated state. If the overseer crashes mid-phase, the orchestrator automatically respawns it (up to 3 times per phase).

### 4. The SDLC Pipeline: Humans at the Right Moments

egg structures work into phases with mandatory human gates:

```
┌──────────┐      ┌──────────┐      ┌──────────────┐      ┌──────────┐
│  REFINE  │─────▶│   PLAN   │─────▶│  IMPLEMENT   │─────▶│    PR    │
└────┬─────┘      └────┬─────┘      └──────────────┘      └────┬─────┘
     │                 │                                       │
Human gate        Human gate                              Human merge
```

1. **Refine**: Agents analyze the task, research the codebase, produce requirements. Reviewers validate. Human approves before planning.
2. **Plan**: Architect recommends approach, task planner breaks it into discrete tasks with acceptance criteria, risk analyst flags concerns. Human approves before any code is written.
3. **Implement**: The plan's tasks are split into a **DAG of independent slices** — each slice runs as its own agent team on its own integration branch with its own BRC consensus and stacked PR. Slices whose dependencies are satisfied run concurrently (up to `EGG_ORCH_MAX_PARALLEL_SLICES`, default 2 per pipeline; a separate `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` cap, default 4, bounds the total across all running pipelines); slices with unmet dependencies wait in subsequent waves. Within each slice, the coder writes code, the tester writes regression tests and adversarially probes the coder's implementation for bugs (NACKing with failing tests as bug reports), and the documenter updates docs. Code and contract reviewers provide line-level feedback; security and concurrency lens reviewers add targeted cross-file analysis and block consensus on a NACK. Cycles continue until all checks pass and BRC consensus is reached for that slice. See [Slice-DAG Implement Phase](docs/architecture/slice-dag.md) for the full model.
4. **PR**: Orchestrator auto-creates the PR from plan metadata. Only a human can merge via GitHub UI.

For **Jira epic-mode pipelines** (when `jira_ticket` resolves to a Jira Epic), an **Apply** phase is inserted between Plan and Implement: the `applier` role drives Jira mutations (epic Description writes, child ticket creates/edits, link creates, Won't-Do handoffs) on operator approval, before code implementation begins. Pass `mode='fresh'` (no existing children) or `mode='reassess'` (existing children to classify) to `submit_task`; `mode='auto'` (default) detects which to use at submit time.

Within each phase, specialized agents run concurrently via BRC (enabled by default for refine, plan, and implement). Here's what a completed pipeline looks like:

```
╔══════════════════════════════════════════════╗
│ ✓ Refine                                     │
│   complete                                   │
│   ✓ refiner                                  │
│   ✓ reviewer_refine  ✓ reviewer_agent_design │
│   [11m25s]                                   │
╚══════════════════════════════════════════════╝
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
│   ✓ reviewer_code  ✓ reviewer_code_holistic   │
│   ✓ reviewer_contract                         │
│   ✓ reviewer_security  ✓ reviewer_concurrency │
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
- `validate_config`: validate a pipeline config without creating a pipeline
- `start_pipeline`: recover a non-RUNNING pipeline (FAILED, AWAITING_HUMAN with resolved decisions, or PENDING)
- `advance_phase` / `start_phase` / `complete_phase`: phase management
- `populate_contract`: populate SDLC contract from plan draft

## Quick Start

```bash
# Clone and install
git clone https://github.com/jwbron/egg.git
cd egg

# Bring up the gateway + orchestrator on k3s
bin/egg-deploy init      # Generate launcher-secret
bin/egg-deploy up        # kubectl apply + wait for readiness
```

Interactive mode (`bin/egg`, `egg --setup`, `egg --public/--private`,
Docker Compose) was removed in [#1762](https://github.com/jwbron/egg/issues/1762).
Drive one-off agent work through the MCP server from any
MCP-compatible host (Claude Code, etc.):

```
submit_task(issue_number=123, repo="owner/name")
babysit_pr(pr_number=42, repo="owner/name")
run_agent_task(phase="refine", roles=["refiner"], repo="owner/name",
               description="Investigate foo")
```

See:

- [Local Quickstart](docs/guides/local-quickstart.md) for k8s-based
  setup.
- [Deployment Guide](docs/guides/deployment.md) for production options.
- [Custom-Phase Guide](docs/guides/custom-phase.md) for the
  `run_agent_task` primitive that replaces interactive mode.
- [Babysit-PR Guide](docs/guides/babysit-pr.md) for the PR-targeted
  BRC cycle.

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
| **Custom harness & multi-provider** | [Harness Configuration](docs/guides/harness-configuration.md) |
| **Sandbox environment** | [Sandbox README](sandbox/README.md) |
| **Contributing** | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Development

```bash
make setup             # Install dev dependencies
make lint              # Run all linters
make test              # Run tests reachable from your diff (changeset-aware narrow default)
make test-all          # Run the full unit-test suite (what CI runs)
make test-integration  # Run integration + security tests (k3s required)
make test-security     # Run security/pentesting tests only
make lint-fix          # Auto-fix lint issues
make security          # Run security scans
make build             # Build Docker images
```

Requires Python >= 3.14. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [docs/guides/testing.md](docs/guides/testing.md) for the changeset-aware test selection model.

## License

MIT License. See [LICENSE](LICENSE) for details.
