# Agent MCP Tools Reference

> Sandbox agents can call pipeline lifecycle operations (BRC consensus,
> HITL decisions, phase context, progress signals, task completion)
> through first-class MCP tools on the Claude Agent SDK `tool_use`
> stream, instead of shelling out to `egg-contract` / `egg-orch` via
> `Bash`.

The tools are exposed as an in-process SDK MCP server built with
[`claude_agent_sdk.create_sdk_mcp_server`](https://github.com/anthropics/claude-agent-sdk-python)
and registered on `ClaudeAgentOptions.mcp_servers` by
[`shared/egg_agent/client.py::run_agent_async`](../../shared/egg_agent/client.py).
There is **no new network service, no new auth layer, no new process**
— the tools run in the agent's own Python interpreter and call the
same handler functions the `egg-contract` / `egg-orch` CLIs call. The
work that introduces them is tracked in
[#1765](https://github.com/jwbron/egg/issues/1765).

## Flag — `EGG_MCP_TOOLS`

The MCP tool surface is **on by default** since [#1942](https://github.com/jwbron/egg/issues/1942). The env var now acts as a kill-switch:

| Flag | Effect |
|------|--------|
| `EGG_MCP_TOOLS` unset or any value not listed below | **Default.** Registers the 15 iteration-1 tools (one server per namespace) on `options.mcp_servers` and appends `SYSTEM_PROMPT_NUDGE` to `options.system_prompt`. |
| `EGG_MCP_TOOLS=false` (or `0` / `no` / `off`) | Opt-out. Code path is byte-identical to the pre-#1765 behaviour — no `mcp_servers` registration, no prompt changes, no import cost. |

Iteration 1 (#1765) shipped the flag default-off while the wire-up burned in.
#1942 flipped the default to on and kept the env var as a rollback
switch; a later follow-up will remove the flag entirely once the
tools are considered stable.

To opt a pipeline out, set `EGG_MCP_TOOLS=false` via pod env, Docker
Compose, or the `env` stanza on any submit-task payload. See
[docs/guides/sdlc-pipeline.md — Agent MCP tools
(EGG_MCP_TOOLS flag)](../guides/sdlc-pipeline.md#agent-mcp-tools-egg_mcp_tools-flag)
for the per-pipeline recipe.

## Tool inventory (15 verbs)

All 15 tools are registered as `@tool`-decorated wrappers in
`sandbox/egg_agent_tools/tools/*.py`. The raw `@tool` name is the verb
itself (e.g. `"propose"`, `"register_open_question"`).

### Tool-name resolution (how Claude sees these tools)

The SDK renders an MCP tool in `tool_use` blocks as
`mcp__<server_key>__<raw_tool_name>`. `build_sandbox_mcp_server`
returns a `{namespace: server}` dict — one SDK MCP server per
namespace, keyed by `sdlc`, `brc`, `phase`, `progress`, or `task` —
and `shared/egg_agent/client.py::run_agent_async` merges that dict
into `options.mcp_servers` unless `EGG_MCP_TOOLS` is explicitly falsy. With raw
`@tool` names declared as plain verbs, Claude's composition
naturally produces the semantic names in the tables below:

- raw name `propose` in server key `brc` → `mcp__brc__propose`
- raw name `register_open_question` in server key `sdlc` →
  `mcp__sdlc__register_open_question`
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
counterpart) — the drift gate will refuse the PR otherwise.

### `mcp__sdlc__*` — HITL and contract-level operations

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__sdlc__register_open_question` | Create a HITL decision (multiple-choice) on the contract. | `handlers.sdlc.register_open_question` | `egg-contract add-decision` |
| `mcp__sdlc__request_feedback` | Create an open-ended feedback request on the contract. | `handlers.sdlc.request_feedback` | `egg-contract add-feedback` |
| `mcp__sdlc__check_hitl_answers` | Return resolved decisions and submitted feedback for the current contract. Optional `phase` filter. | `handlers.sdlc.check_hitl_answers` | — *(new capability)* |

### `mcp__brc__*` — Broadcast-Review-Converge consensus

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__brc__propose` | Broadcast a proposal with summary, artifacts, files-changed, tests-run, tasks, and commit SHA. | `handlers.brc.brc_propose` | `egg-orch consensus propose` |
| `mcp__brc__ack` | Acknowledge (ACK) a peer's proposal. | `handlers.brc.brc_ack` | `egg-orch consensus ack` |
| `mcp__brc__nack` | Reject (NACK) a peer's proposal with blocker list. | `handlers.brc.brc_nack` | `egg-orch consensus nack` |
| `mcp__brc__confirm` | Signal CONFIRMED — producer acknowledges all reviewer ACKs. | `handlers.brc.brc_confirm` | `egg-orch consensus confirmed` |
| `mcp__brc__get_state` | Full structured consensus state (JSON; accepts `verbose: bool`). | `handlers.brc.brc_get_state` | — *(CLI `egg-orch consensus status` prints text; this tool returns the dict)* |
| `mcp__brc__list_blocking` | Return the list of agent roles currently blocking consensus (derived view). | `handlers.brc.brc_list_blocking` | — *(new capability)* |

### `mcp__phase__*` — Phase context

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__phase__get_context` | Bundle `EGG_PIPELINE_ID`, `EGG_PHASE`, `EGG_AGENT_ROLE`, the role-filtered task list, and prior-phase artifact paths (`.egg-state/drafts/`, `.egg-state/agent-outputs/`). | `handlers.phase.phase_get_context` | — *(new capability)* |
| `mcp__phase__get_assigned_tasks` | Return only the tasks assigned to the caller's role (`EGG_AGENT_ROLE`) from the contract. | `handlers.phase.phase_get_assigned_tasks` | — *(filtered view over `egg-contract show`)* |

Some fields on `mcp__phase__get_context` are best-effort (e.g.
`active_peers`, `reviewer_peers`, `hitl_pending`); iteration 1 treats
them as optional and promotes them in iteration 2
([#1917](https://github.com/jwbron/egg/issues/1917)).

### `mcp__progress__*` — Progress signals

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__progress__emit` | Emit a structured progress event: required `step` (step name) and `state` (`working`/`blocked`/`complete`), optional `detail` and `blocker`. | `handlers.progress.progress_emit` | `egg-orch progress emit` |
| `mcp__progress__signal_error` | Signal an error to the orchestrator (`--error <msg>` payload + recoverable flag). | `handlers.progress.progress_signal_error` | `egg-orch signal error` |
| `mcp__progress__heartbeat` | Send a heartbeat so the orchestrator knows the agent is alive. | `handlers.progress.progress_heartbeat` | `egg-orch signal heartbeat` |

### `mcp__task__*` — Task completion

| Tool | Purpose | Handler | CLI counterpart |
|------|---------|---------|-----------------|
| `mcp__task__complete` | Mark a contract task complete, optionally linking a commit SHA. | `handlers.task.task_complete` | `egg-contract complete-task` |

Total: **15 tools** across 5 namespaces, covering the BRC consensus
loop, HITL (decisions + feedback + answers), phase context, progress
signals, and task completion — every verb a pipeline agent issues on
the hot path.

## Input/output schemas

Input schemas are derived automatically from the argparse subparsers
that back the CLI counterparts (`sandbox/egg_lib/orch_cli.py::create_parser`
and `sandbox/egg_lib/contract_cli.py::create_parser`) by
`sandbox/egg_agent_tools/schemas.py::derive_schema_from_argparse`.
Each tool may supply a per-tool override dict for cases where argparse
help is insufficient (e.g. richer descriptions or tighter enum
constraints). Tools with no CLI counterpart — `brc_get_state`,
`brc_list_blocking`, `phase_get_context`, `phase_get_assigned_tasks`,
`check_hitl_answers` — declare their JSON schema directly in
`schemas.py`.

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
(symmetric match — extras in either direction fail CI).

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
`progress` / `task` key) makes the composed
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
loop. Handlers **raise** exceptions (`GatewayError`, `TimeoutError`)
— they **never** call `sys.exit`. The `@tool` wrapper catches those
exceptions and returns them as structured tool-result error blocks
of the form `{is_error: True, content: [{type: "text", text:
<message>}]}`, which the agent can surface as a tool error and
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
> — notably `make_gateway_request`, which backs every handler in
> `egg_agent_tools` and was refactored in TASK-1-3 to raise
> `GatewayError` instead of exiting. This rule is about **handlers**,
> not shell CLI shims: unrefactored `cmd_*` functions in
> `sandbox/egg_lib/orch_cli.py` may still call `sys.exit(1)` on
> argparse-level errors (e.g. missing `--role`), which is fine
> because they run in their own process, not inside the agent SDK
> loop. When adding a new verb for
> [#1917](https://github.com/jwbron/egg/issues/1917), inherit this
> contract: handlers raise; `@tool` wrappers catch; any `sys.exit`
> lives only in a CLI shim that runs as a subprocess, never in code
> imported into the agent event loop.

See
[`.egg-state/drafts/1765-plan.md`](../../.egg-state/drafts/1765-plan.md)
for the full plan and
[`.egg-state/agent-outputs/1765-architect-output.json`](../../.egg-state/agent-outputs/1765-architect-output.json)
for the architect's technical decisions.

## CLI surface preserved (decision-4)

Existing `sandbox/bin/egg-*` CLIs are **not deprecated**. Every
refactored `cmd_*` function in `sandbox/egg_lib/contract_cli.py` and
`sandbox/egg_lib/orch_cli.py` still:

- Accepts the same argparse flags.
- Prints the same stdout text.
- Exits with the same codes.

Only the internal call flow changes — `cmd_*` now builds a request
dict from `argparse.Namespace`, calls the shared `handlers.*`
function, and renders the response for stdout. Humans, bash scripts,
recovery tooling, and the existing test suite see zero behaviour
change. Parity is enforced by committed fixture tests under
`tests/sandbox/test_contract_cli.py` and
`tests/sandbox/test_orch_cli.py` (no auto-record — every expected
value is in the repo).

See [Orchestrator CLI reference](orchestrator-cli.md) and [SDLC Contract
reference](sdlc-contract.md) for the complete shell CLI surface.

## Known limitations

- **Harness coverage (decision-3):** Only the `claude_agent_sdk`
  harness registers the MCP tools in iteration 1. The experimental
  `EGG_HARNESS=egg` path is **not yet covered** — when it graduates
  from experimental to supported, a parallel registration will land.
- **Iteration-2 verbs (decision-8):** The capability audit surfaced
  roughly 15 additional verbs (peer, checkpoint, anchor, overseer,
  task-gap) that are out of scope for iteration 1 and are tracked in
  [#1917](https://github.com/jwbron/egg/issues/1917). Agents that
  need those still shell out to the corresponding CLI.
- **Timeouts:** The SDK's default 60 s MCP-tool timeout is sufficient
  for all 15 iteration-1 verbs (none are long-running). If a tool ever
  needs to exceed 60 s, it must be restructured as a
  start/poll/complete triplet — handled in a follow-up.
- **Observability:** Native SDK `tool_use` naming is enough for
  iteration 1 — `mcp__brc__propose` vs `Bash` surfaces cleanly in
  checkpoint logs. No changes to the
  [Checkpoint Browser](checkpoint-browser.md) are required.

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
| `tests/sandbox/egg_agent_tools/test_tools.py` | `@tool` wrappers (JSON-serialised success; `is_error=True` structured block on handler exception). |
| `tests/sandbox/egg_agent_tools/test_server.py` | `build_sandbox_mcp_server` registers the 15 tools; `SYSTEM_PROMPT_NUDGE` symmetric drift test. |
| `tests/sandbox/egg_agent_tools/test_schemas.py` | `derive_schema_from_argparse` correctness + override merge. |
| `tests/sandbox/egg_agent_tools/test_sdk_surface.py` | SDK import smoke (fails loud on incompatible SDK upgrade). |
| `tests/shared/egg_agent/test_client.py` | Flag-on/flag-off wire-up in `run_agent_async`; `can_use_tool` passes `mcp__*` tool names. |
| `tests/sandbox/test_contract_cli.py`, `tests/sandbox/test_orch_cli.py` | CLI parity against committed fixtures. |
| `tests/tools/test_mcp_cli_drift.py` | Every tool with a `cli_command` attribute dispatches the same handler as its CLI. |
| `integration_tests/test_sandbox_mcp_tools_e2e.py` | Marker-gated live SDK round-trip — asserts the agent's first tool_use block names an `mcp__*` tool. |

## Related

- [Orchestrator CLI](orchestrator-cli.md) — full `egg-orch` shell
  surface (still the source of truth for human operators).
- [SDLC Contract](sdlc-contract.md) — full `egg-contract` shell
  surface.
- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — per-pipeline
  opt-out recipe for `EGG_MCP_TOOLS`.
- [Concurrent Execution Guide](../guides/concurrent-execution.md) —
  where BRC + consensus + message-bus live, which the `mcp__brc__*`
  namespace exposes.
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) —
  `EGG_MCP_TOOLS` alongside other sandbox env flags.
- [Custom Harness](../architecture/custom-harness.md) — harness
  coverage (decision-3): MCP tools are `claude_agent_sdk`-only in
  iteration 1.
- [#1765](https://github.com/jwbron/egg/issues/1765) — the
  originating issue and capability audit.
- [#1917](https://github.com/jwbron/egg/issues/1917) — tracks the
  iteration-2 verbs (peer, checkpoint, anchor, overseer, task-gap).
