# Release note — `wait_for_status_change` (host-side event-driven wake)

> **Superseded by [#2211](https://github.com/jwbron/egg/issues/2211).** The
> `wait_for_status_change` MCP tool was removed in favour of the
> `egg-orch pipeline wait-status` Bash CLI — same `/status/wait`
> route, but the loop runs on the orchestrator instead of costing
> a full LLM turn on every 25 s cap-elapsed return. The route, event
> allowlist, cursor protocol, and concurrency model documented below
> still describe the underlying wire contract; only the client
> changes. See
> [`pipelines-wait-status-cli.md`](pipelines-wait-status-cli.md) for
> the migration rationale and the new client surface.

**Issue:** [#1932](https://github.com/jwbron/egg/issues/1932) — make
the SDLC skill's host-side monitor loop event-triggered instead of
time-triggered, so the host wakes within ~1 s of an
`OVERSEER_ALERT`, phase transition, or HITL gate instead of up to
25 s late.

## What changed

The SDLC skill's Phase 3 / Phase S5 monitor loop previously polled
`get_status(task_id, wait=25)` on a pure 25-second time-based sleep.
The 25 s cap exists because the Claude Code streamable-HTTP MCP
client times out ~30 s into a tool call, so the skill returned to
the LLM every 25 s even when nothing on the pipeline had changed —
dashboard re-rendered, state reconciled, another poll issued. During
quiet phases (long test runs, large-diff reviews, idle BRC consensus)
this was mostly wasted tokens, and it delayed reaction to events the
operator actually cared about — `OVERSEER_ALERT`, phase transitions,
`needs_input` HITL gates — by up to a full poll interval.

Issue #1932 ships the host-side counterpart of #1919's sandbox-side
event primitives. Concretely:

1. **New MCP tool `wait_for_status_change(task_id, wait=25,
   since=<cursor>)`** — sibling of `get_status`, registered on the
   orchestrator's streamable-HTTP MCP surface. Blocks server-side for
   up to 25 s, returns immediately on any pipeline-relevant event.
2. **New HTTP route `GET /api/v1/pipelines/<id>/status/wait`** — the
   server-side implementation backing the MCP tool. Composes the
   in-process `EventBus` (phase / decision / terminal events) with
   the existing `message_store.get_messages` long-poll
   (`OVERSEER_ALERT`, `CONSENSUS_*`).
3. **`Event.sequence: int`** — additive monotonic counter on the
   `Event` dataclass, populated under the existing `EventBus._lock`,
   threaded into `to_dict()`. Backwards-compatible — existing
   callers do not pass `sequence` explicitly.
4. **New Prometheus metric `egg_inflight_host_waits`** — gauge with
   label `endpoint=pipelines.status_wait`, mirroring the existing
   `egg_inflight_long_polls`. Lets operators monitor host-wait
   pressure on the orchestrator's Waitress thread pool.
5. **`EGG_ORCH_WAITRESS_THREADS` default raised from 16 → 24** in
   `orchestrator/env_config.py`. Each `wait_for_status_change`
   call costs 2 threads (one main worker + one daemon thread
   running `message_store.get_messages`); the new default absorbs
   that load on top of the existing sandbox-side `message wait-loop`
   waits. Refuse-to-boot floor stays at 4. Operators who set the env
   var explicitly are unaffected.
6. **SDLC skill (`skills/sdlc/SKILL.md`) updated** — Phase 3 and
   Phase S5 monitor loops switch their subsequent-poll call from
   `get_status(task_id, wait=25)` to
   `wait_for_status_change(task_id, wait=25, since=<last_cursor>)`.
   First poll still uses `get_status(task_id)`. The skill threads
   the response `cursor` field through `since` on every subsequent
   call.

`get_status` itself is **unchanged**. Code and skills consuming it
are unaffected — it remains the canonical one-shot snapshot tool.

## Rationale

- **Token savings during quiet phases.** A pipeline that sits idle
  for 10 minutes used to round-trip 24 full status snapshots
  (≈40 tool calls when you count Claude Code's request/response
  framing). With `wait_for_status_change`, idle minutes return the
  minimal `no_change: true` envelope (~7 fields vs the full
  ~12-field snapshot), and the skill reuses the cached snapshot
  for the unchanged fields. The expected reduction is the
  ratio of "minimal envelope size + cached re-render" to "full
  snapshot per cycle" — substantial during long quiet phases
  but not yet measured against production pipelines (a tester
  follow-up will quantify the gain).
- **Sub-second reaction latency** to the events that actually need
  human attention. A new `OVERSEER_ALERT` posted at second 5 of a
  poll cycle previously waited 20 s before the host saw it; it now
  unblocks the wait within milliseconds.
- **Snapshot→wait race window closed** by the opaque cursor. An
  event that fires between the prior `get_status` snapshot and the
  next `wait_for_status_change` call still wakes the wait, because
  the cursor records the per-source tip seen at snapshot time.
- **Liveness preserved** by construction. The 25 s server-side cap
  per call plus immediate skill loop re-entry bounds the aggregate
  quiet interval at ~25 s + one LLM turn ≤ ~55 s, well inside the
  aspirational 60 s liveness floor. The overseer remains the
  primary deadlock detector — its `OVERSEER_ALERT` is in the
  trigger allowlist.

## Event-trigger allowlist

The new route is wired with an **explicit allowlist**, not a
denylist:

| Trigger | Source | Notes |
|---------|--------|-------|
| `OVERSEER_ALERT` | message bus | Surface to the user. |
| `CONSENSUS_CONFIRMED` | message bus | Producer or global consensus. |
| `CONSENSUS_NACK` | message bus | A reviewer NACKed. |
| `CONSENSUS_RE_REVIEW` | message bus | A producer re-proposed. |
| `PHASE_STARTED` | EventBus | New phase began. |
| `PHASE_COMPLETED` | EventBus | Phase ended. |
| `PIPELINE_COMPLETED` | EventBus | Terminal success. |
| `PIPELINE_FAILED` | EventBus | Terminal failure. |
| `PIPELINE_CANCELLED` | EventBus | Operator cancelled. |
| `DECISION_CREATED` | EventBus | New HITL gate. |
| `CONTEXT_PR_SKIPPED` | EventBus + message bus | Context PR hook skipped during plan→implement transition. |
| `CONTEXT_PR_FAILED` | EventBus + message bus | Context PR hook raised during plan→implement transition. |

**Explicitly excluded:** `DECISION_RESOLVED` (the post-
`provide_input` event — would cause the host to self-wake on its
own action), `AGENT_STARTED` / `AGENT_COMPLETED` (intermediate
agent-lifecycle noise), `CONSENSUS_PROPOSE` / `CONSENSUS_ACK`
(intermediate consensus-protocol noise — only the consensus *result*
is in the allowlist).

## Response envelopes

Two structurally distinct envelopes — the skill branches on the
`no_change` key, **not** on the `changed` boolean alone, so the
branch is structural rather than a conditional read of `changed`.

```json
// Path A — changed: true (event fired before timeout)
{
  "changed": true,
  "trigger": "event",                      // or "message"
  "event_type": "OVERSEER_ALERT",          // present when trigger == "event"
  // "messages": [ ... ],                  // present when trigger == "message"
  "cursor": "msg:1738012734-0|evt:142",
  "current_phase": "plan",
  "status": "running",
  "phase_elapsed_seconds": 127,
  "pipeline":          { ... },
  "running_agents":    [ ... ],
  "completed_agents":  [ ... ],
  "pending_decisions": [ ... ],
  "recent_messages":   [ ... ],
  "concurrent": { "consensus": { ... } }
}

// Path B — no_change: true (25 s elapsed, no event)
{
  "changed": false,
  "no_change": true,
  "current_phase": "plan",
  "status": "running",
  "phase_elapsed_seconds": 152,
  "concurrent": { "consensus": { ... } },
  "cursor": "msg:1738012750-0|evt:148"
}
```

Path A is a **superset of `get_status`** plus
`changed/trigger/(event_type|messages)/cursor`. Path B carries
exactly seven top-level keys, including `concurrent.consensus` so
consensus drift never goes invisible during quiet phases.

## Cursor protocol

The opaque `cursor` is shaped `msg:<redis_stream_id>|evt:<seq>`,
either half may be empty (`msg:|evt:5` means "no message seen, EventBus
tip at seq 5"). The server parses the halves independently and routes
the message-bus half to `since_id` and the EventBus half to a
per-pipeline `event.sequence` gate. Callers treat the cursor as
opaque and thread it through `since` on the next call.

## Rollback path

The change is structured so the new server-side primitives can be
rolled back independently of the skill update.

- **Skill-only revert** — reverting only the `skills/sdlc/SKILL.md`
  change keeps the new MCP tool registered server-side but dormant
  (the skill goes back to calling `get_status(task_id, wait=25)`).
  `get_status` semantics are unchanged, so the skill returns to its
  pre-#1932 behaviour with no other moving parts.
- **Server-side revert** — reverting the route, MCP tool schema,
  `Event.sequence` field, and metric leaves the skill calling a tool
  that no longer exists; the skill would error on the call. **Revert
  the skill first** if rolling both back.
- **Daemon-thread lame-duck is bounded at 25 s** so a server-side
  revert mid-flight cannot leak threads past process shutdown — the
  daemon threads are `daemon=True` and exit when their inner
  `get_messages(wait=25)` returns.
- **`Event.sequence` is additive** — existing EventBus consumers
  ignoring the new field continue to work unchanged. Callers
  inspecting `Event.to_dict()` see the new key but can ignore it.
- **`EGG_ORCH_WAITRESS_THREADS` default bump** — operators who set
  the env var explicitly are unaffected by the default change. The
  new default only applies when the env var is unset.

## Future work

- **Literal 60 s liveness watchdog** — the current liveness guarantee
  is aspirational (25 s × loop re-entry ≤ ~55 s, inside the
  60 s floor by construction). A defence-in-depth follow-up would
  add an explicit watchdog timer in the skill that fires a
  no-change render if the wait stays silent past 60 s. Risk-analyst
  R7 in [`.egg-state/agent-outputs/1932-risk_analyst-output.json`](../../.egg-state/agent-outputs/1932-risk_analyst-output.json).
- **Python SDK MCP surface parity ([#1920](https://github.com/jwbron/egg/issues/1920))** — `wait_for_status_change`
  ships on the streamable-HTTP MCP surface only for v1. The SDLC
  skill is the only consumer today; in-sandbox agents use
  `egg-orch message wait-loop` which already has event-driven wake.
  When #1920's Python SDK MCP surface lands, register the new tool
  in parallel. Risk-analyst R11.
- **`message_store.get_messages` cancellation signal** — the
  daemon-thread lame-duck (up to 25 s after the route returns) is
  acceptable in practice but could be eliminated by accepting a
  `threading.Event` in the wait loop and polling it every ~500 ms.
  Mechanical refactor; out of scope for #1932. Risk-analyst R14.

## References

- [Agent Wait Patterns — §7 Host-Side Waits](../reference/agent-wait-patterns.md#7-host-side-waits--wait_for_status_change) —
  full envelope contract, trigger allowlist, cursor protocol, and
  concurrency model.
- [Agent Wait Patterns — §8 `EGG_ORCH_WAITRESS_THREADS`](../reference/agent-wait-patterns.md#8-egg_orch_waitress_threads--thread-pool--long-poll-coupling) —
  the new 16 → 24 default and the 2-threads-per-host-wait sizing
  rule.
- [Orchestrator Architecture — MCP Server](../architecture/orchestrator.md#api-endpoints) —
  the updated MCP tool inventory.
- [SDLC Skill](../../skills/sdlc/SKILL.md) — host-side consumer
  (Phase 3 and Phase S5).
- [Issue #1932](https://github.com/jwbron/egg/issues/1932) —
  original problem statement.
- [Issue #1919](https://github.com/jwbron/egg/issues/1919) —
  sandbox-side event primitives (`XREAD BLOCK` with `message_type`
  filter, `HEARTBEAT` state) that #1932 is the host-side counterpart
  of.
- [`.egg-state/drafts/1932-plan.md`](../../.egg-state/drafts/1932-plan.md) — full plan (4 phases).
- [`.egg-state/agent-outputs/1932-architect-output.json`](../../.egg-state/agent-outputs/1932-architect-output.json) — architecture analysis.
- [`.egg-state/agent-outputs/1932-risk_analyst-output.json`](../../.egg-state/agent-outputs/1932-risk_analyst-output.json) — risk register (R1-R17).
- [`.egg-state/drafts/1932-analysis.md`](../../.egg-state/drafts/1932-analysis.md) — refine-phase analysis.
