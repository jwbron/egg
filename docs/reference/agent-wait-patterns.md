# Agent Wait Patterns

> Canonical reference for how concurrent agents wait for BRC messages — the
> single one-liner you should copy, the five anti-patterns to avoid, the
> exit-code contract for `egg-orch message wait`, the `HEARTBEAT` metadata
> schema, and the operator-facing env vars that couple the client-side wait
> cap to the gateway and Waitress thread pool.
>
> Audience: agents (for the copy-paste idiom), prompt maintainers (for the
> Don'ts), and operators (for the env-var couplings).

This document consolidates the wait-behaviour contract introduced by
[#1897](https://github.com/jwbron/egg/issues/1897). It is the authoritative
source for the canonical wait idiom — the
[Concurrent Execution Guide](../guides/concurrent-execution.md#message-bus)
links here rather than duplicating the contract.

## 1. The Canonical Idiom

Every concurrent agent — producer and reviewer — waits for BRC messages by
running **exactly one command** during its STAY ALIVE step:

```bash
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED \
  --for CONSENSUS_RE_REVIEW \
  --for OVERSEER_ALERT
```

`wait-loop` is a thin wrapper around `egg-orch message wait` that keeps
issuing long-poll wait calls **forever**, server-side, until one of the
listed message types arrives. It exits cleanly only on terminal match or on
a permanent error — there is no outer timeout, no `for i in 1..N`, no
`sleep N`. The LLM has zero degrees of freedom in how it waits.

### Producer STAY ALIVE

Producers listen for the three terminal signals:

| `--for` value | Meaning | Action on exit |
|---------------|---------|----------------|
| `CONSENSUS_CONFIRMED` | Global consensus reached — orchestrator will SIGTERM shortly | Print and exit 0 |
| `CONSENSUS_RE_REVIEW` | Re-review requested (peer re-proposed) — re-confirm via `egg-orch consensus confirmed` | Print and exit 0 |
| `OVERSEER_ALERT` | Overseer escalation — read the alert body and comply | Print and exit 0 |

```bash
# Producer idiom (paste verbatim from your prompt)
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED \
  --for CONSENSUS_RE_REVIEW \
  --for OVERSEER_ALERT
```

### Reviewer STAY ALIVE

Reviewers additionally need to wake when a producer proposes:

| `--for` value | Meaning | Action on exit |
|---------------|---------|----------------|
| `CONSENSUS_PROPOSE` | A producer proposed — review and ACK/NACK | Print and exit 0 |
| `CONSENSUS_RE_REVIEW` | Producer re-proposed — re-review | Print and exit 0 |
| `CONSENSUS_CONFIRMED` | Global consensus reached | Print and exit 0 |
| `OVERSEER_ALERT` | Overseer escalation | Print and exit 0 |

```bash
# Reviewer idiom (paste verbatim from your prompt)
egg-orch message wait-loop \
  --for CONSENSUS_PROPOSE \
  --for CONSENSUS_RE_REVIEW \
  --for CONSENSUS_CONFIRMED \
  --for OVERSEER_ALERT
```

### Why a wrapper and not a naked `message wait`?

`egg-orch message wait` has a bounded `--timeout` (clamped by
`EGG_MESSAGE_POLL_MAX_WAIT`, see §6). `wait-loop` stitches those bounded
calls together into a "block forever server-side" behaviour so the agent
can issue one command and **do nothing else** until the orchestrator
SIGTERMs it or a terminal event arrives.

A transient inner-call error (exit 2 — HTTP 5xx, ECONNRESET, etc.) makes
the wrapper back off (≤ 2 s in test mode, exponential in production) and
retry. A permanent error (exit 3 — 4xx, bad pipeline id, argparse misuse)
makes the wrapper exit 1 so the agent fails fast.

## 2. The Four Anti-Patterns (from #1897)

Each of these was observed in production pipelines before #1897 and
caused real latency or bus pollution. Do **not** use any of them.

### Anti-pattern 1 — Self-confirming in a tight loop

```bash
# ❌ DO NOT DO THIS
for i in 1 2 3 4 5 6 7 8 9 10; do
  echo "=== Poll $i at $(date)"
  egg-orch consensus confirmed
  ...
done
```

**Why it's wrong:** each `consensus confirmed` call is idempotent since
[#1896](https://github.com/jwbron/egg/pull/1896), but wrapping it in a
for-loop still emits N log lines per second, wasting bus-adjacent
monitoring cycles. Observed case: architect emitted 20+ identical
`CONSENSUS_CONFIRMED (pending_acks)` messages in 90 seconds.

**Fix:** issue one `egg-orch consensus confirmed` call and transition to
`wait-loop`. If it exits 2 (pending_acks), `wait-loop` will wake you on
`CONSENSUS_RE_REVIEW` or on the next state change.

### Anti-pattern 2 — Long blocking `sleep`

```bash
# ❌ DO NOT DO THIS
sleep 300 && egg-orch consensus status 2>&1 && git fetch origin ...
```

**Why it's wrong:** a 5-minute `sleep` is 5 minutes during which the
agent cannot receive NACKs, react to peer proposals, or notice a pipeline
cancellation. The agent appears crashed to the overseer.

**Fix:** use `wait-loop` — it blocks on the bus, not on wall-clock time,
and wakes within ≤ 2 s of the relevant event.

### Anti-pattern 3 — Multi-iteration poll loops

```bash
# ❌ DO NOT DO THIS
for i in 1 2 3 4 5 6 7 8; do
  echo "--- Check $i ($(date -u +%H:%M:%S)) ---"
  egg-orch message poll --wait 60 ...
done
```

**Why it's wrong:** LLMs improvise outer loops from training-data idioms.
Observed case: documenter entered an 8-iteration × 60 s loop and missed a
NACK that arrived 6 minutes earlier — the message sat unread in its
inbox.

**Fix:** `wait-loop` is the outer loop, server-side. You never write
`for i in …; do …; done`.

### Anti-pattern 4 — `QUESTION` bus messages as informal status

```bash
# ❌ DO NOT DO THIS
egg-orch message send --to all --type QUESTION \
  --subject "Tester orienting - any ETA?"
```

**Why it's wrong:** `QUESTION` had no handler and no guaranteed
respondent. Messages like this are bus noise.

**Fix:** `QUESTION` was removed in #1897. Use the typed alternatives:

- `HEARTBEAT` — "I'm alive, here's my state" (see §4)
- `HANDOFF` — "I need you to act on this artifact"
- `STATUS` / `PROGRESS` — informational, no reply expected

### Anti-pattern 5 — Producer waits on `CONSENSUS_CONFIRMED` before its own confirm has succeeded (#2064)

```bash
# ❌ DO NOT DO THIS — happens when a producer treats the post-confirm
# STAY ALIVE wait_loop as the recovery path for a `pending_acks` confirm.
egg-orch consensus propose ...
egg-orch consensus confirmed              # returns status='pending_acks'
                                          # because another producer
                                          # hasn't proposed yet
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED \             # ← circular: own confirm
  --for CONSENSUS_RE_REVIEW \             #   is part of what generates
  --for OVERSEER_ALERT --timeout 60       #   this signal globally
```

**Why it's wrong:** the global `CONSENSUS_CONFIRMED` signal only fires
when **every** agent — including this producer — has confirmed. Waiting
on it before the producer's own confirm has been accepted by the
tracker is a self-deadlock. Observed in pipeline `issue-1965`: the
documenter sat in this wait for ~36 minutes, woken only by the
overseer's `agent-heartbeat-stall` band-aid.

The orchestrator's `/messages/wait` endpoint now rejects this pattern
with **HTTP 400** when the caller's role is in producer state
`WORKING` or `PROPOSED` and the wait includes `CONSENSUS_CONFIRMED` —
the wrapper surfaces this as exit code 3 (permanent error). Read the
error: it tells you what to wait for instead.

**Fix:** the post-confirm STAY ALIVE wait is only legitimate **after**
your own confirm has succeeded (status `confirmed`, not `pending_acks`).
For a `pending_acks` recovery loop, the orchestrator re-arms the
"ready to confirm" STATUS nudge on every rejection path ([#2100](https://github.com/jwbron/egg/issues/2100)), so in all cases you can wait for `CONSENSUS_STATUS` — it fires automatically when the blocking condition clears:

- **Global zero-proposal** (another producer hasn't proposed): wait on
  `CONSENSUS_STATUS` (and `OVERSEER_ALERT`). The nudge fires when the
  laggard proposes and the guard clears; do **not** manually re-issue
  `confirmed` before it arrives.
- **Your reviewers haven't ACKed yet**: wait on `CONSENSUS_ACK,CONSENSUS_NACK`
  per the producer-lifecycle Step 4 idiom, then re-issue confirm when
  the orchestrator's directed STATUS nudge ("ready to confirm")
  arrives.

## 3. Exit-Code Contract for `egg-orch message wait`

`egg-orch message wait` returns a deterministic exit code so the wrapper
(and any other caller) can decide whether to retry, continue, or fail
fast. This contract is identical for every transport.

| Exit code | Semantic | When it fires | Caller action |
|-----------|----------|---------------|----------------|
| **0** | Matched | One or more messages of a `--for TYPE` arrived before `--timeout` elapsed. Message JSON printed to stdout. | Consume the message. If it's a terminal type, exit; otherwise loop. |
| **1** | Timeout | No matching message within `--timeout`. stdout is empty. | Retry — this is the normal path for long-poll cycles. |
| **2** | Transient | HTTP 5xx, `ECONNRESET`, JSON parse failure, or similar recoverable error. | Back off and retry. Safe to retry indefinitely. |
| **3** | Permanent | HTTP 4xx (other than 408), bad pipeline id, auth failure, argparse misuse (e.g. missing `--for`), rate-limit 429. | Do **not** retry in a tight loop. Escalate / fail. |

### Examples

```bash
# Normal consumption — exit 0
egg-orch message wait --for CONSENSUS_PROPOSE --timeout 30
echo "exit=$?"   # 0 if something arrived, 1 if the 30s elapsed empty

# Argparse misuse — exit 3
egg-orch message wait --timeout 30
# Prints: error: at least one --for TYPE is required
# echo "exit=$?"   # 3

# Orchestrator 503 during a deployment — exit 2
# (retry-safe — wait-loop applies exponential backoff)
EGG_ORCHESTRATOR_URL=http://does-not-exist:9849 \
  egg-orch message wait --for CONSENSUS_CONFIRMED --timeout 5
# echo "exit=$?"   # 2
```

### Filtering by producer

`--from ROLE` narrows the match to messages with that `from_role`. This is
useful for reviewers watching a single producer:

```bash
egg-orch message wait --for CONSENSUS_PROPOSE --from coder --timeout 60
```

### `wait-loop` composite behaviour

`wait-loop` consumes the exit-code contract and turns it into a
loop-forever semantic:

| Inner exit | `wait-loop` action |
|------------|--------------------|
| 0 (match)  | Print matched message JSON, then exit 0 |
| 1 (timeout) | Silently continue the loop — no output |
| 2 (transient) | Back off (≤ 2 s test mode, exponential in production) and continue |
| 3 (permanent) | Exit 1 so the agent fails fast |

### "New events only" default (issue #1925)

A cursor-less `wait` / `wait-loop` call starts at the **stream tip** —
it only matches events added **after** the call begins. Events that
already exist in the message stream (including the agent's own
just-sent `CONSENSUS_CONFIRMED`) are skipped.

Rationale: before #1925, a cursor-less wait scanned from the beginning
of the stream, so once the stream contained any matching event,
every subsequent `wait-loop` invocation returned instantly with the
same already-seen message instead of blocking for the next one. That
forced agents to either spin in foreground or manually construct a
`--since <last_seen_id>` on each call.

**Race: send → wait.** There is a small window between the agent
sending its own CONSENSUS_CONFIRMED and entering `wait-loop` during
which a peer event could arrive — with the default new-events-only
behaviour, that event would not unblock the wait. In practice this
window is milliseconds and the peer event case is rare at that
instant, but if you need zero-drop semantics capture an anchor
message ID before your send and pass it explicitly:

```bash
# zero-drop pattern — anchor BEFORE the send, wait from the anchor
anchor=$(egg-orch message poll --limit 1 --json | jq -r '.messages[0].id // empty')
egg-orch consensus confirmed
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED --for CONSENSUS_RE_REVIEW --for OVERSEER_ALERT \
  ${anchor:+--since "$anchor"}
```

With `--since`, the wait starts at the named ID (exclusive) and
includes any event — your own send, a peer's, or one that arrived
in the window — that arrived after the anchor.

### Cursor threading across waits (issue #1995)

Every `egg-orch message wait` / `wait-loop` response and every
`mcp__brc__wait_for_event` / `mcp__brc__wait_loop` return dict now
carries a `cursor` field that callers should thread into the `since`
parameter of the next call. Without threading, events that arrive
between a returning wait and the subsequent wait call are missed —
the same wait→wait race window §7 closes on the host side.

The `cursor` is opaque from the caller's perspective:

| Wait outcome | `cursor` value |
|--------------|----------------|
| Match (one or more messages returned) | ID of the **last** delivered message |
| Timeout (no messages) | Current stream tip at server response time |
| Stream empty on timeout | `null` / unset — caller may keep its prior cursor or omit `since` |

`wait-loop` already threads the cursor internally between its own
iterations, so a cursor-less call that rides through several timeouts
before matching does not reopen the race. The surfaced cursor on the
outer return closes the same race across successive `wait-loop`
invocations by the agent.

**Recommended pattern (BRC producer loop):**

```python
cursor = None
while not consensus_done:
    resp = mcp__brc__wait_loop(
        for_types=["CONSENSUS_ACK", "CONSENSUS_NACK", "CONSENSUS_RE_REVIEW", "OVERSEER_ALERT"],
        since=cursor,
    )
    cursor = resp.get("cursor", cursor)
    for msg in resp["messages"]:
        ...  # process ACK / NACK / re-review / alert
```

Legacy callers that ignore `cursor` and don't pass `since` keep their
pre-#1995 behaviour — still correct for the common "wait once, exit"
shape, still vulnerable to the wait→wait race for multi-ACK loops.

## 4. `HEARTBEAT` Message Type

`HEARTBEAT` is a typed message agents emit on state transitions so the
orchestrator and overseer can distinguish "still working" from "crashed".
It replaces ad-hoc `QUESTION` / `PROGRESS`-based heartbeat patterns and
complements the legacy `PROGRESS`-heartbeat event path (both paths are
recognised by `HealthMonitor` in this release).

### Body schema

The heartbeat uses a flat JSON body posted to `POST /api/v1/pipelines/<id>/heartbeat`.
The `from_role` field identifies the sender; `state` carries the
machine-actionable status. There is no nested `metadata` envelope.

```json
{
  "from_role": "coder",
  "state": "WORKING",
  "waiting_on": "coder",
  "since": "2026-04-23T06:29:00Z",
  "body": "(optional human-readable summary)"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `from_role` | string | Yes | The agent role emitting the heartbeat. |
| `state` | enum string | Yes | One of `WORKING`, `WAITING_ON_ROLE`, `WAITING_FOR_EVENT`, `PROPOSED`, `IDLE`. |
| `waiting_on` | string | Required **iff** `state == WAITING_ON_ROLE` | The agent role this agent is blocked on. Server-side validation rejects `WAITING_ON_ROLE` with a missing or empty `waiting_on`. |
| `since` | ISO-8601 string | Optional | When the agent entered this state. Useful for stall detection. |
| `body` | string | Optional | Human-readable summary. |

Posting a `WAITING_ON_ROLE` heartbeat without `waiting_on` returns
HTTP 400, and a malformed `HEARTBEAT` POST also returns HTTP 400. This
is intentional: heartbeats are load-bearing for stall detection, so
malformed ones must fail loudly, not silently.

### When to emit

Emit `HEARTBEAT` on **state transitions only** — not on a fixed tick:

- Entering `WORKING` after ORIENT
- Transitioning to `WAITING_ON_ROLE` (e.g. waiting for a peer to propose)
- Producers entering `PROPOSED` after submitting their proposal
- Transitioning to `IDLE` between tasks

A dedup on the server side drops back-to-back identical `(state,
waiting_on)` heartbeats, so repeated emissions on the same state are
harmless but unnecessary.

`WAITING_FOR_EVENT` is the one exception: it is a liveness keep-alive
emitted automatically by `mcp__brc__wait_loop` while it is blocked on
a message filter (issue #2036). Agents do **not** emit it manually —
the wait primitive owns its lifecycle and emits one beat on entry,
one every 60 s while blocked, and a final `WORKING` transition on
exit. The server-side dedup deliberately lets `WAITING_FOR_EVENT`
duplicates through so the overseer's "no heartbeat for N seconds"
detector receives a steady liveness signal during long waits. The
rate limit (§5) still applies.

### Why separate from `PROGRESS` / `STATUS`?

`PROGRESS` carries free-form step descriptions and powers the
`progress_report` health probe. `STATUS` is a generic informational
channel for peers. `HEARTBEAT` exists specifically to:

1. **Reset `last_heartbeat`** in `HealthMonitor` — the `MESSAGE_SENT`
   subscription treats any `message_type == 'HEARTBEAT'` as proof of
   life. Agents adopting `HEARTBEAT` no longer need to emit
   `PROGRESS`-typed heartbeats to satisfy the legacy probe.
2. **Carry typed state** that the overseer can read without parsing
   free-form strings — `WAITING_ON_ROLE` is machine-actionable.
3. **Rate-limit independently** (see §5) so a heartbeat storm cannot
   crowd out real progress events.

> **Legacy path retained**: the `PROGRESS`-heartbeat event path at
> `orchestrator/health_monitor.py` still resets `last_heartbeat`. This
> preserves the current behaviour for agents that have not yet adopted
> `HEARTBEAT`. A follow-up issue will deprecate the legacy path once
> adoption reaches 100%.

### CLI

```bash
# WORKING — default entry heartbeat
egg-orch message heartbeat --state WORKING

# WAITING_ON_ROLE — waiting_on is required
egg-orch message heartbeat --state WAITING_ON_ROLE --waiting-on coder

# PROPOSED — after submitting a proposal
egg-orch message heartbeat --state PROPOSED

# IDLE — between tasks
egg-orch message heartbeat --state IDLE
```

Errors return CLI exit 3 (permanent) so callers do not retry in a tight
loop:

- Invalid `state` → exit 3
- `WAITING_ON_ROLE` missing `--waiting-on` → exit 3
- Rate-limit 429 (see §5) → exit 3

## 5. `EGG_HEARTBEAT_RATE_LIMIT` — Per-Role Heartbeat Cap

The orchestrator rate-limits `HEARTBEAT` emissions to prevent a runaway
loop from flooding the bus. Limits are keyed by `(pipeline_id,
agent_role)` with minute granularity (sliding window).

| Env var | Default | Scope | Effect |
|---------|---------|-------|--------|
| `EGG_HEARTBEAT_RATE_LIMIT` | `20` | Per `(pipeline_id, agent_role)` per minute | Exceeding returns HTTP 429 with a `Retry-After` header |

### 429 response shape

```json
{
  "error": "rate_limited",
  "retry_after": 42
}
```

The `retry_after` value is the seconds-until-reset; clients should not
retry sooner than that. The CLI surfaces 429 as exit 3 (permanent from
the caller's perspective) so agents do not retry in a tight loop and
compound the spam.

### Why per-role, not per-pipeline?

A runaway loop in one role (e.g. a compaction cascade in `coder`) must
not silently back-pressure the rate limit for other roles. The
per-role keying ensures that one misbehaving agent cannot starve others
of heartbeat capacity.

## 6. `EGG_MESSAGE_POLL_MAX_WAIT` — Long-Poll Cap Coupling

`egg-orch message wait --timeout N` is clamped server-side by
`EGG_MESSAGE_POLL_MAX_WAIT`. The default is **60 seconds** — tuned to
match the gateway's baked-in Squid `read_timeout` and `request_timeout`.

| Env var | Default | Clamp behaviour |
|---------|---------|-----------------|
| `EGG_MESSAGE_POLL_MAX_WAIT` | `60` | `GET /messages/wait?timeout=N` with `N > cap` is clamped to `cap`. Must be ≥ 1. |

### The Squid coupling — **READ THIS BEFORE RAISING**

The gateway (a Squid reverse proxy) has two directives that cap how long
a connection can be held open:

- `squid.conf: read_timeout` — max seconds between bytes on a backend
  socket (default in the gateway image: ~60 s).
- `squid.conf: request_timeout` — max seconds for a full request /
  response cycle (default in the gateway image: ~60 s).

**These directives are baked into the gateway image at build time.**
They live inside `gateway/squid.conf` and are copied into the image via
the Dockerfile. They are **NOT** k8s ConfigMap values. Raising
`EGG_MESSAGE_POLL_MAX_WAIT` above the Squid cap causes any blocked
long-poll that exceeds the Squid cap to return **HTTP 504** at the
gateway — the orchestrator does not see the timeout, the client does.

**Procedure to raise the long-poll cap above 60 s:**

1. Edit `gateway/squid.conf`, raising both `read_timeout` and
   `request_timeout` to `N` seconds where `N >= EGG_MESSAGE_POLL_MAX_WAIT`.
2. Rebuild the gateway image (`make build-gateway` or equivalent).
3. Roll the gateway deployment.
4. Set `EGG_MESSAGE_POLL_MAX_WAIT=N` on the orchestrator deployment
   (a k8s ConfigMap / env edit is sufficient at this step).
5. Verify a `GET /messages/wait?timeout=$((N + 5))` returns 200 with
   an empty body after `N` seconds (not a 504).

The orchestrator refuses to silently misbehave: at boot, if
`EGG_MESSAGE_POLL_MAX_WAIT > 90`, it emits a `warnings.warn` **and** a
WARNING-level log line naming the gateway's `squid.conf`
`read_timeout` and `request_timeout` directives. Operators who see
that warning in their bring-up logs have done the k8s half but not the
gateway-image half.

There is also a deliberately-misconfigured integration test
(`test_misconfigured_cap_504`) that exercises the 504 path so the named
failure mode cannot regress silently.

## 7. Host-Side Waits — `wait_for_status_change`

The first six sections cover **sandbox-side** waits: an agent inside a
sandbox container waits for BRC messages via `egg-orch message wait` /
`wait-loop`. This section covers the **host-side** wait — the SDLC
skill running in a Claude Code session on the operator's host waits for
pipeline state changes via the `wait_for_status_change` MCP tool.

`wait_for_status_change` is the event-triggered sibling of `get_status`
landed by [#1932](https://github.com/jwbron/egg/issues/1932). It exists
because the SDLC skill's monitor loop previously polled
`get_status(task_id, wait=25)` on a pure time-based sleep — every 25 s
the orchestrator returned a full snapshot regardless of whether
anything had changed, burning tokens during quiet phases and delaying
reactions to `OVERSEER_ALERT`, phase transitions, and HITL gates by up
to a full poll interval.

`wait_for_status_change` blocks server-side and returns **immediately**
when any pipeline-relevant event arrives, or returns a minimal
no-change envelope on the 25 s timeout so the dashboard re-render
stays cheap. `get_status` itself is unchanged — it remains the
correct one-shot snapshot tool.

> **Audience:** prompt maintainers wiring up host-side polling, and
> operators sizing the orchestrator's Waitress thread pool (see
> §8 — the `EGG_ORCH_WAITRESS_THREADS` default raised from 16 → 24
> to absorb the host-side wait load).

### 7.1 The two response envelopes

`wait_for_status_change` returns one of two structurally distinct
envelopes. The skill's render path branches on the `no_change` key,
**not** on the `changed` boolean alone — `no_change` is a separate
top-level key for exactly this purpose.

```json
// Path A — changed: true, trigger: "event" (EventBus event fired)
{
  "changed": true,
  "trigger": "event",
  "event_type": "phase.started",             // wire value — e.g. "phase.started", "decision.created", "pipeline.completed"
  "cursor": "msg:1738012734-0|evt:142",
  "current_phase": "plan",
  "status": "running",
  "phase_elapsed_seconds": 127,
  "pipeline":          { "id": "...", "repo": "...", "issue_number": 1932, ... },
  "running_agents":    [ ... ],
  "completed_agents":  [ ... ],
  "pending_decisions": [ ... ],
  "recent_messages":   [ ... ],
  "concurrent": { "consensus": { ... } }
}

// Path A — changed: true, trigger: "message" (message-bus wake)
{
  "changed": true,
  "trigger": "message",
  "messages": [ { "type": "OVERSEER_ALERT", ... } ],   // array of new messages
  "cursor": "msg:1738012740-0|evt:142",
  "current_phase": "plan",
  "status": "running",
  "phase_elapsed_seconds": 130,
  "pipeline":          { "id": "...", "repo": "...", "issue_number": 1932, ... },
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

| Field | Path A | Path B | Notes |
|-------|--------|--------|-------|
| `changed` | `true` | `false` | Always present. |
| `no_change` | absent | `true` | **Distinct top-level key** — branch on this, not on `!changed`. |
| `trigger` | `"event"` or `"message"` | absent | Names which source unblocked the wait. |
| `event_type` | string when `trigger == "event"` | absent | Wire-format value from `EventType`, e.g. `phase.started`, `decision.created`, `pipeline.completed`. |
| `messages` | array when `trigger == "message"` | absent | Passed through `_apply_delphi_filter` for consistency with the message bus route; currently a no-op for the host caller (`role=None`). |
| `cursor` | always | always | Opaque `msg:<id>|evt:<seq>`. Thread into next call's `since`. |
| `current_phase`, `status` | always | always | Refreshed on every call. |
| `phase_elapsed_seconds` | when current phase has a `started_at` | when current phase has a `started_at` | Absent at phase boundaries (the new phase hasn't recorded `started_at` yet) and on pending phases. Fall back to `phase_started_at` (full envelope only) or wall-clock when absent. |
| `concurrent.consensus` | when consensus data is available | when consensus data is available | Absent on non-BRC pipelines. The Path B minimal envelope ships it whenever it would have been on Path A — so consensus drift never goes invisible during quiet phases on BRC pipelines, and is correctly absent for non-BRC pipelines. |
| `pipeline`, `running_agents`, `completed_agents`, `pending_decisions`, `recent_messages` | always | absent | On Path B, reuse the cached values from the prior Path A response. |
| `concurrent.agents` | when concurrent data is available | absent | On Path B, reuse the cached value from the prior Path A response. |

Path A is a **superset of `get_status`** plus `changed/trigger/
(event_type|messages)/cursor`. Drop-in compatible with any code that
already consumes `get_status` output.

### 7.2 Event-trigger allowlist

The route is wired with an **explicit allowlist** of trigger types —
not a denylist. Anything not on this list will not wake the wait,
even if it changes pipeline state.

| Trigger | Source | Notes |
|---------|--------|-------|
| `OVERSEER_ALERT` | message bus | Surface the alert to the user via the existing overseer flow. |
| `CONSENSUS_CONFIRMED` | message bus | Consensus reached for a producer or globally. |
| `CONSENSUS_NACK` | message bus | A reviewer NACKed; producer must re-propose. |
| `CONSENSUS_RE_REVIEW` | message bus | A producer re-proposed; reviewers must re-review. |
| `PHASE_STARTED` | EventBus | New phase began (e.g. plan → implement). Wire value: `phase.started`. |
| `PHASE_COMPLETED` | EventBus | Phase ended. Wire value: `phase.completed`. |
| `PIPELINE_COMPLETED` | EventBus | Terminal success. Wire value: `pipeline.completed`. |
| `PIPELINE_FAILED` | EventBus | Terminal failure. Wire value: `pipeline.failed`. |
| `PIPELINE_CANCELLED` | EventBus | Operator cancelled the pipeline. Wire value: `pipeline.cancelled`. |
| `DECISION_CREATED` | EventBus | New HITL gate; surface to the user. Wire value: `decision.created`. |

> **Wire values vs Python constants:** The names in this table are the Python
> `EventType` constant names. The JSON responses use **dotted lowercase wire
> values** (e.g. `phase.started`, `decision.created`). Always compare against
> wire values in code — see §7.1 response fields for the exact strings.

**Explicitly excluded:** `DECISION_RESOLVED`. This is the post-
`provide_input` event and would cause the host to self-wake on an
action it just initiated. Agent-lifecycle events (`AGENT_STARTED`,
`AGENT_COMPLETED`), `CONSENSUS_PROPOSE`, and `CONSENSUS_ACK` are
also excluded — they are intermediate consensus-protocol noise the
host does not need to render.

### 7.3 The opaque cursor protocol

Every response carries a `cursor` field of shape `msg:<id>|evt:<seq>`.
The two halves cover the two underlying sources:

- `msg:<redis_stream_id>` — the message-bus cursor, identical to the
  `--since` cursor used by `egg-orch message wait`. Either half may
  be empty: `msg:|evt:5` means "no message seen yet, EventBus tip is
  at sequence 5".
- `evt:<seq>` — the in-process EventBus monotonic sequence counter
  (a new field on the `Event` dataclass, populated under the existing
  EventBus lock). Each `EventBus.publish()` call increments the
  counter, so consumers can prove "I have seen everything up to
  seq N".

The cursor is **opaque**. Callers should treat it as a string and
thread it through `since` on the next call. The server parses the
two halves independently and routes the message-bus half to the
`get_messages(since_id=...)` long-poll and the EventBus half to a
per-pipeline sequence gate. This closes the snapshot→wait race window
on **both** sources: an event that fired between the prior snapshot
and the next call still wakes the wait.

A cursor-less call (omit `since` or pass `""`) starts from the
**tip** of both sources — the call only matches events that arrive
after the call begins. Same semantics as the `wait` / `wait-loop`
default since #1925.

### 7.4 Concurrency model — queue + daemon thread

The host route uses **2 threads per in-flight wait** for up to the
wait duration. The implementation pattern:

```text
                                    ┌───────────────────────────┐
HTTP request ────► main worker ────►│  q = Queue(maxsize=16)    │
                                    └───────────────────────────┘
                                          ▲                    ▲
                                          │ put_nowait         │ put_nowait
                                          │ (try/except Full)  │ (try/except Full)
                                          │                    │
                          ┌───────────────────────┐   ┌──────────────────────────────┐
                          │ wildcard EventBus     │   │ daemon thread                │
                          │ handler (filtered by  │   │ message_store.get_messages(  │
                          │ pid + type + seq>...) │   │   wait=25, wait_for_types=…) │
                          └───────────────────────┘   └──────────────────────────────┘

                          main worker:  q.get(timeout=wait)
                                        ── on event/msg, unsubscribe handler, return
                                        ── on queue.Empty,  unsubscribe handler, return minimal envelope
```

- **Main worker thread** — the Waitress worker handling the HTTP
  request. Blocks on `q.get(timeout=wait)`. First source to push a
  result wins; the other source's output is discarded.
- **Daemon `Thread`** — runs `message_store.get_messages(wait=25,
  wait_for_types=[...])` and pushes onto the same queue when it
  returns. Spawned per call, exits when the inner long-poll returns
  (event match, queue full, or 25 s timeout).
- **Wildcard EventBus handler** — registered against the in-process
  `EventBus`, filters on `(event.pipeline_id == pid, event.event_type
  ∈ allowlist, event.sequence > event_since_seq)` and `put_nowait`'s
  the matching event. Unsubscribed in the route's `finally`.

#### Daemon-thread lame-duck (accepted)

When the EventBus path wakes the route first, the daemon thread
running `get_messages(wait=25)` continues blocking inside the message
store for up to 25 s after the route returns. We accept this:

- The thread is `daemon=True`, so it does **NOT** block process
  shutdown.
- Its cost is one thread (drawn from the same Python thread pool
  Waitress uses) for ≤ 25 s per lame-duck.
- At steady state the lame-duck is either absorbed by the next call
  or times out harmlessly.
- Operators can observe the pressure via the
  `egg_inflight_host_waits` Prometheus gauge (route-call count) plus
  the existing `egg_inflight_long_polls` (sandbox long-poll count).

If saturation becomes a real issue, a follow-up can add an explicit
cancellation signal to `message_store.get_messages` (a
`threading.Event` polled every ~500 ms) — a mechanical refactor,
out of scope for the initial #1932 PR.

#### Queue-full path

`Queue(maxsize=16)` bounds the per-call queue. If a burst of EventBus
events fills the queue between subscribe and `q.get()`, additional
events are dropped with a `WARNING` log naming the pipeline_id; the
route still returns with the first delivered event. This is a
deliberate dropped-events policy — the cursor on the returned event
points the next call past the gap, so missed intermediate events do
not block forward progress.

### 7.5 Error responses

The route uses the orchestrator's standard `make_error_response`
helper, so every error body has the shape
`{"success": false, "message": "..."}` (with an optional `details`
key when the route adds context). There is no `error` key, no
`detail` key, no per-error custom fields — clients should read the
human-readable explanation from `message`.

| Status | When | Body shape |
|--------|------|------------|
| **400** | Malformed `since` cursor — does not match the `msg:[^|]*\|evt:-?\d*` regex. | `{"success": false, "message": "Invalid 'since' cursor — expected 'msg:<id>|evt:<seq>' (either half may be empty)."}` |
| **400** | Non-integer `wait` query parameter. | `{"success": false, "message": "Invalid 'wait' query parameter: must be an integer"}` |
| **400** | Malformed `pipeline_id` (path parameter fails the orchestrator's pipeline-id format check). | `{"success": false, "message": "Invalid pipeline ID format: <pipeline_id>"}` |
| **404** | Unknown `pipeline_id`. | `{"success": false, "message": "Pipeline <pipeline_id> not found"}` |
| **200** | Event match (Path A), message match (Path A), or timeout (Path B). | See §7.1. |

`wait` values outside the `[1, GET_STATUS_MAX_WAIT]` range are
**not** an error — they are clamped silently to the bound. Only
non-integer `wait` strings produce a 400.

The route does **not** retry on transient errors — the MCP client
(skill) handles its own retry. Permanent errors (400/404) propagate
to the skill as MCP tool errors, which the skill should surface to
the user rather than silently retry.

### 7.6 Liveness floor (aspirational)

The 25 s server-side cap on every `wait_for_status_change` call,
combined with the skill's immediate loop re-entry on each return,
bounds the aggregate quiet interval at **~25 s + one LLM turn ≤
~55 s** — well inside the aspirational 60 s liveness floor by
construction. The skill does not need a second timing mechanism.

The **overseer is the primary deadlock detector**. It emits
`OVERSEER_ALERT` on stalls, which is in the trigger allowlist, so a
wedged pipeline wakes the host naturally via the early-return path.
A future hard 60 s liveness watchdog (covered as "Future work" in
the [release note](../releases/wait-for-status-change.md)) would be
a defence-in-depth addition; the current design relies on the
construction above for liveness.

### 7.7 Worked example

```text
# poll cycle in pseudocode (the skill itself uses MCP tool calls,
# but the shape is the same)

# first poll — one-shot snapshot (no cursor)
last_status = get_status(task_id)
render_full_dashboard(last_status)

# bootstrap cursor — first wait_for_status_change, no since
resp = wait_for_status_change(task_id, wait=25)
last_cursor = resp.cursor
if not resp.no_change:
    last_status = resp
    render_full_dashboard(last_status)

# subsequent polls — event-triggered wait
while not last_status.status in {"complete", "failed", "cancelled"}:
    resp = wait_for_status_change(task_id, wait=25, since=last_cursor)
    last_cursor = resp.cursor
    if resp.no_change:
        # Path B — refresh four fields, reuse cached snapshot
        last_status.current_phase           = resp.current_phase
        last_status.status                  = resp.status
        last_status.phase_elapsed_seconds   = resp.phase_elapsed_seconds
        last_status.concurrent.consensus    = resp.concurrent.consensus
        render_dashboard_lite(last_status)   # cheap re-render
    else:
        # Path A — replace cached snapshot, render full dashboard
        last_status = resp
        render_full_dashboard(last_status)
        TERMINAL_STATES = {"pipeline.completed", "pipeline.failed", "pipeline.cancelled"}
        if resp.event_type in TERMINAL_STATES:
            break
        if resp.event_type == "decision.created":
            handle_hitl(last_status.pending_decisions)
```

## 8. `EGG_ORCH_WAITRESS_THREADS` — Thread-Pool / Long-Poll Coupling

The orchestrator runs under the Waitress WSGI server. Each blocking
long-poll occupies **one thread** for the full wait duration. If the
thread pool is too small relative to concurrent long-poll volume, normal
short requests (health probes, signal handlers) can queue behind
blocked long-polls and trigger spurious k8s readiness-probe restarts.

| Env var | Default | Minimum (refuse-to-boot) | Effect |
|---------|---------|--------------------------|--------|
| `EGG_ORCH_WAITRESS_THREADS` | `24` | `4` | Sets Waitress `threads=` on `serve()`. Values `< 4` cause the orchestrator to `sys.exit(78)` (EX_CONFIG) at boot with an ERROR log. |

> **Default raised from 16 → 24 in [#1932](https://github.com/jwbron/egg/issues/1932)** to absorb the host-side
> `wait_for_status_change` load on top of the existing sandbox-side
> `message wait-loop` waits. Each `wait_for_status_change` call costs
> **2 threads** for up to the wait duration (one main worker + one
> daemon thread running `message_store.get_messages` — see §7.4).
> Operators who set `EGG_ORCH_WAITRESS_THREADS` explicitly are
> unaffected; the new default only applies when the env var is unset.

### Sizing rule of thumb

> **Thread budget = (concurrent long-poll count) + (concurrent
> host-wait count × 2) + (headroom for short requests)**. With
> `EGG_MESSAGE_POLL_MAX_WAIT=60`, each sandbox agent holds one thread
> for up to 60 s; each host `wait_for_status_change` holds 2 threads
> for up to 25 s. For a six-agent concurrent pipeline plus one host
> session, 24 threads leaves 16 threads free for short requests
> after sandbox waits (`6 × 1 = 6`) and host waits (`1 × 2 = 2`),
> which is safe.

If you raise `EGG_MESSAGE_POLL_MAX_WAIT`, run more than ~6 concurrent
agents, or run multiple concurrent host sessions on the same
orchestrator, raise `EGG_ORCH_WAITRESS_THREADS` accordingly. The
orchestrator exports two Prometheus gauges so you can monitor
saturation:

- `egg_inflight_long_polls` — sandbox-side `message wait` calls in
  flight.
- `egg_inflight_host_waits` (new in #1932, label `endpoint=
  pipelines.status_wait`) — host-side `wait_for_status_change` route
  calls in flight. **Does not** count the lame-duck daemon thread
  (see §7.4) — that is bounded at 25 s and does not need separate
  metric coverage.

If `egg_inflight_long_polls + 2 × egg_inflight_host_waits` approaches
the configured thread count, raise it.

> **Why not Gunicorn?** Gunicorn migration is out of scope for #1897 and
> tracked as a follow-up issue. The current Waitress server is sufficient
> once the thread pool is sized correctly.

## 9. Related Documentation

- [Concurrent Execution Guide — Message Bus](../guides/concurrent-execution.md#message-bus) — the message-bus HTTP surface
- [Concurrent Execution Guide — Consensus Wrapper](../guides/concurrent-execution.md#consensus-wrapper) — how the wrapper uses SSE + `wait-loop`
- [Orchestrator CLI Reference — `egg-orch message`](orchestrator-cli.md#common-workflows) — full command surface
- [Pipeline Health Monitoring](../guides/pipeline-health-monitoring.md) — how `HEARTBEAT` feeds stall detection
- [Orchestrator Architecture — MCP Server](../architecture/orchestrator.md#api-endpoints) — full MCP tool inventory including `wait_for_status_change`
- [SDLC Skill](../../skills/sdlc/SKILL.md) — host-side consumer of `wait_for_status_change` (see §Phase 3 and §Phase S5)
- [Release note — `wait_for_status_change`](../releases/wait-for-status-change.md) — rationale, rollback, and follow-up work for #1932
- [Issue #1897](https://github.com/jwbron/egg/issues/1897) — original bug report with the four observed anti-patterns
- [Issue #1932](https://github.com/jwbron/egg/issues/1932) — host-side event-driven wake (this section's source issue)
