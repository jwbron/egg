# egg

**Autonomous software engineering with structural guarantees.**

egg takes an idea, refines it into a plan, and turns that plan into reviewed pull requests. The agents do the labor: they research the codebase, draft requirements, write code and tests, and review each other's work. They do not make the decisions that matter. Whether the requirements are right, whether the plan is sound, how to resolve an ambiguity, whether anything merges: those judgments stay with a human, and the pipeline stops and asks rather than guessing.

Underneath, rule-breaking isn't merely discouraged, it's impossible. Untrusted LLM agents work inside a zero-credential sandbox. A trusted gateway sidecar holds every secret and validates every privileged operation. A deliberative consensus protocol forces agents to review each other's *actual* artifacts before anything ships. The result is autonomy on the work and human authority on the decisions.

> *Inspired by Andy Weir's short story "The Egg": a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

> **Status:** egg is under heavy development. The core workflow is functional and runs end-to-end, but expect breakages and changing behavior.

## The Problem

LLM agents are capable enough to write real code. They are not reliable enough to be trusted with real credentials, real branches, and real merge buttons. The standard approach, system prompts that say "please don't merge" or "please run tests first," fails because:

- **Prompts are suggestions, not constraints.** Agents ignore them under pressure.
- **Agents self-approve their own work.** They hallucinate that tests pass.
- **Concurrent agents produce sycophantic reviews** ("looks good!") with no actual evaluation.
- **A single agent writing and reviewing its own code is a conflict of interest,** not a workflow.

egg moves enforcement out of the prompt and into infrastructure, and keeps a human on every decision the agents should not make alone.

## What Makes egg Different

### 1. The Gateway: Infrastructure Over Prompts

The gateway is a trusted sidecar that sits between every agent and the outside world. Agents use ordinary tools (`git`, `gh`, `curl`); transparent wrappers route every operation through the gateway for policy enforcement.

**What the gateway enforces:**

- **No credentials in the sandbox.** The agent environment has zero tokens and zero keys. The gateway holds every credential (GitHub, Anthropic, Jira, Confluence, LiteLLM) and injects them into proxied requests. Agents never see or handle secrets.
- **No merging.** The merge endpoint doesn't exist. There's no prompt saying "don't merge"; the capability is simply absent from the agent's world.
- **Phase-locked operations.** Every git/gh operation is validated against the pipeline's current SDLC phase. An agent in the plan phase physically cannot push code; an agent implementing one slice cannot rewrite the contract.
- **Branch ownership.** Agents may only push to `egg/`-prefixed branches (or branches with an open egg-authored PR). Role-based file restrictions reject pushes that touch protected paths with `403 restricted_path_modified`.
- **Network isolation.** Each pipeline's mode defaults to its repo's GitHub visibility — private or internal repos get private mode (the sandbox reaches the Anthropic API and nothing else, enforced by a Squid proxy and Cilium NetworkPolicies); public repos get public mode (all external access is proxied and audited through the gateway). An explicit `network_mode` on the pipeline config overrides the auto-detect.
- **Bounded Jira/Confluence access.** Optional, private-mode-only gateway wrappers expose read verbs (ticket/page reads, JQL/CQL search) plus a bounded write extension (create/edit ticket, add comment, create link), gated by a project/space allowlist with per-verb schemas and response redaction.

This is zero-trust architecture applied to AI agents. The agent doesn't need to be trustworthy because the environment is structurally safe. See [Capability Removal](docs/design/capability-removal.md) for the design philosophy and the [Gateway README](gateway/README.md) for the policy surface.

### 2. Agent Teams: Deliberative Consensus, Not Vote Counting

When multiple agents work concurrently, they must agree that their combined output is coherent. The naive approach, each agent telling a central orchestrator "I'm ready," fails because agents are unreliable self-assessors.

egg replaces orchestrator-decreed consensus with **Deliberative Consensus**: agents review each other's actual work, cite specific evidence, and individually confirm agreement through the **Broadcast-Review-Converge (BRC)** protocol.

```
Phase 1: Broadcast     Each producer (coder, tester, documenter) completes work
                       and proposes it with structured attestations: commit SHAs,
                       files changed, tests run, risks considered.

Phase 2: Review        Reviewers evaluate proposals from assigned producers.
                       ACKs must cite specific file paths, line numbers, commit SHAs.
                       NACKs must include specific, actionable objections.
                       Generic "looks good" is rejected by schema validation.

Phase 3: Converge      When all reviewers have ACKed all assigned producers, each
                       agent independently confirms. The orchestrator observes
                       consensus; it does not decide it.
```

