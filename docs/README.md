# Egg Documentation

Complete documentation for egg: Docker sandbox for Claude Code CLI as an autonomous software engineering agent.

> **For LLMs**: Start with the [Documentation Index](index.md) for efficient navigation.
> This documentation follows the [llms.txt](https://llmstxt.org/) standard - see [llms.txt](llms.txt).

> **Note**: Documentation should generally live close to code in service directories (e.g., `host-services/slack-notifier/README.md`). This directory is for general, cross-cutting documentation only.

## Documentation Structure

### [Setup](setup/)
Initial installation and configuration guides.

- **[Slack Quickstart](setup/slack-quickstart.md)** - Get notifications working in 10 minutes
- **[Slack App Setup](setup/slack-app-setup.md)** - Detailed Slack app configuration
- **[Bidirectional Setup](setup/slack-bidirectional.md)** - Two-way Slack communication

### [User Guide](user-guide/)
How to use egg day-to-day.

- **[Overview](user-guide/README.md)** - Common tasks and workflow summary
- **Slash Commands** - See [.claude/commands](../sandbox/.claude/commands/README.md)

### [Architecture](architecture/)
System design and technical details.

- **[Overview](architecture/README.md)** - High-level system architecture
- **[Slack Integration](architecture/slack-integration.md)** - Bidirectional messaging design
- **[Host Notifier](architecture/host-slack-notifier.md)** - Notification system details

### [Reference](reference/)
Quick reference guides and troubleshooting.

- **[Slack Quick Reference](reference/slack-quick-reference.md)** - Common Slack operations

### [Development](development/)
For contributors and developers.

- **[Project Structure](development/STRUCTURE.md)** - Directory conventions and guidelines
- **Contributing Guide** (planned)
- **Testing** (planned)

### [ADRs](adr/)
Architecture Decision Records.

- **[Autonomous Software Engineer](adr/in-progress/ADR-Autonomous-Software-Engineer.md)** - Main system architecture
- **[Message Queue Integration](adr/not-implemented/ADR-Message-Queue-Slack-Integration.md)** - Pub/Sub migration plan

## Quick Links

**Getting Started:**
1. Run `./setup.sh` in project root
2. [Slack Setup](setup/slack-quickstart.md) - Configure Slack integration
3. Start container: `bin/egg`

**Common Tasks:**
- [Managing Notifications](reference/slack-quick-reference.md)
- [Viewing Logs](../bin/README.md)

**Architecture:**
- [Main README](../README.md) - Project overview
- [Slack Integration](architecture/slack-integration.md)
- [ADR](adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full architecture details
