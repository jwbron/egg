# Orchestrator CLI

Run `egg-orch --help` for full usage. All commands support `--json`. Full reference: `$EGG_REPO_PATH/docs/reference/orchestrator-cli.md`

**Use the structured orchestrator tools — do not compose an `egg-orch`
command for the `Bash` tool.** Several subcommands carry LLM-authored
free text — `overseer alert --summary "…" --detail "…" --recommend "…"`,
`progress emit --step "…" --detail "…" --blocker "…"`,
`signal error --error "…"`, `anchor init --task "…"`. In a `Bash`
command string the shell interprets backticks, `$(...)`, `$VAR`, `<`,
`>`, `;`, `|`, and `&` — so prose that contains them (a markdown code
span, a URL, a `<` comparison) is silently corrupted, and a backtick or
`$(...)` span is *executed* as a command rather than stored. The
structured tools pass each field as data and never touch a shell:

- **`claude_agent_sdk` harness** — the `mcp__brc__*` / `mcp__progress__*`
  tools (mapped below).
- **`EGG_HARNESS=egg`** — the `EggOrch` tool: subcommand as `command`,
  each flag and value as a separate `args` element.

The `egg-orch` commands below are the reference for what each operation
does and stay available to human operators; agents invoke them through
the structured tool.

**Essential commands:**

| Command | Purpose |
|---------|---------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch pipeline status <id>` | Get pipeline status |
| `egg-orch phase get [<id>]` | Get current phase |
| `egg-orch signal complete --commit <sha>` | Signal completion |
| `egg-orch signal error --error <msg> --recoverable` | Signal error |
| `egg-orch signal heartbeat` | Send heartbeat |
| `egg-orch decision list [<id>]` | List HITL decisions |
| `egg-orch progress emit --step <text> --state <working\|blocked\|complete>` | Emit structured progress event |
| `egg-orch progress query [--agent <role>]` | Query structured progress events |
| `egg-orch overseer alert --anomaly <type> --priority <low\|medium\|high> --summary <text> [--detail <text>] [--recommend <text>]` | Broadcast OVERSEER_ALERT to all agents in the pipeline |
| `egg-orch health alerts` | List active deterministic health alerts |
| `egg-orch health resolve [<id>] --agent-id <id> --alert-type <type>` | Resolve (remove) health alerts for an agent |
| `egg-orch anchor init --task <text>` | Create initial anchor for current agent |
| `egg-orch anchor update [--status <s>] [--progress <json>]` | Update agent anchor (atomic) |
| `egg-orch anchor show [--agent <id>] [--team]` | Show own, another agent's, or team anchor |
| `egg-orch anchor validate` | Validate anchor schema and size limits |
| `egg-orch anchor cleanup` | Remove orphaned anchor files |

Pipeline ID/agent role can be omitted when `EGG_PIPELINE_ID`/`EGG_AGENT_ROLE` are set.

**Key env vars**: `EGG_ORCHESTRATOR_URL`, `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `EGG_ISSUE_NUMBER`, `EGG_BRANCH`, `EGG_REPO_PATH`, `GATEWAY_URL`, `AGENT_ANCHOR_ID`

**Related CLIs**: `egg-contract`, `egg-pipeline-watch`, `egg-checkpoint`

## MCP tool equivalents (`claude_agent_sdk` harness)

On the `claude_agent_sdk` harness the operations above are also exposed
as in-process MCP tools, which share the same handler the CLI uses
(drift-gate enforced). Prefer them for the reason in the callout above:
free-text routed to the CLI through the `Bash` tool is mangled by the
shell.

BRC consensus + heartbeats:

- `mcp__brc__propose` — Prefer this over `egg-orch consensus propose`. Producer broadcasts a proposal.
- `mcp__brc__ack` — Prefer this over `egg-orch consensus ack`. Reviewer ACKs a proposal.
- `mcp__brc__nack` — Prefer this over `egg-orch consensus nack`. Reviewer NACKs with a blocker reason.
- `mcp__brc__confirm` — Prefer this over `egg-orch consensus confirmed`. Producer confirms after all reviewers ACK. Returns `ok: True` only when the producer transitioned to CONFIRMED; on `ok: False` (status `pending_acks`) the transition was rejected — read `message` for the reason (e.g. `producer_not_fully_acked`, `global_zero_proposal`, `stale_acks`) and take corrective action before retrying.
- `mcp__brc__send_heartbeat` — Prefer this over `egg-orch message heartbeat`. Emit a structured HEARTBEAT to the dedicated `/heartbeat` endpoint.

> **Blocking waits use Bash, not MCP** (#2211). Both MCP transports cap tool calls below typical quiet-phase intervals (~30 s streamable-HTTP, ~60 s in-process SDK), so every cap-elapsed return is a wasted LLM turn. For STAY ALIVE blocking waits use `egg-orch message wait` / `egg-orch message wait-loop` via Bash — the canonical idiom is in `docs/reference/agent-wait-patterns.md` §1.

Progress + overseer (iter-2 added the overseer surface):

- `mcp__progress__emit` — Prefer this over `egg-orch progress emit`. Emit a structured progress event (step/state/detail/blocker).
- `mcp__progress__signal_error` — Prefer this over `egg-orch signal error`. Signal a recoverable / unrecoverable error.
- `mcp__progress__heartbeat` — Prefer this over `egg-orch signal heartbeat`. Send a coarse-grained heartbeat.
- `mcp__progress__overseer_alert` — Prefer this over `egg-orch overseer alert`. Broadcast an `OVERSEER_ALERT` to all agents in the pipeline. **Producers blocked by reviewer NACKs (or proactive scope questions) on operator-decidable architectural choices — use `mcp__sdlc__register_open_question`, not this. Alerts are informational; decisions are HITL gates. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts".**
- `mcp__progress__query_status` — Prefer this over `egg-orch pipeline status`. Read structured pipeline status (agent matrix, BRC phase, blocked roles). Note: the MCP tool lives in the `progress` namespace per decision-5; the CLI lives in the `pipeline` subcommand subtree (decision-17 keeps the drift-gate symmetric with `overseer_alert`).

No-CLI BRC introspection (iteration 1 + 2):

- `mcp__brc__get_state` — Returns the full structured BRC consensus state as JSON.
- `mcp__brc__list_blocking` — Returns the list of agent roles currently blocking consensus.
- `mcp__brc__read_peer_artifact` — Reads `.egg-state/brc-history/<pipeline>-<phase>.json` filtered by `peer_role` with `limit`/`cursor` pagination. No CLI by design (reviewer-forensics helper; operators inspect the files directly).
- `mcp__brc__resolve_obligation` — Mark a reviewer's conditional-ACK obligation as satisfied in-cycle (#2338). Required: `reviewer_role`, `producer_role`. Optional: `commit_sha`, `note`. The orchestrator rejects self-resolution (`resolver_role == producer_role`), so the producer cannot drive their own resolution — typically the tester (or any non-producer satisfier) calls this after cherry-picking the conditioning commit. No CLI by design; in-cycle resolution flows through the MCP surface.

See [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
for the full 29-verb inventory.
