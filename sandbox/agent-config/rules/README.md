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

**Windows checkouts**: Git on Windows defaults `core.symlinks` to `false` unless Developer Mode (or admin rights) is on. Without it, the committed `AGENTS.md` symlinks at the repo root and under `gateway/`, `orchestrator/`, `sandbox/` materialize as one-line text files containing the literal string `CLAUDE.md`. That degrades to harmless noise (the file is no longer a discoverable rules alias) rather than breaking anything; egg's runtime path is Linux containers, so the symlinks behave correctly there.

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
  - Pipeline-lifecycle surface (CLI only since #2908 slice-6)

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

### Pipeline-lifecycle surface (CLI only since #2908 slice-6)

Sandbox agents drive HITL / BRC / phase / progress / task /
checkpoint operations through the `egg-orch` / `egg-contract` /
`egg-checkpoint` shell CLIs. The previous in-process Claude Agent
SDK MCP tool surface (`mcp__sdlc__*`, `mcp__brc__*`,
`mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`,
`mcp__checkpoint__*`) and its `EGG_MCP_TOOLS` gating flag were
retired in [#2908](https://github.com/jwbron/egg/issues/2908)
slice-6 — the CLI is now the single agent surface. Free-text args
that previously motivated the MCP surface (shell metacharacter
corruption) route through the slice-5 `--<arg>-file PATH` / stdin
(`-` sentinel) prose-arg channels on every prose-bearing
subcommand. The shared handler layer at
`sandbox/egg_agent_tools/handlers/*.py` is preserved and continues
to back the CLI; the operator-facing orchestrator MCP server (port
9850) is unaffected. Full reference:
`$EGG_REPO_PATH/docs/reference/agent-tools.md`.

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
