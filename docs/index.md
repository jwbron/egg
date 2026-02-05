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

### Architecture

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | High-level system design, components, data flows |
| [Host-Container Boundary](architecture/host-container-boundary.md) | Security boundary between host services and container |
| [Slack Integration](architecture/slack-integration.md) | Bidirectional Slack messaging design |

### Setup Guides

| Document | Description |
|----------|-------------|
| [Setup Overview](setup/README.md) | Installation and configuration summary |
| [Slack Quickstart](setup/slack-quickstart.md) | Get Slack notifications working quickly |
| [Slack App Setup](setup/slack-app-setup.md) | Detailed Slack app configuration |
| [GitHub App Setup](setup/github-app-setup.md) | GitHub App permissions and installation |

### Reference

| Document | Description |
|----------|-------------|
| [Features - Source Mapping](FEATURES.md) | Map of all features to their implementation locations |
| [Slack Quick Reference](reference/slack-quick-reference.md) | Common Slack operations and commands |
| [Log Persistence](reference/log-persistence.md) | Container log persistence and correlation |

### Development

| Document | Description |
|----------|-------------|
| [Project Structure](development/STRUCTURE.md) | Directory conventions and organization |

### Troubleshooting

| Document | Description |
|----------|-------------|
| [GitHub Auth Issues](troubleshooting/github-auth-in-long-running-containers.md) | Token expiry and refresh in containers |

### Audits & Reviews

| Document | Description |
|----------|-------------|
| [Audit Reports Index](audits/README.md) | Repository audits and reviews |
| [Migration Audit (2026-02)](audits/2026-02-migration-audit.md) | Post james-in-a-box migration review |
| [Issues Checklist](audits/issues-checklist.md) | Actionable remediation checklist |

## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Slack integration changes** | [Slack Integration](architecture/slack-integration.md) | [Slack Quick Reference](reference/slack-quick-reference.md) |
| **Adding new host services** | [Architecture Overview](architecture/README.md) | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) |
| **Security-related changes** | [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) | [Host-Container Boundary](architecture/host-container-boundary.md), [Git Isolation](adr/implemented/ADR-Git-Isolation-Architecture.md) |

## Quick Navigation

**Getting Started:**
1. [Main README](../README.md) - Project overview
2. [Setup Overview](setup/README.md) - Installation guide

**Understanding the System:**
1. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
2. [Architecture Overview](architecture/README.md) - Component design
3. [Project Structure](development/STRUCTURE.md) - Code organization

---

*Last updated: 2026-02-04*
