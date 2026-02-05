# Container Infrastructure Features

Core egg container management and development environment.

## Overview

The egg container provides a sandboxed development environment:
- **Container Management**: Build, run, exec operations via the `egg` CLI
- **Custom Commands**: Slash commands for common agent operations
- **Gateway Integration**: All git/GitHub operations route through the gateway sidecar

## Features

### Claude Custom Commands

**Purpose**: Slash command system for common agent operations including metrics display.

**Location**: [`sandbox/.claude/commands/`](../../sandbox/.claude/commands/README.md)

**Available commands:**
- `/show-metrics` - Generate activity report

### Container Management System

**Purpose**: The `egg` CLI provides the primary interface for starting, managing, and interacting with the sandboxed Docker environment. Includes container lifecycle management, log viewing, and configuration validation.

**Location**:
- [`sandbox/egg`](../../sandbox/egg) - Main egg CLI script
- [`sandbox/egg_lib/`](../../sandbox/egg_lib/) - CLI library modules

**Key modules:**
- `cli.py` - CLI command handling and argument parsing
- `config.py` - Configuration management
- `docker.py` - Docker container operations
- `gateway.py` - Gateway sidecar communication
- `network_mode.py` - Public/private network mode handling

### Docker Development Environment Setup

**Purpose**: Automates installation of development tools in the Docker container, including Python, Node.js, Go, Java, PostgreSQL, Redis, and development utilities.

**Location**: [`sandbox/docker-setup.py`](../../sandbox/docker-setup.py)

### Container Entrypoint

**Purpose**: Orchestrates container startup including environment configuration, gateway connectivity, Claude Code launch, and session management.

**Location**: [`sandbox/entrypoint.py`](../../sandbox/entrypoint.py)

### Git/GitHub Wrappers

**Purpose**: Intercept all git and GitHub CLI commands, routing them through the gateway sidecar for credential injection and policy enforcement.

**Location**: [`sandbox/bin/`](../../sandbox/bin/)
- `git` - Git wrapper (routes push, fetch, clone through gateway)
- `gh` - GitHub CLI wrapper (routes PR/issue operations through gateway)
- `git-credential-github-token` - Credential helper for git operations

### Container Directory Communication

**Purpose**: Shared directory structure enabling communication between container and host.

**Location**: Documented in [`sandbox/README.md`](../../sandbox/README.md)

```
~/sharing/                    # Shared with host
├── notifications/           # Agent -> Human (notifications)
├── incoming/                # Human -> Agent (tasks)
├── responses/               # Human -> Agent (responses)
└── context/                 # Persistent knowledge across rebuilds

~/context-sync/              # Read-only context (mounted from host)
├── confluence/             # Confluence documentation
└── jira/                   # JIRA tickets
```

## Related Documentation

- [Sandbox README](../../sandbox/README.md) - Full sandbox documentation
- [Claude Code Configuration](../../sandbox/.claude/README.md) - Agent rules and commands
- [Architecture Overview](../architecture/README.md) - System design

## Source Files

| Component | Path |
|-----------|------|
| Claude Custom Commands | [`sandbox/.claude/commands/`](../../sandbox/.claude/commands/) |
| Container Management | [`sandbox/egg`](../../sandbox/egg), [`sandbox/egg_lib/`](../../sandbox/egg_lib/) |
| Docker Setup | [`sandbox/docker-setup.py`](../../sandbox/docker-setup.py) |
| Container Entrypoint | [`sandbox/entrypoint.py`](../../sandbox/entrypoint.py) |
| Git/GitHub Wrappers | [`sandbox/bin/`](../../sandbox/bin/) |
| LLM Integration | [`sandbox/llm/`](../../sandbox/llm/) |
