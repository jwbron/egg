# Claude Agent Rules

This directory contains instructions for the AI agent (Claude Code) operating in this sandboxed environment.

## How These Files Are Used

Claude Code reads `CLAUDE.md` files automatically when starting. During container startup, all rule files are combined into a single `CLAUDE.md`:

**Installation:**
- `~/CLAUDE.md` → All rules combined

**Why one file?** Since `~/repos/` is mounted from the host (not copied), we can't reliably write to it during container startup. Combining all rules into `~/CLAUDE.md` ensures they're always available.

**Note**: `CLAUDE.md` is the [official Claude Code format](https://www.anthropic.com/engineering/claude-code-best-practices) for providing context and instructions to the agent.

## File Guide

### Core Agent Instructions

- **mission.md** - Start here
  - Your role as autonomous software engineering agent
  - Operating model (you do implementation, human does review/deploy)
  - Workflow: beads → gather context → plan → implement → test → PR
  - Decision-making framework (when to proceed vs ask)
  - Quality standards and communication style

- **environment.md** - Technical constraints
  - Sandbox security model
  - GitHub CLI (`gh`) for PRs and issues
  - File system layout and access
  - Services and package installation

- **beads-usage.md** - Persistent task tracking (MANDATORY)
  - Quick reference for beads commands
  - Status flow and labeling conventions

- **context-tracking.md** - Persistent context for Slack/PR work
  - Slack thread context via task_id
  - GitHub PR context tracking
  - Integration with Beads

- **egg-branding.md** - Attribution and identity (IMPORTANT)
  - Commits/PRs must be attributed to "egg", not "Claude"
  - NEVER include "Generated with Claude Code" branding
  - GitHub comment sign-off format

### Code Standards

- **code-standards.md** - Tech stack and code standards
  - Technologies (Python, React, TypeScript)
  - Code style guidelines
  - Common commands

### Quality & Communication

- **pr-descriptions.md** - PR writing guidelines
  - Standard PR format
  - Length targets

- **test-workflow.md** - Test discovery and execution
  - Dynamic test discovery
  - Testing workflow integration

- **notification-template.md** - Async notification formatting
  - When to send notifications
  - Template and threading

## Reference Documentation

The following content has been moved to `docs/reference/` for on-demand access:

- **culture.md** - L3-L4 engineering behavioral standards
- **conversation-analysis-criteria.md** - Performance assessment dimensions

See `~/repos/egg/docs/index.md` for navigation to all documentation.

## Design Principles

These rules follow the [LLM Documentation Index Strategy](../../docs/adr/ADR-LLM-Documentation-Index-Strategy.md):

- **Index, Don't Dump** - Rules are concise; detailed docs are referenced
- **Pull, Don't Push** - Agent fetches relevant docs on-demand
- **Avoid Redundancy** - Each concept documented once, referenced elsewhere

## Maintenance

When updating rules:
- Keep files focused and concise
- Reference docs instead of duplicating content
- Rebuild container to apply changes: `./egg`
