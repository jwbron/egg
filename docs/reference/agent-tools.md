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

## Opt-in — `EGG_MCP_TOOLS`

The MCP tool surface is gated behind an environment variable:

| Flag | Current default | Effect |
|------|------------------|--------|
| `EGG_MCP_TOOLS=true` (or `1` / `yes`) | — | Registers the 15 iteration-1 tools on `options.mcp_servers['egg']` and appends `SYSTEM_PROMPT_NUDGE` to `options.system_prompt`. |
| `EGG_MCP_TOOLS` unset / `false` / `0` | **Default** | Code path is byte-identical to the pre-#1765 behaviour. Non-opt-in pipelines pay no import cost and see no prompt changes. |

The default is **off** for iteration 1 so opt-in pipelines can burn in
the surface against a subset of workloads. A follow-up PR will flip
the default to `true` once metrics show ≥15 % reduction in
turns-per-phase for refine/plan. A later follow-up PR will remove the
flag entirely.

Set the flag on a pipeline via pod env, Docker Compose, or the
`env` stanza on any submit-task payload. See
[docs/guides/sdlc-pipeline.md — Agent MCP tools
(EGG_MCP_TOOLS flag)](../guides/sdlc-pipeline.md#agent-mcp-tools-egg_mcp_tools-flag)
for the per-pipeline opt-in recipe.

## Tool inventory (15 verbs)

All tools land on the SDK as `mcp__<namespace>__<verb>`, where
`<namespace>` is one of `sdlc`, `brc`, `phase`, `progress`, `task`.
The SDK server name is always `egg` — hence the `mcp__egg__*` server
qualifier is collapsed into the tool name when the SDK normalises it.

Every tool with a shell-CLI counterpart declares a `cli_command`
attribute (e.g. `("egg-orch", "consensus", "propose")`) so a CI drift
test (`tests/tools/test_mcp_cli_drift.py`) can assert the MCP tool and
the CLI subparser dispatch the same handler function. If a handler
moves, both surfaces move together or CI fails.

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
| `mcp__progress__emit` | Emit a structured progress event (`step`, `state`, `detail`, `blocker`). | `handlers.progress.progress_emit` | `egg-orch progress emit` |
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

When `EGG_MCP_TOOLS=true`, `run_agent_async` appends a short bootstrap
paragraph (`≤200` words) to `options.system_prompt`. The paragraph is
**generated programmatically** at module import from `TOOL_NAMESPACES`
— it is not a hand-authored string literal — so adding or renaming a
namespace updates the nudge automatically. A unit test
(`tests/sandbox/egg_agent_tools/test_server.py::test_prompt_nudge_drift`)
asserts every `mcp__<namespace>__` substring in the nudge corresponds
to a registered namespace in `TOOL_NAMESPACES` and vice versa
(symmetric match — extras in either direction fail CI).

Typical rendering (iteration 1):

```
You have first-class MCP tools for the operations you perform in
every phase. Prefer them over Bash-ing the corresponding egg CLI.

Tool namespaces:
- `mcp__sdlc__*` — register a HITL decision, request open-ended
  feedback, check for human answers.
- `mcp__brc__*` — propose, ACK, NACK, confirm; query structured BRC
  state; list agents currently blocking consensus.
- `mcp__phase__*` — get your phase context (role, pipeline id,
  assigned tasks, prior-phase artifacts) and fetch your task list.
- `mcp__progress__*` — emit a progress update, heartbeat, or
  signal an error.
- `mcp__task__*` — mark a task complete.

Use the tool directly; do not run `egg-orch consensus propose`,
`egg-contract add-decision`, etc. through Bash when an MCP tool
covers the same capability. The shell CLIs remain available for
other tooling but are slower and less reliable for agent use.
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Agent container (Python, claude_agent_sdk)                      │
│                                                                 │
│  shared/egg_agent/client.py::run_agent_async                    │
│      └── EGG_MCP_TOOLS=true ──▶ build_sandbox_mcp_server()      │
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
exceptions and returns them as structured tool-result error blocks.
The CLI `cmd_*` shim catches the same `GatewayError` and renders the
pre-#1765 stderr message + exit code for human callers, so shell
behaviour is byte-identical.

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
  opt-in recipe for `EGG_MCP_TOOLS`.
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
