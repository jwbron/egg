# Architecture Documentation

Technical design and system architecture.

## System Overview

egg runs as two Docker containers working together:

- **Gateway sidecar** (trusted) - Holds credentials, enforces policies, proxies all external access
- **Sandbox container** (untrusted) - Where the LLM agent runs with no credentials and restricted network

See the [main README](../../README.md) for the architecture diagram.

## Key Design Principles

**Sandboxing:**
- Docker container isolation
- No credentials in sandbox container
- All git/GitHub operations proxied through gateway
- Network restricted via Squid proxy (private mode)

**Credential Isolation:**
- Gateway injects Anthropic API keys at proxy time
- Gateway injects GitHub tokens for git/gh operations
- Sandbox never sees raw credentials

**Access Control:**
- Branch ownership (agent can only push to `egg/*` branches)
- Phase-based operation restrictions (git/gh operations filtered by SDLC phase)
- No merge capability (gateway has no merge endpoint)
- Force push and destructive operations blocked

## Components

| Component | Role | Documentation |
|-----------|------|---------------|
| **Gateway** | Credential injection, policy enforcement, HTTP proxy | [Gateway README](../../gateway/README.md) |
| **Sandbox** | Agent execution environment, git/gh wrappers | [Sandbox README](../../sandbox/README.md) |
| **Shared Libraries** | Config, logging, git utilities | [Shared README](../../shared/README.md) |

## Key Architectural Decisions

See [ADR Overview](../adr/README.md) for the full list. Key decisions:

- [Git Isolation Architecture](../adr/implemented/ADR-Git-Isolation-Architecture.md) - Worktree isolation via gateway
- [Gateway Credential Injection](../adr/implemented/ADR-Gateway-Credential-Injection.md) - Zero-credential sandbox
- [Anthropic API Credential Injection](../adr/implemented/ADR-Anthropic-API-Credential-Injection.md) - API key proxy
- [Declarative Setup Architecture](../adr/implemented/ADR-Declarative-Setup-Architecture.md) - Python-based setup
- [Standardized Logging Interface](../adr/implemented/ADR-Standardized-Logging-Interface.md) - Structured JSON logging
- [Internet Tool Access Lockdown](../adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) - Public/private network modes

## Design Guidelines

- [Agent Mode Design](../guides/agent-mode-design.md) - When to let the agent operate freely vs. when constraints are appropriate

## See Also

- [ADR: Autonomous Software Engineer](../adr/in-progress/ADR-Autonomous-Software-Engineer.md) - Full system architecture
- [Setup Guides](../setup/) - Installation and configuration
- [Project Structure](../development/STRUCTURE.md) - Directory layout
