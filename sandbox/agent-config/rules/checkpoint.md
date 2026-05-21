# Checkpoint Browser

Use `egg-checkpoint` to browse agent checkpoints (transcripts, tool calls, files, token usage). Full reference: `$EGG_REPO_PATH/docs/reference/checkpoint-browser.md`

**Use the structured checkpoint tools — do not compose an `egg-checkpoint`
command for the `Bash` tool.** The `search --text <t>` query and other
free-text inputs are silently corrupted by shell metacharacters
(backticks, `$(...)`, `$VAR`, `<`, `>`, `;`, `|`, `&`) when routed
through a `Bash` command string, and a backtick or `$(...)` span is
*executed* as a command rather than searched. The `mcp__checkpoint__*`
tools (mapped below) pass each field as data and never touch a shell.

The `egg-checkpoint` commands below are the reference for what each
operation does and stay available to human operators; agents invoke them
through the structured tool.

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

## MCP tool equivalents

The operations above are also exposed as in-process MCP tools, which
share the same `collect_checkpoints` / `load_checkpoint` /
`search_checkpoints` helpers the CLI uses (drift-gate enforced). Prefer
them for the reason in the callout above: free-text routed to the CLI
through the `Bash` tool is mangled by the shell. Iteration-2
([#1917](https://github.com/jwbron/egg/issues/1917)) added the **core
3** verbs (per decision-3) — `browse`, `context`, and `cost` are still
CLI-only and tracked for a follow-up.

- `mcp__checkpoint__list` — Prefer this over `egg-checkpoint list`. Returns `{items, next_cursor}` paginated by `limit` (default 100) + opaque `cursor`.
- `mcp__checkpoint__show` — Prefer this over `egg-checkpoint show`. Returns a single checkpoint dict; raises `HandlerError` for unknown id.
- `mcp__checkpoint__search` — Prefer this over `egg-checkpoint search`. Substring search returning `{items, next_cursor}` with `limit`/`cursor` pagination.

Pagination: `list` and `search` both accept optional `limit` (int,
default 100) and `cursor` (opaque string). The handler returns
`{items: [...], next_cursor: <str|None>}`. Pass the returned
`next_cursor` back as `cursor` to fetch the next page; a `None`
`next_cursor` means the page is the last one. Tampered cursors are
rejected with `HandlerError`.

See [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
for the full 29-verb inventory.
