# egg

**Autonomous software engineering that stays aligned with the human who asked for it.**

egg turns an idea (a GitHub issue, a Jira epic, a rough description) into reviewed, mergeable pull requests. Agent teams do the labor: they research the codebase, refine requirements, plan the work, write code and tests, and review each other's output across runs that span hours, many slices, and multiple repositories. Humans make every decision that matters: whether the requirements are right, whether the plan is sound, how each ambiguity resolves, and whether anything merges.

The system's central concern is keeping those two sides aligned. Feedback gates sit where redirecting is cheapest; peer review must cite evidence from real artifacts; context management never lets an agent quietly forget what you asked for; and the environment makes rule-breaking structurally impossible rather than merely discouraged.

> *Inspired by Andy Weir's short story "The Egg": a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

> **Status:** egg is under heavy development. The core workflow is functional and runs end-to-end (egg develops egg, including multi-hour, many-slice pipelines on its own repository), but expect breakages and changing behavior.

## The Problem

LLM agents are capable enough to write real code across long, multi-step tasks. Three things stop teams from letting them:

1. **They can't be trusted with real credentials and real merge buttons.** Prompts saying "please don't merge" are suggestions, not constraints. Agents ignore them under pressure, and a prompt injection can turn any instruction against you.
2. **They are unreliable self-assessors.** An agent announcing its work is done means the agent *thinks* it's done. Agents hallucinate passing tests, and concurrent agents rubber-stamp each other with sycophantic "looks good!" reviews.
3. **Over long tasks, they quietly lose the plot.** Context windows fill, lossy auto-compaction silently drops the constraints the work depends on, and the output drifts away from what the human actually asked for.

egg's answer to all three is the same: move the guarantee out of the prompt and into infrastructure, and keep a human on every decision the agents should not make alone.

## How egg Works: Four Principles

### 1. Human Authority Is Structural

Untrusted agents work inside a zero-credential sandbox. A trusted **gateway sidecar** sits between every agent and the outside world: agents use ordinary tools (`git`, `gh`, `curl`), and transparent wrappers route every operation through the gateway for policy enforcement.

- **No credentials in the sandbox.** The agent environment has zero tokens and zero keys. The gateway holds every credential (GitHub, Anthropic, Jira, Confluence, LiteLLM) and injects them into proxied requests. Agents never see or handle secrets.
- **No merging.** The merge endpoint doesn't exist. There is no prompt saying "don't merge"; the capability is simply absent from the agent's world.
- **Phase-locked operations.** Every git/gh operation is validated against the pipeline's current SDLC phase. An agent in the plan phase physically cannot push code; an agent implementing one slice cannot rewrite the contract.
- **Branch ownership.** Agents may only push to `egg-`/`egg/`-prefixed branches (or branches with an open egg-authored PR). Role-based file restrictions reject pushes that touch protected paths with `403 restricted_path_modified`.
- **Network isolation.** Each pipeline's mode defaults to its repo's GitHub visibility. Private or internal repos get private mode: the sandbox reaches the Anthropic API and nothing else, enforced by a Squid proxy and Cilium NetworkPolicies. Public repos get public mode: all external access is proxied and audited through the gateway. Package-manager egress (npm, GitHub Packages) flows through the proxy with credentials injected at the proxy layer, never in the sandbox.
- **Bounded Jira/Confluence access.** Optional, private-mode-only gateway wrappers expose read verbs plus a bounded write extension, gated by a project/space allowlist with per-verb schemas and response redaction.

Above the enforcement layer, the pipeline funnels every consequential judgment to a person. Each phase boundary is a **human gate**: a person reviews the agents' artifacts and approves before the pipeline advances. Whenever an agent or the monitoring plane hits something it should not resolve alone (ambiguous requirements, a design fork, an infrastructure error, repeated failed cycles), egg pauses and queues a formal **HITL decision** instead of guessing. The operator answers through `provide_input` on the MCP surface and the pipeline resumes exactly where it stopped. egg never auto-resolves these decisions, even in otherwise automated runs.

The division of labor is explicit:

