# Claude Code Commands

This directory contains slash command documentation for Claude Code CLI in the sandboxed environment.

## Available Commands

### /show-metrics
Generate a monitoring report showing recent agent activity.

**Usage**: `/show-metrics`

**What it does**:
- Shows API usage metrics
- Task completion statistics
- Context source usage

**File**: `show-metrics.md`

## How Commands Work

These commands are **slash commands** for Claude Code. They are markdown files that provide instructions to Claude on how to respond when you use the command syntax.

When you invoke a slash command in Claude Code:
1. Claude reads the instructions from the corresponding command file
2. Executes the workflow described in the file
3. Uses available tools (Read, Write, Bash, etc.) to complete the task

**IMPORTANT**: Use `/` (slash) prefix, NOT `@` prefix for commands.

## Command Syntax

| Correct | Incorrect |
|---------|-----------|
| `/show-metrics` | `@show-metrics` |

## Viewing Command Details

Inside the container:
```bash
# List all commands
ls ~/.claude/commands/

# View a specific command
cat ~/.claude/commands/show-metrics.md

# Or use Read tool in Claude Code
# Read: ~/.claude/commands/show-metrics.md
```

## Location in Container

These files are installed to `~/.claude/commands/` during Docker image build and are automatically available to Claude Code when running in the sandboxed environment.

## Adding New Commands

1. Create a new `.md` file in `sandbox/.claude/commands/`
2. Follow the format of existing commands
3. Rebuild the Docker image: `./egg --rebuild`
4. New command will be available at `~/.claude/commands/<command-name>.md`

## Command Format

Each command file should include:
- **Purpose**: What the command does
- **Usage**: Example syntax with `/` prefix
- **Workflow**: Step-by-step instructions for Claude
- **Critical Rules**: DO's and DON'Ts
- **Examples**: Sample interactions

Claude Code will follow these instructions when users invoke the slash command syntax.

---

**Last Updated**: 2026-02-05
