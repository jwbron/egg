# Analysis: Event-driven wake for SDLC skill's monitor loop (host-side)

> Issue: #1932 | Phase: refine

## Problem Statement

The SDLC skill in `skills/sdlc/SKILL.md` drives a Claude Code host session
through Phase 3 (Monitor) and Phase S3 (Short-flow Monitor). On every loop
iteration the skill calls the orchestrator's `get_status` MCP tool with
`wait=25` (§ Phase 3 step 1 at `skills/sdlc/SKILL.md:317-319` and
§ Phase S3 step 1 at `skills/sdlc/SKILL.md:1178-1180`). The tool uses a
pure time-based sleep (`asyncio.sleep(min(wait, 25))` in
`orchestrator/mcp_server.py:50-67`) — it blocks for 25s regardless of
whether anything on the pipeline has actually changed — then runs the sync
status handler and returns a fresh snapshot.

The 25s cap is not a poll-interval choice; it is enforced by the upstream
streamable-HTTP MCP transport inside Claude Code, which abandons tool
calls at roughly the 30-second mark
(`orchestrator/mcp_server.py:38-42`, citing anthropics/claude-code#20335).
Raising the cap is out of scope per the issue's "Out of scope" section.

Because the wait is blind to events, every idle poll cycle pays the full
25s even when the pipeline sits unchanged (long `make test` runs, large
diff reviews, idle BRC consensus waiting on a missing reviewer, etc.).
Each return to the LLM re-renders the dashboard, re-classifies messages,
re-evaluates consensus, and starts another wait. The effects are:

1. **Wasted tokens** during quiet phases — the LLM sees and re-emits the
   same status payload repeatedly.
2. **Delayed reaction** — up to 25s before Claude sees a new
   `OVERSEER_ALERT`, a `pending_decisions` entry, a phase transition, or
   a terminal state. `OVERSEER_ALERT` deserves particular emphasis:
   `skills/sdlc/SKILL.md:369-397` prescribes user-facing alert
   surfacing, but the alert sits on the server for up to a full poll
   interval before the host even asks.

The desired outcome is that `get_status` behaves like `message wait`
already does for in-sandbox agents (issue #1919): block on the server
event loop and return **immediately** when anything that would cause the
host to act has landed, otherwise fall through to the 25s cap with a
minimal no-change payload.

## Current Behavior

### Host-side polling (SDLC skill)

The skill's monitor loops tell the host LLM:

> **Subsequent polls**: `get_status(task_id, wait=25)` — the tool waits
> 25 seconds on the server event loop before fetching status.

and immediately after:

> **Important: The `wait` parameter on `get_status` handles the polling
> delay internally.** Do not use separate `sleep` commands or background
> sleeps for the poll interval.

Both §Phase 3 (lines 313-347) and §Phase S5 (lines 1174-1206) share that
wording. The loop body (dashboard render → overseer check → consensus
check → transitions → elapsed time → next `get_status`) runs end-to-end
on every wake, cache-miss or otherwise.

### How `get_status` uses `wait` today

`orchestrator/mcp_server.py:50-67` — the `wait` kwarg is popped off the
MCP arguments in an `async` wrapper and fed to `_async_sleep`:

```python
async def _apply_get_status_wait(tool_name: str, kwargs: dict) -> None:
    if tool_name != "get_status":
        return
    wait = kwargs.pop("wait", 0)
    if isinstance(wait, bool):
        return
    if isinstance(wait, (int, float)) and wait > 0:
        await _async_sleep(min(wait, GET_STATUS_MAX_WAIT))
```

`GET_STATUS_MAX_WAIT = 25` and the comment explicitly flags the upstream
timeout: *"Must stay safely under Claude Code's streamable-HTTP MCP
tool-call timeout (~30s …)."* The sync handler at
`orchestrator/mcp_tools.py:1548-1655` then issues its normal REST fetches
(`/api/v1/pipelines/{id}`, `/messages?limit=10`), reads worktree drafts
for pending decisions, and returns the full status envelope regardless of
whether anything changed during the wait.

The `get_status` tool schema itself
(`orchestrator/mcp_tools.py:277-304`) documents `wait` as a "polling
delay" — there is no `since` cursor, no event filter, no short-circuit
on activity.

### Server-side primitives that already exist (landed in #1919)

The issue proposes "reuse the event primitives landed in #1919". Those
primitives are:

- **`GET /api/v1/pipelines/<id>/messages/wait`**
  (`orchestrator/routes/messages.py:347-436`) — blocks on `XREAD BLOCK`
  with a `message_type` filter via `wait_for_types`. Supports `from`,
  `role`, `since_id`, and `from_tip=True` (default when no `since_id` is
  given, per issue #1925). Clamped by `EGG_MESSAGE_POLL_MAX_WAIT`
  (default 60s).
- **`Redis` backend** (`orchestrator/redis_message_store.py:158-329`) —
  native `XREAD BLOCK` with per-type filtering and a caller-supplied
  timeout. The in-memory backend implements the same contract with a
  condition-variable.
- **Pipeline SSE stream** — `/api/v1/pipelines/<id>/stream`
  (`orchestrator/routes/pipelines.py:12002-12062`) already emits
  `phase.started`, `phase.completed`, `pipeline.completed`,
  `pipeline.failed`, and `decision.created` events
  (`orchestrator/routes/pipelines.py:523-553`, `events.py:35-91`).
- **Waitress thread-pool + channel_timeout** — sized via
  `EGG_ORCH_WAITRESS_THREADS` (default 16, refuse-to-boot below 4) and
  `channel_timeout = 2 × EGG_MESSAGE_POLL_MAX_WAIT`
  (`orchestrator/cli.py:280-310`). Long-poll concurrency is already
  accounted for.
- **`HEARTBEAT` messages and overseer alerts** — `OVERSEER_ALERT`
  messages are broadcast on the bus for every anomaly (see
  `orchestrator/tests/test_overseer_monitor.py:1837-1870`), so
  event-triggered wake on message types is already the standard path.

### In-sandbox analogue

In-sandbox agents have already moved from time-based to event-triggered
wait. `docs/reference/agent-wait-patterns.md:18-80` defines the canonical
`egg-orch message wait-loop --for CONSENSUS_* --for OVERSEER_ALERT`
idiom. Host-side monitoring is the last remaining time-based polling
site, and issue #1932 is explicitly the "host-side counterpart" to that
work.

## Constraints

- **25s cap is immutable in this scope.** `GET_STATUS_MAX_WAIT = 25` is
  bounded by the Claude Code streamable-HTTP MCP client timeout and the
  fact that `MCP_TOOL_TIMEOUT` is ignored on that transport
  (anthropics/claude-code#20335). Raising it requires upstream changes
  and is out of scope per the issue.
- **Gateway Squid `read_timeout` is 60 s** (`gateway/squid.conf:135-137`,
  `gateway/squid-allow-all.conf:117-119`). Sandbox agents traverse Squid;
  the host MCP call does not (the SDLC skill runs in the user's Claude
  Code, not in a sandbox container), so Squid is not in the host path.
  But `EGG_MESSAGE_POLL_MAX_WAIT`-style clamps on the server still apply,
  and the host-side wait must stay ≤ 25s regardless.
- **Waitress thread pool is finite** (default 16). Every in-flight host
  poll that blocks on a server-side wait holds one worker. With O(10)
  concurrent pipelines and one host session per pipeline that is still
  well under the pool cap, but the budget is shared with every other
  long-poll socket (sandbox `message wait-loop` calls, SSE streams).
  Metric `egg_inflight_long_polls` already exists from #1919.
- **Backend parity.** The new event wake must work identically on the
  Redis backend (XREAD BLOCK) and the in-memory backend (condition
  variable). The in-memory backend is what CI exercises by default.
- **No schema migration allowed mid-flight.** Pipelines in motion at
  deploy time must continue to work — `get_status` is already widely
  used and the change must be additive or backwards-compatible.
- **Liveness floor.** Per the issue, the host cannot rely purely on
  events: if the overseer itself wedges, no event may ever arrive. The
  issue prescribes a ~60s max quiet interval with a cheap no-change
  payload, but note that any single `get_status` call is still capped at
  25s by the MCP transport — the liveness floor is about **total quiet
  time across successive calls**, not the duration of one call.
- **Deduplication already present.** The SDLC skill tracks seen
  `OVERSEER_ALERT` UUIDs to avoid re-prompting
  (`skills/sdlc/SKILL.md:397`). If the server starts short-circuiting on
  "new" messages, the client-side dedup becomes redundant **only** if
  the server filter is authoritative on what counts as "new since last
  poll"; otherwise both paths must continue to coexist.
- **MCP tool surface is versioned via prompts.** The SDLC SKILL.md is the
  contract — changes to `get_status` semantics (or a new sibling tool)
  require corresponding updates in two places (§Phase 3 step 1 and
  §Phase S5 step 1) plus the §MCP Tools Reference.
- **HITL decision wake-up is critical.** Currently the SDLC skill
  surfaces `pending_decisions` on the next poll cycle. With event-driven
  wake, `decision.created` is emitted synchronously from the request
  that creates the decision (`pipelines.py:11029`) — firing the wake is
  safe. But `provide_input` resolution triggers the same cycle; we must
  not wake the monitor on events the host itself just sent.

## Options Considered

### Option A: Add a new sibling MCP tool `wait_for_status_change`

**Approach.** Add `wait_for_status_change(task_id, wait=25, since=...)`
as a new MCP tool alongside `get_status`. The tool:

1. Reads the current pipeline snapshot (cheap — single
   `/pipelines/{id}` fetch).
2. Opens a server-side blocking read against a new
   `/api/v1/pipelines/<id>/wait` endpoint that subscribes to the
   EventBus (for `phase.*`, `pipeline.*`, `decision.created`) **and**
   long-polls `message_store.get_messages` with
   `wait_for_types=['OVERSEER_ALERT', 'CONSENSUS_CONFIRMED',
   'CONSENSUS_NACK', 'CONSENSUS_RE_REVIEW']`.
3. Returns immediately on any event with a fresh `get_status`
   payload. Returns a minimal `{changed: false, current_phase, status,
   phase_elapsed_seconds}` payload on 25s timeout.

The SDLC skill calls `get_status(task_id)` once at loop start and
`wait_for_status_change(task_id, wait=25)` on every subsequent
iteration.

**Pros:**
- Clean separation: `get_status` stays a pure status read,
  `wait_for_status_change` is the event wake.
- Opt-in for callers — non-SDLC consumers (babysit-pr, agent-diagnose,
  tests) keep current behaviour.
- Signature matches the in-sandbox `message wait` / `wait-loop` idiom
  that users already know (docs/reference/agent-wait-patterns.md).
- The minimal no-change payload is an explicit affordance for cheap
  dashboard re-render on timeout.

**Cons:**
- New tool surface (schema, FastMCP binding, tests, docs).
- The SDLC skill must gain branching logic ("first poll vs subsequent
  poll") — risk of LLM drift on the boundary.
- Doubles the number of polling primitives; future authors have to
  decide which one to call.

### Option B: Retrofit `get_status` with event-driven wait

**Approach.** Change `_apply_get_status_wait` so that when `wait > 0`
and a new `events=true` argument is set (or by default), the wait
subscribes to the same EventBus + message-type XREAD BLOCK as Option
A. Return early with the full status envelope on any event; return the
full status envelope on 25s timeout (unchanged shape).

**Pros:**
- Smallest SKILL.md churn — "just call `get_status(task_id, wait=25)`"
  is the same sentence as today.
- Every existing consumer benefits immediately (babysit-pr, etc.).
- No second tool to document.

**Cons:**
- Semantic change to an existing tool — external MCP consumers that
  pin `get_status` on specific 25s cadence may observe earlier returns
  and re-render faster than expected (probably a feature, but still a
  behaviour change).
- Full status envelope on every wake (no cheap no-change shortcut).
- No `since` cursor makes "same event keeps waking me" race-y unless
  the server remembers per-session cursors — which it currently does
  not, and adding session state to a stateless Streamable HTTP
  transport is non-trivial.
- Testing gets harder: `get_status` tests now need to exercise both
  event and timeout paths; existing tests that assert "`wait=25` ≈ 25s
  duration" break.

### Option C: Host-side SSE consumption instead of a wait tool

**Approach.** Replace the poll loop with a persistent SSE connection to
`/api/v1/pipelines/<id>/stream`. The skill instructs Claude to open
the stream (via a new `stream_pipeline` MCP tool that returns
event-at-a-time chunks) and render the dashboard per event received.

**Pros:**
- Maximally event-driven — no polling at all.
- Reuses the existing SSE infrastructure (`orchestrator/sse.py`,
  `routes/pipelines.py:12002`).

**Cons:**
- **Streamable-HTTP MCP transport does not support streaming tool
  responses in the form Claude Code consumes.** The current FastMCP
  binding returns a single JSON string from each tool call
  (`mcp_server.py:159-176`, `json.dumps(result, indent=2)`); SSE
  chunking would require a different transport or polling adapter.
- Much larger blast radius — new transport, new session state,
  cross-platform UX (when the host sleeps, when the user closes the
  laptop, when the browser tab rotates).
- Does not fit the "host-side counterpart of #1919" framing — #1919
  is event-triggered long-poll, not SSE.

### Option D: Pure client-side change — shorter polls with timers

**Approach.** Leave the server alone. Change the skill to call
`get_status(task_id, wait=5)` more frequently and short-circuit
rendering when nothing has changed since the last poll (hash of a
canonical subset of the response).

**Pros:**
- No orchestrator change at all.
- Keeps `get_status` semantically pure.

**Cons:**
- Defeats the point of the issue — still burns tokens on 5s cadence
  during quiet phases, still reacts slowly (5s ≈ 25s / 5 for the
  average event).
- Dashboard-hash logic in the prompt is fragile; LLMs are unreliable
  at canonical hashing.
- Doesn't exploit any of the #1919 primitives.

## Recommended Approach

**Option A** (new `wait_for_status_change` sibling tool) with the event
set the issue prescribes:

| Trigger | Source | Server mechanism |
|---------|--------|------------------|
| New `OVERSEER_ALERT` | message bus | `message_store.get_messages(wait_for_types=['OVERSEER_ALERT'], from_tip=True)` |
| `pending_decisions` / HITL gate | EventBus `DECISION_CREATED` + re-query | `events.subscribe(EventType.DECISION_CREATED, …)` |
| Phase transition | EventBus `PHASE_STARTED` / `PHASE_COMPLETED` | `events.subscribe(…)` |
| Terminal state | EventBus `PIPELINE_COMPLETED` / `PIPELINE_FAILED` | `events.subscribe(…)` |
| Consensus state change | message bus | `wait_for_types=['CONSENSUS_CONFIRMED', 'CONSENSUS_RE_REVIEW', 'CONSENSUS_NACK']` + short-circuit on `concurrent.consensus` delta |

Rationale:

- Keeps `get_status` unchanged → no risk to non-SDLC consumers.
- Server-side primitive (new endpoint, e.g.
  `/api/v1/pipelines/<id>/status/wait`) is a thin composition of
  existing EventBus + `message_store.get_messages` work from #1919 —
  no new storage, no new cursor semantics beyond what `wait_messages`
  already handles.
- The minimal no-change payload on timeout is an explicit contract —
  the SDLC skill can short-circuit dashboard re-render cheaply without
  an LLM-level hash.
- Liveness floor is honored by the 25s hard cap on each call; the
  skill keeps calling `wait_for_status_change` in its existing loop,
  so the *aggregate* quiet interval is bounded by how fast Claude
  re-issues the tool. A 60s quiet-interval guard on the server (issue
  text) can be reframed as "first-call timeout of 25s is already well
  inside the 60s floor" — so the loop structure in Phase 3 naturally
  enforces the floor.
- Prompt updates are localized: §Phase 3 step 1 and §Phase S5 step 1
  (and the §MCP Tools Reference) replace `get_status(task_id,
  wait=25)` → `wait_for_status_change(task_id, wait=25)`. Everything
  else in the monitor loop is unchanged.

Secondary benefits:

- We can drop the 10s `recent_messages` fetch on the timeout path
  (use the cached snapshot from loop start), reducing request volume.
- Client-side `OVERSEER_ALERT` deduplication
  (`skills/sdlc/SKILL.md:397`) can stay as-is — the server filter and
  the client dedup are complementary (server wakes on any
  `OVERSEER_ALERT`, client decides whether to prompt).

Open risks that must be addressed in the plan phase:

- **Race: host-sent input.** When the user answers a HITL decision via
  `provide_input`, the resulting `decision.resolved` event must NOT
  wake the same-session `wait_for_status_change` — otherwise the
  dashboard re-renders instantly showing the same now-resolved
  decision. Mitigation: filter by event types the host actually cares
  about (exclude `DECISION_RESOLVED`) or carry a `since_event_id`
  cursor.
- **Event backlog.** A pipeline can emit `phase.started` while the
  host is mid-render and not yet blocked. Default `from_tip=True`
  (which is what #1925 fixed for `/messages/wait`) means a pre-
  existing event will NOT wake the next call. That is probably what
  we want for steady-state, but on the **first** transition from
  `get_status` → `wait_for_status_change` there is a race window. A
  `since` cursor (message ID or event sequence) is the clean answer.
- **Transitions vs. HITL dedup.** The issue asks for "consensus state
  change". The message-level trigger (`CONSENSUS_CONFIRMED`,
  `CONSENSUS_RE_REVIEW`, `CONSENSUS_NACK`) is a proxy — NACKs without
  re-proposes, per-reviewer-ACKs etc. may also count. The plan phase
  must decide the exact trigger set.

## Open Questions

All questions are registered via `egg-contract add-decision` or
`egg-contract add-feedback` below. The markdown block after each
command is the registered artifact.

### Multiple-choice decisions

```
egg-contract add-decision --question "Which implementation option should we take?" --options "Option A: new `wait_for_status_change` MCP tool (recommended)" "Option B: retrofit `get_status` with event-driven wait in place" "Option C: host-side SSE consumption via a new `stream_pipeline` tool" "Option D: pure client-side change — shorter polls + rendering guard"
```

```
egg-contract add-decision --question "Which event set should trigger early return?" --options "Minimal: OVERSEER_ALERT + DECISION_CREATED + terminal state (pipeline.completed/failed)" "Issue-as-written: above + phase transition (PHASE_STARTED/COMPLETED) + consensus state change (CONSENSUS_CONFIRMED/NACK/RE_REVIEW)" "Maximal: above + CONSENSUS_PROPOSE + AGENT_FAILED + AGENT_TIMEOUT + OVERSEER heartbeat gaps"
```

```
egg-contract add-decision --question "Should the timeout payload be a minimal no-change envelope or a full status envelope?" --options "Minimal envelope: {changed: false, current_phase, status, phase_elapsed_seconds} — cheap re-render, SKILL.md must branch on `changed`" "Full envelope: same shape as get_status — zero SKILL.md branching, higher cost per timeout" "Hybrid: full on first timeout of a session, minimal thereafter"
```

```
egg-contract add-decision --question "Should we expose a `since` / `since_event_id` cursor parameter to avoid re-firing on already-seen events?" --options "Yes: add `since` string parameter; host passes the most recent event ID from the prior call (prevents stuck-on-same-event races)" "No: rely on default `from_tip=True` semantics from #1925 and accept the transition-race window" "Auto-session: server stores per-session cursors keyed on (pipeline_id, caller-token) — avoids client-side bookkeeping but adds state to a stateless transport"
```

```
egg-contract add-decision --question "Should the event-driven wait be wired into the EventBus only, the message-type long-poll only, or both?" --options "Both (recommended): EventBus for phase/decision/terminal events, message_store.get_messages for OVERSEER_ALERT/CONSENSUS_* — matches where each event actually lives" "EventBus only: also emit OVERSEER_ALERT and CONSENSUS_* onto the EventBus so the wait endpoint subscribes to one source" "Message-type long-poll only: also emit phase/decision/terminal events onto the message bus so there is one wait primitive"
```

```
egg-contract add-decision --question "Should the SDLC skill keep the 10s `recent_messages` fetch on every poll, or only on the `changed: true` path?" --options "Keep on every poll (status quo) — no change to dashboard rendering contract" "Only on `changed: true` — timeout path uses cached recent_messages from the prior wake" "Remove entirely from wait path; fetch on-demand when OVERSEER_ALERT fires" "Defer to plan phase — depends on the envelope shape decision above"
```

```
egg-contract add-decision --question "How should `provide_input` interaction with the wait loop be handled?" --options "Filter out DECISION_RESOLVED events from the trigger set — wait only on decisions that still need input" "Carry a `since_event_id` cursor so host-originated events don't wake the host" "Take no special action — self-waking is harmless because the next render will correctly show no pending decisions"
```

### Open-ended feedback

```
egg-contract add-feedback --question "Are there host-side consumers of `get_status` besides the SDLC skill's Phase 3/S5 monitor loop that should also benefit from event-driven wake (e.g. babysit-pr, agent-diagnose, external tools)? If so, does that push us toward Option B (retrofit) instead of Option A (sibling tool)?" --question "What is the expected concurrency load? Specifically: how many host sessions per orchestrator in production, and does the current EGG_ORCH_WAITRESS_THREADS=16 budget accommodate one long-poll per host session on top of per-agent waits?" --question "Should `wait_for_status_change` be available over the Python SDK MCP tools surface (see PR #1920) as well as the streamable-HTTP MCP server, or only the latter?" --question "Is the 60s liveness-floor constraint from the issue body literal (must guarantee a return at most every 60s of wall-clock time regardless of events) or aspirational (cap any single call at 25s and rely on loop composition)?" --question "Should the new wait path emit its own metric (parallel to `egg_inflight_long_polls`) so operators can distinguish host-side waits from sandbox-side waits in the Waitress thread budget?" --question "Are there known upstream plans or timelines to raise the streamable-HTTP MCP client timeout (anthropics/claude-code#20335)? If it lifts within the next quarter the 25s cap becomes moot and the design may want to accommodate that."
```

---

## Complexity Assessment

**medium**

Why:
- Scope touches three files of logic (new route under `orchestrator/routes/`,
  new MCP tool in `orchestrator/mcp_tools.py` + `mcp_server.py`, SKILL.md
  updates in `skills/sdlc/SKILL.md`) plus tests and docs.
- All primitives already exist (EventBus, `message_store.get_messages`
  with `wait_for_types`, waitress thread budget) — no architectural
  change, mostly composition.
- Known patterns: #1919 shipped the analogous agent-side primitive;
  reuse is the intended path.
- Risk surface is real (race with host-originated events, backend
  parity, prompt drift) but scoped and enumerable.

Not **low**: more than one file, introduces new contract surface, has
non-trivial races to reason about.

Not **high**: no new subsystem, no cross-cutting refactor, no new
transport.

---

*Authored-by: egg*


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

## Resolved Questions

### Choice decisions

**Which implementation option should we take?**
Answer: Option A: new wait_for_status_change sibling MCP tool (recommended)

**Which event set should trigger early return from the host-side wait?**
Answer: Issue-as-written: OVERSEER_ALERT + DECISION_CREATED + terminal state + phase transition (PHASE_STARTED/COMPLETED) + consensus state change (CONSENSUS_CONFIRMED/NACK/RE_REVIEW)

**Should the timeout payload (no-event-in-25s) be minimal or a full status envelope?**
Answer: Minimal envelope: {changed: false, current_phase, status, phase_elapsed_seconds} — SKILL.md branches on `changed`

**Should we expose a `since` / `since_event_id` cursor parameter?**
Answer: Yes — add `since` string parameter; host passes the most recent event ID from the prior call to prevent stuck-on-same-event races

**Should the event-driven wait be wired into the EventBus, message-type long-poll, or both?**
Answer: Both — EventBus for phase/decision/terminal events, message_store.get_messages for OVERSEER_ALERT/CONSENSUS_*

**Should the SDLC skill keep the 10-message recent_messages fetch on every poll, or only on the `changed: true` path?**
Answer: Only on `changed: true` — timeout path reuses cached recent_messages from the prior wake

**How should `provide_input` interaction with the wait loop be handled?**
Answer: Filter out DECISION_RESOLVED events from the trigger set — wait only on decisions that still need input

### Feedback

**Are there host-side consumers of get_status besides the SDLC skill that should benefit?**
Answer: SDLC skill only — keep Option A scoped. Other callers keep get_status.

**Expected concurrency load and EGG_ORCH_WAITRESS_THREADS=16 budget?**
Answer: Raise default or document cap in plan. Call out the budget risk explicitly.

**Should wait_for_status_change be available over the Python SDK MCP tools surface (PR #1920) as well as streamable-HTTP?**
Answer: Not sure / skip — defer to plan phase.

**Is the 60s liveness-floor constraint literal or aspirational?**
Answer: Not sure / skip — defer to plan phase.

**Should the new wait path emit its own metric parallel to egg_inflight_long_polls?**
Answer: Yes — add egg_inflight_host_waits metric so operators can distinguish host-side from sandbox-side waits.

**Upstream plans to raise streamable-HTTP MCP client timeout (anthropics/claude-code#20335)?**
Answer: Keep design flexible in case it lifts — parameterize the cap so raising it later is a one-line change.
