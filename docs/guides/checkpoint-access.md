# Checkpoint Access Guide

Checkpoints capture agent session context (transcripts, tool calls, files touched, token usage) as versioned JSON on the `egg/checkpoints/v2` git branch. In multi-agent pipelines, checkpoints enable agents to discover what other agents did, what files they touched, and how many tokens they used.

## Global Options

All `egg-checkpoint` commands support these top-level options:

- `--repo-path PATH` — Repository path (defaults to `EGG_REPO_PATH` or current directory)
- `--checkpoint-repo OWNER/REPO` — External checkpoint repository in `owner/repo` format. Overrides auto-detection from repository settings.
  Use this when querying checkpoints from a different repository than the current one.

Both `--repo-path` and `--checkpoint-repo` can be placed **before or after** the subcommand name:

```bash
# These are equivalent:
egg-checkpoint --checkpoint-repo jwbron/egg-checkpoints list --issue 42
egg-checkpoint list --checkpoint-repo jwbron/egg-checkpoints --issue 42
```

If the flag is supplied in both positions, the last value wins.

The checkpoint repository is resolved in this order:
1. `--checkpoint-repo` CLI flag
2. `EGG_CHECKPOINT_REPO` environment variable
3. `repositories.yaml` config lookup (auto-detected from git remote)
4. Gateway fallback via `source_repo`

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

# Code reviewer checkpoints (composite BRC role)
egg-checkpoint list --agent-type reviewer_code --issue 1707

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

**Flags**: `--issue`, `--repo`, `--limit`, `--json`

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

### `egg-checkpoint cost`

Show cost breakdown (token usage and USD) aggregated by phase and agent type.

```bash
# Cost for a specific pipeline
egg-checkpoint cost --pipeline $EGG_PIPELINE_ID

# Cost for an issue
egg-checkpoint cost --issue 530

# Cost for a PR
egg-checkpoint cost --pr 42

# JSON output for programmatic use
egg-checkpoint cost --issue 530 --json
```

**Flags**: `--pipeline`, `--issue`, `--pr`, `--limit`, `--json`

**Output**: Displays a table with per-phase/per-agent breakdowns showing input tokens, output tokens, and estimated cost in USD. JSON output includes checkpoint count and detailed breakdown array.

### `egg-checkpoint search`

Search checkpoint transcripts for matching text (case-insensitive substring).

```bash
# Search for a specific topic
egg-checkpoint search --text "issue 898"

# Narrow search with metadata filters
egg-checkpoint search --text "authentication" --agent-type coder --phase implement

# Search failed sessions for error messages
egg-checkpoint search --text "ImportError" --status failed

# JSON output for programmatic use
egg-checkpoint search --text "migration" --json
```

**Flags**: `--text` (required), `--issue`, `--pr`, `--session`, `--branch`, `--trigger`, `--status`, `--agent-type`, `--phase`, `--pipeline`, `--repo`, `--limit` (default 20), `--json`

