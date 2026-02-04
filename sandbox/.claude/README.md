# Claude Code Configuration

Configuration files for Claude Code CLI integration.

## Structure

### commands/
Slash commands available in Claude Code sessions.

These are invoked with `/command-name` syntax.

**Available commands:**
- `/load-context` - Load accumulated knowledge
- `/save-context` - Save session learnings
- `/create-pr` - Generate PR description
- `/update-confluence-doc` - Prepare Confluence updates
- `/beads-status` - Show current task status
- `/beads-sync` - Sync beads with git
- `/show-metrics` - Generate activity report

### rules/
Agent behavior rules and guidelines.

These define how Claude operates within egg.

**Core rules:**
- `mission.md` - Agent mission, workflow, and responsibilities
- `environment.md` - Sandbox environment constraints
- `beads-usage.md` - Persistent task tracking (MANDATORY)

**Context rules:**
- `slack-thread-context.md` - Slack thread memory
- `github-pr-context.md` - PR context tracking

**Quality standards:**
- `khan-academy.md` - Tech stack and code standards
- `khan-academy-culture.md` - Engineering culture and competencies
- `pr-descriptions.md` - PR writing guidelines
- `test-workflow.md` - Test discovery and execution

**Communication:**
- `notification-template.md` - Async notification formatting
- `conversation-analysis-criteria.md` - Performance assessment

## Usage

Claude Code automatically loads these files when running in the container.

**Slash Commands:**
```
/load-context my-project
/save-context my-project
/create-pr
```

**Rules:**
Rules are automatically applied. See individual files for details.

## See Also
- [Commands README](commands/README.md)
- [Rules README](rules/README.md)
