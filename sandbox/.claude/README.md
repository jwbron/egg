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

**Quality standards:**
- `code-standards.md` - Tech stack and code standards
- `pr-descriptions.md` - PR writing guidelines
- `test-workflow.md` - Test workflow and execution

## Usage

Claude Code automatically loads these files when running in the container.

**Slash Commands:**
```
/show-metrics
```

**Rules:**
Rules are automatically applied. See individual files for details.

## See Also
- [Commands README](commands/README.md)
- [Rules README](rules/README.md)
