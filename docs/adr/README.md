# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) documenting significant technical decisions for egg.

## What is an ADR?

An ADR captures the context, decision, and consequences of an architectural choice. They help future contributors understand why the system is designed the way it is.

## Directory Structure

ADRs are organized by implementation status:

- **`in-progress/`** - ADRs actively being implemented
- **`implemented/`** - ADRs fully implemented and in production
- **`not-implemented/`** - ADRs proposed but not yet started

## ADR Index

### Implemented

| ADR | Summary |
|-----|---------|
| [Anthropic API Credential Injection](implemented/ADR-Anthropic-API-Credential-Injection.md) | Use ANTHROPIC_BASE_URL to route API traffic through gateway for credential injection |
| [Context Sync Strategy](implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) | Hybrid approach: GitHub MCP implemented ✅, JIRA MCP pending ❌, Confluence sync retained |
| [Declarative Setup Architecture](implemented/ADR-Declarative-Setup-Architecture.md) | Python-based declarative setup replacing bash scripts |
| [Git Isolation Architecture](implemented/ADR-Git-Isolation-Architecture.md) | Gateway sidecar design for credential isolation |
| [Standardized Logging Interface](implemented/ADR-Standardized-Logging-Interface.md) | Structured JSON logging with GCP compatibility |

### In Progress

| ADR | Summary |
|-----|---------|
| [Autonomous Software Engineer](in-progress/ADR-Autonomous-Software-Engineer.md) | Core system architecture, security model, operating principles |
| [Internet Tool Access Lockdown](in-progress/ADR-Internet-Tool-Access-Lockdown.md) | Security restrictions on agent network access (Phase 1 implemented, Phase 2 planned) |

### Not Implemented

| ADR | Summary |
|-----|---------|
| [Per-Container Repository Mode](not-implemented/ADR-Per-Container-Repository-Mode.md) | Session-based per-container repository mode management |

## ADR Template

When creating a new ADR, include:

1. **Title** - Clear, descriptive name
2. **Status** - Proposed, Accepted, Deprecated, Superseded
3. **Context** - What problem are we solving?
4. **Decision** - What did we decide?
5. **Consequences** - What are the trade-offs?

### Important Guidelines

**DO NOT include time-based estimates** in ADRs (e.g., "Week 1-2", "Phase 1 takes 3 weeks"). Time estimates are not reliable or relevant for LLM-assisted development where task completion depends on many unpredictable factors. Instead:

- Use **phase numbering** without time estimates (Phase 1, Phase 2, etc.)
- Define **success criteria** for each phase
- List **dependencies** between phases
- Focus on **what** needs to be done, not **when**

Place new ADRs in `not-implemented/` until work begins.

## See Also

- [Architecture Overview](../architecture/README.md)
- [Documentation Index](../index.md)
