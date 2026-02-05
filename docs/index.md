# Documentation Index

> egg: LLM-powered guided autonomous software engineering agent in a Docker sandbox

This index helps both humans and LLMs navigate the documentation efficiently.

## Core Documentation

### Architecture Decision Records (ADRs)

| Document | Description |
|----------|-------------|
| [ADR Overview](adr/README.md) | Index of all ADRs and their status |
| [Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | Core system architecture, security model, and design decisions |
| [Context Sync Strategy](adr/implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) | How external data (Confluence, JIRA, GitHub) is synced |
| [Git Isolation Architecture](adr/implemented/ADR-Git-Isolation-Architecture.md) | Gateway sidecar design for credential isolation |

### Strategy

| Document | Description |
|----------|-------------|
| [Collaboration Effectiveness](collaboration-effectiveness.md) | Why egg's public, async model enables team collaboration with AI |

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design and security model |

### Setup Guides

| Document | Description |
|----------|-------------|
| [Setup Overview](setup/README.md) | Installation and configuration summary |
| [GitHub App Setup](setup/github-app-setup.md) | GitHub App permissions and installation |

### Reference

| Document | Description |
|----------|-------------|
| [Log Persistence](reference/log-persistence.md) | Container log persistence and correlation |

### Development

| Document | Description |
|----------|-------------|
| [Project Structure](development/STRUCTURE.md) | Directory conventions and organization |

### Troubleshooting

| Document | Description |
|----------|-------------|
| [GitHub Auth Issues](troubleshooting/github-auth-in-long-running-containers.md) | Token expiry and refresh in containers |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Gateway changes** | [Architecture Overview](architecture/README.md) | [ADR: Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |
| **Security-related changes** | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | [Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |
| **@mention trigger setup** | [Mention Trigger Setup](guides/mention-trigger-setup.md) | [Architecture Overview](architecture/README.md) |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview
2. [Setup Overview](setup/README.md) - Installation guide

**Understanding the System:**
1. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
2. [Architecture Overview](architecture/README.md) - Component design
3. [Project Structure](development/STRUCTURE.md) - Code organization

---

*Last updated: 2026-02-05*
