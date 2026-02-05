# egg Feature List

> **Purpose:** This list documents the features available in egg for human-guided autonomous software development.
>
> **Total Features:** 22 top-level features

## Table of Contents

- [Communication](#communication)
- [Context Management](#context-management)
- [GitHub Integration](#github-integration)
- [Custom Commands](#custom-commands)
- [LLM Interface](#llm-interface)
- [Container Infrastructure](#container-infrastructure)
- [Utilities](#utilities)
- [Security Features](#security-features)
- [Configuration](#configuration)

---

## Communication

### 1. Slack Notifier Service
**Location:**
- `host-services/slack/slack-notifier/slack-notifier.py`
- `host-services/slack/slack-notifier/slack-notifier.service`
- `host-services/slack/slack-notifier/setup.sh`
- `bin/setup-slack-notifier`

Host-side systemd service that monitors ~/.egg-sharing/notifications/ and sends Slack DMs when files are created. Supports thread replies via YAML frontmatter, message batching (15-second window), auto-chunking for long content, and automatic retry.

### 2. Slack Receiver Service
**Location:**
- `host-services/slack/slack-receiver/slack-receiver.py`
- `host-services/slack/slack-receiver/slack-receiver.service`
- `host-services/slack/slack-receiver/setup.sh`
- `bin/setup-slack-receiver`

Receives incoming Slack DMs via Socket Mode and triggers egg container processing via egg --exec. Supports thread context detection, conversation history, user authentication/allowlisting, and remote control commands.

**Components:**

- **Remote Control via Slack** (`host-services/slack/slack-receiver/host_command_handler.py`)
  - Enables egg management through Slack DM commands: /egg status/restart/rebuild/logs and /service list/status/restart/start/stop/logs.
- **Slack Thread Context Preservation** (`host-services/slack/slack-receiver/slack-receiver.py`)
  - Maintains full conversation history for Slack threads by fetching all messages and including them in task files with YAML frontmatter.
- **Slack User Authentication** (`host-services/slack/slack-receiver/slack-receiver.py`)
  - Validates incoming Slack messages against a configurable list of allowed users, blocking unauthorized requests.
- **Container Process Monitoring** (`host-services/slack/slack-receiver/slack-receiver.py`)
  - Monitors egg container processes in background threads, streams output to logs, and creates failure notifications on errors or timeouts.
- **Message Chunking** (`host-services/slack/slack-receiver/slack-receiver.py`)
  - Automatically splits long messages into multiple chunks within Slack limits, breaking on natural boundaries.

### 3. Slack Message Processor
**Location:** `sandbox/egg-tasks/slack/incoming-processor.py`

Container-side processor for incoming Slack messages that routes them to Claude Code for task execution. Handles thread context, YAML frontmatter parsing, and automatic notifications for success/failure states.

### 4. Container Notifications Library
**Location:** `shared/notifications.py`

Python library for sending Slack notifications from within the container. Supports simple notifications, context for threading, and specialized notifications for PRs and code pushes.

## Context Management

### 5. Context Sync Service
**Location:**
- `host-services/sync/context-sync/sync_all.py`
- `host-services/sync/context-sync/manage_scheduler.sh`
- `host-services/sync/context-sync/context-sync.service`
- `host-services/sync/context-sync/setup.sh`

Multi-connector tool that automatically syncs external knowledge sources (Confluence, JIRA) to ~/context-sync/ for AI agent access. Runs hourly via systemd timer, supports incremental sync, and provides search functionality.

**Components:**

- **Base Connector Framework** (`host-services/sync/context-sync/connectors/base.py`)
  - Abstract base class defining standard interface for all sync connectors with config validation, sync operations, and cleanup.
- **Systemd Timer Scheduler** (`host-services/sync/context-sync/systemd/context-sync.service`)
  - Automated scheduling using systemd user timers for reliable hourly documentation syncing with configurable frequency.

### 6. Confluence Connector
**Location:**
- `host-services/sync/context-sync/connectors/confluence/connector.py`
- `host-services/sync/context-sync/connectors/confluence/sync.py`
- `host-services/sync/context-sync/connectors/confluence/config.py`

Syncs Confluence documentation including ADRs, runbooks, and team docs to local markdown files. Preserves page hierarchy, includes comments, creates hierarchical navigation indexes, and supports incremental sync.

### 7. JIRA Connector
**Location:**
- `host-services/sync/context-sync/connectors/jira/connector.py`
- `host-services/sync/context-sync/connectors/jira/sync.py`
- `host-services/sync/context-sync/connectors/jira/config.py`

Syncs JIRA tickets to local markdown files based on configurable JQL queries. Includes ticket comments, attachment metadata, work logs, and converts Atlassian Document Format to clean markdown with incremental sync support.

### 8. JIRA Ticket Processor
**Location:** `sandbox/egg-tasks/jira/jira-processor.py`

Monitors and analyzes JIRA tickets assigned to the user, using Claude to parse requirements, extract action items, assess scope, and send proactive Slack notifications.

### 9. Sprint Ticket Analyzer
**Location:** `sandbox/egg-tasks/jira/analyze-sprint.py`

Analyzes tickets in the active sprint to provide actionable recommendations including next steps and suggestions for backlog tickets to pull in. Generates grouped Slack notifications.

## GitHub Integration

### 10. GitHub Command Handler
**Location:** `sandbox/egg-tasks/github/command-handler.py`, `sandbox/egg-tasks/github/README.md`

Processes user commands received via Slack for GitHub operations like 'review PR 123' or '/pr review 123 webapp'. Parses commands and delegates to appropriate handlers.

### 11. GitHub Processor
**Location:** `sandbox/egg-tasks/github/github-processor.py`

Container-side processor for GitHub-related tasks triggered via Slack commands.

### 12. GitHub App Token Generator
**Location:** `sandbox/tools/github-app-token.py`

Generates short-lived (1 hour) GitHub App installation access tokens from stored credentials. Used by egg launcher to authenticate gh CLI and git operations without SSH keys.

## Custom Commands

### 13. Claude Custom Commands
**Location:** `sandbox/.claude/commands/README.md`

Slash command system for common agent operations including task status and metrics display.

**Components:**

- **Show Metrics Command** (`sandbox/.claude/commands/show-metrics.md`)
  - Generates monitoring reports with API usage, task completion statistics, and optimization insights.

## LLM Interface

### 14. Claude Code Integration
**Location:**
- `sandbox/llm/__init__.py`
- `sandbox/llm/config.py`
- `sandbox/llm/runner.py`
- `sandbox/llm/result.py`
- `sandbox/llm/claude/`

Claude Code interface providing both interactive and programmatic access to Claude models. Supports API key authentication and OAuth login. All egg-tasks use `from llm import run_agent` for consistent LLM interactions.

**Components:**

- **Interactive Mode** (`sandbox/llm/runner.py`)
  - Launches Claude Code CLI with `--dangerously-skip-permissions` for autonomous operation in the sandboxed container.
- **Programmatic Mode** (`sandbox/llm/claude/runner.py`)
  - Claude Agent SDK integration for non-interactive task execution with streaming output support.
- **Result Handling** (`sandbox/llm/result.py`)
  - `AgentResult` dataclass providing standardized success/error status, stdout/stderr capture, and return codes.
- **Authentication**
  - API key via `ANTHROPIC_API_KEY` environment variable
  - OAuth via `ANTHROPIC_AUTH_METHOD=oauth` for Claude's built-in OAuth flow

## Container Infrastructure

### 15. Egg Container Management System
**Location:**
- `bin/egg`
- `bin/view-logs`
- `host-services/shared/egg_exec.py`
- `host-services/shared/__init__.py`

The core 'egg' command provides the primary interface for starting, managing, and interacting with the sandboxed Docker development environment. Includes container lifecycle management, log viewing, and the egg --exec mechanism for host-to-container task execution.

**Components:**

- **Egg Execution Wrapper** (`host-services/shared/egg_exec.py`)
  - Standardized interface for host services to execute container-side processors via egg --exec, handling path translation, JSON parsing, and timeout management.
- **Container Log Viewer** (`bin/view-logs`)
  - Provides convenient access to Docker container logs for debugging and monitoring container activity.

### 16. Docker Development Environment Setup
**Location:** `bin/docker-setup.py`

Automates complete installation of development tools in the Docker container, including Python 3.11, Node.js 20.x, Go, Java 11, PostgreSQL, Redis, and various development utilities with cross-platform support for Ubuntu and Fedora.

### 17. Container Directory Communication System
**Location:** `sandbox/README.md`

Shared directory structure enabling communication between container and host including notifications (agent -> human), incoming (human -> agent), responses, and context directories.

## Utilities

### 18. Documentation Search Utility
**Location:** `host-services/sync/context-sync/utils/search.py`

Provides local full-text search across all synced documentation with context and relevance ranking. Supports filtering by space, case-sensitive search, and statistics display.

### 19. Sync Maintenance Tools
**Location:** `host-services/sync/context-sync/utils/maintenance.py`

Provides sync status monitoring showing statistics across spaces and pages, and cleanup utilities to find and remove orphaned files.

### 20. Test Discovery Tool
**Location:** `sandbox/scripts/discover-tests.py`, `sandbox/tools/discover-tests.py`

Dynamically discovers test configurations and frameworks in any codebase. Supports Python (pytest/unittest), JavaScript (Jest/Mocha/Vitest/Playwright), Go, and Java (Gradle/Maven). Provides recommended test commands.

## Security Features

### 21. In-Memory Token Refresh
**Location:** `gateway/token_refresher.py`

The gateway sidecar manages GitHub App installation tokens in-memory, automatically refreshing them 15 minutes before expiry. Features include:
- Thread-safe token caching
- Graceful degradation on refresh failure (uses cached token up to 3 consecutive failures)
- Fail-closed behavior after max failures (clears cache to prevent stale token use)

## Configuration

### 22. Master Setup System
**Location:** `setup.sh`

Comprehensive installation and configuration script for all egg host components. Handles initial setup, updates, and force reinstalls with interactive prompts, dependency checking, service management, and configuration validation.

**Components:**

- **Dependency Management** (`setup.sh`)
  - Automated detection and installation of required dependencies including Python (uv), Go, and Docker.
- **Systemd Service Management** (`setup.sh`)
  - Manages systemd user services for all egg components including daemon reload, service restart, and status monitoring.
- **Shared Directory Structure Setup** (`setup.sh`)
  - Creates and manages the shared directory structure (~/.egg-sharing) for notifications, incoming messages, responses, and context data.
- **GitHub App Authentication Setup** (`setup.sh`)
  - Interactive wizard for configuring GitHub App authentication including App ID, Installation ID, and private key management.
- **Slack Integration Configuration** (`setup.sh`)
  - Validates and configures Slack bot tokens (SLACK_TOKEN) and app tokens (SLACK_APP_TOKEN) for bidirectional communication.
- **Docker Image Pre-Build** (`setup.sh`)
  - Pre-builds the egg Docker image during setup so the first 'egg' command runs quickly.

---

*Last Updated: 2026-01-28*
