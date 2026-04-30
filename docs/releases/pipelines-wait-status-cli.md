# Release note — `egg-orch pipeline wait-status` CLI (host-side Bash wait)

**Issue:** [#2211](https://github.com/jwbron/egg/issues/2211) —
replace the MCP wait tools with a Bash CLI to eliminate the
wake-storm caused by the MCP transport's tool-call cap. Long-poll
waits don't fit the MCP transport: the streamable-HTTP MCP transport
caps tool calls at ~30 s
([anthropics/claude-code#20335](https://github.com/anthropics/claude-code/issues/20335),
closed not-planned) and the in-process SDK MCP caps at ~60 s. Every
cap-elapsed return cost a full LLM turn even on a pipeline with zero
state change.

## What changed

1. **Three MCP tools removed.**
   - `wait_for_status_change` (host, streamable-HTTP MCP) — superseded by
     `egg-orch pipeline wait-status`.
   - `mcp__brc__wait_for_event` (sandbox, in-process SDK MCP) — superseded
     by the existing `egg-orch message wait` CLI.
   - `mcp__brc__wait_loop` (sandbox, in-process SDK MCP) — superseded by
     the existing `egg-orch message wait-loop` CLI. The MCP wrapper's
     outer-loop was always defeated by the SDK's tool-call cap (the
     wrapper is itself a tool call), so the CLI was the only path that
     ever actually looped.
2. **New CLI** `egg-orch pipeline wait-status <id> [--since <cursor>]`
   in `orchestrator/cli.py`. Loops `GET /api/v1/pipelines/<id>/status/wait`,
   threads the cursor, emits one JSON-line per pipeline-relevant event on
   stdout, silent on `no_change`. Exit codes per the
   [§3 contract](../reference/agent-wait-patterns.md#3-egg-orch-message-wait--exit-code-contract):
   0 terminal, 1 max-iter (test only), 2 transient, 3 permanent.
3. **SDLC skill rewritten.** Phase 3 (Monitor) and Phase S5
   (`babysit_pr`) call the new CLI via Bash. `get_status` MCP stays
   for one-shot snapshots. The cursor-handling protocol persists but
   moves from "thread `response.cursor` into next MCP call's
   `since`" to "thread last JSON-line's `cursor` into next Bash
   invocation's `--since`".
4. **Sandbox prompts unchanged.** `egg-orch message wait` and
   `egg-orch message wait-loop` were already the canonical idiom per
   `sandbox/agent-config/rules/mission.md` and §1 of
   `agent-wait-patterns.md`. Removing the MCP variants just deletes a
   path agents weren't supposed to use.
5. **Route unchanged.** `/api/v1/pipelines/<id>/status/wait` is still
   the underlying wire endpoint. The 25 s `GET_STATUS_MAX_WAIT` cap,
   the EventBus + message-bus event allowlist, the opaque
   `msg:<id>|evt:<seq>` cursor, and the queue + daemon-thread
   concurrency model documented in
   [§7 of `agent-wait-patterns.md`](../reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status)
   are all preserved.
6. **Waitress thread budget unchanged.** The route still holds 2
   threads per in-flight wait. The 16 → 24 default raised in #1932
   stays — the CLI calls the same route, so per-call thread cost is
   identical to the prior MCP-driven path.

## Rationale

A fully idle implement phase under the MCP variant produced ~144
host-LLM turns/hour (3600 s ÷ 25 s cap) and ~60 sandbox turns/hour
per agent (3600 s ÷ 60 s cap), with zero state change. Each turn
re-reads full conversation context and re-renders the dashboard.
Moving the loop into the CLI process collapses a quiet hour to ~6
LLM turns (3600 s ÷ 600 s Claude Code Bash cap) — a 24× reduction
in token cost during quiet phases.

Event-triggered behavior is preserved: `OVERSEER_ALERT`, phase
transitions, terminal pipeline states, HITL `DECISION_CREATED`, and
consensus messages all still wake the wait within ~1 s and surface
to the LLM as the next stdout JSON-line.

## Why a single carve-out from "prefer MCP"

[#1917](https://github.com/jwbron/egg/issues/1917) established
"prefer MCP over Bash CLI" across the (then-)30-verb tool surface. The new
rule is **MCP for state queries, Bash CLI for blocking waits** —
not a wholesale reversal. State queries (`get_status`,
`get_consensus_status`, `get_pipeline_snapshot`, etc.) fit the MCP
short-call shape and stay MCP. Only the wait verbs need to block
longer than the MCP cap allows; the carve-out is documented in
[`docs/reference/agent-tools.md`](../reference/agent-tools.md) and
[`sandbox/agent-config/rules/orchestrator.md`](../../sandbox/agent-config/rules/orchestrator.md).

## Migration

Atomic swap. The MCP tool removal, skill prompt rewrite, CLI add,
and doc updates land in the same PR — splitting causes a window
where the skill calls a non-existent tool. No deprecation period:
the MCP tools were thin shims over the route, the route is
unchanged, and the CLI surfaces the same data.

## Cancellation

When the CLI receives `SIGTERM`, the local process closes its HTTP
connection, but Waitress does **not** proactively interrupt a
synchronous handler on client disconnect — the route handler keeps
running until its `wake_q.get(timeout=...)` returns or the 25 s cap
elapses. The route's `finally` block (which unsubscribes the
EventBus handler) only runs once that wake completes. The lame-duck
daemon thread documented in
[§7.4](../reference/agent-wait-patterns.md#74-concurrency-model--queue--daemon-thread)
also continues for ≤ 25 s after disconnect. In practice the
EventBus subscription is bounded at ≤ 25 s after a SIGTERM — same
upper bound as the prior MCP variant; "detects disconnect →
unsubscribes" is not what Waitress actually does here.

## Rollback

The MCP tool registrations and SDK `@tool` shims are removed in this
PR. Rolling back means restoring the shims and the SKILL.md MCP
invocations from the prior release. The route, the cursor protocol,
the event allowlist, and the Waitress sizing rule are unchanged
between the two clients, so a rollback only touches client-side
files (`orchestrator/mcp_tools.py`, `sandbox/egg_agent_tools/tools/message.py`,
`skills/sdlc/SKILL.md`, doc files). No data-shape migration.

## Future work

- **Drop `GET_STATUS_MAX_WAIT = 25` for non-MCP callers.** The 25 s
  cap exists for the MCP transport. The CLI doesn't need it. A
  follow-up could parameterise the cap per-caller, letting the CLI
  block ~10 min server-side per call (matching Bash cap) and
  eliminating the intra-process loop entirely.
- **Bash background + file polling.** Surveyed during refine. Would
  trade wake-storm for read-storm; not pursued. Recorded here so the
  alternative is discoverable if the trade-off shifts.

## References

- [Agent Wait Patterns — §7 Host-Side Waits](../reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) — full envelope, cursor, and concurrency contract
- [Agent Tools Reference](../reference/agent-tools.md) — the "prefer MCP" doctrine and the wait-verb carve-out
- [Release note — `wait_for_status_change`](wait-for-status-change.md) — superseded; original #1932 design
- [SDLC Skill](../../skills/sdlc/SKILL.md) — host-side consumer
- [Issue #1897](https://github.com/jwbron/egg/issues/1897) — sandbox-side `egg-orch message wait` design and wait-pattern catalogue
- [Issue #1932](https://github.com/jwbron/egg/issues/1932) — MCP variant superseded by this issue
- [Issue #2211](https://github.com/jwbron/egg/issues/2211) — this issue
