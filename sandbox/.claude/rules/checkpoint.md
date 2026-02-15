# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints — session transcripts, tool calls, files touched, and token usage stored on the `egg/checkpoints/v2` branch.

## Commands

| Command | Purpose |
|---------|---------|
| `egg-checkpoint list` | List checkpoints with multi-dimensional filtering |
| `egg-checkpoint show <id>` | Display full checkpoint details (by ID or commit SHA) |
| `egg-checkpoint browse --issue <n>` | Group checkpoints by session for an issue |
| `egg-checkpoint context` | Cross-agent context summary by phase and agent |

All commands support `--json` for machine-readable output.

## Filtering

All list/context filters use AND logic. Common filters:

| Filter | Flag | Example |
|--------|------|---------|
| Issue | `--issue N` | `--issue 530` |
| PR | `--pr N` | `--pr 42` |
| Pipeline | `--pipeline ID` | `--pipeline issue-530` |
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

## Related CLIs

- `egg-orch` — Orchestrator operations (pipelines, phases, decisions)
- `egg-contract` — SDLC contract operations (tasks, commits, feedback)

See `$EGG_REPO_PATH/docs/guides/checkpoint-access.md` for the full guide.
