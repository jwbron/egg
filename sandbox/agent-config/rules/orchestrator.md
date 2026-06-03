# Orchestrator CLI

Run `egg-orch --help` for full usage. All commands support `--json`. Full reference: `$EGG_REPO_PATH/docs/reference/orchestrator-cli.md`

**Route free text through `--<arg>-file PATH` or stdin, not a bare
`--<arg> "…"`.** Several subcommands carry LLM-authored prose —
`overseer alert --summary "…" --detail "…" --recommend "…"`,
`progress emit --step "…" --detail "…" --blocker "…"`,
`signal error --error "…"`, `anchor init --task "…"`,
`consensus propose --summary "…"`, `consensus nack --reason "…"`,
`brc resolve-obligation --note "…"`. In a `Bash` command string the
shell interprets backticks, `$(...)`, `$VAR`, `<`, `>`, `;`, `|`, and
`&` — so prose that contains them (a markdown code span, a URL, a `<`
comparison) is silently corrupted, and a backtick or `$(...)` span is
*executed* as a command rather than stored. The slice-5 prose-arg
channels (introduced in [#2908](https://github.com/jwbron/egg/issues/2908)
slice-5) let you route the value as data: pass `--<arg>-file PATH` to
read from a file, or `--<arg> -` to read from stdin. Mixing forms is
rejected — exactly one source per argument. Example:

```bash
cat > /tmp/summary.md <<'EOF'
Long-form proposal summary with `code spans`, $vars, and <comparators>.
EOF
egg-orch consensus propose --summary-file /tmp/summary.md \
  --files-changed shared/foo.py shared/bar.py
```

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

## BRC consensus verbs

- `egg-orch consensus propose --summary-file PATH --files-changed F1 F2 …` — Producer broadcasts a proposal. Pushes committed work to origin via the gateway by default (pass `--no-push` if already pushed through another route).
- `egg-orch consensus ack --producer-role <role>` — Reviewer ACKs a proposal. Add `--pre-merge-condition-file PATH` for a conditional ACK that surfaces a "Pre-merge Obligations" section on the auto-created PR ([Conditional ACK reference](../../../docs/reference/conditional-ack.md)).
- `egg-orch consensus nack --producer-role <role> --reason-file PATH` — Reviewer NACKs with a blocker reason.
- `egg-orch consensus confirmed` — Producer confirms after all reviewers ACK. Exit code 0 = transitioned to CONFIRMED; exit code 2 + JSON `status="pending_acks"` = rejected by the orchestrator (e.g. `producer_not_fully_acked`, `global_zero_proposal`, `stale_acks`) — read the message and take corrective action before retrying.
- `egg-orch message heartbeat --state <WORKING|WAITING_ON_ROLE|WAITING_FOR_EVENT|PROPOSED|IDLE> [--waiting-on <peer>]` — Emit a structured HEARTBEAT to the dedicated `/heartbeat` endpoint.

> **Blocking waits use `wait-loop`** ([#2211](https://github.com/jwbron/egg/issues/2211)). For STAY ALIVE blocking waits use `egg-orch message wait` / `egg-orch message wait-loop` — the canonical idiom is in `docs/reference/agent-wait-patterns.md` §1. Under the BRC event-pump wrapper (slice-2 of #2908; default since slice-4), the wrapper owns the wait — you are invoked one-shot per actionable event.

## BRC introspection verbs (slice-1 / slice-5 of #2908)

- `egg-orch brc next-action --role <role>` — Derive the next BRC action for the role: `wait` / `propose` / `ack` / `nack` / `confirm` / `complete` plus the matching event payload. The deterministic wrapper consumes this directly (no LLM round-trip).
- `egg-orch brc get-state [--verbose]` — Full BRC consensus state JSON.
- `egg-orch brc list-blocking [--json]` — Roles currently blocking consensus. Default output is newline-delimited (shell-friendly); `--json` returns an array.
- `egg-orch brc read-peer-artifact --phase <phase> --peer-role <role> [--message-type <type>] [--limit <N>] [--cursor <tok>]` — Paginated read over the local `.egg-state/brc-history/<id>-<phase>.json` log.
- `egg-orch brc resolve-obligation --reviewer-role <r> --producer-role <p> [--commit-sha <sha>] [--note-file PATH]` — Mark a reviewer's conditional-ACK obligation as satisfied in-cycle ([#2338](https://github.com/jwbron/egg/issues/2338)). The orchestrator rejects self-resolution (`resolver_role == producer_role`), so the producer cannot drive their own resolution — typically the tester (or any non-producer satisfier) calls this after cherry-picking the conditioning commit.

## Progress + overseer

- `egg-orch progress emit --step <text> --state <working|blocked|complete> [--detail-file PATH] [--blocker <id>]` — Emit a structured progress event.
- `egg-orch signal error --error-file PATH [--recoverable]` — Signal a recoverable / unrecoverable error.
- `egg-orch signal heartbeat` — Send a coarse-grained heartbeat.
- `egg-orch overseer alert --anomaly <type> --priority <low|medium|high> --summary-file PATH [--detail-file PATH] [--recommend-file PATH]` — Broadcast an `OVERSEER_ALERT` to all agents. **Producers blocked by reviewer NACKs (or proactive scope questions) on operator-decidable architectural choices — use `egg-contract add-decision`, not this. Alerts are informational; decisions are HITL gates. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts".**
- `egg-orch pipeline status` — Read structured pipeline status (agent matrix, BRC phase, blocked roles). Pipeline ID is resolved from `EGG_PIPELINE_ID` / `EGG_ISSUE_NUMBER`.

Full reference: [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
and [`docs/reference/orchestrator-cli.md`](../../../docs/reference/orchestrator-cli.md).
