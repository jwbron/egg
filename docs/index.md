# Documentation Index

> egg: A structurally enforced SDLC pipeline for autonomous LLM agents — turning GitHub issues into reviewed pull requests with mandatory human gates.

This index helps both humans and LLMs navigate the documentation efficiently.

## Core Documentation

### Architecture Decision Records (ADRs)

| Document | Description |
|----------|-------------|
| [ADR Overview](adr/README.md) | Index of all ADRs and their status |
| [Git Isolation Architecture](adr/implemented/ADR-Git-Isolation-Architecture.md) | Gateway sidecar design for credential isolation |
| [Gateway Credential Injection](adr/implemented/ADR-Gateway-Credential-Injection.md) | Zero-credential sandbox design |
| [Anthropic API Credential Injection](adr/implemented/ADR-Anthropic-API-Credential-Injection.md) | API key proxy via gateway |
| [Declarative Setup Architecture](adr/implemented/ADR-Declarative-Setup-Architecture.md) | Python-based declarative setup |
| [Standardized Logging Interface](adr/implemented/ADR-Standardized-Logging-Interface.md) | Structured JSON logging |
| [Internet Tool Access Lockdown](adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) | Public/private network modes |
| [SDLC Pipeline](adr/implemented/ADR-SDLC-Pipeline.md) | Structurally enforced agent checkpoints and verification gates |

### Strategy

| Document | Description |
|----------|-------------|
| [The Agentic Feedback Loop](agentic-feedback-loop.md) | The foundational work-review-feedback cycle that drives quality in agent-human collaboration |
| [Why egg Works](collaboration-effectiveness.md) | How the public, sandboxed, async model delivers safety, quality, and collaboration |

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design and security model |
| [Orchestrator Architecture](architecture/orchestrator.md) | Orchestrator deployment modes and sandbox-to-orchestrator communication |

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
| [Agent Teams](guides/agent-teams.md) | Agent team communication, peer consensus protocol, and evidence-backed deliberation |
| [Concurrent Execution](guides/concurrent-execution.md) | Concurrent agent execution: message bus, readiness signaling, consensus protocol |
| [Tier 3 Dispatch](guides/tier3-dispatch.md) | Phase-level parallel dispatch for high-complexity tasks |
| [Checkpoint Access](guides/checkpoint-access.md) | Querying cross-agent checkpoints in multi-agent pipelines |
| [Coordinator Agent](guides/coordinator.md) | Dynamic agent orchestration via conversational coordinator with MCP server |

### Reference

