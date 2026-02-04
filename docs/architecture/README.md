# Architecture Documentation

Technical design and system architecture.

## Documents

### [Slack Integration](slack-integration.md)
Detailed Slack bidirectional messaging design.

**Covers:**
- Notification flow (agent → Slack)
- Incoming message flow (Slack → agent)
- Thread-based conversations
- File watching and triggers

### [Host Slack Notifier](host-slack-notifier.md)
Implementation details of the Slack notification system.

**Covers:**
- Notifier architecture
- Message formatting
- Error handling
- Systemd integration

## Key Architectural Decisions

**Sandboxing:**
- Docker container isolation
- No credentials in container
- Read-write mount for code
- Network: outbound only

**Communication:**
- Slack for human interaction
- File-based for agent output
- Systemd for service management

**Security:**
- Credential isolation
- No git push from container
- No cloud deployment from container
- Human reviews and approves all changes

## See Also
- [ADR: Autonomous Software Engineer](../adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full system architecture
- [Setup Guides](../setup/) - Installation and configuration