| The agents do | A human decides |
|---------------|-----------------|
| Research the codebase, draft and review requirements | Whether the requirements are right (refine gate) |
| Propose an approach and slice the work into a DAG | Whether the plan and approach are sound (plan gate) |
| Draft Jira mutations for an epic | Whether to apply them (apply gate, epic-mode) |
| Write code, tests, and docs; reach peer consensus | When peer review can't converge, how to break the tie |
| Surface ambiguities and open questions | How to resolve each one (feedback) |
| Detect stalls, loops, and errors | How to recover when self-correction fails (escalations) |
| Open and stack PRs from plan metadata | Whether anything merges (only humans merge) |

This is zero-trust architecture applied to AI agents: the agent doesn't need to be trustworthy because the environment is structurally safe, and the human doesn't need to audit for danger because the dangerous capabilities don't exist. See [Capability Removal](docs/design/capability-removal.md), the [Gateway README](gateway/README.md), and [HITL Decisions](docs/hitl-decisions.md).

### 2. Review Is Evidence, Not Vibes

When multiple agents work concurrently, they must agree that their combined output is coherent. The naive approach (each agent telling a central orchestrator "I'm ready") fails because agents are unreliable self-assessors. egg replaces orchestrator-decreed consensus with **Deliberative Consensus**: agents review each other's actual work and individually confirm agreement through the **Broadcast-Review-Converge (BRC)** protocol.

```
Phase 1: Broadcast     Each producer (coder, tester, documenter) completes work
                       and proposes it with structured attestations: commit SHAs,
                       files changed, tests run, risks considered.

Phase 2: Review        Reviewers evaluate proposals from assigned producers,
                       grounded in the actual git artifacts. Objections must be
                       specific and actionable; generic "looks good" is rejected
                       by schema validation.

Phase 3: Converge      When all reviewers have ACKed all assigned producers, each
                       agent independently confirms. The orchestrator observes
                       consensus; it does not decide it.
```

**Anti-sycophancy by design:**

- **Delphi-style ordering.** Reviewers form independent judgments from git artifacts *before* seeing the producer's self-assessment. The server withholds producer metadata until the reviewer submits its own evaluation.
- **Costly signals.** Proposals and reviews require structured attestations tied to real artifacts (commit SHAs, file paths, test counts), which are mechanically hard to fake without doing the work.
- **Commitment devices.** Proposals have cooldown periods; retracting one requires citing specific new information; after repeated flip-flops the agent is locked out and escalated to a human.
- **Adversarial tester.** The tester is a dual role: it reviews and hardens the coder's tests (the coder authors its own tests), adds missing regression and adversarial cases, and probes the implementation for bugs, NACKing with a failing test as the bug report.

