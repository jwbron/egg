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
  - Workflow: gather context → plan → implement → test → PR
  - Decision-making framework (when to proceed vs ask)
  - Quality standards and communication style

- **environment.md** - Technical constraints
  - Sandbox security model
  - Network modes (public vs private)
  - GitHub CLI (`gh`) for PRs and issues
  - File system layout and access
  - Services and package installation

### Code Standards

- **code-standards.md** - Tech stack and code standards
  - Technologies (Python, React, TypeScript)
  - Code style guidelines
  - Common commands

### Quality & Communication

- **pr-descriptions.md** - PR writing guidelines
  - Standard PR format
  - Length targets

- **test-workflow.md** - Test workflow and execution
  - Testing workflow integration

- **contract.md** - SDLC contract CLI commands
  - `egg-contract` command reference (show, add-commit, update-notes, add-decision, add-feedback)

## Design Principles

- **Index, Don't Dump** - Rules are concise; detailed docs are referenced
- **Pull, Don't Push** - Agent fetches relevant docs on-demand
- **Avoid Redundancy** - Each concept documented once, referenced elsewhere

See `~/repos/egg/docs/index.md` for navigation to all documentation.

## Maintenance

When updating rules:
- Keep files focused and concise
- Reference docs instead of duplicating content
- Rebuild container to apply changes: `./egg`
