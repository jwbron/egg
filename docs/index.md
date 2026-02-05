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

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design and security model |

### Setup Guides

| Document | Description |
|----------|-------------|
| [Setup Overview](setup/README.md) | Installation and configuration summary |
| [GitHub App Setup](setup/github-app-setup.md) | GitHub App permissions and installation |
| [GitHub Auth Comparison](setup/github-auth-comparison.md) | Auth method options (GitHub App vs PAT) |

### Features

| Document | Description |
|----------|-------------|
| [Container Infrastructure](features/container-infrastructure.md) | Container management and development environment |
| [GitHub Action](features/github-action.md) | Running egg in GitHub Actions CI/CD |

### Guides

| Document | Description |
|----------|-------------|
| [@mention Trigger Setup](guides/mention-trigger-setup.md) | Trigger egg via GitHub @mentions |

### Reference

| Document | Description |
|----------|-------------|
| [Log Persistence](reference/log-persistence.md) | Container log persistence and correlation |

### Development

| Document | Description |
|----------|-------------|
| [Project Structure](development/STRUCTURE.md) | Directory conventions and organization |
| [Contributing](../CONTRIBUTING.md) | Development setup, workflow, and PR process |

### Troubleshooting

| Document | Description |
|----------|-------------|
| [GitHub Auth Issues](troubleshooting/github-auth-in-long-running-containers.md) | Token expiry and refresh in containers |

## Component Documentation

Each major component has its own README with detailed documentation:

| Component | Location | Description |
|-----------|----------|-------------|
| [Gateway Sidecar](../gateway/README.md) | `gateway/` | Policy enforcement, credential injection, API endpoints |
| [Sandbox Container](../sandbox/README.md) | `sandbox/` | Agent environment, tools, entrypoint |
| [Shared Libraries](../shared/README.md) | `shared/` | Config, logging, and git utilities |
| [Configuration](../config/README.md) | `config/` | Repository and host configuration |
| [CLI Entry Points](../bin/README.md) | `bin/` | `egg` and `setup-gateway` commands |
| [GitHub Action](../action/README.md) | `action/` | Composite action for GitHub Actions |
| [Claude Code Config](../sandbox/.claude/README.md) | `sandbox/.claude/` | Agent rules and slash commands |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Gateway changes** | [Architecture Overview](architecture/README.md) | [ADR: Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md), [Gateway README](../gateway/README.md) |
| **Security-related changes** | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | [Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |
| **Sandbox changes** | [Sandbox README](../sandbox/README.md) | [Container Infrastructure](features/container-infrastructure.md) |
| **Configuration changes** | [Config README](../config/README.md) | [egg_config README](../shared/egg_config/README.md) |
| **@mention trigger setup** | [Mention Trigger Setup](guides/mention-trigger-setup.md) | [Architecture Overview](architecture/README.md) |
| **GitHub Action setup** | [GitHub Action](features/github-action.md) | [ADR: GitHub Actions](adr/in-progress/ADR-GitHub-Actions-Support.md) |
| **Adding tests** | [Contributing](../CONTRIBUTING.md) | [Project Structure](development/STRUCTURE.md) |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview and quick start
2. [Setup Overview](setup/README.md) - Installation and configuration
3. [Contributing](../CONTRIBUTING.md) - Development setup

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
3. [Project Structure](development/STRUCTURE.md) - Code organization

**Operating egg:**
1. [GitHub Action](features/github-action.md) - CI/CD integration
2. [@mention Trigger Setup](guides/mention-trigger-setup.md) - Trigger via comments
3. [Log Persistence](reference/log-persistence.md) - Log management
4. [Troubleshooting](troubleshooting/github-auth-in-long-running-containers.md) - Common issues

---

*Last updated: 2026-02-05*
