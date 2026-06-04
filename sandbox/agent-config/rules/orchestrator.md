# Orchestrator CLI

Run `egg-orch --help` for full usage. All commands support `--json`. Full reference: `$EGG_REPO_PATH/docs/reference/orchestrator-cli.md`

**For free-text args that have a slice-5 prose-arg channel, route the value through `--<arg>-file PATH` or stdin (`--<arg> -`), not a bare `--<arg> "…"`.** In a `Bash` command string the shell interprets backticks, `$(...)`, `$VAR`, `<`, `>`, `;`, `|`, and `&` — so prose that contains them (a markdown code span, a URL, a `<` comparison) is silently corrupted, and a backtick or `$(...)` span is *executed* as a command rather than stored.

The slice-5 prose-arg channels (introduced in [#2908](https://github.com/jwbron/egg/issues/2908) slice-5) cover **only** these four args today: `--summary` (on `consensus propose`), `--reason` (on `consensus ack` / `consensus nack` / `consensus withdraw`), `--note` (on `brc resolve-obligation`), and `--files-reviewed` (on `consensus ack` / `consensus nack`, one path per line). Mixing forms is rejected — exactly one source per argument. Other prose-bearing flags (`--detail`, `--recommend`, `--error`, `--task`, `--pre-merge-condition`) do **not** have file/stdin channels yet; pass them as bare strings and avoid shell metacharacters in the value.

Example:

```bash
cat > /tmp/summary.md <<'EOF'
Long-form proposal summary with `code spans`, $vars, and <comparators>.
EOF
egg-orch consensus propose --summary-file /tmp/summary.md \
  --files-changed shared/foo.py shared/bar.py --push
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

**Related CLIs**: `egg-contract`, `egg-pipeline-watch`

## BRC consensus verbs

- `egg-orch consensus propose --summary-file PATH --files-changed F1 F2 … --push` — Producer broadcasts a proposal. `--push` is **opt-in** (default off); pass it on every pipeline-session proposal, because the gateway blocks plain `git push` for pipeline sessions and `--push` carries the `consensus_push` marker the gateway requires.
- `egg-orch consensus ack <producer_role> --files-reviewed F1 F2 … --ack-version N` — Reviewer ACKs a proposal. `<producer_role>` is **positional** (not a `--producer-role` flag). Add `--pre-merge-condition "…"` for a conditional ACK that surfaces a "Pre-merge Obligations" section on the auto-created PR ([Conditional ACK reference](../../../docs/reference/conditional-ack.md)). `--pre-merge-condition` is inline-only; there is no `-file` variant yet.
- `egg-orch consensus nack <producer_role> --reason-file PATH --files-reviewed F1 F2 … --nack-version N` — Reviewer NACKs with a blocker reason. `<producer_role>` is **positional**.
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

- `egg-orch progress emit --step <text> --state <working|blocked|complete> [--detail <text>] [--blocker <id>]` — Emit a structured progress event. (No `-file` channel exists for `--detail` today; the slice-5 prose-arg work only covered `consensus propose / ack / nack / withdraw` and `brc resolve-obligation`.)
- `egg-orch signal error --error <msg> [--recoverable]` — Signal a recoverable / unrecoverable error. (No `--error-file` channel today.)
- `egg-orch signal heartbeat` — Send a coarse-grained heartbeat.
- `egg-orch overseer alert --anomaly <type> --priority <low|medium|high> --summary <text> [--detail <text>] [--recommend <text>]` — Broadcast an `OVERSEER_ALERT` to all agents. (No `-file` channels on `--summary` / `--detail` / `--recommend` today.) **Producers blocked by reviewer NACKs (or proactive scope questions) on operator-decidable architectural choices — use `egg-contract add-decision`, not this. Alerts are informational; decisions are HITL gates. See [`mission.md`](mission.md) → "HITL Decisions vs. Operational Alerts".**
- `egg-orch pipeline status` — Read structured pipeline status (agent matrix, BRC phase, blocked roles). Pipeline ID is resolved from `EGG_PIPELINE_ID` / `EGG_ISSUE_NUMBER`.

Full reference: [`docs/reference/agent-tools.md`](../../../docs/reference/agent-tools.md)
and [`docs/reference/orchestrator-cli.md`](../../../docs/reference/orchestrator-cli.md).
