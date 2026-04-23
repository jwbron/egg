# Agent Wait Patterns

> Canonical reference for how concurrent agents wait for BRC messages — the
> single one-liner you should copy, the four anti-patterns to avoid, the
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
| `state` | enum string | Yes | One of `WORKING`, `WAITING_ON_ROLE`, `PROPOSED`, `IDLE`. |
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

## 7. `EGG_ORCH_WAITRESS_THREADS` — Thread-Pool / Long-Poll Coupling

The orchestrator runs under the Waitress WSGI server. Each blocking
long-poll occupies **one thread** for the full wait duration. If the
thread pool is too small relative to concurrent long-poll volume, normal
short requests (health probes, signal handlers) can queue behind
blocked long-polls and trigger spurious k8s readiness-probe restarts.

| Env var | Default | Minimum (refuse-to-boot) | Effect |
|---------|---------|--------------------------|--------|
| `EGG_ORCH_WAITRESS_THREADS` | `16` | `4` | Sets Waitress `threads=` on `serve()`. Values `< 4` cause the orchestrator to `sys.exit(78)` (EX_CONFIG) at boot with an ERROR log. |

### Sizing rule of thumb

> **Thread budget = (concurrent long-poll count) + (headroom for short
> requests)**. With `EGG_MESSAGE_POLL_MAX_WAIT=60`, each agent holds one
> thread for up to 60 s. For a six-agent concurrent pipeline, 16 threads
> leaves 10 threads free for short requests, which is safe.

If you raise `EGG_MESSAGE_POLL_MAX_WAIT` or run more than ~6 concurrent
agents, raise `EGG_ORCH_WAITRESS_THREADS` accordingly. The orchestrator
exports `egg_inflight_long_polls` (Prometheus gauge) so you can
monitor saturation; if the peak value approaches the thread count, raise
the thread count.

> **Why not Gunicorn?** Gunicorn migration is out of scope for #1897 and
> tracked as a follow-up issue. The current Waitress server is sufficient
> once the thread pool is sized correctly.

## 8. Related Documentation

- [Concurrent Execution Guide — Message Bus](../guides/concurrent-execution.md#message-bus) — the message-bus HTTP surface
- [Concurrent Execution Guide — Consensus Wrapper](../guides/concurrent-execution.md#consensus-wrapper) — how the wrapper uses SSE + `wait-loop`
- [Orchestrator CLI Reference — `egg-orch message`](orchestrator-cli.md#common-workflows) — full command surface
- [Pipeline Health Monitoring](../guides/pipeline-health-monitoring.md) — how `HEARTBEAT` feeds stall detection
- [Issue #1897](https://github.com/jwbron/egg/issues/1897) — original bug report with the four observed anti-patterns
