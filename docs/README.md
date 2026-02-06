# Egg Documentation

Complete documentation for egg: a hardened sandbox for autonomous LLM code agents.

> **For LLMs**: Start with the [Documentation Index](index.md) for efficient navigation.

## Documentation Structure

### [Architecture](architecture/)
System design and technical details.

- **[Overview](architecture/README.md)** - Gateway + sandbox architecture

### [Development](development/)
For contributors and developers.

- **[Project Structure](development/STRUCTURE.md)** - Directory conventions and guidelines
- **[Contributing](../CONTRIBUTING.md)** - Development setup and PR process

### [ADRs](adr/)
Architecture Decision Records.

- **[ADR Index](adr/README.md)** - All decisions and their status
- **[Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md)** - Main system architecture

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

**Understanding the System:**
1. [Architecture Overview](architecture/README.md) - Component design
2. [ADR: Autonomous SE](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture
3. [Project Structure](development/STRUCTURE.md) - Code organization