**Anti-sycophancy by design:**

- **Delphi-style ordering.** Reviewers form independent judgments from git artifacts *before* seeing the producer's self-assessment. The server withholds producer metadata until the reviewer submits their own evaluation.
- **Costly signals.** Proposals and reviews require structured attestations tied to real artifacts (commit SHAs, file paths, test counts), which are mechanically hard to fake without doing the work.
- **Commitment devices.** Proposals have cooldown periods; retracting one requires citing specific new information; after repeated flip-flops the agent is locked out and escalated to a human.
- **Adversarial tester.** The tester is a dual role: it reviews-and-hardens the coder's tests (the coder authors its own tests), adds missing regression and adversarial cases, and probes the coder's implementation for bugs, NACKing with a failing test as the bug report.

The review topology is asymmetric and sparse: reviewers evaluate producers, not each other, which keeps overhead at a handful of review edges instead of full pairwise review across the team. See [Agent Teams and Deliberative Consensus](docs/guides/agent-teams.md) for the full protocol, research foundations, and failure-mode analysis.

### 3. The Overseer: AI Monitoring AI

The overseer watches all other agents in real time. It runs on every pipeline automatically, has no code access (it can file GitHub issues but cannot read or modify repository contents), and follows a corrective-action ladder:

```
Detect anomaly (stall, loop, error, off-track behavior)
    │
    ├─→ Auto-nudge: send a corrective message to the stuck agent
    ├─→ Redirect: send targeted instructions to change approach
    ├─→ Restart agent: stop and respawn the stuck agent (preserves worktree, up to 2x)
    ├─→ HITL escalation: queue a decision for human review
    ├─→ Restart phase (HITL): restart all phase agents after human approval
    ├─→ File a diagnostic GitHub issue with full context
    └─→ Slack notification to the team
```

Health monitoring is **two-tier**. Tier 1 is deterministic and LLM-free: orchestrator-side tripwires for heartbeat timeouts and stalls (longer thresholds during the implement phase to accommodate deep work). Tier 2 is the overseer agent itself: a Haiku classifier runs every poll cycle to flag anomalies, a Sonnet-class decision-maker reasons about ambiguous situations and chooses a corrective action, and an Opus-class advisor is consulted only when a Haiku-flagged anomaly *and* a Tier-1 alert are active simultaneously. Infrastructure errors (git failures, gateway rejections, permission denials) fast-path straight to a human-in-the-loop decision.

The overseer is phase-scoped: spawned at the start of each phase and torn down when that phase completes, advances, or fails, giving each phase a fresh instance with no accumulated state. If it crashes mid-phase, the orchestrator respawns it (up to 3x per phase). See [Pipeline Health Monitoring](docs/guides/pipeline-health-monitoring.md).

### 4. The SDLC Pipeline: Humans at the Right Moments

egg structures work into phases with mandatory human gates:

```
┌──────────┐      ┌──────────┐      ┌───────────┐      ┌──────────────────────┐
│  REFINE  │─────▶│   PLAN   │─────▶│  APPLY*   │─────▶│      IMPLEMENT       │
└────┬─────┘      └────┬─────┘      └─────┬─────┘      └──────────────────────┘
     │                 │                  │             splits into a DAG of
Human gate        Human gate        Human gate*        stacked-PR slices; humans
                                  (*Jira epic-mode      merge each PR via GitHub
                                    only)
```

