# Release note — Agent MCP tools (iteration 1)

**Issue:** [#1765](https://github.com/jwbron/egg/issues/1765) — make
egg-internal tools discoverable to sandbox agents so they do not burn
tool calls re-deriving them from source.

## What changed

Sandbox agents can now invoke 15 pipeline-lifecycle verbs as
first-class Claude Agent SDK MCP tools on the same `tool_use` stream
they already handle, instead of shelling out to `egg-contract` /
`egg-orch` via `Bash`.

The tools run **in-process** via `claude_agent_sdk.create_sdk_mcp_server`
— no new network service, no new auth layer, no new process. The MCP
`@tool` wrappers and the existing CLI `cmd_*` functions call the same
pure handler functions (`sandbox/egg_agent_tools/handlers/*.py`), and a
CI drift test (`tests/tools/test_mcp_cli_drift.py`) enforces the
one-to-one mapping.

## Flag — `EGG_MCP_TOOLS`

The surface is **on by default** since [#1942](https://github.com/jwbron/egg/issues/1942). Iteration 1 shipped it default-off; the flag was flipped once the wire-up was stable. The env var is retained as a rollback switch:

| Flag | Effect |
|------|--------|
| `EGG_MCP_TOOLS` unset or any value not listed below | **Default.** Register the 15 tools and append `SYSTEM_PROMPT_NUDGE` to `options.system_prompt`. |
| `EGG_MCP_TOOLS=false` / `0` / `no` / `off` | Opt-out. Code path is byte-identical to the pre-#1765 behaviour — no import cost, no prompt changes. |

## Iteration-1 verbs (15 total)

Namespaces (each backed by its own SDK MCP server, keyed by
namespace in `options.mcp_servers`): `mcp__sdlc__*`, `mcp__brc__*`,
`mcp__phase__*`, `mcp__progress__*`, `mcp__task__*`. Names below are
the literal tool names Claude sees in `tool_use` blocks — see
[docs/reference/agent-tools.md — Tool-name resolution](../reference/agent-tools.md#tool-name-resolution-how-claude-sees-these-tools).

| # | Tool | Handler | CLI counterpart |
|---|------|---------|-----------------|
| 1 | `mcp__sdlc__register_open_question` | `handlers.sdlc.register_open_question` | `egg-contract add-decision` |
| 2 | `mcp__sdlc__request_feedback` | `handlers.sdlc.request_feedback` | `egg-contract add-feedback` |
| 3 | `mcp__sdlc__check_hitl_answers` | `handlers.sdlc.check_hitl_answers` | — *(new capability)* |
| 4 | `mcp__brc__propose` | `handlers.brc.brc_propose` | `egg-orch consensus propose` |
| 5 | `mcp__brc__ack` | `handlers.brc.brc_ack` | `egg-orch consensus ack` |
| 6 | `mcp__brc__nack` | `handlers.brc.brc_nack` | `egg-orch consensus nack` |
| 7 | `mcp__brc__confirm` | `handlers.brc.brc_confirm` | `egg-orch consensus confirmed` |
| 8 | `mcp__brc__get_state` | `handlers.brc.brc_get_state` | *(structured form of `egg-orch consensus status`)* |
| 9 | `mcp__brc__list_blocking` | `handlers.brc.brc_list_blocking` | — *(new capability)* |
| 10 | `mcp__phase__get_context` | `handlers.phase.phase_get_context` | — *(new capability)* |
| 11 | `mcp__phase__get_assigned_tasks` | `handlers.phase.phase_get_assigned_tasks` | *(filtered view over `egg-contract show`)* |
| 12 | `mcp__progress__emit` | `handlers.progress.progress_emit` | `egg-orch progress emit` |
| 13 | `mcp__progress__signal_error` | `handlers.progress.progress_signal_error` | `egg-orch signal error` |
| 14 | `mcp__progress__heartbeat` | `handlers.progress.progress_heartbeat` | `egg-orch signal heartbeat` |
| 15 | `mcp__task__complete` | `handlers.task.task_complete` | `egg-contract complete-task` |

## Backward compatibility

The existing `sandbox/bin/egg-*` CLIs are **not deprecated**
(decision-4). Every refactored `cmd_*` function in
`sandbox/egg_lib/contract_cli.py` and `sandbox/egg_lib/orch_cli.py`
keeps the same argparse flags, stdout text, and exit codes. Humans,
bash scripts, recovery tooling, and the existing test suite see zero
behaviour change. Parity is enforced by committed-fixture tests.

## SDK pin

`claude-agent-sdk` is now pinned to the literal range
**`>=0.1.65,<0.2`** in both `sandbox/pyproject.toml` (the
`dependencies` array) and the `CLAUDE_AGENT_SDK_VERSION` build ARG in
`sandbox/Dockerfile`. `0.1.65` is the version where the
`create_sdk_mcp_server` + `@tool` surface this package depends on was
confirmed; `<0.2` guards against the pre-1.0 API breakage risk-analyst
R2 flagged. A smoke test
(`tests/sandbox/egg_agent_tools/test_sdk_surface.py`) imports
`claude_agent_sdk.create_sdk_mcp_server` and `claude_agent_sdk.tool`
at module load time so a future pre-1.0 bump fails CI loudly at
test-collection time rather than silently breaking every sandbox.

## Ongoing guardrails

- **Drift CI gate** (`tests/tools/test_mcp_cli_drift.py`): every tool
  that has a shell-CLI counterpart must declare a `cli_command`
  tuple (e.g. `("egg-orch", "consensus", "propose")`) on its
  `ToolRegistration`; the test asserts the tool wrapper and the CLI
  subparser both dispatch to the same handler function. Tools with no
  CLI counterpart set `cli_command=None` and are skipped. **New verbs
  must register a `cli_command` attribute (or explicit `None`) or
  drift CI fails**, which is the mechanism keeping MCP tool surface
  and shell-CLI surface from silently diverging.
- **SYSTEM_PROMPT_NUDGE drift tests**
  (`tests/sandbox/egg_agent_tools/test_server.py::TestSystemPromptNudge::test_each_namespace_appears_in_nudge`
  and `test_nudge_substrings_back_to_registered_namespaces`):
  symmetric match between `mcp__<namespace>__` substrings in the
  rendered nudge and registered namespaces in `TOOL_NAMESPACES`.
  Extras in either direction fail CI.

## Follow-ups

- **Default flipped — [#1942](https://github.com/jwbron/egg/issues/1942):**
  The tool surface is now on by default; `EGG_MCP_TOOLS=false` is the
  rollback switch. A later follow-up will remove the flag entirely.
- **Iteration 2 verbs — [#1917](https://github.com/jwbron/egg/issues/1917):**
  Roughly 15 additional verbs surfaced in the capability audit (peer,
  checkpoint, anchor, overseer, task-gap) are tracked there.

## References

- [Agent MCP Tools reference](../reference/agent-tools.md) — full
  tool surface (15 iteration-1 verbs; see the reference for the current inventory), schemas, architecture, testing matrix.
- [SDLC Pipeline Guide — Agent MCP tools section](../guides/sdlc-pipeline.md#agent-mcp-tools-egg_mcp_tools-flag)
  — per-pipeline configuration.
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md)
  — `EGG_MCP_TOOLS` alongside other sandbox env flags.
- [`.egg-state/drafts/1765-plan.md`](../../.egg-state/drafts/1765-plan.md)
  — full plan (20 tasks across 6 phases).
- [`.egg-state/agent-outputs/1765-architect-output.json`](../../.egg-state/agent-outputs/1765-architect-output.json)
  — architect technical decisions.
- [`.egg-state/drafts/1765-analysis.md`](../../.egg-state/drafts/1765-analysis.md)
  — refine-phase analysis (A/B/C/D evaluation and capability audit).
