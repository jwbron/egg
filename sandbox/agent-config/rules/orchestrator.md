# Orchestrator CLI

Run `egg-orch --help` for full usage. All commands support `--json`. Full reference: `$EGG_REPO_PATH/docs/reference/orchestrator-cli.md`

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

## Prefer MCP tools over the CLI

Sandbox agents on the default harness should call the in-process MCP
tools instead of shelling out — they share the same handler the CLI
uses (drift-gate enforced) and avoid a subprocess + JSON parsing step.

BRC consensus + heartbeats:

- `mcp__brc__propose` — Prefer this over `egg-orch consensus propose`. Producer broadcasts a proposal.
- `mcp__brc__ack` — Prefer this over `egg-orch consensus ack`. Reviewer ACKs a proposal.
- `mcp__brc__nack` — Prefer this over `egg-orch consensus nack`. Reviewer NACKs with a blocker reason.
- `mcp__brc__confirm` — Prefer this over `egg-orch consensus confirmed`. Producer confirms after all reviewers ACK. Returns `ok: True` only when the producer transitioned to CONFIRMED; on `ok: False` (status `pending_acks`) the transition was rejected — read `message` for the reason (e.g. `producer_not_fully_acked`, `global_zero_proposal`, `stale_acks`) and take corrective action before retrying.
- `mcp__brc__wait_for_event` — Prefer this over `egg-orch message wait`. Block on typed BRC messages.
- `mcp__brc__wait_loop` — Prefer this over `egg-orch message wait-loop`. Loop wait_for_event with retry on transient errors.
- `mcp__brc__send_heartbeat` — Prefer this over `egg-orch message heartbeat`. Emit a structured HEARTBEAT to the dedicated `/heartbeat` endpoint.

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

See [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
for the full 30-verb inventory.
