# Documentation Index

> egg: A hardened sandbox for autonomous LLM code agents with infrastructure-enforced security controls.

This index helps both humans and LLMs navigate the documentation efficiently.

## Core Documentation

### Architecture Decision Records (ADRs)

| Document | Description |
|----------|-------------|
| [ADR Overview](adr/README.md) | Index of all ADRs and their status |
| [Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | Core system architecture, security model, and design decisions |
| [Context Sync Strategy](adr/implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) | How external data (Confluence, JIRA, GitHub) is synced |
| [Git Isolation Architecture](adr/implemented/ADR-Git-Isolation-Architecture.md) | Gateway sidecar design for credential isolation |
| [Gateway Credential Injection](adr/implemented/ADR-Gateway-Credential-Injection.md) | Zero-credential sandbox design |
| [Anthropic API Credential Injection](adr/implemented/ADR-Anthropic-API-Credential-Injection.md) | API key proxy via gateway |
| [Declarative Setup Architecture](adr/implemented/ADR-Declarative-Setup-Architecture.md) | Python-based declarative setup |
| [Standardized Logging Interface](adr/implemented/ADR-Standardized-Logging-Interface.md) | Structured JSON logging |
| [Internet Tool Access Lockdown](adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) | Public/private network modes |
| [GitHub Actions Support](adr/in-progress/ADR-GitHub-Actions-Support.md) | Running egg as a GitHub Action |

### Strategy

| Document | Description |
|----------|-------------|
| [Why egg Works](collaboration-effectiveness.md) | How the public, sandboxed, async model delivers safety, quality, and collaboration |

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design and security model |

### Development

| Document | Description |
|----------|-------------|
| [Project Structure](development/STRUCTURE.md) | Directory conventions and organization |
| [Contributing](../CONTRIBUTING.md) | Development setup, workflow, and PR process |

### Guides

| Document | Description |
|----------|-------------|
| [Agent-Mode Design](guides/agent-mode-design.md) | When to let egg operate freely vs. when constraints are appropriate |
| [GitHub Automation](guides/github-automation.md) | Built-in review bots, autofixer, @mention, and self-improvement workflows |

### SDLC Pipeline Templates

| Document | Description |
|----------|-------------|
| [Analysis Template](templates/analysis.md) | Problem analysis template for the refine phase |
| [Plan Template](templates/plan.md) | Implementation plan template with task ID format for the plan phase |

## Component Documentation

Each major component has its own README with detailed documentation:

| Component | Location | Description |
|-----------|----------|-------------|
| [Gateway Sidecar](../gateway/README.md) | `gateway/` | Policy enforcement, credential injection, API endpoints |
| [Sandbox Container](../sandbox/README.md) | `sandbox/` | Agent environment, tools, entrypoint |
| [Shared Libraries](../shared/README.md) | `shared/` | Config, logging, git utilities, and SDLC contracts |
| [Configuration](../config/README.md) | `config/` | Repository and host configuration |
| [CLI Entry Points](../bin/README.md) | `bin/` | `egg` and `setup-gateway` commands |
| [GitHub Action](../action/README.md) | `action/` | Composite action for GitHub Actions |
| [Claude Code Config](../sandbox/.claude/README.md) | `sandbox/.claude/` | Agent rules and slash commands |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Gateway changes** | [Architecture Overview](architecture/README.md) | [ADR: Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md), [Gateway README](../gateway/README.md) |
| **Security-related changes** | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | [Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |
| **Sandbox changes** | [Sandbox README](../sandbox/README.md) | [Architecture Overview](architecture/README.md) |
| **Configuration changes** | [Config README](../config/README.md) | [egg_config README](../shared/egg_config/README.md) |
| **GitHub Action setup** | [ADR: GitHub Actions](adr/in-progress/ADR-GitHub-Actions-Support.md) | [Architecture Overview](architecture/README.md) |
| **Adding tests** | [Contributing](../CONTRIBUTING.md) | [Project Structure](development/STRUCTURE.md) |
| **Setting up GitHub automation** | [GitHub Automation](guides/github-automation.md) | [Agent-Mode Design](guides/agent-mode-design.md), [GitHub Action](../action/README.md) |
| **Designing agent workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) |
| **Adding bot workflows** | [Agent-Mode Design](guides/agent-mode-design.md) | [Action README](../action/README.md), existing workflows in `.github/workflows/` |
| **SDLC pipeline changes** | [Architecture Overview](architecture/README.md) | [Plan Template](templates/plan.md), [Analysis Template](templates/analysis.md), workflows in `.github/workflows/sdlc-*.yml` |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview and quick start
2. [Contributing](../CONTRIBUTING.md) - Development setup

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
3. [Project Structure](development/STRUCTURE.md) - Code organization

---

*Last updated: 2026-02-07*
