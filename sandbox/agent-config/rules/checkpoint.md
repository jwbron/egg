# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints (transcripts, tool calls, files, token usage). Full reference: `$EGG_REPO_PATH/docs/reference/checkpoint-browser.md`

**`egg-checkpoint`'s free-text args (`--text` on `search`, etc.) do NOT have file/stdin channels yet.** The slice-5 prose-arg channels added file/stdin variants to `egg-orch` only; `shared/egg_contracts/checkpoint_cli.py` was not touched. When passing LLM-authored prose to `egg-checkpoint`, keep the value free of shell metacharacters (no backticks, no `$(...)`, no unquoted `<`, `>`, `;`, `|`, `&`) — in a `Bash` command string the shell interprets them and the query is silently corrupted (a backtick or `$(...)` span is *executed* as a command rather than searched). Quote the entire value with single quotes when possible.

Adding `-file` / stdin channels to the checkpoint CLI is a follow-up; until then, narrow the search via the structured filters (`--issue`, `--pipeline`, `--agent-type`, `--phase`, `--status`) instead of attempting to land long free-text patterns through the shell.

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

## Pagination

`egg-checkpoint list` and `egg-checkpoint search` accept optional
`--limit <int>` (default 100) and `--cursor <opaque>` (the cursor token
returned by the previous call). A `null` `next_cursor` in the response
means the page is the last one. Tampered cursors are rejected.

Full reference: [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
and [`docs/reference/checkpoint-browser.md`](../../../docs/reference/checkpoint-browser.md).
