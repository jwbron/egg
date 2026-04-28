# Agent MCP Tools Reference

> Sandbox agents can call pipeline lifecycle operations (BRC consensus,
> HITL decisions, phase context, progress signals, task completion,
> checkpoint browsing) through first-class MCP tools on the Claude
> Agent SDK `tool_use` stream, instead of shelling out to
> `egg-contract` / `egg-orch` / `egg-checkpoint` via `Bash`.

The tools are exposed as an in-process SDK MCP server built with
[`claude_agent_sdk.create_sdk_mcp_server`](https://github.com/anthropics/claude-agent-sdk-python)
and registered on `ClaudeAgentOptions.mcp_servers` by
[`shared/egg_agent/client.py::run_agent_async`](../../shared/egg_agent/client.py).
There is **no new network service, no new auth layer, no new process**
— the tools run in the agent's own Python interpreter and call the
same handler functions the `egg-contract` / `egg-orch` /
`egg-checkpoint` CLIs call. Iteration 1 (the mechanism + 18 verbs) is
tracked in [#1765](https://github.com/jwbron/egg/issues/1765);
iteration 2 (12 additional verbs covering the rest of the capability
audit) is tracked in
[#1917](https://github.com/jwbron/egg/issues/1917).

## Flag — `EGG_MCP_TOOLS`

The MCP tool surface is **on by default** since [#1942](https://github.com/jwbron/egg/issues/1942). The env var now acts as a kill-switch:

| Flag | Effect |
|------|--------|
| `EGG_MCP_TOOLS` unset or any value not listed below | **Default.** Registers the 30 tools (one server per namespace) on `options.mcp_servers` and appends `SYSTEM_PROMPT_NUDGE` to `options.system_prompt`. |
| `EGG_MCP_TOOLS=false` (or `0` / `no` / `off`) | Opt-out. Code path is byte-identical to the pre-#1765 behaviour — no `mcp_servers` registration, no prompt changes, no import cost. |

Iteration 1 (#1765) shipped the flag default-off while the wire-up burned in.
#1942 flipped the default to on and kept the env var as a rollback
switch; a later follow-up (decision-9 in #1917) will remove the flag
entirely once iter-2 has burned in.

To opt a pipeline out, set `EGG_MCP_TOOLS=false` via pod env, Docker
Compose, or the `env` stanza on any submit-task payload. See
[docs/guides/sdlc-pipeline.md — Agent MCP tools
(EGG_MCP_TOOLS flag)](../guides/sdlc-pipeline.md#agent-mcp-tools-egg_mcp_tools-flag)
for the per-pipeline recipe.

## Tool inventory (30 verbs)

All 30 tools are registered as `@tool`-decorated wrappers in
`sandbox/egg_agent_tools/tools/*.py`. The raw `@tool` name is the verb
itself (e.g. `"propose"`, `"register_open_question"`).

### Tool-name resolution (how Claude sees these tools)

The SDK renders an MCP tool in `tool_use` blocks as
`mcp__<server_key>__<raw_tool_name>`. `build_sandbox_mcp_server`
returns a `{namespace: server}` dict — one SDK MCP server per
namespace, keyed by `sdlc`, `brc`, `phase`, `progress`, `task`, or
`checkpoint` — and `shared/egg_agent/client.py::run_agent_async`
merges that dict into `options.mcp_servers` unless `EGG_MCP_TOOLS` is
explicitly falsy. With raw `@tool` names declared as plain verbs,
Claude's composition naturally produces the semantic names in the
tables below:

- raw name `propose` in server key `brc` → `mcp__brc__propose`
- raw name `register_open_question` in server key `sdlc` →
  `mcp__sdlc__register_open_question`
- raw name `list` in server key `checkpoint` → `mcp__checkpoint__list`
- ...and so on for every verb.

The tables list the **SDK-visible tool names** (what appears in
`tool_use` blocks and what agents call). The `ToolRegistration.name`
attribute in `sandbox/egg_agent_tools/tools/_registry.py` carries
the same full name for drift-test introspection and nudge
generation. The authoritative sources are the shipping `TOOL_LIST`
and per-namespace dict returned by
`sandbox/egg_agent_tools/server.py::build_sandbox_mcp_server()`, plus
the `SYSTEM_PROMPT_NUDGE` generated at import time by
`sandbox/egg_agent_tools/server.py::_render_nudge()`.

Every tool with a shell-CLI counterpart declares a `cli_command`
attribute on its `ToolRegistration` (e.g. `("egg-orch", "consensus",
"propose")`) so a CI drift test
(`tests/tools/test_mcp_cli_drift.py`) can assert the MCP tool and
the CLI subparser dispatch the same handler function. If a handler
moves, both surfaces move together or CI fails. Adding a new tool
means adding a `cli_command` attribute on the registration (or
explicitly setting it to `None` for new verbs with no CLI
counterpart) — the drift gate will refuse the PR otherwise. Tools
that set `cli_command=None` are governed by an additional gate (see
[`cli_command=None` rationale](#cli_commandnone-rationale-pattern-decision-13))
that requires the handler docstring to explain why no CLI exists.

### `mcp__sdlc__*` — HITL and contract-level operations

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__sdlc__register_open_question` | Create a HITL decision (multiple-choice) on the contract. | `handlers.sdlc.register_open_question` | `egg-contract add-decision` |
| `mcp__sdlc__request_feedback` | Create an open-ended feedback request on the contract. | `handlers.sdlc.request_feedback` | `egg-contract add-feedback` |
| `mcp__sdlc__check_hitl_answers` | Return resolved decisions and feedback (submitted or pending) for the current contract. Without a `phase` arg, returns HITL across all phases; pass `phase` to narrow to a single phase. | `handlers.sdlc.check_hitl_answers` | — *(no CLI; new capability)* |
| `mcp__sdlc__show_contract` | Read the current contract as a dict. Optional `fields=[…]` projection returns only the named top-level keys; an unknown field raises `HandlerError` (no silent skip). State-machine effect: **read-only**. | `handlers.sdlc.show_contract` | `egg-contract show` |
| `mcp__sdlc__verify_criterion` | Mark an acceptance criterion verified on the contract. **REVIEWER role only** — the gateway rejects non-REVIEWER writers; the handler does not re-check (decision-7). State-machine effect: marks the criterion verified; no-op if already verified. | `handlers.sdlc.verify_criterion` | `egg-contract verify-criterion` |

### `mcp__brc__*` — Broadcast-Review-Converge consensus

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__brc__propose` | Push committed changes to origin then broadcast a `CONSENSUS_PROPOSE` signal. See [push behavior](#brc_propose-push-behavior) below. | `handlers.brc.brc_propose` | `egg-orch consensus propose --push` |
| `mcp__brc__ack` | Acknowledge (ACK) a peer's proposal. Optional `pre_merge_condition` (str) turns this into a **conditional ACK** — the work is approved but a human must perform the named action before merging (e.g. `git mv old/path new/path`). The obligation is rendered as a "Pre-merge Obligations" section on the auto-created PR. Leave empty for an unconditional ACK. When non-empty, the condition is validated like `reason`: boilerplate and short values are rejected with 400. | `handlers.brc.brc_ack` | `egg-orch consensus ack` |
| `mcp__brc__nack` | Reject (NACK) a peer's proposal with blocker list. | `handlers.brc.brc_nack` | `egg-orch consensus nack` |
| `mcp__brc__confirm` | Signal CONFIRMED — producer acknowledges all reviewer ACKs. Returns `ok=True` when the transition to CONFIRMED succeeded. Returns `ok=False` with `status="pending_acks"` when the orchestrator rejected the transition (e.g. not yet fully ACKed, stale ACKs); this is transient — retry after polling for outstanding ACKs. Equivalent to CLI exit code 0 vs 2. | `handlers.brc.brc_confirm` | `egg-orch consensus confirmed` |
| `mcp__brc__get_state` | Full structured consensus state (JSON; accepts `verbose: bool`). | `handlers.brc.brc_get_state` | — *(no CLI; CLI `egg-orch consensus status` prints text — this tool returns the dict)* |
| `mcp__brc__list_blocking` | Return the list of agent roles currently blocking consensus (derived view). | `handlers.brc.brc_list_blocking` | — *(no CLI; new capability)* |
| `mcp__brc__send_heartbeat` | Emit a structured `HEARTBEAT` (schema-validated, per-role deduped, rate-limited) to the dedicated `/heartbeat` endpoint. Use `state=WAITING_ON_ROLE` + `waiting_on=<peer>` while blocking on BRC. Valid states: `WORKING`, `WAITING_ON_ROLE`, `WAITING_FOR_EVENT`, `PROPOSED`, `IDLE`. | `handlers.message.message_heartbeat` | `egg-orch message heartbeat` |

> **Blocking waits use Bash, not MCP** (#2211). Long-poll waits don't fit the MCP transport — both transports cap tool calls below typical quiet-phase intervals (~30 s streamable-HTTP, ~60 s in-process SDK), and every cap-elapsed return is a wasted LLM turn. Use `egg-orch message wait` / `egg-orch message wait-loop` (sandbox) and `egg-orch pipeline wait-status` (host) via Bash. The §1 idiom in `docs/reference/agent-wait-patterns.md` is the canonical shape.
| `mcp__brc__read_peer_artifact` | Read entries from `.egg-state/brc-history/<pipeline_id>-<phase>.json` filtered by `peer_role`, with `limit`/`cursor` pagination (default `limit=50`). `pipeline_id` is resolved server-side from `EGG_PIPELINE_ID` / `EGG_ISSUE_NUMBER` (agents cannot pass an arbitrary id; path-traversal hardening). Returns `{items: [...], next_cursor: <str|None>, skipped_malformed: <int>}`. | `handlers.brc.read_peer_artifact` | — *(no CLI; reviewer-forensics helper that reads local files; operators inspect the files directly)* |

#### `brc_propose` push behavior

`mcp__brc__propose` pushes committed changes to origin via the gateway
before broadcasting the proposal. The `push` parameter defaults to
`true`; set it to `false` if you have already pushed through another
route. Push failure short-circuits the handler — no proposal is sent
for an un-pushed artifact.

### `mcp__phase__*` — Phase context

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__phase__get_context` | Bundle `EGG_PIPELINE_ID`, `EGG_PHASE`, `EGG_AGENT_ROLE`, the role-filtered task list, and prior-phase artifact paths (`.egg-state/drafts/`, `.egg-state/agent-outputs/`). | `handlers.phase.phase_get_context` | — *(no CLI; new capability)* |
| `mcp__phase__get_assigned_tasks` | Return only the tasks assigned to the caller's role (`EGG_AGENT_ROLE`) from the contract. | `handlers.phase.phase_get_assigned_tasks` | — *(no CLI; filtered view over `egg-contract show`)* |
| `mcp__phase__complete_phase` | Mutate `phases.<p>.status` to `"complete"` via the gateway `/api/v1/contract/mutate` path. State-machine effect: **transitions phase status to complete; downstream `phase_complete` signal fires.** | `handlers.phase.complete_phase` | `egg-contract complete-phase` |

Some fields on `mcp__phase__get_context` remain best-effort (e.g.
`active_peers`, `reviewer_peers`, `hitl_pending`); promotion to
required is tracked as a separate follow-up after iter-2 burn-in
(decision-6 in #1917).

### `mcp__progress__*` — Progress signals + overseer surface

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__progress__emit` | Emit a structured progress event: required `step` (step name) and `state` (`working`/`blocked`/`complete`), optional `detail` and `blocker`. | `handlers.progress.progress_emit` | `egg-orch progress emit` |
| `mcp__progress__signal_error` | Signal an error to the orchestrator (`--error <msg>` payload + recoverable flag). | `handlers.progress.progress_signal_error` | `egg-orch signal error` |
| `mcp__progress__heartbeat` | Send a heartbeat so the orchestrator knows the agent is alive (coarse-grained; for fine-grained BRC heartbeats use `mcp__brc__send_heartbeat`). | `handlers.progress.progress_heartbeat` | `egg-orch signal heartbeat` |
| `mcp__progress__overseer_alert` | Broadcast an `OVERSEER_ALERT` to all agents in the pipeline (`to_role="all"` hard-coded). | `handlers.progress.overseer_alert` | `egg-orch overseer alert` |
| `mcp__progress__query_status` | `GET /api/v1/pipelines/<pipeline_id>/status` — read the structured pipeline status (agent matrix, BRC phase, blocked roles). `pipeline_id` is resolved server-side from `EGG_PIPELINE_ID` / `EGG_ISSUE_NUMBER`; agents cannot query arbitrary pipelines (path-traversal / cross-pipeline-read hardening). When the pipeline is wedged between phases (pipeline is `running`, current phase is `complete`, no pending decisions, no successor scheduled for >60 s), the response includes `wedged_no_successor: {phase, completed_at, since_seconds}`. | `handlers.progress.query_status` | `egg-orch pipeline status` |

### `mcp__task__*` — Task-level mutations

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__task__complete` | Mark a contract task complete, optionally linking a commit SHA. State-machine effect: **transitions task status to complete; idempotent**. | `handlers.task.task_complete` | `egg-contract complete-task` |
| `mcp__task__add_commit` | Link a commit SHA to a task. State-machine effect: **records the commit on the task; does not mark the task complete**. | `handlers.task.add_commit` | `egg-contract add-commit` |
| `mcp__task__update_notes` | Append implementation notes to a task. | `handlers.task.update_notes` | `egg-contract update-notes` |
| `mcp__task__mark_gap` | Append a structured coverage-gap entry to `phases.<p>.tasks.<t>.gaps[]`. **Tester role writes; coder role reads.** Handler stamps `created_at` (ISO-8601 UTC) and generates a stable `gap-<N>` id from `max(existing) + 1`. Validation rejects missing `from_role` / `to_role` / `description`. | `handlers.task.mark_gap` | — *(no CLI; tester→coder coverage-gap handoff is agent-to-agent; operators don't need it)* |

### `mcp__checkpoint__*` — Checkpoint browsing

The checkpoint namespace is new in iter-2 (decision-3: ship the
**core 3** verbs — `list`, `show`, `search`). The CLI `browse`,
`context`, and `cost` subcommands stay shell-only for now and are
tracked for a follow-up. The handlers import three pure helpers from
`shared/egg_contracts/checkpoint_cli.py` (`collect_checkpoints`,
`load_checkpoint`, `search_checkpoints`) extracted from the existing
`cmd_*` functions, so the CLI and the handler share one code path
(decision-20: helper extraction, no new gateway endpoint).

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__checkpoint__list` | List checkpoints filtered by pipeline / role / date-range. Returns `{items: [...], next_cursor: <str|None>}` with `limit`/`cursor` pagination (default `limit=100`). | `handlers.checkpoint.checkpoint_list` | `egg-checkpoint list` |
| `mcp__checkpoint__show` | Resolve a checkpoint id → dict. Raises `HandlerError` for an unknown id. | `handlers.checkpoint.checkpoint_show` | `egg-checkpoint show` |
| `mcp__checkpoint__search` | Substring search over checkpoint metadata; returns `{items, next_cursor}` with `limit`/`cursor` pagination (default `limit=100`). | `handlers.checkpoint.checkpoint_search` | `egg-checkpoint search` |

Total: **30 tools** across 6 namespaces (`sdlc`, `brc`, `phase`,
`progress`, `task`, `checkpoint`), covering the BRC consensus loop,
HITL (decisions + feedback + answers), phase context + completion,
progress signals + overseer alerts + status queries, task completion
+ commits + notes + coverage-gaps, and checkpoint browsing — every
verb a pipeline agent issues on the hot path. Both the count (`30`)
and the namespace set (`{sdlc, brc, phase, progress, task,
checkpoint}`) are asserted by
`tests/sandbox/egg_agent_tools/test_server.py::test_prompt_nudge_drift`
so the prose numbers in this doc cannot drift silently.

## Conventions

### Pagination convention (decision-12)

Verbs that return a potentially large list paginate via opaque
cursors instead of start/poll/complete triplets:

| Verb | Default `limit` |
|------|-----------------|
| `mcp__brc__read_peer_artifact` | 50 |
| `mcp__checkpoint__list` | 100 |
| `mcp__checkpoint__search` | 100 |

The handler returns `{items: [...], next_cursor: <str|None>}`. Pass
the returned `next_cursor` back as the next call's `cursor` to fetch
the next page; a `None` `next_cursor` means the page is the last one.
The internal encoding of `cursor` is implementation-defined and must
not be parsed or constructed by agents — treat it as an opaque token
that round-trips through the handler. Tampered cursors are rejected
with `HandlerError`. The defaults are
sized to keep a worst-case page under the SDK's 60 s MCP timeout; if
you know your dataset is small, raise `limit` to skip the second
round-trip.

### `cli_command=None` rationale pattern (decision-13)

A `ToolRegistration` declares `cli_command=None` for verbs that have
no CLI counterpart on purpose (new agent-only capabilities or
deliberately-no-CLI affordances). For these verbs the drift gate
(`tests/tools/test_mcp_cli_drift.py`) skips the CLI parity check, but
a separate gate (`tests/tools/test_rule_doc_drift.py`, assertion C)
asserts the handler docstring is non-empty AND contains the substring
`"no CLI"` or `"no-CLI"` so the rationale is captured at the source
and discoverable from the registration. Today the `cli_command=None`
verbs are:

- `mcp__sdlc__check_hitl_answers` — no CLI; aggregates HITL state across phases.
- `mcp__brc__get_state` — no CLI; CLI `egg-orch consensus status` prints text, the tool returns the dict.
- `mcp__brc__list_blocking` — no CLI; derived view over BRC state.
- `mcp__brc__read_peer_artifact` — no CLI; reviewer-forensics helper that reads local files; operators inspect the files directly.
- `mcp__phase__get_context` — no CLI; environment + filtered task list bundle.
- `mcp__phase__get_assigned_tasks` — no CLI; filtered view over `egg-contract show`.
- `mcp__task__mark_gap` — no CLI; tester→coder coverage-gap handoff is agent-to-agent.

When adding a new `cli_command=None` verb, the handler docstring
must explain the no-CLI rationale; CI fails otherwise.

### Two-way rule-doc drift gate (decision-11)

`tests/tools/test_rule_doc_drift.py` asserts a two-way invariant:

- **A.** Every `Prefer this over `egg-…`` line in
  `sandbox/agent-config/rules/*.md` and
  `sandbox/egg_lib/data/hitl_editing_rules.md` resolves to a tool in
  `TOOL_REGISTRY`.
- **B.** Every registration with `cli_command != None` has a matching
  `Prefer this over …` line in at least one of those docs.
- **C.** Every registration with `cli_command == None` has a handler
  docstring containing `"no CLI"` or `"no-CLI"` (the rationale gate
  above).

The gate keeps rule docs and the registry from drifting in either
direction. When adding a new tool, add the `Prefer this over …` line
to the appropriate rule doc in the same PR; CI fails otherwise.

## Input/output schemas

Input schemas are derived automatically from the argparse subparsers
that back the CLI counterparts (`sandbox/egg_lib/orch_cli.py::create_parser`
and `sandbox/egg_lib/contract_cli.py::create_parser`) by
`sandbox/egg_agent_tools/schemas.py::derive_schema_from_argparse`.
Each tool may supply a per-tool override dict for cases where argparse
help is insufficient (e.g. richer descriptions or tighter enum
constraints). Tools with no CLI counterpart — `brc_get_state`,
`brc_list_blocking`, `phase_get_context`, `phase_get_assigned_tasks`,
`check_hitl_answers`, `brc_read_peer_artifact`, `task_mark_gap` —
declare their JSON schema directly in `schemas.py` (or alongside the
`@tool` definition).

Output: every tool returns the handler's dict response serialised as a
JSON string per the
[SDK tool contract](https://github.com/anthropics/claude-agent-sdk-python).
On error, the `@tool` wrapper catches `GatewayError` / `TimeoutError`
/ generic `Exception` and returns a structured
`{is_error: True, content: [{type: "text", text: <message>}]}` block.
Gateway flakes therefore surface as tool errors the agent can retry —
never as an agent crash.

## System-prompt nudge (`SYSTEM_PROMPT_NUDGE`)

When the flag is on (the default), `run_agent_async` appends a short bootstrap
paragraph (`≤200` words) to `options.system_prompt`. The paragraph is
**generated programmatically** at module import from `TOOL_NAMESPACES`
— it is not a hand-authored string literal — so adding or renaming a
namespace updates the nudge automatically. A unit test
(`tests/sandbox/egg_agent_tools/test_server.py::test_prompt_nudge_drift`)
asserts every `mcp__<namespace>__` substring in the nudge corresponds
to a registered namespace in `TOOL_NAMESPACES` and vice versa
(symmetric match — extras in either direction fail CI), AND asserts
`len(TOOL_REGISTRY) == 30` plus
`set(TOOL_NAMESPACES.keys()) == {"sdlc", "brc", "phase", "progress",
"task", "checkpoint"}` so a future iteration cannot drift the prose
counts in this file silently.

**The source of truth is `sandbox/egg_agent_tools/server.py::_render_nudge()`.**
This doc does NOT embed a copy of the rendered string — the template
currently iterates over every registered namespace and emits one
bullet per namespace plus a short description, then closes with a
sentence instructing the agent to prefer the `mcp__*` tools over
Bash. To see the exact text your agent will receive, read
`_render_nudge()` or inspect
`sandbox.egg_agent_tools.SYSTEM_PROMPT_NUDGE` at import time. The
renderer is intentionally namespace-driven so the nudge and
`TOOL_NAMESPACES` cannot drift — changes to the tool list update the
nudge on the next import, and the drift test in `test_server.py`
keeps both sides honest.

The nudge points agents at `mcp__<namespace>__*`, which is the
literal name Claude sees in `tool_use` blocks — the per-namespace
server split (one SDK MCP server per `sdlc` / `brc` / `phase` /
`progress` / `task` / `checkpoint` key) makes the composed
`mcp__<server_key>__<raw_name>` resolve directly to the semantic
name the nudge advertises. No mental prefix-prepending required.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Agent container (Python, claude_agent_sdk)                      │
│                                                                 │
│  shared/egg_agent/client.py::run_agent_async                    │
│      └── EGG_MCP_TOOLS ≠ falsy ▶ build_sandbox_mcp_server()     │
│                                                                 │
│                ┌─── sandbox/egg_agent_tools/ ───┐               │
│                │  server.py    (SDK factory)    │               │
│                │  schemas.py   (argparse→JSON)  │               │
│                │  tools/*.py   (@tool wrappers) │               │
│                │       │                        │               │
│                │       │ asyncio.to_thread()    │               │
│                │       ▼                        │               │
│                │  handlers/*.py (pure req→res)  │               │
│                │       │                        │               │
│                │       │ make_gateway_request   │               │
│                │       ▼                        │               │
│                │  GatewayError on 5xx/timeout   │               │
│                └────────┬────────────────────────┘              │
└────────────────────────────┬───────────────────────────────────┘
                             │ HTTP (same gateway path the CLIs use)
                             ▼
                    gateway / orchestrator
```

Checkpoint handlers do NOT go through the gateway — they import the
three pure helpers `collect_checkpoints` / `load_checkpoint` /
`search_checkpoints` from `shared/egg_contracts/checkpoint_cli.py` and
operate on local git-ref state. The CLI keeps its argparse + stdout
shape; the handler returns dicts. The drift gate asserts the handler
dispatches through the same helper path the CLI uses.

### Why in-process?

- **Network-mode neutral.** No sandbox-egress requirement — the MCP
  server runs in the agent's own interpreter. Works identically in
  `public` and `private` network modes.
- **Authz by construction.** Sandbox agents cannot invoke orchestrator
  MCP tools they do not register; there is no second MCP server to
  lock down.
- **Zero new dependency.** `claude-agent-sdk` is already a sandbox
  dependency; the `create_sdk_mcp_server` / `@tool` surface is what
  the SDK ships for exactly this case.

### Async + error discipline

`@tool` wrappers invoke their handlers via `asyncio.to_thread(handler,
req)` so the sync `urllib` gateway I/O does not block the agent event
loop. Handlers **raise** exceptions (`GatewayError`, `TimeoutError`,
`HandlerError`) — they **never** call `sys.exit`. The `@tool` wrapper
catches those exceptions and returns them as structured tool-result
error blocks of the form `{is_error: True, content: [{type: "text",
text: <message>}]}`, which the agent can surface as a tool error and
retry. The CLI `cmd_*` shim catches the same `GatewayError` and
renders the pre-#1765 stderr message + exit code for human callers,
so shell behaviour is byte-identical.

> **Handler rule — MUST NEVER `sys.exit`.** Every handler under
> `sandbox/egg_agent_tools/handlers/*.py` returns a dict response or
> raises a typed exception. A handler that calls `sys.exit` would
> terminate the Python interpreter the Claude Agent SDK is running in
> and bring the entire agent down (see the risk-analyst R1 note in
> `.egg-state/agent-outputs/1765-risk_analyst-output.json`). The
> same rule applies transitively to any helper imported by a handler
> — notably `make_gateway_request`, which backs every gateway-fronted
> handler in `egg_agent_tools` and was refactored in TASK-1-3 of
> #1765 to raise `GatewayError` instead of exiting; the same rule
> applies to the iter-2 checkpoint helpers, which return dicts and
> raise `HandlerError` instead of calling `sys.exit`. This rule is
> about **handlers**, not shell CLI shims: unrefactored `cmd_*`
> functions in `sandbox/egg_lib/orch_cli.py` may still call
> `sys.exit(1)` on argparse-level errors (e.g. missing `--role`),
> which is fine because they run in their own process, not inside
> the agent SDK loop. When adding a new verb, inherit this contract:
> handlers raise; `@tool` wrappers catch; any `sys.exit` lives only
> in a CLI shim that runs as a subprocess, never in code imported
> into the agent event loop.

See
[`.egg-state/drafts/1765-plan.md`](../../.egg-state/drafts/1765-plan.md)
for the iter-1 plan,
[`.egg-state/agent-outputs/1765-architect-output.json`](../../.egg-state/agent-outputs/1765-architect-output.json)
for the iter-1 architect's technical decisions, and
[`.egg-state/drafts/1917-plan.md`](../../.egg-state/drafts/1917-plan.md)
for the iter-2 plan that adds the remaining 12 verbs and the rule-doc
drift gate.

## CLI surface preserved (decision-4 of #1765)

Existing `sandbox/bin/egg-*` CLIs are **not deprecated**. Every
refactored `cmd_*` function in `sandbox/egg_lib/contract_cli.py`,
`sandbox/egg_lib/orch_cli.py`, and `shared/egg_contracts/checkpoint_cli.py`
still:

- Accepts the same argparse flags.
- Prints the same stdout text.
- Exits with the same codes.

Only the internal call flow changes — `cmd_*` now builds a request
dict from `argparse.Namespace`, calls the shared `handlers.*`
function (or shared helper, in the case of checkpoint), and renders
the response for stdout. Humans, bash scripts, recovery tooling, and
the existing test suite see zero behaviour change. Parity is enforced
by committed fixture tests under `tests/sandbox/test_contract_cli.py`,
`tests/sandbox/test_orch_cli.py`, and
`tests/shared/egg_contracts/test_checkpoint_cli*.py` (no auto-record
— every expected value is in the repo).

See [Orchestrator CLI reference](orchestrator-cli.md), [SDLC Contract
reference](sdlc-contract.md), and
[Checkpoint Browser reference](checkpoint-browser.md) for the
complete shell CLI surface.

## Known limitations

- **Harness coverage (decision-3 of #1765):** Only the
  `claude_agent_sdk` harness registers the MCP tools. The
  experimental `EGG_HARNESS=egg` path is **not yet covered** — when
  it graduates from experimental to supported, a parallel
  registration will land (decision-10 of #1917 keeps this deferred).
- **Anchor verbs (decision-2 of #1917):** The capability audit also
  surfaced `anchor_init` / `anchor_update` / `anchor_get`. They are
  deferred to iteration 3 so the anchor design can be done
  deliberately. The phantom `egg-orch anchor *` CLI references in
  `sandbox/agent-config/rules/orchestrator.md` will be retracted
  alongside the iter-3 anchor MCP landing.
- **Directed peer messaging (decision-14 of #1917):**
  `brc_send_message` / `brc_poll_messages` remain deferred pending
  the REQUEST/REPLY subsystem.
- **Checkpoint `browse` / `context` / `cost`:** Excluded from iter-2
  per decision-3 (core 3 only — `list` / `show` / `search`); these
  three remain CLI-only.
- **Phase-context field promotion (decision-6 of #1917):**
  `active_peers` / `reviewer_peers` / `hitl_pending` on
  `mcp__phase__get_context` stay best-effort; promotion to required
  is a separate follow-up.
- **`EGG_MCP_TOOLS` flag removal (decision-9 of #1917):** Kept for
  iter-2 burn-in; removal is a third follow-up.
- **Timeouts:** The SDK's default 60 s MCP-tool timeout is sufficient
  for all 30 verbs (none are long-running). Pagination (decision-12
  of #1917) keeps `read_peer_artifact` / `checkpoint_list` /
  `checkpoint_search` page sizes well under the limit. If a future
  tool needs to exceed 60 s, it must be restructured as a
  start/poll/complete triplet — handled in a follow-up.
- **Observability:** Native SDK `tool_use` naming is enough today —
  `mcp__brc__propose` vs `Bash` surfaces cleanly in checkpoint logs.
  No changes to the [Checkpoint Browser](checkpoint-browser.md) are
  required.

## Version pin

`claude-agent-sdk` is pinned to `>=0.1.65,<0.2` in
`sandbox/pyproject.toml` and the `CLAUDE_AGENT_SDK_VERSION` ARG in
`sandbox/Dockerfile`. A smoke test at
`tests/sandbox/egg_agent_tools/test_sdk_surface.py` imports
`claude_agent_sdk.create_sdk_mcp_server` and `claude_agent_sdk.tool`
at module load time; if a future pre-1.0 SDK bump changes that
surface, CI fails at test-collection time with a clear pointer to the
SDK release notes rather than silently breaking every sandbox.

## Testing

| Test | Purpose |
|------|---------|
| `tests/sandbox/egg_agent_tools/test_handlers_*.py` | Unit tests for each handler (happy-path, missing-arg, 5xx gateway → `GatewayError`). |
| `tests/sandbox/egg_agent_tools/handlers/test_*.py` | Per-handler unit tests for the iter-2 verbs (`show_contract`, `add_commit`, `update_notes`, `complete_phase`, `verify_criterion`, `read_peer_artifact`, `overseer_alert`, `query_status`, `checkpoint`, `mark_gap`). |
| `tests/sandbox/egg_agent_tools/test_tools.py` | `@tool` wrappers (JSON-serialised success; `is_error=True` structured block on handler exception). |
| `tests/sandbox/egg_agent_tools/test_server.py` | `build_sandbox_mcp_server` registers all 30 tools; `SYSTEM_PROMPT_NUDGE` symmetric drift test; derived-count assertions (`len(TOOL_REGISTRY) == 30` and the 6-namespace set). |
| `tests/sandbox/egg_agent_tools/test_schemas.py` | `derive_schema_from_argparse` correctness + override merge. |
| `tests/sandbox/egg_agent_tools/test_sdk_surface.py` | SDK import smoke (fails loud on incompatible SDK upgrade). |
| `tests/sandbox/egg_agent_tools/test_full_tool_registry.py` | Integration test: loads `TOOL_LIST` via `create_sdk_mcp_server`; asserts no registration errors and that completion/mutation verbs (`task_complete`, `phase__complete_phase`, `task__add_commit`, `sdlc__verify_criterion`) name the state-machine effect in their description. |
| `tests/shared/egg_agent/test_client.py` | Flag-on/flag-off wire-up in `run_agent_async`; `can_use_tool` passes `mcp__*` tool names. |
| `tests/sandbox/test_contract_cli.py`, `tests/sandbox/test_orch_cli.py`, `tests/shared/egg_contracts/test_checkpoint_cli*.py` | CLI parity against committed fixtures. |
| `tests/tools/test_mcp_cli_drift.py` | Every tool with a `cli_command` attribute dispatches the same handler as its CLI subparser (or shared helper, for checkpoint). |
| `tests/tools/test_rule_doc_drift.py` | Two-way rule-doc invariant: (A) every `Prefer this over `egg-…`` line resolves to a `TOOL_REGISTRY` entry; (B) every `cli_command != None` registration has a matching rule-doc line; (C) every `cli_command == None` registration has a handler docstring mentioning `"no CLI"` or `"no-CLI"` (decision-13 gate). |
| `tests/shared/egg_contracts/test_models_gaps.py` | Pydantic round-trip for `Task.gaps`; back-compat with old contract fixtures (parse to `gaps: []`). |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` | Marker-gated live SDK round-trip — asserts the agent's first `tool_use` block names an `mcp__*` tool. |

## Related

- [Orchestrator CLI](orchestrator-cli.md) — full `egg-orch` shell
  surface (still the source of truth for human operators).
- [SDLC Contract](sdlc-contract.md) — full `egg-contract` shell
  surface.
- [Checkpoint Browser](checkpoint-browser.md) — full `egg-checkpoint`
  shell surface (CLI `browse` / `context` / `cost` remain CLI-only).
- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — per-pipeline
  opt-out recipe for `EGG_MCP_TOOLS`.
- [Concurrent Execution Guide](../guides/concurrent-execution.md) —
  where BRC + consensus + message-bus live, which the `mcp__brc__*`
  namespace exposes.
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) —
  `EGG_MCP_TOOLS` alongside other sandbox env flags.
- [Custom Harness](../architecture/custom-harness.md) — harness
  coverage (decision-3 of #1765): MCP tools are
  `claude_agent_sdk`-only today.
- [#1765](https://github.com/jwbron/egg/issues/1765) — iteration 1
  (mechanism + 18 verbs).
- [#1917](https://github.com/jwbron/egg/issues/1917) — iteration 2
  (12 additional verbs + rule-doc drift gate + decision-13 gate).
- [#1955](https://github.com/jwbron/egg/issues/1955) — closed by
  iteration 2's `mcp__sdlc__show_contract` + state-machine writes.
