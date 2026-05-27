# Claude Agent Rules

This directory contains instructions for the AI agent (Claude Code) operating in this sandboxed environment.

## How These Files Are Used

Claude Code reads `CLAUDE.md` files automatically when starting. During container startup, all rule files are combined into a single `CLAUDE.md`:

**Installation:**
- `~/.claude/CLAUDE.md` → All rules combined (user-level global config)
- `~/.claude/AGENTS.md` → Symlink to `CLAUDE.md` (cross-tool industry-norm alias)

A matching `AGENTS.md` symlink is also created in the agent's CWD next to the project-level `CLAUDE.md` symlink, so AGENTS.md-aware frontends discover the same rules without a second source of truth.

**Why one file?** Since `~/repos/` is mounted from the host (not copied), we can't reliably write to it during container startup. Combining all rules into `~/.claude/CLAUDE.md` ensures they're always available regardless of CWD.

**Note**: `CLAUDE.md` is the [official Claude Code format](https://www.anthropic.com/engineering/claude-code-best-practices) for providing context and instructions to the agent. `AGENTS.md` is the convention adopted by other agent tools — keeping both names as symlinks to the same content makes the rules portable.

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
  - Environment flags (`EGG_MCP_TOOLS`, etc.)

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

- **orchestrator.md** - Orchestrator CLI commands
  - `egg-orch` command reference (health, pipeline, signal, phase, decision, container, gateway)

- **contract.md** - SDLC contract CLI commands
  - `egg-contract` command reference (show, complete-task, complete-phase, add-commit, update-notes, add-decision, add-feedback)

- **checkpoint.md** - Checkpoint browser CLI commands
  - `egg-checkpoint` command reference (list, show, browse, context)

### Agent MCP tools (default on)

Sandbox agents see in-process Claude Agent SDK MCP tools for HITL / BRC / phase / progress / task / checkpoint operations on the same `tool_use` stream they already handle (on by default since #1942; set `EGG_MCP_TOOLS=false` on the pod env to opt out). Prefer these (`mcp__sdlc__*`, `mcp__brc__*`, `mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`, `mcp__checkpoint__*`) over shelling out to `egg-contract` / `egg-orch` / `egg-checkpoint` via Bash. The shell CLIs remain available for human operators, tests, and recovery scripts. Full reference: `$EGG_REPO_PATH/docs/reference/agent-tools.md`.

### Recovery

- **anchor-recovery.md** - Post-compaction recovery protocol
  - How to restore working state from agent anchor after context clear
  - Step-by-step: read anchor → catch up messages → verify files → resume
  - Full guide: `$EGG_REPO_PATH/docs/guides/anchor-recovery.md`

- **branch-recovery.md** - Detached-HEAD recovery in pipeline sessions
  - When you end up on detached HEAD, advance your work branch with
    `git update-ref refs/heads/<your-branch> <sha>`
  - Background: #2162

- **push-recovery.md** - Recovering from a rejected push
  - What happens when the gateway rejects `git push` for restricted-path
    modifications, and the conditional-ACK pattern from #1998

## Design Principles

- **Index, Don't Dump** - Rules are concise; detailed docs are referenced
- **Pull, Don't Push** - Agent fetches relevant docs on-demand
- **Avoid Redundancy** - Each concept documented once, referenced elsewhere

See `$EGG_REPO_PATH/docs/index.md` for navigation to all documentation.

## Maintenance

When updating rules:
- Keep files focused and concise
- Reference docs instead of duplicating content
- Rebuild container to apply changes: `./egg`
