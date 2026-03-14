# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints — session transcripts, tool calls, files touched, and token usage stored on the `egg/checkpoints/v2` branch.

## When to Use

Checkpoints are most valuable when you need context from other agents in the pipeline:

- **Tester**: Review the coder's session before writing tests — understand what changed and why (`egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`)
- **Documenter**: Find all changed files across agents to ensure documentation covers everything (`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`)
- **Integrator**: Get a pipeline overview and cost summary before integrating (`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files` and `egg-checkpoint cost --pipeline $EGG_PIPELINE_ID`)
- **Coder (revision)**: Check prior failed sessions to avoid repeating mistakes (`egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed`)

## Checkpoint Repository Configuration

By default, `egg-checkpoint` looks for checkpoints in the current repo's `egg/checkpoints/v2` branch. If checkpoints are stored in a separate repository, configure it via:

1. `--checkpoint-repo OWNER/REPO` CLI flag (highest priority)
2. `EGG_CHECKPOINT_REPO` environment variable
3. `repositories.yaml` config lookup (auto-detected)
4. Gateway fallback via `source_repo`

Example: `EGG_CHECKPOINT_REPO=jwbron/egg-checkpoints egg-checkpoint list --limit 5`

## Commands

| Command | Purpose |
|---------|---------|
| `egg-checkpoint list` | List checkpoints with multi-dimensional filtering |
| `egg-checkpoint show <id>` | Display full checkpoint details (by ID or commit SHA) |
| `egg-checkpoint browse --issue <n>` | Group checkpoints by session for an issue |
| `egg-checkpoint context` | Cross-agent context summary by phase and agent |
| `egg-checkpoint cost` | Show cost breakdown (token usage and USD) by phase and agent type |
| `egg-checkpoint search --text <t>` | Search checkpoint transcripts for matching text |

All commands support `--json` for machine-readable output.

## Filtering

All list/context filters use AND logic. Common filters:

| Filter | Flag | Example |
|--------|------|---------|
| Issue | `--issue N` | `--issue 530` |
| PR | `--pr N` | `--pr 42` |
| Pipeline | `--pipeline ID` | `--pipeline issue-530` |
| Repo | `--repo OWNER/REPO` | `--repo jwbron/egg` |
| Agent | `--agent-type TYPE` | `--agent-type coder` |
| Phase | `--phase PHASE` | `--phase implement` |
| Status | `--status STATUS` | `--status failed` |

## Common Workflows

**See what another agent did:**
```bash
egg-checkpoint list --issue $EGG_ISSUE_NUMBER --agent-type coder --phase implement
egg-checkpoint show ckpt-<id>
```

**Get full pipeline context (integrator/tester):**
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

**"No checkpoints found"**: If you see this message with a hint about `--checkpoint-repo`:
1. Check if checkpoints are in a separate repo — set `EGG_CHECKPOINT_REPO=OWNER/REPO`
2. Check if `repositories.yaml` exists (it may not be present in the sandbox)
3. Try listing without metadata filters — some checkpoints (ad-hoc sessions) have no issue/pipeline metadata

**Finding unlabeled checkpoints**: Non-pipeline sessions may lack issue/pipeline metadata. Use `egg-checkpoint list --limit 20` without filters, or search by transcript content: `egg-checkpoint search --text "your topic"`

## Related CLIs

- `egg-orch` — Orchestrator operations (pipelines, phases, decisions)
- `egg-contract` — SDLC contract operations (tasks, commits, feedback)

See `$EGG_REPO_PATH/docs/guides/checkpoint-access.md` for the full guide.
