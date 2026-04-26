# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints — session transcripts, tool calls, files touched, and token usage stored on the `egg/checkpoints/v2` branch.

## When to Use

Checkpoints are most valuable when you need context from other agents in the pipeline:

- **Tester**: Review the coder's session before writing tests — understand what changed and why (`egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`)
- **Documenter**: Find all changed files across agents to ensure documentation covers everything (`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`)
- **Checker**: Get a pipeline overview and cost summary before checking (`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files` and `egg-checkpoint cost --pipeline $EGG_PIPELINE_ID`)
- **Coder (revision)**: Check prior failed sessions to avoid repeating mistakes (`egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed`)

## Checkpoint Repository Configuration

By default, `egg-checkpoint` looks for checkpoints in the current repo's `egg/checkpoints/v2` branch. If checkpoints are stored in a separate repository, configure it via:

1. `--checkpoint-repo OWNER/REPO` CLI flag (highest priority)
2. `EGG_CHECKPOINT_REPO` environment variable
3. `repositories.yaml` config lookup (auto-detected)
4. Gateway fallback via `source_repo`

The `--checkpoint-repo` and `--repo-path` flags can be placed **before or after** the subcommand name — both positions are accepted:

```bash
# Both of these are equivalent:
egg-checkpoint --checkpoint-repo owner/repo-checkpoints list --issue 42
egg-checkpoint list --checkpoint-repo owner/repo-checkpoints --issue 42
```

If the flag is supplied in both positions, the last value wins (standard argparse behavior).

## Commands

| Command | Purpose |
|---------|---------|
| `egg-checkpoint list` | List checkpoints with multi-dimensional filtering |
| `egg-checkpoint show <id>` | Display full checkpoint details (by ID or commit SHA) |
| `egg-checkpoint browse --issue <n>` | Group checkpoints by session for an issue |
| `egg-checkpoint context` | Cross-agent context summary by phase and agent |
| `egg-checkpoint cost` | Show cost breakdown (token usage and USD) by phase and agent type |
| `egg-checkpoint search --text <t>` | Search checkpoint transcripts for matching text |

All commands support `--json` for machine-readable output. When `--json` is set and results are empty, the CLI emits valid parseable JSON to stdout (`[]` for list-shaped commands; a structured empty object for `context` and `cost`) instead of plain text, ensuring downstream consumers can always `json.loads()` the output.

## Filtering

All list/context filters use AND logic. Common filters:

| Filter | Flag | Example |
|--------|------|---------|
| Issue | `--issue N` | `--issue 530` |
| PR | `--pr N` | `--pr 42` |
| Pipeline | `--pipeline ID` | `--pipeline issue-530` |
| Repo | `--repo OWNER/REPO` | `--repo owner/repo` |
| Agent | `--agent-type TYPE` | `--agent-type coder` or `--agent-type reviewer_code` |
| Phase | `--phase PHASE` | `--phase implement` |
| Status | `--status STATUS` | `--status failed` |

### Composite BRC Role Filtering

The `--agent-type` flag accepts both coarse agent types (e.g., `reviewer`) and composite BRC role names:

| Composite Role | Description |
|----------------|-------------|
| `reviewer_code` | Code quality reviewer (fan-out, slice-by-slice) |
| `reviewer_code_holistic` | Holistic code reviewer (cross-module coherence) |
| `reviewer_contract` | Contract compliance reviewer |
| `reviewer_agent_design` | Agent design reviewer |
| `reviewer_refine` | Refinement reviewer |
| `reviewer_plan` | Plan reviewer |

When a composite role name is used, the CLI filters by `AgentType.REVIEWER` at the index level, then post-filters by loading each checkpoint and matching the `session.agent_role` field.

**Limitations:**
- Composite role filtering works only via the **direct-git path**. When querying through the gateway HTTP API, composite roles collapse to `reviewer` — the gateway does not preserve the full role name in its index.
- Reviewer agents may not produce checkpoints if session-end triggers don't fire (e.g., container killed before checkpoint write). An empty result for a reviewer role does not necessarily mean the reviewer was inactive.