On top of the protocol sits a **review-quality layer** ([#3523](https://github.com/jwbron/egg/issues/3523), rolling out in stages): reviewers emit versioned **structured findings** (each with a concrete failure scenario) and the server computes the ACK/NACK verdict from them, so models own the judgment and code owns the mechanics; a verification ladder requires blocking findings to be reproduced; a deterministic **risk router** decides which reviewer lenses run at what effort for each slice, with hard floors (unrouted files get full review, security-sensitive paths always get the security reviewer); and a shared-evidence prompt prefix, assembled once by an unprivileged `evidence_gatherer`, lets parallel reviewers start from the same cheap, cached ramp-up. Reviewers can also issue a **conditional ACK** that attaches a merge-time human obligation, surfaced on the PR body.

The review topology is asymmetric and sparse: reviewers evaluate producers, not each other, which keeps overhead at a handful of review edges instead of full pairwise review. See [Agent Teams and Deliberative Consensus](docs/guides/agent-teams.md), [Review Quality](docs/reference/review-quality.md), and [Conditional ACK](docs/reference/conditional-ack.md).

### 3. Mechanics Live in Code; Judgment Lives in Models

egg's longest-running design lesson: never make protocol progress depend on a model volunteering to behave. Everything mechanical (waiting, routing, state exchange, verdict arithmetic, health detection) belongs to deterministic code; the models are consulted only for judgment.

- **Orchestrator-owned event loop.** Agents never idle and never wait on a message bus. The orchestrator owns all waiting and spawns a short-lived, one-shot agent pod only when there is an actionable event for it, with a per-event prompt composed deterministically. No idle pods, no model-held control flow. See [On-Demand Agent Lifecycle](docs/architecture/on-demand-agent-lifecycle.md).
- **Served coordination state.** Anything an agent consumes but doesn't own is served from one authoritative source; workspace synchronization is performed by the harness, deterministically; prompts carry judgment, never sync mechanics. This retired a whole class of replica-drift incidents. See [Served Coordination State](docs/architecture/coordination-state.md).
- **Deterministic health detection, on-demand adjudication.** Pipeline monitoring is a **detection plane** of pure, exception-isolated detectors evaluated in-process on every tick; the overwhelming majority of observations yield no finding and no LLM call. Only a finding marked as genuinely ambiguous spawns an on-demand **overseer** agent (Opus tier) to adjudicate that one finding. Its verdict is advisory: a bounded corrective vocabulary (`nudge_agent`, `respawn_cohort`, `open_operator_hitl`) is executed by the orchestrator identity, rate-limited, deduplicated, and audited. Infrastructure errors fast-path to a human decision. See [Overseer Architecture](docs/architecture/overseer.md) and [Pipeline Health Monitoring](docs/guides/pipeline-health-monitoring.md).
- **Degrade safely, never lose work.** Job supervision tracks failure streaks with backoff and escalates instead of retry-looping; a circuit breaker stops non-converging cycles; committed-but-unpushed agent work is auto-salvaged to `egg/recovered/*` refs; uncommitted work on agent exit becomes a HITL recovery decision; API rate-limit exhaustion pauses and paces retries across the cap window instead of failing the run; auth failures fail fast. See [Agent Recovery](docs/reference/agent-recovery.md) and [Post-Agent Commit](docs/reference/post-agent-commit.md).

### 4. Context Is a Budgeted Resource

Long-horizon autonomy fails quietly: not with an error, but with an agent that no longer remembers the constraint you gave it four hours ago. egg treats the context window as an engineered, budgeted resource with structural protections:

- **Durable BRC memory.** Each role keeps a distilled, per-pipeline memory artifact (last-reviewed commit, prior verdicts and objections, decision log), written at review time and re-read on re-entry, so a freshly spawned one-shot agent re-enters a review cycle with continuity instead of amnesia. See [BRC Memory Artifact](docs/architecture/brc-memory.md).
- **Anchors.** The operator's task statement and deterministically derived review anchors are re-asserted in every per-event prompt, and a persistent anchor mechanism recovers agent state after compaction. See [Anchor Recovery](docs/guides/anchor-recovery.md).
- **Context discipline** ([#3200](https://github.com/jwbron/egg/issues/3200), opt-in via `EGG_CONTEXT_DISCIPLINE`): a small, byte-stable **protected root** (role contract, task anchor, directives) stays resident; bulk content (diffs, transcripts, memory) is demoted to a **queryable environment** pulled just-in-time through served handles; and a proactive, deterministic **threshold reseed** rebuilds the session before the harness's lossy auto-compaction can silently drop the anchors the work depends on. See [BRC Context Discipline](docs/architecture/context-discipline.md).

## The Pipeline

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

1. **Refine.** Agents analyze the task, research the codebase, and produce requirements; reviewers validate. A simplifier also produces a jargon-free, human-focused summary of the analysis. A human approves before planning begins.
2. **Plan.** An architect recommends an approach, a task planner breaks it into discrete tasks with acceptance criteria and a **DAG of slices**, and a risk analyst flags concerns. A simplifier produces a jargon-free companion to the plan. A human approves before any code is written.
3. **Apply** *(Jira epic-mode only)*. When the task resolves to a Jira Epic, an `applier` role drives Jira mutations (epic description writes, child-ticket creates/edits, link creates, Won't-Do handoffs) on operator approval, before implementation begins.
4. **Implement.** The plan's slices are scheduled as a **DAG**: each slice runs as its own agent team on its own integration branch, with its own BRC consensus and its own stacked PR. Slices whose dependencies are satisfied run concurrently, bounded per pipeline and process-wide; dependent slices wait for later waves. Within a slice the coder writes code and its own tests, the tester reviews and hardens those tests while adversarially probing for bugs, and the documenter updates docs, while code, contract, security, and concurrency reviewers provide line-level feedback and can block consensus.

Pipelines can also span **multiple repositories**: each slice maps to exactly one repo, cross-repo work is expressed as slices in different repos connected by ordinary dependency edges, and a cross-repo merge gate sequences the dependent PRs (the downstream PR stays draft until its upstream merges). See [Slice-DAG Implement Phase](docs/architecture/slice-dag.md).

There is **no separate "PR" phase**. The pipeline's context PR (`egg/<id>/work` into `main`) is opened up-front at the plan-to-implement boundary; slice PRs stack onto it and are created automatically by the orchestrator as each slice reaches consensus. Only a human can merge, via the GitHub UI. See the [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md).

A completed pipeline looks like this:

```
╔═══════════════════════════════════════════════════════╗
│ ✓ Refine                                    complete  │
│   ✓ refiner  ✓ simplifier                             │
│   ✓ reviewer_refine  ✓ reviewer_agent_design          │
│   ✓ first_principles_reviewer                         │
╚═══════════════════════════════════════════════════════╝
    │
    ▼
╔══════════════════════════════════════════════╗
│ ✓ Plan                              complete │
│   ✓ architect  ✓ simplifier                  │
│   ✓ task_planner  ✓ risk_analyst             │
│   ✓ reviewer_plan                            │
╚══════════════════════════════════════════════╝
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

## Beyond the Pipeline: GitHub Automation

egg also ships a suite of sandboxed GitHub Actions bots that run under the same zero-credential, gateway-enforced controls as pipeline agents:

- **AI Code Review** reviews PRs and posts line-level feedback; **Address Review Feedback** closes the loop by acting on review comments (from humans or bots), enabling automated review-fix-re-review cycles.
- **Design Review** and **Contract Verification** apply project-specific rules and check implementations against their SDLC contracts.
- **Check Autofixer** diagnoses CI failures and fixes them (deterministic fixers first, then an agent); **Conflict Resolver** clears merge conflicts; **Doc Updater** keeps docs in sync with merged code.

All of these are packaged as **reusable workflows** that external repositories can call, so a repo can adopt egg's review loop without adopting the full pipeline. See [GitHub Automation](docs/guides/github-automation.md) and [Reusable Workflows](docs/guides/reusable-workflows.md).

## Architecture

egg deploys as a set of containers on **Kubernetes (k3s)**, split across two namespaces: trusted services in `egg-system` and untrusted agent Jobs in `egg-agents`, with Cilium NetworkPolicies enforcing isolation between them.

```
┌──────────────────────────── egg-system (trusted) ────────────────────────────┐
│                                                                              │
│  ┌───────────────────────┐   ┌───────────────────────────┐   ┌────────────┐  │
│  │     Orchestrator      │   │     Gateway Sidecar       │   │   Redis    │  │
│  │                       │   │                           │   │ (message   │  │
│  │  • Pipeline state     │   │  • Zero-trust credential  │   │  store)    │  │
│  │  • Event loop +       │◀─▶│    injection              │   └────────────┘  │
│  │    slice scheduler    │   │  • Phase-locked git/gh    │   ┌────────────┐  │
│  │  • BRC consensus      │   │  • Branch ownership       │   │  LiteLLM   │  │
│  │  • Detection plane    │   │  • Restricted-path gates  │   │  (proxy)   │  │
│  │  • HITL decisions     │   │  • Network isolation      │   │  optional  │  │
│  │  • MCP server :9850   │   │  • Jira/Confluence wrap   │   │  non-Claude│  │
│  │  • REST API   :9849   │   │    :9848 (+ proxy :3129)  │   │  routing   │  │
│  └───────────────────────┘   └──────────────▲────────────┘   │   :4000    │  │
│                                             │                └────────────┘  │
└─────────────────────────────────────────────┼────────────────────────────────┘
                                              │ all privileged ops + LLM calls
┌──────────────────────────── egg-agents ─────┼───────────── (untrusted) ──────┐
│   ┌─────────────────────────────────────────┴────────────────────────────┐  │
│   │  Sandbox agent Jobs: short-lived, spawned one-shot per event         │  │
│   │  • Claude Code via the Agent SDK (egg_agent)                         │  │
│   │  • Standard git/gh wrappers → gateway   • No credentials             │  │
│   │  • Per-agent git worktree               • No merge endpoint          │  │
│   └──────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** the agent cannot bypass controls because the capabilities don't exist in its environment. This is infrastructure enforcement, not behavioral control. See the [Architecture Overview](docs/architecture/README.md) and [Kubernetes Migration](docs/architecture/kubernetes-migration.md).

| Component | Namespace | Role | Trust |
|-----------|-----------|------|-------|
| **Orchestrator** | `egg-system` | Pipeline state, the BRC event loop, slice scheduling, detection plane, HITL, MCP server (`:9850`) + REST API (`:9849`) | Trusted |
| **Gateway** | `egg-system` | Credential injection, phase/branch/path enforcement, network isolation, Jira/Confluence wrappers (`:9848`, proxy `:3129`) | Trusted |
| **Redis** | `egg-system` | Message-store backend for the inter-agent bus and coordination state | Trusted |
| **LiteLLM** | `egg-system` | Optional proxy for routing individual agent roles to non-Claude models (`:4000`); inert by default | Trusted |
| **Sandbox** | `egg-agents` | Untrusted agent Jobs: Claude Code via the Agent SDK, per-agent worktree, zero credentials, spawned one-shot per event | Untrusted |

## Driving Pipelines: the MCP Server

The orchestrator exposes an MCP server (port `9850`) so you can drive pipelines from Claude Code or any MCP-compatible client. The legacy interactive CLI (`bin/egg`) and Docker Compose deployment were removed in [#1762](https://github.com/jwbron/egg/issues/1762): agents are headless, and the human operates as an MCP client.

```
submit_task(issue_number=123, repo="owner/name")
```

Representative tools (see the [Orchestrator CLI](docs/reference/orchestrator-cli.md) and [MCP Deployment Tools](docs/reference/mcp-deployment-tools.md) for the full set):

- **Lifecycle:** `submit_task`, `cancel_task`, `list_tasks`, `start_pipeline`
- **Monitoring:** `get_status`, `get_phase`, `get_pipeline_snapshot`, `get_consensus_status`, `check_health`
- **HITL & coordination:** `provide_input`, `answer_feedback`, `send_message`
- **Phase & agent control:** `advance_phase`, `start_phase`, `complete_phase`, `restart_agent`, `restart_phase`, `populate_contract`
- **Live operations:** `update_pipeline_config` (swap an agent's model or widen a consensus timeout on a running pipeline)
- **Recovery:** `list_agent_local_commits`, `salvage_agent_commits`, `prune_stale_worktrees`
- **Debugging:** `list_containers`, `get_container_logs`, `get_agent_transcript`, `get_service_logs`, `validate_config`
- **Deployment:** `get_deployment_context`, `validate_deployment_manifests`, `validate_network_isolation`, `rebuild_and_rollout`

Host-side CLIs in `bin/` (`egg-sdlc`, `egg-status`, `egg-pipeline-watch`, `egg-onboarding-docs`) wrap the same APIs for monitoring and visualization, and the `skills/` directory ships operator skills (`sdlc`, `agent-diagnose`, `deployment-diagnose`, `egg-setup`) for MCP-connected Claude Code hosts.

## More Capabilities

| Capability | What it does | Docs |
|------------|--------------|------|
| **Jira epic mode** | Epic detection and routing, a reassess sweep that classifies child tickets (done / in-flight / updatable), Won't-Do drain, and a bounded, allowlisted Jira write surface | [Jira Wrapper](docs/reference/jira-wrapper.md) |
| **Per-agent model routing** | Route any agent role to a non-Claude model through LiteLLM, with a hot-reloadable routing policy, proactive switchover, and reactive fallback chains | [Per-Agent Models](docs/guides/per-agent-models.md) · [Upstream Routing](docs/architecture/upstream-routing.md) |
| **Observability** | Structured OpenTelemetry-aligned JSON logging, persisted agent transcripts, and diagnostic skills with evidence boundaries and redaction | [Logging](docs/architecture/logging.md) · [Deployment Diagnostics](docs/guides/deployment-diagnostics.md) |
| **Repo onboarding docs** | Generate baseline repository documentation with `egg-onboarding-docs` | [Documentation Onboarding](docs/guides/github-automation.md#documentation-onboarding) |
| **Secret redaction** | Defense-in-depth redaction of credentials in logs and agent-visible output | [Redaction](docs/reference/redaction.md) |

## Quick Start

egg deploys to a local **k3s** cluster. (Docker Compose was removed in [#1762](https://github.com/jwbron/egg/issues/1762); `bin/egg-deploy up/down/build/logs` are deprecated stubs.)

```bash
# 1. Clone
git clone https://github.com/jwbron/egg.git
cd egg

# 2. Install k3s with the Cilium CNI (required for NetworkPolicy enforcement)
make k3s-setup

# 3. Generate host-side config (secrets, config.yaml, repositories.yaml templates)
bin/egg-deploy init
#    then edit ~/.config/egg/secrets.env  (ANTHROPIC_API_KEY or OAuth token,
#    GitHub PAT, gateway policy) and ~/.config/egg/repositories.yaml

# 4. Build images, publish them to the local registry, create secrets, and deploy
make build
make k3s-push
make k3s-secrets
make deploy

# 5. Verify
kubectl get pods -n egg-system

# 6. Connect your Claude Code session to the orchestrator's MCP server
claude mcp add --transport http --scope user egg http://localhost:9850/mcp
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
| `orchestrator/` | Central SDLC pipeline engine: the event loop, slice DAG scheduling, BRC consensus, detection plane, health monitoring, MCP server |
| `gateway/` | Trusted policy-enforcement sidecar: validates git/gh/Jira/Confluence operations, injects credentials, enforces network isolation |
| `sandbox/` | Untrusted agent container: Claude Code config, the `egg_agent` runtime, git/gh wrappers, host-side CLIs |
| `shared/` | Shared Python packages (`egg_agent`, `egg_config`, `egg_contracts`, `egg_git`, `egg_logging`, `egg_anchor`, …) plus agent prompt templates |
| `config/` | Repository and host configuration templates; `config/litellm/` builds the LiteLLM image |
| `k8s/` | Kustomize manifests: `base/` plus `overlays/local/` |
| `action/` | Composite GitHub Action and review-bot prompt builders |
| `skills/` | Operator skills for MCP-connected hosts (`sdlc`, `agent-diagnose`, `deployment-diagnose`, `egg-setup`) |
| `docs/` | All documentation: guides, architecture, references |
| `tests/` | Unit-test suite (mirrors the component layout) |
| `integration_tests/` | Cross-component integration and security tests (k3s required) |
| `metrics/` | Self-improvement metrics data |
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
| **Review quality (structured findings, risk router)** | [Review Quality](docs/reference/review-quality.md) |
| **Slice-DAG implement phase & multi-repo** | [Slice-DAG Implement Phase](docs/architecture/slice-dag.md) |
| **SDLC pipeline** | [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) |
| **Human-in-the-loop decisions** | [HITL Decisions](docs/hitl-decisions.md) |
| **On-demand agent lifecycle** | [On-Demand Agent Lifecycle](docs/architecture/on-demand-agent-lifecycle.md) |
| **Overseer & health monitoring** | [Overseer Architecture](docs/architecture/overseer.md) · [Pipeline Health Monitoring](docs/guides/pipeline-health-monitoring.md) |
| **Long-context discipline** | [BRC Context Discipline](docs/architecture/context-discipline.md) · [BRC Memory](docs/architecture/brc-memory.md) · [Anchor Recovery](docs/guides/anchor-recovery.md) |
| **GitHub automation & reusable workflows** | [GitHub Automation](docs/guides/github-automation.md) · [Reusable Workflows](docs/guides/reusable-workflows.md) |
| **Agent roles & permissions** | [Agent Roles Reference](docs/reference/agent-roles.md) |
| **Non-Claude model routing** | [Upstream Routing](docs/architecture/upstream-routing.md) · [Per-Agent Models](docs/guides/per-agent-models.md) |
| **Kubernetes / k3s** | [Kubernetes Migration](docs/architecture/kubernetes-migration.md) |
| **Sandbox environment** | [Sandbox README](sandbox/README.md) |
| **The feedback-loop model behind the pipeline** | [The Agentic Feedback Loop](docs/architecture/agentic-feedback-loop.md) · [Why egg Works](docs/architecture/collaboration-effectiveness.md) |
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
