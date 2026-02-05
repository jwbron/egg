# Egg Documentation

Complete documentation for egg: Docker sandbox for Claude Code CLI as an autonomous software engineering agent.

> **For LLMs**: Start with the [Documentation Index](index.md) for efficient navigation.

> **Note**: Documentation should generally live close to code in service directories (e.g., `host-services/slack-notifier/README.md`). This directory is for general, cross-cutting documentation only.

## Documentation Structure

### [Setup](setup/)
Initial installation and configuration guides.

- **[GitHub App Setup](setup/github-app-setup.md)** - GitHub App permissions and installation
- **Slash Commands** - See [.claude/commands](../sandbox/.claude/commands/README.md)

### [Architecture](architecture/)
System design and technical details.

- **[Overview](architecture/README.md)** - High-level system architecture

### [Reference](reference/)
Quick reference guides and troubleshooting.

- **[Log Persistence](reference/log-persistence.md)** - Container log persistence and correlation

### [Development](development/)
For contributors and developers.

- **[Project Structure](development/STRUCTURE.md)** - Directory conventions and guidelines
- **Contributing Guide** (planned)
- **Testing** (planned)

### [ADRs](adr/)
Architecture Decision Records.

- **[Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md)** - Main system architecture

## Quick Links

**Getting Started:**
1. Run `./setup.py` in project root
2. [Setup Overview](setup/README.md) - Installation and configuration
3. Start container: `bin/egg`

**Common Tasks:**
- [Viewing Logs](../bin/README.md)

**Architecture:**
- [Main README](../README.md) - Project overview
- [ADR](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture details
