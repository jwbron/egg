# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints (transcripts, tool calls, files, token usage). Full reference: `$EGG_REPO_PATH/docs/reference/checkpoint-browser.md`

**Commands**: `list`, `show <id>`, `browse --issue <n>`, `context`, `cost`, `search --text <t>`. All support `--json`.

**Common filters**: `--issue N`, `--pipeline ID`, `--agent-type TYPE`, `--phase PHASE`, `--status STATUS`

**Quick examples:**
```bash
egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement
egg-checkpoint list --agent-type reviewer_code --issue 1707
egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files
egg-checkpoint search --text "error" --status failed --limit 10
```

**Checkpoint repo config**: By default uses current repo's `egg/checkpoints/v2` branch. Override with `--checkpoint-repo OWNER/REPO` or `EGG_CHECKPOINT_REPO` env var. The `--checkpoint-repo` flag works before or after the subcommand name.

**Composite reviewer roles**: `--agent-type` accepts `reviewer_code`, `reviewer_contract`, `reviewer_agent_design`, `reviewer_refine`, `reviewer_plan` (direct-git path only; collapses to `reviewer` via gateway).

**Empty results**: The CLI prints which repo/branch was searched to stderr. With `--json`, empty results produce valid JSON (`[]` or structured empty object).
