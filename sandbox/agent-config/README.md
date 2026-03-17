# Claude Code Configuration

Configuration files for Claude Code CLI integration.

## Structure

### commands/
Slash commands available in Claude Code sessions.

These are invoked with `/command-name` syntax.

**Available commands:**
- `/show-metrics` - Generate activity report

### rules/
Agent behavior rules and guidelines.

These define how Claude operates within egg.

**Core rules:**
- `mission.md` - Agent mission, workflow, and responsibilities
- `environment.md` - Sandbox environment constraints
- `checkpoint.md` - Checkpoint browser for cross-agent context discovery

**Quality standards:**
- `code-standards.md` - Tech stack and code standards
- `pr-descriptions.md` - PR writing guidelines
- `test-workflow.md` - Test workflow and execution

### skills/ (installed at startup)
Skills installed into Claude Code from `skills/` at the repo root. Each subdirectory is a skill with a `SKILL.md` file.

**Available skills:**
- `/auto-pr` - Submit a task to the orchestrator as a lightweight single-agent pipeline (no HITL gates) and surface the resulting PR link
- `/egg-setup` - Walk through initial egg setup or update an existing configuration
- `/sdlc` - Run an egg SDLC pipeline: full lifecycle (default) or lightweight coder+reviewer with `--short`

## Usage

Claude Code automatically loads these files when running in the container.

**Slash Commands:**
```
/show-metrics
```

**Skills:**
```
/auto-pr <task description> [--repo owner/name]
/egg-setup [--check | --update secrets | --update repos | --update config]
/sdlc [--short] [issue# or description] [--repo owner/name]
```

**Rules:**
Rules are automatically applied. See individual files for details.

## See Also
- [Commands README](commands/README.md)
- [Rules README](rules/README.md)
