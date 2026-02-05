# Egg Documentation

Complete documentation for egg: a hardened sandbox for autonomous LLM code agents.

> **For LLMs**: Start with the [Documentation Index](index.md) for efficient navigation.

## Documentation Structure

### [Setup](setup/)
Installation and configuration guides.

- **[Setup Overview](setup/README.md)** - Quick start and prerequisites
- **[GitHub App Setup](setup/github-app-setup.md)** - GitHub App permissions and installation
- **[GitHub Auth Comparison](setup/github-auth-comparison.md)** - Auth method options

### [Architecture](architecture/)
System design and technical details.

- **[Overview](architecture/README.md)** - Gateway + sandbox architecture

### [Features](features/)
Feature documentation and capabilities.

- **[Container Infrastructure](features/container-infrastructure.md)** - Container management and development environment
- **[GitHub Action](features/github-action.md)** - Running egg in GitHub Actions CI/CD

### [Guides](guides/)
How-to guides for specific tasks.

- **[@mention Trigger Setup](guides/mention-trigger-setup.md)** - Trigger egg via GitHub @mentions

### [Reference](reference/)
Quick reference guides.

- **[Log Persistence](reference/log-persistence.md)** - Container log persistence and correlation

### [Development](development/)
For contributors and developers.

- **[Project Structure](development/STRUCTURE.md)** - Directory conventions and guidelines
- **[Contributing](../CONTRIBUTING.md)** - Development setup and PR process

### [ADRs](adr/)
Architecture Decision Records.

- **[ADR Index](adr/README.md)** - All decisions and their status
- **[Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md)** - Main system architecture

### [Troubleshooting](troubleshooting/)
Common issues and solutions.

- **[GitHub Auth Issues](troubleshooting/github-auth-in-long-running-containers.md)** - Token expiry and refresh

### [Plans](plans/)
Implementation plans for future work.

- **[GitHub Actions Implementation](plans/github-actions-implementation-plan.md)** - GitHub Actions integration plan

## Component READMEs

Each component directory contains its own README with detailed documentation:

- **[Gateway](../gateway/README.md)** - Policy enforcement gateway (API endpoints, policy rules)
- **[Sandbox](../sandbox/README.md)** - Agent container (entrypoint, tools, wrappers)
- **[Shared Libraries](../shared/README.md)** - Reusable libraries (config, logging, git)
- **[Configuration](../config/README.md)** - Repository and host configuration
- **[GitHub Action](../action/README.md)** - Composite action for CI/CD
- **[CLI](../bin/README.md)** - CLI entry points

## Quick Links

**Getting Started:**
1. [Main README](../README.md) - Project overview
2. [Setup Overview](setup/README.md) - Installation and configuration

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
3. [Project Structure](development/STRUCTURE.md) - Code organization
