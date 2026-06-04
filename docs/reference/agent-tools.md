# Agent Pipeline-Lifecycle Surface

> Sandbox agents drive pipeline lifecycle operations (BRC consensus,
> HITL decisions, phase context, progress signals, task completion)
> **through the `egg-orch` / `egg-contract` shell CLIs**. The in-process Claude Agent SDK MCP
> tool surface was **retired in
> [#2908](https://github.com/jwbron/egg/issues/2908) slice-6** — the
> CLI is the single agent surface. The operator-facing orchestrator
> MCP server (port 9850) is unaffected.

## Why retired

The agent-side MCP tool surface (one in-process SDK MCP server per
namespace, registered on `ClaudeAgentOptions.mcp_servers` and gated by
`EGG_MCP_TOOLS`) was introduced in
[#1765](https://github.com/jwbron/egg/issues/1765) and expanded in
[#1917](https://github.com/jwbron/egg/issues/1917). It coexisted with
the long-running shell CLIs and a drift gate
(`tests/tools/test_mcp_cli_drift.py`) kept the two surfaces honest by
asserting they dispatched through the same handlers.

The dual surface had a steady carrying cost:

- **Two surfaces to keep in sync.** Every new verb had to add a `@tool`
  wrapper, a registration with a `cli_command` attribute, a
  `Prefer this over …` rule-doc line, and pass the drift gate. Every
  rename had to flow through both sides.
- **MCP-loop reentry was a seam non-Claude models fell out of.** The
  long-lived "agent re-enters a blocking `egg-orch message wait-loop`
  between every BRC event" pattern depended on the model holding the
  conversation across events. Claude usually does;
  `qwen3.7-max` does not (`#2906`) — it exits success after one match,
  the wrapper sees no `CONSENSUS_CONFIRMED`, the 3-restart cap trips
  (`#2806`), and the pipeline `FAIL`s. Prompt-only mitigations narrow
  the seam for one model; the seam itself is per-model. The new
  **deterministic wrapper-driven event pump** (slice-2 of #2908)
  reframes consensus-agent execution as one-shot invocations and
  `egg-orch` is the wrapper-side surface the bash needs — an LLM
  round-trip through MCP would be wasted overhead.
- **Free-text prose** that used to be the MCP surface's primary
  advantage (no shell quoting) was partially reworked in slice-5 of
  #2908 — the `egg-orch` CLI gained `--<arg>-file PATH` / stdin (`-`
  sentinel) channels on a **specific four-arg set**: `--summary` (on
  `consensus propose`), `--reason` (on `consensus ack` /
  `consensus nack` / `consensus withdraw`), `--note` (on
  `brc resolve-obligation`), and `--files-reviewed` (on `consensus ack`
  / `consensus nack`, one path per line). Other prose flags
  (`--question`, `--options`, `--notes`, `--detail`, `--recommend`,
  `--error`, `--task`, `--pre-merge-condition`, `--text`) do **not**
  have file/stdin channels yet — pass them as bare shell-safe strings.
  Extending the channels to the rest of the `egg-orch` surface and to
  `egg-contract` is a follow-up. The structured-tool
  advantage on the four covered args is real; it is now delivered by
  the CLI itself.

Slice-6 of #2908 deleted the in-process MCP surface: the seven
`@tool` namespace files at `sandbox/egg_agent_tools/tools/*.py`, the
four infrastructure files (`__init__` / `_common` / `_registry` /
`_tool_compat`), the `SYSTEM_PROMPT_NUDGE` constant, the
`build_sandbox_mcp_server` factory, the MCP-registration block in
`shared/egg_agent/client.py`, and the MCP↔CLI drift gate at
`tests/tools/test_mcp_cli_drift.py`. The `EGG_MCP_TOOLS` env flag is
no longer recognised. The handler layer at
`sandbox/egg_agent_tools/handlers/*.py` is preserved and continues to
back the CLI.

## What is preserved

### Shared handler layer

The pure-Python request → response handlers at
`sandbox/egg_agent_tools/handlers/*.py` are **kept** — they back the
CLI today (they always have; the MCP tools and the CLI both called
through them). Slice-6 collapses **two surfaces to one** (the CLI),
not two to zero. Every gateway-fronted operation an agent issues on
the BRC / phase / contract / task hot path still runs
through the same handler — the only thing gone is the in-process MCP
wrapping.

### Operator-facing MCP server (`orchestrator/mcp_server.py`)

The **operator-facing** orchestrator MCP server (port 9850; tools
like `submit_task`, `get_status`, `provide_input`, `list_tasks`,
`cancel_task`, `restart_agent`, `restart_phase`, `advance_phase`,
`complete_phase`, `populate_contract`, …) is **unaffected**. It runs in the
orchestrator process, not the sandbox, and is the surface operators /
external MCP clients use to drive pipelines from outside the sandbox.
Slice-6 only removes the sandbox-side agent tool surface.

See
[Architecture → Orchestrator → MCP Server (`/mcp`)](../architecture/orchestrator.md#mcp-server-mcp)
for the orchestrator MCP tool inventory.

### CLI subcommands added during #2908

Slices 1 and 5 of #2908 added several `egg-orch brc <verb>`
subcommands that previously existed only as MCP verbs (and a new
wrapper-only `brc next-action`). These continue to ship and are
documented under
[Orchestrator CLI → BRC verb-level operations (`egg-orch brc`)](orchestrator-cli.md#brc-verb-level-operations-egg-orch-brc):

| Subcommand | Purpose |
|------------|---------|
| `egg-orch brc next-action` | Derive the next BRC action for a role (`wait` / `propose` / `ack` / `nack` / `confirm` / `complete`) plus the event payload. New in slice-1; no MCP counterpart by design — the deterministic wrapper consumes the derivation directly. |
| `egg-orch brc get-state` | Full BRC consensus state JSON. |
| `egg-orch brc list-blocking` | Roles currently blocking consensus. |
| `egg-orch brc resolve-obligation` | Mark a reviewer's conditional-ACK obligation satisfied in-cycle ([#2338](https://github.com/jwbron/egg/issues/2338)). |
| `egg-orch brc read-peer-artifact` | Paginated read over the local `.egg-state/brc-history/<id>-<phase>.json` log. |

## The agent's CLI surface today

Three CLIs cover every pipeline-lifecycle operation an agent issues
on the hot path. Each one is the source of truth for its subdomain;
the linked references list every subcommand, every flag, and every
exit-code contract.

| CLI | Reference | Covers |
|-----|-----------|--------|
| `egg-orch` | [Orchestrator CLI](orchestrator-cli.md) | BRC consensus (`consensus propose / ack / nack / withdraw / confirmed`), BRC introspection (`brc next-action / get-state / list-blocking / read-peer-artifact / resolve-obligation`), message bus (`message send / wait / wait-loop / heartbeat`), pipeline status (`pipeline status / wait-status`), progress (`progress emit / query`), signals (`signal heartbeat / error / readiness / complete`), overseer alerts (`overseer alert`), health, phase, decision, container, anchor. |
| `egg-contract` | [SDLC Contract](sdlc-contract.md) | Contract reads (`show`), task / phase mutations (`add-commit`, `update-notes`, `complete-task`, `complete-phase`, `verify-criterion`), HITL gates (`add-decision`, `add-feedback`). |

### Passing free text safely (prose-arg channels — slice-5 of #2908)

A specific set of `egg-orch` subcommands accept LLM-authored prose
through one of three channels so shell metacharacters cannot corrupt
or execute the content. Slice-5 of #2908 covers **only** the four
arg-names listed below today; other prose flags (`--question`,
`--options`, `--notes`, `--detail`, `--recommend`, `--error`,
`--task`, `--pre-merge-condition`, `--text`) take only the inline
`--<arg> "value"` form and must be kept shell-safe at the call site.
Extending the channels to the rest of `egg-orch` and to `egg-contract`
is a follow-up.

| Channel | When to use |
|---------|-------------|
| `--<arg> "value"` | Short, single-line prose with no shell metacharacters. |
| `--<arg>-file PATH` | Prose that contains backticks, `$(...)`, `$VAR`, `<`, `>`, `;`, `|`, `&`, or spans multiple lines. The CLI reads the file's contents and treats it as the argument value. |
| `--<arg> -` (stdin sentinel) | Prose piped in from another command. The CLI reads stdin and treats it as the argument value. |

Args that have the file/stdin channels today:

| Subcommand | Arg | File flag | Stdin form |
|------------|-----|-----------|------------|
| `egg-orch consensus propose` | `--summary` | `--summary-file PATH` | `--summary -` |
| `egg-orch consensus ack` | `--reason` *(reserved — see below)* | `--reason-file PATH` | `--reason -` |
| `egg-orch consensus ack` | `--files-reviewed` (list) | `--files-reviewed-file PATH` (one path per line) | — |
| `egg-orch consensus nack` | `--reason` | `--reason-file PATH` | `--reason -` |
| `egg-orch consensus nack` | `--files-reviewed` (list) | `--files-reviewed-file PATH` | — |
| `egg-orch consensus withdraw` | `--reason` | `--reason-file PATH` | `--reason -` |
| `egg-orch brc resolve-obligation` | `--note` | `--note-file PATH` | `--note -` |

Mixing forms is rejected — exactly one source per argument. The
canonical recipes are:

```bash
# Producer re-propose (positional producer_role is implicit — the proposer
# operates on its own behalf; --push is opt-in but required in pipeline sessions):
cat > /tmp/summary.md <<'EOF'
Long-form proposal summary with `code spans`, $vars, and <comparators>.
The shell never touches this — the file path is the only thing the CLI sees.
EOF
egg-orch consensus propose \
  --summary-file /tmp/summary.md \
  --files-changed shared/foo.py shared/bar.py \
  --push

# Reviewer NACK (producer_role is POSITIONAL, not --producer-role;
# --files-reviewed and --nack-version are required):
cat > /tmp/reason.md <<'EOF'
Blocker: file `shared/foo.py:42` calls `bar()` without holding the lock.
EOF
egg-orch consensus nack coder \
  --reason-file /tmp/reason.md \
  --files-reviewed shared/foo.py shared/bar.py \
  --nack-version 3

# Reviewer ACK (producer_role positional; --files-reviewed and --ack-version required):
egg-orch consensus ack coder \
  --files-reviewed shared/foo.py shared/bar.py \
  --ack-version 3
```

See [Orchestrator CLI](orchestrator-cli.md) for the per-subcommand
flag inventory and exit-code contract.

### Long-poll waits use `wait-loop` (not a tool re-entry)

Blocking waits go through `egg-orch message wait-loop` (sandbox) and
`egg-orch pipeline wait-status` (host), not an MCP `wait_for_event`
tool. This was [#2211](https://github.com/jwbron/egg/issues/2211) in
the MCP era — both MCP transports cap tool calls below typical
quiet-phase intervals (~30 s streamable-HTTP, ~60 s in-process SDK),
and every cap-elapsed return is a wasted LLM turn. The canonical idiom
is in
[Agent Wait Patterns §1](agent-wait-patterns.md#1-canonical-stay-alive-egg-orch-message-wait-loop).

Under the BRC event-pump wrapper (slice-2 of #2908; default since
slice-4), the wrapper bash owns the wait — the agent is invoked
one-shot per actionable event and exits naturally between events,
so the wait surface above is owned by the wrapper, not the agent.
See [Mission → You are an event handler — the wrapper owns the
wait](../../sandbox/agent-config/rules/mission.md) and
[BRC Memory Artifact](../architecture/brc-memory.md) for the
continuity model.

### Error handling

The CLIs catch gateway-side `GatewayError` / `TimeoutError` /
`HandlerError` and render an actionable stderr message + non-zero
exit code. Handlers never call `sys.exit` — that rule (originally
imposed for the agent SDK's event loop in #1765) is preserved
because the CLI shims still import the same handler modules and a
handler that called `sys.exit` would terminate the CLI process
mid-call without writing an error envelope. The CLI `cmd_*`
functions in `sandbox/egg_lib/orch_cli.py` and
`sandbox/egg_lib/contract_cli.py` may still call
`sys.exit(1)` on argparse-level errors (e.g. missing `--role`),
which is fine because they run in their own process.

## Output-size cap (`EGG_TOOL_OUTPUT_CAP_BYTES`, [#2805](https://github.com/jwbron/egg/issues/2805))

The output-size cap continues to apply to the **orchestrator MCP
server** (`handle_tool_call` → `cap_result_dict`) via the shared
`shared/egg_tool_output.py` helper, which bounds every operator-facing
MCP tool result as model-context / cost discipline before it crosses
the Claude Agent SDK reader. The cap is **not** the crash-prevention
layer for the SDK reader (the upstream 1 MiB buffer was the original
concern, but egg raises that to 32 MiB at
`ClaudeAgentOptions.max_buffer_size`, [#2884](https://github.com/jwbron/egg/issues/2884) —
see
[Agent Recovery → SDK Reader Buffer](agent-recovery.md#sdk-reader-buffer-the-crash-prevention-layer)).

| Variable | Default | Effect |
|----------|---------|--------|
| `EGG_TOOL_OUTPUT_CAP_BYTES` | `102400` (100 KB) | Max serialized size of a single tool result. Output above the cap is replaced with a structured head-preview marker (`_egg_truncated`) that names how to narrow the call, or — for unpaginated content — spilled to a temp file (`_egg_output_spilled`) the agent can `Read`/`grep`, with a small inline preview. |

At ~4 B/token for prose/JSON, the 100 KB default ≈ ~25k tokens — a
sensible upper bound for a single model-bound tool result. A
non-positive or non-integer value is **ignored with a logged warning**
(the operator is not left believing a cap is in effect when it isn't);
the helper falls back to the 100 KB default. The orchestrator measures
the cap against `indent=2`-serialized JSON (matching what its MCP
server ships).

The previous agent-side `invoke_handler` → `cap_text` chokepoint went
away with the agent MCP tools. Agent-side CLI calls are subject only
to the orchestrator-side cap (when the CLI's response originates from
a gateway round-trip the orchestrator's MCP server already capped) and
the per-tool predictive output cap below.

**Built-in tool cap (complementary):** Built-in Claude Code tools
(`Read`, `Grep`, etc.) run inside the CLI and can't be wrapped the
same way. [#2876](https://github.com/jwbron/egg/issues/2876) adds a
PreToolUse hook that predicts when a result would be excessive
*before* the tool runs and denies the call with a narrowing hint. See
[Agent Recovery → Predictive Output Cap](agent-recovery.md#predictive-output-cap-pretooluse)
for the heuristic table and the `EGG_TOOL_OUTPUT_CAP` /
`EGG_READ_CAP_BYTES` operator knobs.

## Architecture (post-#2908)

```
┌─────────────────────────────────────────────────────────────────┐
│ Agent container (Python, claude_agent_sdk)                      │
│                                                                 │
│  shared/egg_agent/client.py::run_agent_async                    │
│      (no agent-side MCP registration; no SYSTEM_PROMPT_NUDGE)   │
│                                                                 │
│  agent invokes egg-orch / egg-contract via Bash                 │
│                                                                 │
│                ┌── sandbox/egg_lib/ ──┐                          │
│                │  orch_cli.py         │                          │
│                │  contract_cli.py     │                          │
│                └────────┬─────────────┘                          │
│                         │                                        │
│                         ▼                                        │
│                ┌─── sandbox/egg_agent_tools/handlers/ ────┐      │
│                │  pure req→res Python                     │      │
│                │  raises GatewayError / HandlerError      │      │
│                │  asyncio.to_thread() inside CLI shim     │      │
│                └────────┬─────────────────────────────────┘      │
│                         │ make_gateway_request                   │
│                         ▼                                        │
└────────────────────────────┬───────────────────────────────────┘
                             │ HTTP
                             ▼
                    gateway / orchestrator
                       └── operator-facing MCP server (port 9850)
                           unaffected by slice-6 — operator surface
```

## Version pin

`claude-agent-sdk` remains pinned to `>=0.1.65,<0.2` in
`sandbox/pyproject.toml` and the `CLAUDE_AGENT_SDK_VERSION` ARG in
`sandbox/Dockerfile` — the SDK is still the agent runtime. A smoke
test at `tests/sandbox/egg_agent_tools/test_sdk_surface.py` imports
the SDK at module load time so CI fails at test-collection time on an
incompatible upgrade.

## Testing

| Test | Purpose |
|------|---------|
| `tests/sandbox/egg_agent_tools/test_handlers_*.py` | Unit tests for each handler (happy-path, missing-arg, 5xx gateway → `GatewayError`). |
| `tests/sandbox/test_contract_cli.py`, `tests/sandbox/test_orch_cli_consensus_push.py`, `tests/sandbox/test_orch_cli_slice_id.py` | CLI parity against committed fixtures (no auto-record — every expected value is in the repo). |
| `tests/sandbox/egg_lib/test_orch_cli_brc.py`, `tests/sandbox/egg_lib/test_orch_cli_brc_adversarial.py` | `egg-orch brc *` verb-level subcommand parity + adversarial coverage (slice-1 / slice-5 of #2908). |
| `tests/sandbox/egg_lib/test_orch_cli_phase.py` | `egg-orch phase get-context` verb parity. |
| `tests/sandbox/egg_lib/test_orch_cli_prose_args.py`, `tests/sandbox/egg_lib/test_orch_cli_prose_args_adversarial.py` | `--<arg>-file PATH` / stdin (`-` sentinel) prose-arg channel coverage on the four args slice-5 covered (`--summary` / `--reason` / `--note` / `--files-reviewed`). |
| `integration_tests/test_mcp_baseline_capture.py` | Slice-5 baseline-capture exerciser for the per-event wall-clock latency measurement; captured `.egg-state/agent-outputs/latency-mcp-baseline.json` against the then-live MCP surface so the slice-6 post-deletion run can compare. |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` | Marker-gated end-to-end exercise of the agent's tool surface. Slice-6 task-6-4 migrated this from asserting `mcp__*` tool calls to asserting the equivalent `egg-orch consensus ack/nack` CLI invocations through the slice-5 prose-arg channels. |
| `integration_tests/test_mcp_to_cli_latency.py` *(slice-6 task-6-6)* | Per-event wall-clock regression check: reads the slice-5-captured `latency-mcp-baseline.json` and compares against a post-deletion CLI-only run. Fails only if the regression exceeds the 5 % budget; on failure surfaces a structured `OVERSEER_ALERT` priority `medium`. |

Removed alongside the slice-6 deletions:

- `tests/sandbox/egg_agent_tools/test_tools.py`,
  `tests/sandbox/egg_agent_tools/test_server.py`,
  `tests/sandbox/egg_agent_tools/test_schemas.py`,
  `tests/sandbox/egg_agent_tools/test_full_tool_registry.py` — exercised
  the `@tool` wrapper / server-factory / schema-derivation layer that
  task-6-1 deletes.
- `tests/tools/test_mcp_cli_drift.py` — the MCP↔CLI drift contract no
  longer applies (only the CLI surface remains). The shared handler
  layer keeps both surfaces honest in spirit; task-6-3 retires the
  formal drift suite.
- `tests/tools/test_rule_doc_drift.py` (parts referencing the agent
  `TOOL_REGISTRY`) — the rule-doc drift gate previously asserted every
  `Prefer this over `egg-…`` line resolved to an agent-side
  `TOOL_REGISTRY` entry. With the registry retired by task-6-1, that
  invariant is meaningless; whatever residue of the gate referenced
  the registry was removed in the same slice. The non-registry parts
  of the rule-doc gate (e.g. the cross-link sanity it does between
  rule files and `docs/reference/`) survive if any remain.

## Related

- [Orchestrator CLI](orchestrator-cli.md) — full `egg-orch` shell surface.
- [SDLC Contract](sdlc-contract.md) — full `egg-contract` shell surface.
- [Architecture → Orchestrator → MCP Server](../architecture/orchestrator.md#mcp-server-mcp) — operator-facing MCP server (port 9850) and its tool inventory.
- [Agent Wait Patterns](agent-wait-patterns.md) — `wait-loop` idiom, exit-code contract, BRC event-pump wrapper interaction.
- [Concurrent Execution Guide](../guides/concurrent-execution.md) — BRC consensus + message bus the `egg-orch consensus *` subcommands drive.
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) — sandbox env flags (post-slice-6 `EGG_MCP_TOOLS` is gone).
- [#1765](https://github.com/jwbron/egg/issues/1765) — iteration 1 (original agent MCP tool surface; superseded by #2908 slice-6).
- [#1917](https://github.com/jwbron/egg/issues/1917) — iteration 2 (additional agent MCP verbs + rule-doc drift gate; superseded by #2908 slice-6).
- [#2908](https://github.com/jwbron/egg/issues/2908) — BRC event-pump wrapper; slice-6 retired the agent MCP surface.
- [#2906](https://github.com/jwbron/egg/issues/2906) — the qwen3.7-max reentry-seam failure that motivated the wrapper rework.