1. **Refine.** Agents analyze the task, research the codebase, and produce requirements; reviewers validate. A human approves before planning begins.
2. **Plan.** An architect recommends an approach, a task planner breaks it into discrete tasks with acceptance criteria and a **DAG of slices**, and a risk analyst flags concerns. A human approves before any code is written.
3. **Apply** *(Jira epic-mode only)*. When the task resolves to a Jira Epic, an `applier` role drives Jira mutations (epic description writes, child-ticket creates/edits, link creates, Won't-Do handoffs) on operator approval, before implementation begins.
4. **Implement.** The plan's slices are scheduled as a **DAG**: each slice runs as its own agent team on its own integration branch, with its own BRC consensus and its own stacked PR. Slices whose dependencies are satisfied run concurrently (per-pipeline cap `PipelineConfig.max_parallel_slices` at pipeline creation, falling back to `EGG_ORCH_MAX_PARALLEL_SLICES`, default 1 — raise on hosts with capacity; process-wide cap `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES`, default 4); dependent slices wait for later waves. Within a slice the coder writes code and its own tests, the tester reviews-and-hardens the coder's tests (adding missing coverage and adversarially probing for bugs), and the documenter updates docs, while code, contract, security, and concurrency reviewers provide line-level feedback and can block consensus on a NACK.

There is **no separate "PR" phase**. The pipeline's context PR (`egg/<id>/work` into `main`) is opened up-front at the plan-to-implement boundary; slice PRs stack onto it and are created automatically by the orchestrator as each slice reaches consensus. Only a human can merge, via the GitHub UI. See the [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) and [Slice-DAG Implement Phase](docs/architecture/slice-dag.md).

A completed pipeline looks like this:

```
╔══════════════════════════════════════════════╗
│ ✓ Refine                            complete │
│   ✓ refiner                                  │
│   ✓ reviewer_refine  ✓ reviewer_agent_design │
╚══════════════════════════════════════════════╝
    │
    ▼
╔═════════════════════════════════════════╗
│ ✓ Plan                         complete │
│   ✓ architect                           │
│   ✓ task_planner  ✓ risk_analyst        │
│   ✓ reviewer_plan                       │
╚═════════════════════════════════════════╝
    │
    ▼
╔═══════════════════════════════════════════════╗
│ ✓ Implement                          complete │
│   slice-1 ──▶ slice-2 ──▶ slice-3  (DAG)      │
│   ✓ coder  ✓ tester  ✓ documenter             │
│   ✓ reviewer_code  ✓ reviewer_code_holistic   │
│   ✓ reviewer_contract                         │
│   ✓ reviewer_security  ✓ reviewer_concurrency │
╚═══════════════════════════════════════════════╝
    │
    ▼
  stacked PRs on egg/<id>/work into main  (humans merge)
```

### 5. Human-in-the-Loop: The Agents Don't Decide

This is the principle the other four pillars exist to serve: agents do the work, but humans make the consequential calls. The gateway, the consensus protocol, and the overseer all funnel uncertainty and risk upward to a person instead of letting an agent resolve it alone.

Every phase boundary is a gate where a human reviews the agents' artifacts and approves before the pipeline advances. Beyond those gates, whenever an agent or the overseer hits something it should not resolve on its own (ambiguous requirements, a design fork, an infrastructure error, repeated failed cycles), egg pauses the pipeline and queues a formal HITL decision instead of guessing. The operator answers through `provide_input` on the MCP or CLI surface, and the pipeline resumes exactly where it stopped. egg never auto-resolves these decisions, even in otherwise automated runs.

The division of labor is explicit:

| The agents do | A human decides |
|---------------|-----------------|
| Research the codebase, draft and review requirements | Whether the requirements are right (refine gate) |
| Propose an approach and slice the work into a DAG | Whether the plan and approach are sound (plan gate) |
| Draft Jira mutations for an epic | Whether to apply them (apply gate, epic-mode) |
| Write code, tests, and docs; reach BRC consensus | When peer review can't converge, how to break the tie |
| Surface ambiguities and open questions | How to resolve each one (feedback) |
| Flag stalls, loops, and errors (overseer) | How to recover when self-correction fails (escalations) |
| Open and stack PRs from plan metadata | Whether anything merges (only humans merge) |

See [HITL Decisions](docs/hitl-decisions.md) for the decision, feedback, and approval workflow.

## Architecture

egg deploys as a set of containers on **Kubernetes (k3s)**, split across two namespaces: trusted services in `egg-system` and untrusted agent Jobs in `egg-agents`, with Cilium NetworkPolicies enforcing isolation between them.

```
┌─────────────────────────── egg-system (trusted) ───────────────────────────┐
│                                                                            │
│  ┌──────────────────────┐   ┌───────────────────────────┐   ┌───────────┐  │
│  │    Orchestrator      │   │     Gateway Sidecar       │   │  LiteLLM  │  │
│  │                      │   │                           │   │  (proxy)  │  │
│  │  • Pipeline state    │   │  • Zero-trust credential  │   │           │  │
│  │  • Slice scheduler   │◀─▶│    injection              │   │  optional │  │
│  │  • BRC consensus     │   │  • Phase-locked git/gh    │   │  non-     │  │
│  │  • Overseer (2-tier) │   │  • Branch ownership       │   │  Claude   │  │
│  │  • HITL decisions    │   │  • Restricted-path gates  │   │  routing  │  │
│  │  • MCP server :9850  │   │  • Network isolation      │   │   :4000   │  │
│  │  • REST API   :9849  │   │  • Jira/Confluence wrap   │   │           │  │
│  └──────────────────────┘   │   :9848 (+ proxy :3129)   │   └───────────┘  │
│                             └──────────────▲────────────┘                  │
└────────────────────────────────────────────┼───────────────────────────────┘
                                             │ all privileged ops + LLM calls
┌─────────────────────────── egg-agents ─────┼──────────── (untrusted) ──────┐
│   ┌────────────────────────────────────────┴─────────────────────────────┐ │
│   │  Sandbox agent Jobs (one per role per phase/slice)                   │ │
│   │  • Claude Code via the Agent SDK (egg_agent)                         │ │
│   │  • Standard git/gh wrappers → gateway   • No credentials             │ │
│   │  • Per-agent git worktree               • No merge endpoint          │ │
│   └──────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** the agent cannot bypass controls because the capabilities don't exist in its environment. This is infrastructure enforcement, not behavioral control. See the [Architecture Overview](docs/architecture/README.md) and [Kubernetes Migration](docs/architecture/kubernetes-migration.md).

| Component | Namespace | Role | Trust |
|-----------|-----------|------|-------|
| **Orchestrator** | `egg-system` | Pipeline state, slice scheduling, BRC consensus, overseer, HITL, MCP server (`:9850`) + REST API (`:9849`) | Trusted |
| **Gateway** | `egg-system` | Credential injection, phase/branch/path enforcement, network isolation, Jira/Confluence wrappers (`:9848`, proxy `:3129`) | Trusted |
| **LiteLLM** | `egg-system` | Optional proxy for routing individual agent roles to non-Claude models (`:4000`); inert by default (no agent role routes to it unless configured) | Trusted |
| **Sandbox** | `egg-agents` | Untrusted agent Jobs: Claude Code via the Agent SDK, per-agent worktree, zero credentials | Untrusted |

## Driving Pipelines: the MCP Server

The orchestrator exposes an MCP server (port `9850`) so you can drive pipelines from Claude Code or any MCP-compatible client. The legacy interactive CLI (`bin/egg`) and Docker Compose deployment were removed in [#1762](https://github.com/jwbron/egg/issues/1762).

```
submit_task(issue_number=123, repo="owner/name")
```

Representative tools (see the [Orchestrator CLI](docs/reference/orchestrator-cli.md) and [MCP Deployment Tools](docs/reference/mcp-deployment-tools.md) for the full set):

- **Lifecycle:** `submit_task`, `cancel_task`, `list_tasks`, `start_pipeline`
- **Monitoring:** `get_status`, `get_phase`, `get_pipeline_snapshot`, `get_consensus_status`, `check_health`
- **HITL & coordination:** `provide_input`, `answer_feedback`, `send_message`
- **Phase & agent control:** `advance_phase`, `start_phase`, `complete_phase`, `restart_agent`, `restart_phase`, `populate_contract`, `update_pipeline_config`
- **Debugging:** `list_containers`, `get_container_logs`, `get_service_logs`, `validate_config`
- **Deployment:** `get_deployment_context`, `validate_deployment_manifests`, `validate_network_isolation`, `prune_stale_worktrees`, `rebuild_and_rollout`

Host-side CLIs in `bin/` (`egg-sdlc`, `egg-status`, `egg-pipeline-watch`, `egg-onboarding-docs`) wrap the same APIs for monitoring and visualization.

## Quick Start

egg deploys to a local **k3s** cluster. (Docker Compose was removed in [#1762](https://github.com/jwbron/egg/issues/1762); `bin/egg-deploy up/down/build/logs` are deprecated stubs.)

```bash
# 1. Clone
git clone https://github.com/jwbron/egg.git
cd egg

# 2. Install k3s with the Cilium CNI (required for NetworkPolicy enforcement)
make k3s-setup

# 3. Generate host-side config (~/.config/egg/launcher-secret, config.yaml, …)
bin/egg-deploy init
#    then edit ~/.config/egg/secrets.env  (ANTHROPIC_API_KEY or OAuth token,
#    GitHub PAT, gateway policy) and ~/.config/egg/repositories.yaml

# 4. Build images, import them into k3s, create secrets, and deploy
make build
make k3s-import
make k3s-secrets
make deploy

# 5. Verify
kubectl get pods -n egg-system
```

Then drive agent work through the MCP server from any MCP-compatible host (e.g. Claude Code):

```
submit_task(issue_number=123, repo="owner/name")
```

See:

- [Local Quickstart](docs/guides/local-quickstart.md) for end-to-end k3s setup with a worked example.
- [Deployment Guide](docs/guides/deployment.md) for production options and the full k3s flow.
- [Per-Agent Models](docs/guides/per-agent-models.md) for routing a single agent role to a non-Claude model (e.g. Qwen) via the LiteLLM proxy.

## Repo Layout

| Directory | What it is |
|-----------|------------|
| `orchestrator/` | Central SDLC pipeline engine: scheduling, slice DAG, BRC consensus, overseer, health monitoring, MCP server |
| `gateway/` | Trusted policy-enforcement sidecar: validates git/gh/Jira/Confluence operations, injects credentials, enforces network isolation |
| `sandbox/` | Untrusted agent container: Claude Code config, the `egg_agent` runtime, git/gh wrappers, host-side CLIs |
| `shared/` | Shared Python packages (`egg_agent`, `egg_config`, `egg_contracts`, `egg_git`, `egg_logging`, `egg_anchor`, …) plus agent prompt templates |
| `config/` | Repository and host configuration templates; `config/litellm/` builds the LiteLLM image |
| `k8s/` | Kustomize manifests: `base/` plus `overlays/local/` |
| `action/` | Composite GitHub Action and review-bot prompt builders |
| `docs/` | All documentation: guides, architecture, references |
| `integration_tests/` | Cross-component integration and security tests (k3s required) |
| `scripts/` | Build, release, and CI helpers (incl. changeset-aware test selection) |
| `bin/` | Host-side CLI entry points |

## Documentation

Start with **[docs/index.md](docs/index.md)**, which has task-specific lookup tables, architecture docs, and component READMEs.

| Topic | Link |
|-------|------|
| **Full docs index** | [docs/index.md](docs/index.md) |
| **Architecture & security model** | [Architecture Overview](docs/architecture/README.md) |
| **Why capabilities are removed, not forbidden** | [Capability Removal](docs/design/capability-removal.md) |
| **Gateway enforcement** | [Gateway README](gateway/README.md) |
| **Agent teams & deliberative consensus** | [Agent Teams Guide](docs/guides/agent-teams.md) |
| **Slice-DAG implement phase** | [Slice-DAG Implement Phase](docs/architecture/slice-dag.md) |
| **SDLC pipeline** | [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) |
| **Human-in-the-loop decisions** | [HITL Decisions](docs/hitl-decisions.md) |
| **Orchestrator & overseer** | [Orchestrator Architecture](docs/architecture/orchestrator.md) |
| **Health monitoring** | [Pipeline Health Monitoring](docs/guides/pipeline-health-monitoring.md) |
| **Agent roles & permissions** | [Agent Roles Reference](docs/reference/agent-roles.md) |
| **Non-Claude model routing** | [Upstream Routing](docs/architecture/upstream-routing.md) · [Per-Agent Models](docs/guides/per-agent-models.md) |
| **Kubernetes / k3s** | [Kubernetes Migration](docs/architecture/kubernetes-migration.md) |
| **Sandbox environment** | [Sandbox README](sandbox/README.md) |
| **Contributing** | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Development

```bash
make setup             # Install dev dependencies + pre-commit hooks
make lint              # Run all linters (Python, Shell, YAML, Dockerfile, Actions)
make test              # Tests reachable from your diff (changeset-aware narrow default)
make test-all          # Full unit-test suite (CI ground truth); updates LKG on green
make test-integration  # Integration tests (k3s required)
make test-security     # Security/pentesting tests
make lint-fix          # Auto-fix lint issues
make security          # Run security scans
make build             # Build Docker images
```

`make test` is changeset-aware: it narrows to the tests your diff transitively touches (via a `grimp`-backed reverse import graph), so you don't have to guess which suites to run. `make test-all` is the full suite CI enforces. Always use the `make` targets; they resolve to the project's `.venv` automatically.

Requires Python >= 3.14 and the `uv` package manager. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and branching, and [docs/guides/testing.md](docs/guides/testing.md) for the changeset-aware test model.

## License

MIT License. See [LICENSE](LICENSE) for details.
