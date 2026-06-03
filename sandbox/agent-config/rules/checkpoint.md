# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints (transcripts, tool calls, files, token usage). Full reference: `$EGG_REPO_PATH/docs/reference/checkpoint-browser.md`

**Route free text through `--<arg>-file PATH` or stdin, not a bare
`--<arg> "…"`.** The `search --text <t>` query and other free-text
inputs are silently corrupted by shell metacharacters (backticks,
`$(...)`, `$VAR`, `<`, `>`, `;`, `|`, `&`) when routed through a `Bash`
command string, and a backtick or `$(...)` span is *executed* as a
command rather than searched. The slice-5 prose-arg channels
(introduced in [#2908](https://github.com/jwbron/egg/issues/2908)
slice-5) let you route the value as data: pass `--text-file PATH` to
read from a file, or `--text -` to read from stdin. Mixing forms is
rejected — exactly one source per argument. Example:

```bash
cat > /tmp/q.txt <<'EOF'
$BACKTICK_OR_DOLLAR_PAREN_PATTERN
EOF
egg-checkpoint search --text-file /tmp/q.txt --status failed --limit 10
```

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
