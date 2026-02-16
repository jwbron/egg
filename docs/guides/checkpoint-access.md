# Checkpoint Access Guide

Checkpoints capture agent session context (transcripts, tool calls, files touched, token usage) as versioned JSON on the `egg/checkpoints/v2` git branch. In multi-agent pipelines, checkpoints enable agents to discover what other agents did, what files they touched, and how many tokens they used.

## When to Use Checkpoints

- **Reviewing another agent's work**: See what a coder agent did before writing tests
- **Debugging pipeline failures**: Find failed sessions and inspect their transcripts
- **Understanding context**: See what files were touched across an entire issue
- **Auditing**: Track token usage and tool calls across pipeline runs

## CLI Reference

### `egg-checkpoint list`

List checkpoints with multi-dimensional filtering.

```bash
# All checkpoints for current issue
egg-checkpoint list --issue $EGG_ISSUE_NUMBER

# All checkpoints in this pipeline run
egg-checkpoint list --pipeline $EGG_PIPELINE_ID

# Coder checkpoints in implement phase
egg-checkpoint list --agent-type coder --phase implement

# Failed sessions
egg-checkpoint list --status failed

# Combine filters (AND logic)
egg-checkpoint list --issue 530 --agent-type coder --phase implement
```

**Flags**: `--issue`, `--pr`, `--session`, `--branch`, `--trigger`, `--status`, `--agent-type`, `--phase`, `--pipeline`, `--repo`, `--limit`, `--json`

### `egg-checkpoint show`

Display full checkpoint details by ID or commit SHA.

```bash
egg-checkpoint show ckpt-abc123def456
egg-checkpoint show abc1234567890   # by commit SHA
egg-checkpoint show ckpt-abc123def456 --json
```

### `egg-checkpoint browse`

Group checkpoints by session for an issue.

```bash
egg-checkpoint browse --issue 530
```

### `egg-checkpoint context`

Cross-agent context summary grouped by phase and agent type.

```bash
# All context for a pipeline run
egg-checkpoint context --pipeline $EGG_PIPELINE_ID

# Context for an issue with file details
egg-checkpoint context --issue 530 --files

# JSON output for programmatic use
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --json
```

**Flags**: `--pipeline`, `--issue`, `--agent-type`, `--phase`, `--repo`, `--files`, `--limit`, `--json`

## Filtering Guide

All list/context filters use AND logic (all must match). Filters available:

| Filter | Flag | Example |
|--------|------|---------|
| Issue | `--issue N` | `--issue 530` |
| PR | `--pr N` | `--pr 42` |
| Pipeline | `--pipeline ID` | `--pipeline issue-530` |
| Repo | `--repo OWNER/REPO` | `--repo jwbron/egg` |
| Session | `--session ID` | `--session container-abc` |
| Branch | `--branch NAME` | `--branch egg/feature` |
| Trigger | `--trigger TYPE` | `--trigger commit` or `--trigger session_end` |
| Status | `--status STATUS` | `--status failed` |
| Agent | `--agent-type TYPE` | `--agent-type coder` |
| Phase | `--phase PHASE` | `--phase implement` |

## Common Multi-Agent Scenarios

### Tester: Find what the coder changed

```bash
# See coder's checkpoints for this issue
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --agent-type coder --phase implement

# Show details of the most recent one
egg-checkpoint show ckpt-<id>
```

### Integrator: Get full pipeline context

```bash
# Summary of all agents' work in this pipeline
egg-checkpoint context --pipeline $EGG_PIPELINE_ID

# With file details to see what was touched
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files
```

### Debugging: Find failed sessions

```bash
# Find all failed sessions for an issue
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed

# Show the transcript to understand what went wrong
egg-checkpoint show ckpt-<id>
```

## Programmatic Access

```python
from egg_contracts.checkpoint_loader import list_checkpoints_v2, load_checkpoint_by_id_v2

# List checkpoints with filters
summaries = list_checkpoints_v2(
    checkpoints_dir, index_path,
    pipeline_id="issue-530",
    agent_type="coder",
    pipeline_phase="implement",
)

# Load full checkpoint for details
checkpoint = load_checkpoint_by_id_v2(summaries[0].id, checkpoints_dir)
for f in checkpoint.files_touched:
    print(f"{f.operation}: {f.path}")
```
