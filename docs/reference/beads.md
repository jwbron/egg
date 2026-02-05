# Beads Task Tracking

Beads (`bd`) is a lightweight issue tracker with first-class dependency support, designed for autonomous AI agents to maintain persistent task memory across container restarts.

## Why Beads?

- **Persistent memory**: Tasks survive container restarts and session changes
- **Dependency tracking**: Model complex work with blockers and relationships
- **Audit trail**: Record decisions, progress, and context for future reference
- **Cross-reference**: Link tasks to Slack threads, PRs, and JIRA tickets

## Quick Reference

### Before ANY Work

```bash
# Check for in-progress work to resume
bd --allow-stale list --status in_progress

# Search for related existing tasks
bd --allow-stale search "keywords"

# Create new task with labels
bd --allow-stale create "Task title" --labels type:feature,source:slack

# Start working on a task
bd --allow-stale update <id> --status in_progress

# Complete a task
bd --allow-stale update <id> --status closed --notes "Summary. PR #XX created."
```

### The `--allow-stale` Flag

Always use `--allow-stale` in the egg container. This bypasses staleness checks that assume a daemon is running. The daemon is not available in the sandboxed container environment.

## Common Commands

### Creating Tasks

```bash
# Basic task creation
bd --allow-stale create "Fix authentication bug"

# With labels (use key:value format)
bd --allow-stale create "Add dark mode" --labels type:feature,priority:high

# Quick capture (returns only the ID)
bd --allow-stale q "Investigate flaky test"
```

### Listing and Searching

```bash
# List all open tasks
bd --allow-stale list

# Filter by status
bd --allow-stale list --status in_progress
bd --allow-stale list --status open
bd --allow-stale list --status closed

# Filter by label
bd --allow-stale list --label type:bug

# Search title and description
bd --allow-stale search "authentication"

# Show ready work (no blockers)
bd --allow-stale ready
```

### Updating Tasks

```bash
# Change status
bd --allow-stale update TASK-1 --status in_progress
bd --allow-stale update TASK-1 --status closed

# Add notes
bd --allow-stale update TASK-1 --notes "Found root cause in session manager"

# Close with summary
bd --allow-stale close TASK-1 --notes "Fixed in PR #42"
```

### Viewing Details

```bash
# Show full task details
bd --allow-stale show TASK-1

# View comments on a task
bd --allow-stale comments TASK-1

# Add a comment
bd --allow-stale comments TASK-1 --add "Blocked waiting for API access"
```

### Dependencies

```bash
# Add a dependency (TASK-2 blocks TASK-1)
bd --allow-stale dep add TASK-1 TASK-2

# List dependencies
bd --allow-stale dep list TASK-1

# Show blocked tasks
bd --allow-stale blocked

# Show dependency graph
bd --allow-stale graph
```

## Task Statuses

| Status | Meaning |
|--------|---------|
| `open` | Not yet started |
| `in_progress` | Currently being worked on |
| `closed` | Completed |
| `deferred` | Postponed for later |

## Labels

Labels use `key:value` format for filtering:

```bash
# Common label patterns
--labels type:bug
--labels type:feature
--labels type:docs
--labels priority:high
--labels source:slack
--labels source:jira
```

## Integration with egg Workflow

### Start of Session

```bash
# 1. Check for work to resume
bd --allow-stale list --status in_progress

# 2. If resuming, show details
bd --allow-stale show TASK-42

# 3. If starting fresh, search for related work
bd --allow-stale search "topic keywords"
```

### During Work

```bash
# Update status when starting
bd --allow-stale update TASK-42 --status in_progress

# Add notes as you discover things
bd --allow-stale comments TASK-42 --add "Root cause: race condition in token refresh"
```

### After Completing Work

```bash
# Close with summary
bd --allow-stale update TASK-42 --status closed --notes "Fixed race condition in token refresh. PR #65 created."
```

## Database Location

Beads databases are stored in `~/beads/`. Each project can have its own `.beads/` directory with:
- `beads.db` - SQLite database
- `beads.jsonl` - JSONL export for git sync

## Troubleshooting

### "no beads database found"

Initialize a database or point to an existing one:

```bash
# Initialize new database
bd init

# Or use BEADS_DIR environment variable
export BEADS_DIR=~/beads/.beads
```

### "database is locked"

Another process has the database open. Wait or use:

```bash
bd --lock-timeout 5s list
```

### Recovering Lost Tasks

If tasks seem missing, check JSONL files in git:

```bash
bd --allow-stale sync  # Re-sync from JSONL
```

## See Also

- [Workflow Guide](../../CLAUDE.md) - Full agent workflow including beads
- [Setup Guide](../setup/README.md) - Initial configuration