**Note**: Each checkpoint requires loading the full transcript, so the default limit is 20 (lower than `list`'s 50). Use metadata filters to narrow the search space first.

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
| Agent | `--agent-type TYPE` | `--agent-type coder` or `--agent-type reviewer_code` |
| Phase | `--phase PHASE` | `--phase implement` |

### Composite BRC Role Names

The `--agent-type` flag accepts composite BRC role names in addition to coarse agent types. This is useful for finding checkpoints from specific reviewer roles in multi-agent pipelines:

```bash
# Find checkpoints from the code reviewer specifically
egg-checkpoint list --agent-type reviewer_code --issue 1707

# Find checkpoints from the contract reviewer
egg-checkpoint list --agent-type reviewer_contract --pipeline $EGG_PIPELINE_ID
```

Supported composite role names: `reviewer_code`, `reviewer_contract`, `reviewer_agent_design`, `reviewer_refine`, `reviewer_plan`.

**How it works:** The CLI filters by `AgentType.REVIEWER` at the index level, then loads each checkpoint to match the exact `session.agent_role` field.

**Gateway limitation:** Composite role filtering works only via the direct-git path. The gateway HTTP API collapses composite roles to `reviewer`, so `--agent-type reviewer_code` may return no results when querying through the gateway even if matching checkpoints exist. The CLI help text notes this limitation.

**Reviewer checkpoint availability:** Reviewer agents may not produce checkpoints if session-end triggers don't fire. An empty result does not necessarily mean the reviewer was inactive.

### Empty Result Behavior

When a query matches no checkpoints, the CLI now prints the repository and branch it searched to stderr:

```
Searched jwbron/egg branch egg/checkpoints/v2
No checkpoints found matching filters
```

This helps diagnose whether the correct checkpoint source is configured.

When `--json` is set, empty results produce valid parseable JSON to stdout:
- `list`, `browse`, `search`: `[]`
- `context`, `cost`: structured empty object matching the non-empty schema

Exit code is `0` for empty results. Downstream scripts can safely call `json.loads()` on the output without special-casing empty responses.

## Common Multi-Agent Scenarios

### Tester: Find what the coder changed

```bash
# See coder's checkpoints for this pipeline
egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement

# Show details of a specific checkpoint
egg-checkpoint show ckpt-<id>
```

### Documenter: Find all changed files

```bash
# Cross-agent context summary with files touched
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files

# Extract just the file paths from a specific checkpoint
egg-checkpoint show ckpt-<id> --json | jq '.files_touched[] | .path'
```

This is more comprehensive than the coder's handoff data alone — it includes files touched by all agents in the pipeline.

### Coder (revision): Learn from prior failures

```bash
# Find failed sessions for this issue
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed

# Inspect the failed checkpoint to understand what went wrong
egg-checkpoint show ckpt-<id>
```

When re-running after review feedback, checking prior failed sessions helps avoid repeating the same mistakes.

### Get full pipeline context

```bash
# Summary of all agents' work in this pipeline
egg-checkpoint context --pipeline $EGG_PIPELINE_ID

# With file details to see what was touched
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files

# Token usage and cost breakdown by phase and agent
egg-checkpoint cost --pipeline $EGG_PIPELINE_ID
```

### Debugging: Find failed sessions

```bash
# Find all failed sessions for an issue
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed

# Show the transcript to understand what went wrong
egg-checkpoint show ckpt-<id>
```

### Cost Tracking: See token usage and costs

```bash
# See total cost for this pipeline
egg-checkpoint cost --pipeline $EGG_PIPELINE_ID

# See cost breakdown for a specific issue
egg-checkpoint cost --issue $EGG_ISSUE_NUMBER
```

## Troubleshooting

### "No checkpoints found"

The CLI now shows which repository and branch it searched when no results are found (e.g., `Searched jwbron/egg branch egg/checkpoints/v2`). Check the displayed repo/branch — if it's unexpected:

1. **Checkpoints in a separate repo**: Some projects store checkpoints in a dedicated repo (e.g., `owner/project-checkpoints`). Set the `EGG_CHECKPOINT_REPO` env var or use `--checkpoint-repo`.
2. **Missing `repositories.yaml`**: Auto-detection relies on a config file that may not exist in the sandbox. Set the env var instead.
3. **No metadata on checkpoint**: Non-pipeline (ad-hoc) sessions may not have issue, PR, or pipeline metadata. Try listing without filters or search by transcript content.

### Reviewer checkpoints not found

Reviewer agents (`reviewer_code`, `reviewer_contract`, etc.) may not produce checkpoints if session-end triggers don't fire. This can happen when a reviewer's container is stopped before the checkpoint write completes. An empty result does not mean the reviewer was inactive — check pipeline logs for the agent's status.

If `--agent-type reviewer_code` returns no results but `--agent-type reviewer` does, you may be querying through the gateway HTTP API which collapses composite roles. Use the direct-git path (ensure `GATEWAY_URL` is unset) or filter by `reviewer` and inspect results manually.

### Private mode access

In private mode, the gateway must recognise the checkpoint repo as infrastructure to allow access. The checkpoint repo is identified via:

1. `EGG_CHECKPOINT_REPO` environment variable (set on the gateway or sandbox)
2. `checkpoint_repo` in `repositories.yaml` repo settings
3. The session's checkpoint repo (set during session creation)

If the gateway blocks access with "Cannot determine visibility", ensure `EGG_CHECKPOINT_REPO` is set to the checkpoint repo in `owner/repo` format.

### Finding unlabeled checkpoints

Checkpoints from ad-hoc sessions (not part of a pipeline) won't match `--issue` or `--pipeline` filters. Use:

```bash
# List all recent checkpoints without filters
egg-checkpoint list --limit 20

# Search by transcript content
egg-checkpoint search --text "the topic you're looking for"
```

## MCP Tool Access

The orchestrator MCP server (port 9850) provides two checkpoint tools for querying checkpoint data programmatically from Claude Code or other MCP clients.

### `list_checkpoints`

List checkpoints with optional filters.

```
list_checkpoints(issue=1489, phase="implement")
list_checkpoints(pipeline="issue-1489", agent_type="coder")
list_checkpoints(issue=1489, repo="jwbron/egg-checkpoints")
```

**Parameters:** `issue` (int), `pipeline` (string), `agent_type` (string), `phase` (string), `status` (string), `repo` (string, `owner/repo` format), `limit` (int, default 20)

### `search_checkpoints`

Search checkpoint metadata for matching text (searches agent_type, pipeline_phase, pipeline_id, branch, repo, and status fields).

```
search_checkpoints(text="coder", pipeline="issue-1489")
search_checkpoints(text="reviewer", repo="jwbron/egg-checkpoints")
```

**Parameters:** `text` (string, required), `issue` (int), `pipeline` (string), `agent_type` (string), `repo` (string, `owner/repo` format), `limit` (int, default 10)

### Specifying the checkpoint repository

When checkpoints are stored in a separate repository (e.g., `jwbron/egg-checkpoints`), use the `repo` parameter to target it:

```
list_checkpoints(issue=1489, repo="jwbron/egg-checkpoints")
search_checkpoints(text="error", repo="jwbron/egg-checkpoints")
```

The `repo` value is forwarded as `source_repo` to the gateway checkpoint endpoint.

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
