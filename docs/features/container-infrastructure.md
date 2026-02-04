# Container Infrastructure Features

Core egg container management and development environment.

## Overview

The egg container provides a sandboxed development environment:
- **Container Management**: Build, run, exec operations
- **Custom Commands**: Slash commands for common operations

## Features

### Claude Custom Commands

**Purpose**: Slash command system for common agent operations including context management, PR creation, task status, and metrics display.

**Location**: `sandbox/.claude/commands/README.md`

**Components**:
- **Load Context Command** (`sandbox/.claude/commands/load-context.md`)
- **Save Context Command** (`sandbox/.claude/commands/save-context.md`)
- **Create PR Command** (`sandbox/.claude/commands/create-pr.md`)
- **Beads Status Command** (`sandbox/.claude/commands/beads-status.md`)
- **Beads Sync Command** (`sandbox/.claude/commands/beads-sync.md`)
- **Update Confluence Doc Command** (`sandbox/.claude/commands/update-confluence-doc.md`)
- **Show Metrics Command** (`sandbox/.claude/commands/show-metrics.md`)

### Egg Container Management System

**Purpose**: The core 'egg' command provides the primary interface for starting, managing, and interacting with the sandboxed Docker development environment. Includes container lifecycle management and log viewing.

**Location**:
- `bin/egg`
- `host-services/shared/egg_exec.py`
- `host-services/shared/__init__.py`

**Components**:
- **Egg Execution Wrapper** (`host-services/shared/egg_exec.py`)

### Docker Development Environment Setup

**Purpose**: Automates complete installation of development tools in the Docker container, including Python 3.11, Node.js 20.x, Go, Java 11, PostgreSQL, Redis, and various development utilities with cross-platform support for Ubuntu and Fedora.

**Location**: `bin/docker-setup.py`

### Container Directory Communication System

**Purpose**: Shared directory structure enabling communication between container and host including notifications (agent -> human), incoming (human -> agent), responses, and context directories.

**Location**: `sandbox/README.md`

## Related Documentation

- [Environment Reference](../../sandbox/.claude/rules/environment.md)
- [Mission Guide](../../sandbox/.claude/rules/mission.md)

## Source Files

| Component | Path |
|-----------|------|
| Claude Custom Commands | `sandbox/.claude/commands/README.md` |
| Egg Container Management System | `bin/egg` |
| Docker Development Environment Setup | `bin/docker-setup.py` |
| Container Directory Communication System | `sandbox/README.md` |