| Document | Description |
|----------|-------------|
| [Agent Roles](reference/agent-roles.md) | All agent roles: purpose, phase, file access permissions, input/output artifacts |
| [Agent Recovery](reference/agent-recovery.md) | Retry manager, circuit breaker, conflict detection, and resilience utilities |
| [Post-Agent Commit](reference/post-agent-commit.md) | Auto-commit behavior on container exit: phase restrictions, error handling |
| [Redaction](reference/redaction.md) | Checkpoint redaction patterns, security model, and limitations |

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
| [Shared Libraries](../shared/README.md) | `shared/` | Config, logging, git utilities, and SDLC contracts |
| [Configuration](../config/README.md) | `config/` | Repository and host configuration |
| [CLI Entry Points](../bin/README.md) | `bin/` | `egg` and `egg-sdlc` commands |
| [GitHub Action](../action/README.md) | `action/` | Composite action for GitHub Actions |
| [Claude Code Config](../sandbox/.claude/README.md) | `sandbox/.claude/` | Agent rules and slash commands |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Gateway changes** | [Architecture Overview](architecture/README.md) | [ADR: Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md), [Gateway README](../gateway/README.md) |
| **Security-related changes** | [Architecture Overview](architecture/README.md) | [Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |
| **Sandbox changes** | [Sandbox README](../sandbox/README.md) | [Architecture Overview](architecture/README.md) |
| **Configuration changes** | [Config README](../config/README.md) | [egg_config README](../shared/egg_config/README.md) |
| **Docker build / dependency caching** | [Sandbox README](../sandbox/README.md#build-time-dependency-installation) | [Config README](../config/README.md#per-repo-build-commands-dependency-caching) |
| **GitHub Action setup** | [GitHub Action README](../action/README.md) | [Architecture Overview](architecture/README.md) |
| **Adding tests** | [Contributing](../CONTRIBUTING.md) | [Project Structure](development/STRUCTURE.md) |
| **Setting up GitHub automation** | [GitHub Automation](guides/github-automation.md) | [Agent-Mode Design](guides/agent-mode-design.md), [GitHub Action](../action/README.md) |
| **Modifying review criteria** | [Reviewer Sync Guide](../shared/prompts/REVIEWER-SYNC.md) | [GitHub Automation](guides/github-automation.md), [Code Review Criteria](../shared/prompts/code-review-criteria.md) |
| **Using workflows in external repos** | [Reusable Workflows](guides/reusable-workflows.md) | [GitHub Automation](guides/github-automation.md), [GitHub Action](../action/README.md) |
| **Designing agent workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [Architecture Overview](architecture/README.md) |
| **Adding bot workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [Action README](../action/README.md), existing workflows in `.github/workflows/` |
| **SDLC pipeline changes** | [SDLC Pipeline Guide](guides/sdlc-pipeline.md) | [The Agentic Feedback Loop](agentic-feedback-loop.md), [ADR: SDLC Pipeline](adr/implemented/ADR-SDLC-Pipeline.md), [Plan Template](templates/plan.md), [Analysis Template](templates/analysis.md), `orchestrator/` package |
| **Agent teams / peer consensus** | [Agent Teams Guide](guides/agent-teams.md) | [Concurrent Execution Guide](guides/concurrent-execution.md), [SDLC Pipeline Guide](guides/sdlc-pipeline.md) |
| **Concurrent execution mode** | [Concurrent Execution Guide](guides/concurrent-execution.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Checkpoint Access](guides/checkpoint-access.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Tier 3 / phase-level dispatch** | [Tier 3 Dispatch Guide](guides/tier3-dispatch.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Agent Roles Reference](reference/agent-roles.md) |
| **Coordinator / dynamic orchestration** | [Coordinator Agent Guide](guides/coordinator.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Agent-Mode Design](guides/agent-mode-design.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Agent roles and file permissions** | [Agent Roles Reference](reference/agent-roles.md) | [SDLC Pipeline Guide](guides/sdlc-pipeline.md), [Tier 3 Dispatch Guide](guides/tier3-dispatch.md), [Architecture Overview](architecture/README.md) |
| **Agent failure recovery** | [Agent Recovery Reference](reference/agent-recovery.md) | [Concurrent Execution Guide](guides/concurrent-execution.md), [Orchestrator Architecture](architecture/orchestrator.md) |
| **Post-agent auto-commit** | [Post-Agent Commit Reference](reference/post-agent-commit.md) | [Architecture Overview](architecture/README.md) |
| **Checkpoint redaction** | [Redaction Reference](reference/redaction.md) | [Checkpoint Access](guides/checkpoint-access.md), [Architecture Overview](architecture/README.md) |
| **Health check framework** | [Health Checks README](../orchestrator/health_checks/README.md) | [Orchestrator Architecture](architecture/orchestrator.md), [Orchestrator README](../orchestrator/README.md) |
| **Generating repository documentation** | [GitHub Automation: Documentation Onboarding](guides/github-automation.md#documentation-onboarding) | [Onboarding prompt](../shared/prompts/onboarding-docs-prompt.md), `egg-onboarding-docs` CLI |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview and quick start
2. [Contributing](../CONTRIBUTING.md) - Development setup

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [Project Structure](development/STRUCTURE.md) - Code organization

---

*Last updated: 2026-03-12*
