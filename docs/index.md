# Documentation Index

> egg: A structurally enforced SDLC pipeline for autonomous LLM agents — turning GitHub issues into reviewed pull requests with mandatory human gates.

This index helps both humans and LLMs navigate the documentation efficiently.

## Core Documentation

### Design

| Document | Description |
|----------|-------------|
| [Capability Removal](design/capability-removal.md) | Why infrastructure-level constraints beat prompt-based rules for agent safety |

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design and security model |
| [Orchestrator Architecture](architecture/orchestrator.md) | Orchestrator deployment modes and sandbox-to-orchestrator communication |
| [Git Isolation](architecture/git-isolation.md) | Gateway sidecar design for worktree isolation and credential separation |
| [Gateway Auto-Filter](architecture/gateway-auto-filter.md) | Restricted-path rejection on push (`403 restricted_path_modified`) and the commit-authorship registry that backs attribution |
| [Credential Injection](architecture/credential-injection.md) | Zero-credential sandbox with API key proxy via gateway |
| [Network Isolation](architecture/network-isolation.md) | Public/private network modes and domain allowlist |
| [Kubernetes Migration](architecture/kubernetes-migration.md) | Docker to k8s (k3s) migration: architecture, network isolation, developer workflow |
| [SDLC Pipeline](architecture/sdlc-pipeline.md) | Structurally enforced agent checkpoints and verification gates |
| [Slice-DAG Implement Phase](architecture/slice-dag.md) | `Phase` → `Slice` schema rename, forest validation, slice scheduler (waves, two-tier `max_cycles`, failure cascade), stacked-PR reconciler, per-slice branches and BRC trackers |
| [Declarative Setup](architecture/declarative-setup.md) | Python-based declarative setup system |
| [Logging](architecture/logging.md) | Structured JSON logging with OpenTelemetry alignment |
| [The Agentic Feedback Loop](architecture/agentic-feedback-loop.md) | The foundational work-review-feedback cycle that drives quality |
| [Why egg Works](architecture/collaboration-effectiveness.md) | How the public, sandboxed, async model delivers safety and collaboration |
| [Integration-Test Trust Boundary](architecture/integration-test-trust-boundary.md) | Test execution contexts (in-sandbox-agent / trusted-CI-runner / human-operator) and fixture tiers; authoritative reference for plan-phase Trust-Boundary Audit (#2594) |

### Development

| Document | Description |
|----------|-------------|
| [Project Structure](development/STRUCTURE.md) | Directory conventions and organization |
| [Contributing](../CONTRIBUTING.md) | Development setup, workflow, and PR process |
| [Releasing](../RELEASING.md) | Release process and semantic versioning |

### Guides

| Document | Description |
|----------|-------------|
| [Deployment](guides/deployment.md) | Production deployment options: Docker Compose, CLI, GitHub Action |
| [Deploy Migration](guides/deploy-migration.md) | Migrating from legacy deployments |
| [Agent-Mode Design](guides/agent-mode-design.md) | When to let egg operate freely vs. when constraints are appropriate |
| [Agent Development](guides/agent-development.md) | Developing agent strategies |
| [GitHub Automation](guides/github-automation.md) | Built-in review bots, autofixer, conflict resolver, and doc updater workflows |
| [Reusable Workflows](guides/reusable-workflows.md) | Using egg's reusable workflows in external repositories |
| [SDLC Pipeline](guides/sdlc-pipeline.md) | Operational guide for the structurally enforced SDLC pipeline |
| [Agent Teams](guides/agent-teams.md) | Agent team communication and Deliberative Consensus (BRC protocol + evidence-backed deliberation) |
| [Concurrent Execution](guides/concurrent-execution.md) | Concurrent agent execution: message bus, directed coordination, readiness signaling, consensus protocol |
| [Testing](guides/testing.md) | Canonical testing guide: `make test` (changeset-aware narrow default), `make test-all` (full suite), `make test-record-good` (manual LKG override), grimp-backed reverse import-graph selection, sidecar LKG, fallback triggers, `--why` introspection, fail-open exit contract |
| [Checkpoint Access](guides/checkpoint-access.md) | Querying cross-agent checkpoints in multi-agent pipelines |
| [Pipeline Health Monitoring](guides/pipeline-health-monitoring.md) | Two-tier health monitoring: orchestrator tripwires + overseer agent |
| [Babysit-PR](guides/babysit-pr.md) | One-off implement-phase BRC cycle against an existing PR: role-typed producers (coder/tester/documenter) + `reviewer_code`, staging-branch isolation, single final consensus push |
| [Custom-Phase (`run_agent_task`)](guides/custom-phase.md) | Run a single SDLC phase against a repo with an explicitly chosen subset of that phase's roles (refine / plan / implement), MCP-driven replacement for the removed interactive mode |
| [Anchor Recovery](guides/anchor-recovery.md) | Agent post-compaction state recovery via persistent anchors |
| [Deployment Diagnostics](guides/deployment-diagnostics.md) | When to use `/deployment-diagnose` vs `/agent-diagnose`, evidence boundaries, and redaction guarantees |
| [File Decomposition Pattern](guides/decomposition-pattern.md) | Canonical sub-package + explicit re-export barrel pattern for decomposing oversize Python files under the `scripts/file-size-allowlist.yaml` cap; covers conversion mechanics, method-modules-on-class shape, audit recipe, allowlist rebase, and routes-handling convention |

### Deploy

| Document | Description |
|----------|-------------|
| [Resource Sizing](deploy/resource-sizing.md) | Pod CPU/memory requests and limits for gateway, orchestrator, and sandboxes, with observed-usage rationale and QoS choice |

### Reference

| Document | Description |
|----------|-------------|
| [Agent Roles](reference/agent-roles.md) | All agent roles: purpose, phase, file access permissions, input/output artifacts |
| [Agent Recovery](reference/agent-recovery.md) | Retry manager, circuit breaker, conflict detection, and resilience utilities |
| [Post-Agent Commit](reference/post-agent-commit.md) | HITL recovery for uncommitted work on agent exit (replaces auto-commit); auto-salvage of committed-but-unpushed commits to `egg/recovered/…` refs; `list_agent_local_commits` and `salvage_agent_commits` MCP tools |
| [Redaction](reference/redaction.md) | Checkpoint redaction patterns, security model, and limitations |
| [Orchestrator CLI](reference/orchestrator-cli.md) | Full `egg-orch` command reference for pipelines, phases, decisions, containers |
| [Checkpoint Browser](reference/checkpoint-browser.md) | Full `egg-checkpoint` command reference for browsing agent session history |
| [SDLC Contract](reference/sdlc-contract.md) | Full `egg-contract` command reference for tracking tasks, commits, decisions |
| [MCP Deployment Tools](reference/mcp-deployment-tools.md) | Six k8s-facing MCP tools: `get_deployment_context`, `validate_deployment_manifests`, `prune_stale_worktrees`, `validate_network_isolation`, `rebuild_and_rollout`, `get_service_logs` |
| [Agent MCP Tools](reference/agent-tools.md) | In-process SDK MCP tools sandbox agents call on the `tool_use` stream (29 verbs across 6 namespaces: `mcp__sdlc__*`, `mcp__brc__*`, `mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`, `mcp__checkpoint__*`); on by default — set `EGG_MCP_TOOLS=false` to opt out |
| [Agent Wait Patterns](reference/agent-wait-patterns.md) | Canonical `egg-orch message wait-loop` idiom for BRC STAY ALIVE, the five anti-patterns to avoid, the `egg-orch message wait` exit-code contract, the `HEARTBEAT` metadata schema, the `EGG_MESSAGE_POLL_MAX_WAIT` / `EGG_ORCH_WAITRESS_THREADS` env-var couplings, and §7 host-side `egg-orch pipeline wait-status` for event-driven pipeline monitoring |
| [Jira Wrapper](reference/jira-wrapper.md) | `/api/v1/jira/*` gateway endpoints — read verbs (ticket read, JQL search with static project-scope extraction, ticket comments, GET-only execute passthrough) plus the bounded write extension (`ticket/create`, `ticket/edit`, `ticket/comment/add`, `issue-link/create`); private-mode only; project allowlist via `config/context-filters.yaml`; per-verb body schema with size caps; ADF wrapping for plain-text `description` / `comment` bodies (`gateway/jira_adf.py`); caller-supplied idempotency key with 5-minute in-process cache (`gateway/jira_idempotency.py`); operator-configurable `jira.link_types` / `jira.epic_link_field` knobs; audit redaction never logs body content; `not_found` envelope on read 404s |
| [Confluence Wrapper](reference/confluence-wrapper.md) | `/api/v1/confluence/*` read-only gateway endpoints (page read, descendants, footer/inline comments with v1 fallback, space list/pages, CQL search with static space-scope extraction, GET-only execute passthrough); private-mode only; space allowlist via `config/context-filters.yaml`; `not_found` envelope; response redaction (`accountId` / `emailAddress` / user-profile `_links.webui` / user-profile `_links.self`); future write-verb extension points |
| [Conditional ACK](reference/conditional-ack.md) | Reviewer verdict variant: ACK + `--pre-merge-condition "..."` attaches a merge-time human obligation (e.g. `git mv`) that surfaces in `egg-orch consensus status` and in a "Pre-merge Obligations" section on the auto-created PR body |

### SDLC Pipeline Templates

| Document | Description |
|----------|-------------|
| [Analysis Template](templates/analysis.md) | Problem analysis template for the refine phase |
| [Plan Template](templates/plan.md) | Implementation plan template with task ID format for the plan phase |
| [Phase Completion Template](templates/phase-completion.md) | Phase completion comment format with approval checkbox |
| [Feedback Template](templates/feedback.md) | Feedback comment template for open-ended questions |

### SDLC Workflow Documentation

| Document | Description |
|----------|-------------|
| [HITL Decisions](hitl-decisions.md) | Human-in-the-loop decision workflow with formal decisions, feedback comments, and phase approvals |

## Component Documentation

Each major component has detailed documentation:

| Component | Location | Description |
|-----------|----------|-------------|
| [Gateway Sidecar](../gateway/README.md) | `gateway/` | Policy enforcement, credential injection, API endpoints |
| [Orchestrator](../orchestrator/README.md) | `orchestrator/` | Local SDLC pipeline execution, state management, container lifecycle |
| [Sandbox Container](../sandbox/README.md) | `sandbox/` | Agent environment, tools, entrypoint |
| [Shared Libraries](../shared/README.md) | `shared/` | Config, logging, git utilities, SDLC contracts |
| [Logging](../shared/egg_logging/README.md) | `shared/egg_logging/` | Structured JSON logging with grep-friendly inline console format |
| [Configuration](../config/README.md) | `config/` | Repository and host configuration |
| [CLI Entry Points](../bin/README.md) | `bin/` | `egg-sdlc`, `egg-deploy`, and other CLI tools |
| [GitHub Action](../action/README.md) | `action/` | Composite action for GitHub Actions |
| [Claude Code Config](../sandbox/agent-config/README.md) | `sandbox/agent-config/` | Agent rules and slash commands |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Gateway changes** | [Architecture Overview](architecture/README.md) | [Git Isolation](architecture/git-isolation.md), [Gateway Auto-Filter](architecture/gateway-auto-filter.md), [Gateway README](../gateway/README.md) |
| **Security-related changes** | [Architecture Overview](architecture/README.md) | [Git Isolation](architecture/git-isolation.md) |
| **Sandbox changes** | [Sandbox README](../sandbox/README.md) | [Architecture Overview](architecture/README.md) |
| **Configuration changes** | [Config README](../config/README.md) | [egg_config README](../shared/egg_config/README.md) |
| **Docker build / dependency caching** | [Sandbox README](../sandbox/README.md#build-time-dependency-installation) | [Config README](../config/README.md#per-repo-build-commands-dependency-caching) |
| **GitHub Action setup** | [GitHub Action README](../action/README.md) | [Architecture Overview](architecture/README.md) |
| **Adding tests** | [Contributing](../CONTRIBUTING.md) | [Project Structure](development/STRUCTURE.md), [Integration-Test Trust Boundary](architecture/integration-test-trust-boundary.md) |
| **Setting up GitHub automation** | [GitHub Automation](guides/github-automation.md) | [Agent-Mode Design](guides/agent-mode-design.md), [GitHub Action](../action/README.md) |
| **Modifying review criteria** | [Reviewer Sync Guide](../shared/prompts/REVIEWER-SYNC.md) | [GitHub Automation](guides/github-automation.md), [Code Review Criteria](../shared/prompts/code-review-criteria.md) |
| **Using workflows in external repos** | [Reusable Workflows](guides/reusable-workflows.md) | [GitHub Automation](guides/github-automation.md), [GitHub Action](../action/README.md) |
| **Designing agent workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [Architecture Overview](architecture/README.md) |
| **Adding bot workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [Action README](../action/README.md), existing workflows in `.github/workflows/` |
| **SDLC pipeline changes** | [SDLC Pipeline Guide](guides/sdlc-pipeline.md) | [The Agentic Feedback Loop](architecture/agentic-feedback-loop.md), [SDLC Pipeline Architecture](architecture/sdlc-pipeline.md), [Slice-DAG Implement Phase](architecture/slice-dag.md), [Plan Template](templates/plan.md), [Analysis Template](templates/analysis.md), `orchestrator/` package |
| **Slice-DAG / stacked-PR / `Phase`→`Slice` rename** | [Slice-DAG Implement Phase](architecture/slice-dag.md) | [SDLC Pipeline Architecture](architecture/sdlc-pipeline.md), [Plan Template](templates/plan.md), `orchestrator/slice_scheduler.py`, `orchestrator/stacked_pr_reconciler.py`, `shared/egg_contracts/models.py` |
| **Agent teams / Deliberative Consensus** | [Agent Teams Guide](guides/agent-teams.md) | [Concurrent Execution Guide](guides/concurrent-execution.md), [SDLC Pipeline Guide](guides/sdlc-pipeline.md) |
| **Reviewer verdict choices (ACK / NACK / conditional)** | [Conditional ACK Reference](reference/conditional-ack.md) | [Concurrent Execution: Reviewer verdict variants](guides/concurrent-execution.md#reviewer-verdict-variants), [Orchestrator CLI](reference/orchestrator-cli.md) |
| **Agent anchor / recovery changes** | [Anchor Recovery Guide](guides/anchor-recovery.md) | [egg_anchor README](../shared/egg_anchor/README.md), [Orchestrator CLI](reference/orchestrator-cli.md), [Concurrent Execution](guides/concurrent-execution.md) |
| **Babysit-PR / PR BRC cycle** | [Babysit-PR Guide](guides/babysit-pr.md) | [`/babysit-pr` Skill](../skills/babysit-pr/SKILL.md), [GitHub Automation](guides/github-automation.md), [SDLC Pipeline Guide](guides/sdlc-pipeline.md) |
| **One-off single-phase work (custom roster)** | [Custom-Phase Guide](guides/custom-phase.md) | [Agent Roles Reference](reference/agent-roles.md), [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Babysit-PR Guide](guides/babysit-pr.md) |
| **Kubernetes / k3s migration** | [Kubernetes Migration](architecture/kubernetes-migration.md) | [Deployment Guide](guides/deployment.md), [Network Isolation](architecture/network-isolation.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Concurrent execution mode** | [Concurrent Execution Guide](guides/concurrent-execution.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Checkpoint Access](guides/checkpoint-access.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Directed agent coordination** | [Concurrent Execution: Directed Coordination](guides/concurrent-execution.md#directed-coordination) | [Orchestrator CLI](reference/orchestrator-cli.md), [SDLC Pipeline Guide](guides/sdlc-pipeline.md) |
| **Agent STAY ALIVE / bus waits** | [Agent Wait Patterns](reference/agent-wait-patterns.md) | [Concurrent Execution: Message Bus](guides/concurrent-execution.md#how-to-wait), [Orchestrator CLI](reference/orchestrator-cli.md) |
| **Agent roles and file permissions** | [Agent Roles Reference](reference/agent-roles.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Architecture Overview](architecture/README.md) |
| **Agent failure recovery** | [Agent Recovery Reference](reference/agent-recovery.md) | [Concurrent Execution Guide](guides/concurrent-execution.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Restarting stuck agents/phases** | [Agent Recovery Reference](reference/agent-recovery.md#agent-level-restart) | [Pipeline Health Monitoring](guides/pipeline-health-monitoring.md), [Orchestrator CLI](reference/orchestrator-cli.md), [Phase Management MCP Tools](reference/orchestrator-cli.md#phase-management-mcp-tools) |
| **Post-agent exit handling** | [Post-Agent Commit Reference](reference/post-agent-commit.md) | [Architecture Overview](architecture/README.md), [Concurrent Execution](guides/concurrent-execution.md) |
| **Per-agent worktree isolation** | [Concurrent Execution Guide](guides/concurrent-execution.md#per-agent-worktree-isolation) | [Git Isolation Architecture](architecture/git-isolation.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Checkpoint redaction** | [Redaction Reference](reference/redaction.md) | [Checkpoint Access](guides/checkpoint-access.md), [Architecture Overview](architecture/README.md) |
| **Health check framework** | [Health Checks README](../orchestrator/health_checks/README.md) | [Orchestrator Architecture](architecture/orchestrator.md), [Orchestrator README](../orchestrator/README.md) |
| **Pipeline health monitoring** | [Pipeline Health Monitoring](guides/pipeline-health-monitoring.md) | [Health Checks README](../orchestrator/health_checks/README.md), [Agent Roles](reference/agent-roles.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Generating repository documentation** | [GitHub Automation: Documentation Onboarding](guides/github-automation.md#documentation-onboarding) | [Onboarding prompt](../shared/prompts/onboarding-docs-prompt.md), `egg-onboarding-docs` CLI |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview and quick start
2. [Contributing](../CONTRIBUTING.md) - Development setup

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [Project Structure](development/STRUCTURE.md) - Code organization

---