```bash
# Find code reviewer checkpoints for a specific issue
egg-checkpoint list --agent-type reviewer_code --issue 1707

# Find all reviewer checkpoints (all composite roles included)
egg-checkpoint list --agent-type reviewer --pipeline $EGG_PIPELINE_ID
```

### Empty Results

When a query matches no checkpoints, the CLI prints the repository and branch that were searched to stderr, helping diagnose whether the correct checkpoint source was used:

```
Searched owner/repo branch egg/checkpoints/v2
No checkpoints found matching filters
```

When `--json` is set, empty results produce valid JSON on stdout:
- **List-shaped commands** (`list`, `browse`, `search`): `[]`
- **Structured commands** (`context`, `cost`): schema-appropriate empty object

The exit code is always `0` for empty results — this is not an error condition.

## Common Workflows

**See what another agent did:**
```bash
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --agent-type coder --phase implement
egg-checkpoint show ckpt-<id>
```

**Get full pipeline context:**
```bash
egg-checkpoint context --pipeline $EGG_PIPELINE_ID
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files
```

**Find failed sessions:**
```bash
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed
egg-checkpoint show ckpt-<id>
```

**Search transcripts for specific content:**
```bash
egg-checkpoint search --text "issue 898"
egg-checkpoint search --text "authentication" --agent-type coder --phase implement
egg-checkpoint search --text "error" --status failed --limit 10
```

**See token usage and costs:**
```bash
egg-checkpoint cost --pipeline $EGG_PIPELINE_ID
egg-checkpoint cost --issue $EGG_ISSUE_NUMBER
```

## Troubleshooting

**"No checkpoints found"**: The CLI now prints which repository and branch it searched (e.g., `Searched owner/repo branch egg/checkpoints/v2`) to stderr, making it easier to diagnose configuration issues. If the repo/branch shown is unexpected:
1. Check if checkpoints are in a separate repo — set `EGG_CHECKPOINT_REPO=OWNER/REPO` or use `--checkpoint-repo`
2. Check if `repositories.yaml` exists (it may not be present in the sandbox)
3. Try listing without metadata filters — some checkpoints (ad-hoc sessions) have no issue/pipeline metadata

**Reviewer checkpoints not found**: Reviewer agents (`reviewer_code`, `reviewer_contract`, etc.) may not produce checkpoints if session-end triggers don't fire. This can happen when a reviewer's container is stopped before the checkpoint write completes. An empty result does not necessarily mean the reviewer was inactive — check pipeline logs for the agent's status.

**Finding unlabeled checkpoints**: Non-pipeline sessions may lack issue/pipeline metadata. Use `egg-checkpoint list --limit 20` without filters, or search by transcript content: `egg-checkpoint search --text "your topic"`

**Composite role filtering via gateway**: If `--agent-type reviewer_code` returns no results but `--agent-type reviewer` does, you may be querying through the gateway HTTP API which collapses composite roles to `reviewer`. Set `GATEWAY_URL=` (empty) to force the direct-git path, or filter results manually.

## MCP Tools

The orchestrator MCP server (port 9850) exposes checkpoint data via two tools:

| Tool | Purpose |
|------|---------|
| `list_checkpoints` | List checkpoints with filters (issue, pipeline, agent_type, phase, status, repo) |
| `search_checkpoints` | Search checkpoint metadata by text with filters (issue, pipeline, agent_type, repo) |

Both tools accept an optional `repo` parameter to specify the checkpoint repository in `owner/repo` format (e.g., `owner/repo-checkpoints`). This is useful when checkpoints are stored in a separate repository from the main codebase.

**Examples:**
```
list_checkpoints(issue=1489, phase="implement")
list_checkpoints(issue=1489, repo="owner/repo-checkpoints")
search_checkpoints(text="coder", pipeline="issue-1489", repo="owner/repo-checkpoints")
```

## Related CLIs

- `egg-orch` — Orchestrator operations (pipelines, phases, decisions)
- `egg-contract` — SDLC contract operations (tasks, commits, feedback)

See `$EGG_REPO_PATH/docs/guides/checkpoint-access.md` for the full guide.
