# Concurrent Execution Mode

Concurrent execution mode runs all agents for the current pipeline phase simultaneously rather than sequentially in dependency-ordered waves. Agents communicate via the orchestrator message bus and signal readiness for phase completion via a consensus protocol. BRC consensus is active by default for the **refine**, **plan**, and **implement** phases. Additional phases (such as `review`) can be added via the `concurrent_phases` config.

**Implement-phase note**: the implement phase no longer runs as a single team on a shared branch. Instead, the plan's tasks are split into a DAG of independent **slices** — each slice runs its own concurrent agent team on its own integration branch. Concurrent execution within each slice follows the BRC protocol described here. See [Slice-DAG Implement Phase](../architecture/slice-dag.md) for the slice-level orchestration model.

This is distinct from the standard wave-based parallel execution (Tier 2), where agents run in dependency order but multiple independent agents execute in parallel within each wave.

## Configuring Concurrent Execution

BRC concurrent execution is **enabled by default** for the refine, plan, and implement phases via the `concurrent_phases` config field. No additional configuration is required for standard pipelines. Additional phases can be added to `concurrent_phases` as needed.

To activate BRC for every phase (including non-standard phases), set `concurrent_execution: true`:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"concurrent_execution": true}'
```

To disable BRC entirely, set `concurrent_phases` to an empty list:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"concurrent_phases": []}'
```

Relevant `PipelineConfig` fields:

| Field | Default | Description |
|-------|---------|-------------|
| `concurrent_execution` | `false` | Enable BRC for every phase (overrides `concurrent_phases`) |
| `concurrent_phases` | `["refine", "plan", "implement"]` | Phases where BRC is active when `concurrent_execution` is `false` |
| `start_phase` | `null` | Skip earlier phases and begin execution from `"plan"` or `"implement"` |
| `max_concurrent_agents` | `6` | Maximum agents per phase |
| `message_poll_hint_seconds` | `30` | Suggested polling interval for agents |
| `consensus_timeout_minutes` | `null` | Legacy global consensus timeout in minutes. When set, applies to every phase and overrides the phase-aware defaults below. When `null`, each phase falls back to its calibrated default. |
| `consensus_timeout_minutes_refine` | `null` (effective `30`) | Per-phase override for refine. Wins over the legacy global. |
| `consensus_timeout_minutes_plan` | `null` (effective `60`) | Per-phase override for plan. Wins over the legacy global. |
| `consensus_timeout_minutes_implement` | `null` (effective `90`) | Per-phase override for implement. Wins over the legacy global. |
| `brc_consensus_progress_gate_seconds` | `300` | Defer the consensus-timeout `OVERSEER_ALERT` while BRC bus activity (proposals, ACKs/NACKs) or container heartbeats have fired within this window. Set to `0` to disable. |
| `post_consensus_iteration_budget_seconds` | `3600` | Per-iteration wait budget in the post-timeout poll loop. Resets each time a producer issues a new `CONSENSUS_PROPOSE` (initial or NACK→re-propose), giving each iteration a clean clock. |
| `post_consensus_max_total_seconds` | `14400` | Hard ceiling on the total post-timeout wait, regardless of how often the per-iteration budget rebaselines. Must be ≥ `post_consensus_iteration_budget_seconds`. |
| `agent_idle_timeout_minutes` | `60` | Idle agent timeout before termination |

## Agent Startup Protocol

When concurrent execution starts, the `ConcurrentPhaseExecutor` (in `orchestrator/concurrent_executor.py`) queries `get_roles_for_phase(phase, include_reviewers=True)` (from `shared/egg_contracts/agent_roles.py`) to determine which roles to spawn simultaneously using a `ThreadPoolExecutor`. Roles are phase-dependent:

| Phase | Spawned roles |
|-------|--------------|
| `refine` | `refiner`, `simplifier`, `reviewer_refine`, `reviewer_agent_design` (egg repo only) |
| `plan` | `architect`, `task_planner`, `risk_analyst`, `simplifier`, `reviewer_plan` |
| `implement` | `coder`, `tester`, `documenter`, `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`, `reviewer_concurrency` |

**Branch model**: For **refine** and **plan**, all agents operate on the pipeline's shared branch (e.g., `egg/issue-123/work`) and coordinate commits via the message bus to sequence their work and avoid conflicts. For **implement**, each slice runs on its own integration branch (`egg/issue-N/slice-M`); the shared-branch coordination described below applies *within* a slice's agent team — see [Slice-DAG Implement Phase](../architecture/slice-dag.md).

> **Branch namespace (#2399):** The pipeline tip is pushed to `<id>/work` (e.g., `egg/issue-123/work`) so slice integration branches at `<id>/slice-N` can coexist as siblings. A leaf ref at `<id>` and a child at `<id>/slice-N` cannot coexist in git.

**Environment injection**: Each concurrent agent receives:

| Variable | Value | Description |
|----------|-------|-------------|
| `EGG_CONCURRENT_MODE` | `"true"` | Signals to the agent that concurrent mode is active |
| `EGG_MESSAGE_POLL_INTERVAL` | `<seconds>` | Suggested polling interval for the message bus |
| `EGG_BRC_ROLE_TYPE` | `"producer"`, `"reviewer"`, or `"producer,reviewer"` | Agent's role in the BRC review graph |
| `EGG_BRC_REVIEWERS` | Comma-separated roles | Reviewer roles assigned to this producer (producers only) |
| `EGG_BRC_PRODUCERS` | Comma-separated roles | Producer roles this agent must review (reviewers only) |
| `EGG_SLICE_ID` | `"slice-<N>"` | Slice scope for per-slice BRC teams (implement phase only); absent for pipeline-level agents. BRC handlers forward this to the orchestrator so `CONSENSUS_*` signals are routed to the per-slice tracker. Also used for status reads: `mcp__brc__get_state` and `egg-orch consensus status` automatically scope to this value, so a per-slice agent sees its own slice's consensus rather than a misleading pipeline-level reconstruction (#2761). |

Each agent is registered in the peer consensus tracker before spawning begins.

## Consensus Wrapper

> The agent is one-shot per actionable BRC event. The orchestrator is the deterministic BRC loop driver: it derives the next action in-process (`egg-orch brc next-action`) and spawns a one-shot pod per event. There is no in-pod wait loop and no background heartbeat subshell; orchestrator ownership is the only mode. The wrapper around each pod handles exactly the single `EGG_EVENT_ACTION` it was spawned for, then exits. Liveness is governed by an idle/no-progress overseer-alert budget that never transitions the pipeline to FAILED. See [Orchestrator Architecture — BRC Consensus Wrapper](../architecture/orchestrator.md#brc-consensus-wrapper) for the orchestrator-owned event-loop semantics, the wait-filter construction, the idle-budget escalation table, and the supervision model. The wait-side companion is [agent-wait-patterns §10](../reference/agent-wait-patterns.md#10-brc-consensus-wrapper-event-pump-model).

All concurrent agent containers are wrapped with a shell script defined in `orchestrator/consensus_wrapper.py`. The **orchestrator** is the deterministic BRC loop driver: it derives the next action in-process from `egg-orch brc next-action` / `egg-orch brc get-state` and spawns a one-shot pod per actionable event. The wrapper around each pod is one-shot-only — it composes the per-event prompt via `compose_event_prompt`, invokes the agent for exactly the single `EGG_EVENT_ACTION` it was spawned for, and exits. It does not block in `egg-orch message wait-loop` between events and does not run a background heartbeat subshell.

**How it works:**

- The orchestrator derives the next action in-process from `egg-orch brc get-state` / `egg-orch brc next-action`. When a role's BRC work is complete it confirms and the pod exits cleanly (code 0); otherwise it spawns a one-shot pod for the next actionable event (`INVOKE` with a one-shot event prompt). The orchestrator owns the wait between events — the wrapper never blocks in `egg-orch message wait-loop` (see [Wake conditions](../architecture/orchestrator.md#wake-conditions-pre-confirm-vs-post-confirm) for the per-state filters the orchestrator applies).
- Clean exit after handling an event is the expected steady state — no restart, no recovery prompt.
- If the agent exits non-zero (transient signal-class crash, exit 1, or anything else), the wrapper increments `AGENT_FAIL_STREAK`, sleeps 1 s, and the orchestrator spawns the next event normally — there is no per-crash restart, no exponential backoff, and no exit-code branching on the `propose|ack|nack` arm. The `is_transient_crash` / `is_buffer_overflow` / `is_startup_failure` helpers are defined in the wrapper bash template but **not invoked** today; they are retained as named placeholders against a possible future classifier-gated fast-fail path (see `orchestrator/consensus_wrapper.py` L145–150).
- The wrapper does not propagate non-zero agent exits as FAILED transitions. They accrue against the **idle / no-progress safety budget** (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 minutes), which is the sole operator-visible escalation surface. At threshold and `2 ×` threshold the wrapper emits an `OVERSEER_ALERT`; the pipeline is never transitioned to FAILED by this path.

**Key design principle:** Agents must **explicitly** participate in consensus. The orchestrator never auto-signals `READY` on behalf of an agent — it spawns the agent to handle each actionable event and lets the agent issue ACK / NACK / PROPOSE / CONFIRM verdicts itself. Sequencing is the orchestrator's job; *judgment* (what to review, what to fix, when to re-propose) stays with the model.

**Design intent — the orchestrator is the loop, not a safety net.** The orchestrator *is* the BRC loop driver: clean per-event exits are the steady-state contract, not an edge case. It detects consensus and sends SIGTERM to terminate containers when the role is complete; the agent only exits because it finished its event handler or because consensus closed. There is no expensive context-reload-on-restart path.

**Configuration:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_turns` | `1000` | Maximum tool-call turns per agent run (set high so per-event agent invocations have headroom; per-event exits are short-lived, so this cap is never reached). |
| `max_ready_polls` | `10` | Maximum poll cycles (each ~30 s) for the "already-confirmed" guard that provides race-window safety when the wrapper observes `role_complete` between polls. |
| `STARTUP_FAILURE_WINDOW_SECONDS` | `30` | Window (seconds) used by the `is_startup_failure` classifier. The helper is defined but is not invoked by the current `propose|ack|nack` arm, so this knob has no runtime effect today; it is retained against a future classifier-gated fast-fail path. |
| `EGG_BRC_IDLE_BUDGET_MIN` | `30` | Idle / no-progress safety budget in minutes. At threshold and `2 ×` threshold the wrapper emits `mcp__progress__overseer_alert` (anomaly `stuck-phase-transition`) without transitioning the pipeline to FAILED. |
| `EGG_MESSAGE_POLL_INTERVAL` | `30` | Seconds between heartbeat emissions and message polls. |

## Message Bus

Agents communicate with each other during concurrent execution via the orchestrator message bus (`orchestrator/message_store.py` for the shared types and singleton accessor, `orchestrator/redis_message_store.py` for the store). Messages are stored in Redis Streams, surviving orchestrator restarts — the k8s manifests deploy a dedicated Redis (`k8s/base/redis-deployment.yaml`) and pin the orchestrator to it via `EGG_MESSAGE_STORE_BACKEND=redis` ([#2662](https://github.com/jwbron/egg/issues/2662)). Messages are cleared at phase transition. Unit tests run against the same store backed by `fakeredis` (the in-memory fallback backend was removed in [#3159](https://github.com/jwbron/egg/issues/3159)).

### How to Wait

**Agents don't wait — the orchestrator does.** Every blocking read on the message bus belongs outside the agent tier: the orchestrator derives the next BRC event in-process (`egg-orch brc next-action`) and spawns the agent one-shot per actionable event (see [Consensus Wrapper](#consensus-wrapper) above). There is no in-pod wait loop. The agent handles the single event in its prompt and exits naturally; it must never run `egg-orch message wait` / `wait-loop`, an outer poll loop, or a `sleep` — the orchestrator owns the wait, and an agent-tier wait double-waits against it while holding the SDK turn open. The agent-facing contract is stated in the per-event prompt itself (`orchestrator/routes/event_prompt.py`); the wait-side mechanics — the wrapper's `wait-loop` idiom and per-state filters, the five anti-patterns the pump retired, the `egg-orch message wait` exit codes, the `HEARTBEAT` schema, and the `EGG_MESSAGE_POLL_MAX_WAIT` ↔ gateway-Squid coupling — are in [Agent Wait Patterns](../reference/agent-wait-patterns.md).

The wrapper constructs the `--for` filter set conditionally per BRC state ([wake conditions](../architecture/orchestrator.md#wake-conditions-pre-confirm-vs-post-confirm)):

```bash
# Pre-confirm (wrapper-issued) — must NOT include CONSENSUS_CONFIRMED
# (orchestrator rejects it HTTP 400 — own confirm generates the signal; #2064, #2482)
egg-orch message wait-loop \
  --for CONSENSUS_PROPOSE \
  --for CONSENSUS_ACK \
  --for CONSENSUS_NACK \
  --for CONSENSUS_RE_REVIEW \
  --for STATUS \
  --for OVERSEER_ALERT
# `--for STATUS` is required (#2531): once every reviewer has ACKed the current version,
# no further CONSENSUS_ACK/CONSENSUS_NACK arrive; the orchestrator emits a directed
# STATUS (metadata.ready_to_confirm == true) as the only signal that confirms are clear.

# Post-confirm (wrapper-issued) — exits on consensus, re-review, or overseer alert
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED \
  --for CONSENSUS_RE_REVIEW \
  --for OVERSEER_ALERT
```

### Sending Messages

```
POST /api/v1/pipelines/{id}/messages
```

Request body:

```json
{
  "from_role": "coder",
  "to_role": "tester",          // or "all" for broadcast
  "message_type": "PROGRESS",   // PROGRESS, STATUS, HANDOFF, HEARTBEAT, AGENT_FAILED
  "subject": "Implemented auth module",
  "body": "auth.py is complete, tests can begin",
  "metadata": {}
}
```

> `QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897) — it encouraged off-protocol chatter with no handler. Use `HANDOFF` when you need a peer to act, `HEARTBEAT` to advertise state, and typed NACK rationale to ask clarifying questions of a producer you're reviewing.

The pipeline's current phase is automatically attached to each message. This applies to both the general message endpoint and the consensus signal handlers — all `CONSENSUS_*` messages (propose, ACK, NACK, withdraw, confirmed, re-review) and other BRC-adjacent types (`STATUS`, `HANDOFF`, `AGENT_FAILED`, etc.) include the phase field so that downstream consumers like BRC history persistence and PR summary generation can correctly group messages by phase.

### Polling Messages

```
GET /api/v1/pipelines/{id}/messages?role=tester&since_id=<id>&limit=100
```

Query parameters:

| Parameter | Description |
|-----------|-------------|
| `role` | Return messages targeted to this role or broadcast to `"all"` |
| `since_id` | Return only messages after this message ID (for incremental polling). If the cursor is not found (e.g., after a phase-boundary clear or post-compaction anchor recovery), the store falls back to returning all messages rather than an empty list. |
| `limit` | Maximum messages to return (default: 100) |

Messages are returned oldest-first. The `since_id` filter excludes the reference message itself — only messages that follow it are returned. If the cursor ID is no longer present in the store (stale cursor), the endpoint degrades gracefully to a full-history replay instead of silently returning empty — preventing polling agents from stalling after a phase transition or anchor recovery.

### Message Bus Status

```
GET /api/v1/pipelines/{id}/messages/status
```

Returns total message count and a breakdown by message type.

### Message Types

| Type | Purpose |
|------|---------|
| `PROGRESS` | Agent progress updates for other agents |
| `STATUS` | General status announcements |
| `HANDOFF` | Agent signaling completion of a handoff artifact |
| `HEARTBEAT` | Agent state transition (`WORKING`, `WAITING_ON_ROLE`, `WAITING_FOR_EVENT`, `PROPOSED`, `IDLE`) — resets the orchestrator's `last_heartbeat` without emitting a free-form `PROGRESS` entry. `WAITING_FOR_EVENT` is a liveness keep-alive emitted automatically by `egg-orch message wait-loop` while blocked — agents don't need to emit it manually. See [Agent Wait Patterns — HEARTBEAT](../reference/agent-wait-patterns.md#4-heartbeat-message-type) for the metadata schema. |
| `AGENT_FAILED` | Orchestrator notifying agents of a peer failure |
| `CONSENSUS_PROPOSE` | Producer broadcasting its proposal for review |
| `CONSENSUS_ACK` | Reviewer approving a producer's proposal |
| `CONSENSUS_NACK` | Reviewer rejecting a producer's proposal (with reason) |
| `CONSENSUS_WITHDRAW` | Producer withdrawing its proposal (e.g., to address NACK) |
| `CONSENSUS_CONFIRMED` | Agent confirmed after all required reviews are ACKed |
| `CONSENSUS_RE_REVIEW` | Orchestrator notifying a reviewer that their prior confirmation is stale and they must re-review the producer's new proposal version |
| `OVERSEER_ALERT` | Health anomaly or lifecycle alert. Sent by the overseer agent for health anomalies (`from_role: overseer`), and by the orchestrator for event-loop supervision alerts — failure-streak exhaustion / convergence stalls (`from_role: orchestrator`, `_emit_supervision_alert`, #3064). Always carries an explicit `pipeline_id`. |

> **Removed in #1897**: `QUESTION` was dropped from the type vocabulary because it had no delivery semantics and was only used as informal free-form chatter. Agents that need a peer to act should use `HANDOFF`; agents that need to advertise state should use `HEARTBEAT`; reviewers with clarifying questions should put them in the `NACK` rationale so the producer sees them and can address them on re-propose.

### Message Store Backend

Redis Streams is the only message-store backend ([#3159](https://github.com/jwbron/egg/issues/3159) removed the in-memory store and the `auto`/`memory` selection modes that could reach it, along with the [#3077](https://github.com/jwbron/egg/issues/3077) fallback health flag that existed to catch the accidental `auto`→memory path). `EGG_MESSAGE_STORE_BACKEND` unset or `"redis"` selects Redis; any other value — including the removed multi-backend-era `"auto"`/`"memory"` — fails loudly at store creation, as does an unreachable Redis. The k8s deployment keeps the explicit `"redis"` pin ([#2662](https://github.com/jwbron/egg/issues/2662)); local non-k8s development needs a reachable `redis-server`, and unit tests get a `fakeredis`-backed store from `orchestrator/tests/conftest.py`. `integration_tests/test_message_store_backend.py` pins the deployed backend choice and round-trips a message through the live Redis.

**Long-poll semantics:** `GET /messages/wait?for=<TYPE>&timeout=<s>` blocks until a matching message arrives or the timeout elapses, implemented with `XREAD BLOCK` and a server-side type-filter loop. The silent non-blocking fallback that previously lived in `routes/messages.py` was removed in [#1897](https://github.com/jwbron/egg/issues/1897) so backend misconfiguration fails loudly in CI instead of returning empty results. See [Agent Wait Patterns](../reference/agent-wait-patterns.md#3-exit-code-contract-for-egg-orch-message-wait) for the full exit-code contract and the `EGG_MESSAGE_POLL_MAX_WAIT` cap.

**Clear-on-phase-transition safety:** When the store is cleared at phase boundaries, a wait blocked across the clear cannot observe pre-clear messages — it runs out its (bounded) timeout and returns an empty list, after which the caller re-enters against the post-clear stream.

### Per-Phase Cleanup

The message store is cleared when the phase transitions. Each new phase execution starts with an empty message bus for the pipeline. This prevents stale messages from a prior phase from being delivered to agents in the next phase.

**Note:** BRC messages (consensus messages and orchestrator-adjacent types like `HANDOFF`, `AGENT_FAILED`, `STATUS`, etc.) are persisted to `.egg-state/brc-history/{identifier}-{phase}.md` and `.json` at phase completion. Messages are filtered by phase, so each history file contains only that phase's BRC activity. See [BRC History Persistence](#brc-history-persistence) below.

## Directed Coordination

The message bus supports **directed coordination** — structured peer-to-peer messages for coordination needs that fall outside the formal BRC consensus protocol. While consensus messages (`CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, etc.) handle the review-and-converge lifecycle, directed messages handle the day-to-day coordination that keeps agents unblocked and informed.

### Why Not Proposal Text?

A common anti-pattern is embedding coordination requests in proposal summaries — for example, writing "tester agent should push those test files" in a `CONSENSUS_PROPOSE` body. This fails for several reasons:

1. **Discovery is accidental.** The other agent only sees the request if it happens to read the proposal text.
2. **No structured record.** Proposal text is free-form — there's no way to filter, query, or act on coordination requests programmatically.
3. **Wrong audience.** Proposals are broadcast to reviewers, not to the specific agent that needs to act.

Directed messages solve all three: they are delivered to the target agent's poll stream, have a structured type for filtering, and are persisted in BRC history for traceability.

### CLI Syntax

```bash
egg-orch message send --to <role> --type <type> --subject "<subject>" --body "<body>"
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--to` | Yes | Target agent role (e.g., `tester`, `coder`) or `all` for broadcast |
| `--type` | Yes | Message type: `HANDOFF`, `STATUS`, `PROGRESS`, `HEARTBEAT`. (`QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897) — see note below.) |
| `--subject` | No | Short description of the message |
| `--body` | No | Detailed message content |

The pipeline ID is auto-resolved from `EGG_PIPELINE_ID` if set; otherwise pass it as a positional argument.

### When to Use Each Type

| Type | Use when | Example |
|------|----------|---------|
| `HANDOFF` | You've produced an artifact that another agent needs to act on, especially when role boundaries prevent you from completing the work yourself | Tester can't push a `.github/` CI fix → HANDOFF to coder with the required end-state |
| `STATUS` | Your current state affects a peer's decisions or timing | Documenter tells reviewer: "Docs not ready yet, reviewing coder output first" |
| `PROGRESS` | You've completed a milestone that peers may be waiting on | Coder tells tester: "API endpoints committed and pushed" |
| `HEARTBEAT` | You have a machine-actionable state transition to advertise (`WORKING`, `WAITING_ON_ROLE`, `PROPOSED`, `IDLE`) — use `egg-orch message heartbeat --state ...` rather than `message send --type HEARTBEAT` so the dedicated endpoint's schema validation, dedup, and rate limiting apply. (`WAITING_FOR_EVENT` is auto-emitted by `egg-orch message wait-loop` — don't emit it manually.) | Tester enters `WAITING_ON_ROLE` → `egg-orch message heartbeat --state WAITING_ON_ROLE --waiting-on coder`. See [Agent Wait Patterns — HEARTBEAT](../reference/agent-wait-patterns.md#4-heartbeat-message-type). |

> **On `QUESTION` (removed in [#1897](https://github.com/jwbron/egg/issues/1897))**: the old `QUESTION` type had no guaranteed respondent and became a free-form chatter channel. For the typical "I'm blocked until you answer" case:
>
> - If you are a **reviewer** blocked on the producer's intent, put the question in your `egg-orch consensus nack --reason "..."` so the producer sees it in BRC history and addresses it on the next propose.
> - If you are a **producer** blocked on another producer (e.g. tester blocked on coder), use `HANDOFF` with a concrete request rather than a free-form question.
> - If you need to advertise that you are waiting on a peer (so the overseer doesn't classify you as stalled), emit `egg-orch message heartbeat --state WAITING_ON_ROLE --waiting-on <role>`.

### Worked Example: Role-Boundary Handoff (Tester → Coder)

The coder authors and **pushes its own tests** (the test scope is shared with
the tester — see [Agent Roles: Implement Phase](../reference/agent-roles.md#implement-phase)),
so the common coder/tester test exchange needs no coordination message at all.
Sharing the test scope this way avoids the coordination gap where a coder
writes tests it cannot push and the tester has to re-author them ([#1707](https://github.com/jwbron/egg/issues/1707)).

The HANDOFF pattern still applies in the **reverse** direction, for a file
type the tester genuinely can't push. While hardening the coder's tests, the
tester finds a CI workflow needs a new step to run a regression — but the
tester has no write access to `.github/`. It hands the required end-state to
the coder, who stages it under `.github-staging/`:

```bash
# 1. Tester (hardening the coder's tests after the coder's propose) finds the
#    CI workflow doesn't run the new regression suite. It can't push .github/,
#    so it sends a HANDOFF to the coder with the required end-state:
egg-orch message send --to coder --type HANDOFF \
  --subject "CI needs a step for the new regression suite" \
  --body "tests/test_auth_regression.py won't run in CI. The workflow needs a
step invoking it. Please stage the end-state under .github-staging/workflows/
ci.yml (add a 'pytest tests/test_auth_regression.py' step) and call the
staged file out in your PR body so the human reviewer moves it into
.github/ before merge."

# 2. Coder receives the HANDOFF on its next poll cycle:
egg-orch message poll --wait 30

# 3. Coder stages the workflow change under .github-staging/ and pushes it
#    (coder owns everything except docs and .egg-state/).
```

This keeps the role-boundary case the agents actually still hit unblocked, while the common coder/tester test exchange needs no coordination at all.

### Receiving and Acting on Directed Messages

Agents should check for directed messages during their regular poll cycle:

```bash
# Poll for messages (includes both consensus and directed messages)
egg-orch message poll --wait 30
```

When a directed message arrives:

1. **HANDOFF**: Act on the handoff artifact. If it requires work, do the work and acknowledge via a `STATUS` or `PROGRESS` message back.
2. **STATUS/PROGRESS**: Use the information to inform your own work — no response required unless the status changes your plan.
3. **HEARTBEAT**: Peer state transitions are informational — consume them (e.g., to decide whether to send a follow-up `HANDOFF`) but do not reply. The overseer consumes `HEARTBEAT` for stall detection; agents typically only read them to disambiguate "peer is waiting on me" from "peer is making progress elsewhere".

### Best Practices

- **Be specific.** Include file paths, commit SHAs, and concrete details — not just "please handle this."
- **Send early.** Don't wait until your proposal to communicate coordination needs. Send a HANDOFF as soon as you know another agent needs to act.
- **One message per concern.** Don't bundle unrelated coordination requests in a single message.
- **Use the right type.** `HANDOFF` signals "you need to do something"; `STATUS` and `PROGRESS` are informational peer updates; `HEARTBEAT` advertises typed agent state (emit via `egg-orch message heartbeat`, not `message send`).
- **Never use `QUESTION`.** It was removed in [#1897](https://github.com/jwbron/egg/issues/1897). Reviewer-to-producer questions go in `NACK` rationales; producer-to-producer "I need X" goes in `HANDOFF`; "I'm waiting on a peer" goes in a `HEARTBEAT` with `state=WAITING_ON_ROLE`.

## Readiness Signaling Protocol

Agents signal their readiness for phase completion using the `readiness` signal type via the pipeline signal endpoint:

```
POST /api/v1/pipelines/{id}/signal
{
  "signal_type": "readiness",
  "agent_role": "coder",
  "state": "READY",            // WORKING, READY, BLOCKED, OBJECTING
  "reason": "All tasks complete"
}
```

Readiness states:

| State | Meaning |
|-------|---------|
| `WORKING` | Agent is still working (initial state after registration) |
| `READY` | Agent has completed its work and is ready to advance the phase |
| `BLOCKED` | Agent is waiting on something (a peer, a resource, a question) |
| `OBJECTING` | Agent has a concern with advancing the phase |

Agents are auto-registered to `WORKING` state when spawned. An agent can update its state multiple times — for example, moving from `WORKING` to `BLOCKED` when waiting on a peer, then to `READY` when its work is done.

## Consensus Protocol

Phase completion uses the BRC (Broadcast-Review-Converge) protocol implemented in `orchestrator/peer_consensus.py`. Agents are assigned roles in an asymmetric review graph: **producers** create artifacts and propose them for review; **reviewers** evaluate proposals and issue ACK or NACK. Some agents (e.g., `tester`) have dual roles.

### BRC Phase States

Each agent tracks two state machines (producer and reviewer) independently:

| Phase | Applies to | Meaning |
|-------|-----------|---------|
| `WORKING` | Both | Still doing work, no proposal submitted |
| `PROPOSED` | Producers | Proposal broadcast, waiting for reviewer responses |
| `REVIEWING` | Reviewers | Actively reviewing a producer's proposal |
| `CONFIRMED` | Both | All required ACKs received; agent confirmed |

### BRC Protocol Flow

1. **Propose**: Producer completes work, commits and pushes to the remote branch, then sends `CONSENSUS_PROPOSE` with a summary, artifact list, and the pushed commit SHA (`--commit-sha`). The orchestrator rejects proposals whose commit SHA is confirmed absent from the branch (verification failures due to network errors are non-blocking).
2. **Review**: Assigned reviewers discover proposals via polling. Before a reviewer has submitted their own evaluation, the Delphi filter delivers a **redacted** version of the `CONSENSUS_PROPOSE` message (`body` cleared, `metadata.payload` stripped except `version` and `commit_sha`, `metadata.delphi_redacted=True`). This notifies the reviewer that a proposal exists without exposing the producer's self-assessment. The consensus wrapper's `sync_to_proposals` step (#3076, role-gated in #3216) runs before the `ack`/`nack` invocation. For execution reviewers (currently: `tester`), it performs a `git merge --no-edit` of the producer's proposed commit into the reviewer's worktree so they can run the test suite against the merged tree. All other reviewers read peer artifacts via `git show`/`egg-artifact` served reads in the event prompt and skip the working-tree merge entirely (#3216). After reviewing the git artifacts and submitting `CONSENSUS_ACK` or `CONSENSUS_NACK`, subsequent polls return the full unredacted message.
3. **Converge**: When all critical reviewers ACK, the producer sends `CONSENSUS_CONFIRMED`. When all agents are confirmed, the phase advances. Reviewers also call `CONSENSUS_CONFIRMED` after completing all reviews; the protocol enforces multiple guards in both the producer and reviewer confirmation paths (see [Action Guards](#action-guards) and [Deadlock Prevention Guards](#deadlock-prevention-guards) below).
4. **Re-propose**: If a NACK is received, the producer addresses the feedback and re-proposes (with `changed_artifacts` to scope re-evaluation). Flip-flop cycles are capped at `max_flip_flops` (default: 3). If any reviewer had already confirmed on a prior proposal version, they automatically receive a `CONSENSUS_RE_REVIEW` message and are un-confirmed so they re-enter the review loop — preventing a deadlock where a stale-confirmed reviewer can never see the new proposal.

   > **Note — `CONSENSUS_RE_REVIEW` handling:** A `CONSENSUS_RE_REVIEW` wakes the wrapper's wait and the agent is re-invoked with it as the actionable event; the agent **must** act on it in that invocation: reviewers of the re-proposing producer must re-review and ACK/NACK; all other agents must re-confirm via `egg-orch consensus confirmed`. Ignoring this message stalls the pipeline.

   > **Multi-reviewer NACK aggregation barrier ([#2142](https://github.com/jwbron/egg/issues/2142)):** When **two or more distinct reviewers** have NACKed the current proposal version, the orchestrator rejects the producer's first re-propose attempt with HTTP 409 and a structured `open_nacks_blocked` envelope. The response inlines every unresolved NACK (`reviewer`, `reason`, `artifact_refs`, `timestamp`) so the producer can address all blocking findings in one re-propose without a separate fetch. After this single round-trip the producer has been informed of the full NACK set; the retry advances the version. Single-reviewer NACKs do not trigger the barrier — the wrapper already re-invoked the producer with that NACK as its actionable event. The barrier exists to prevent the race where the producer's wait-loop returns one NACK, the producer fixes it and re-proposes, and other in-flight NACKs against the same version are silently superseded — wasting a full review cycle when those reviewers re-NACK the new version verbatim.

   > **Stale-version verdict rejection ([#2142](https://github.com/jwbron/egg/issues/2142)):** A reviewer whose ACK or NACK lands after the producer has re-proposed (i.e. the verdict targets a superseded version) is rejected with HTTP 409 and a structured `stale_version` envelope. The response inlines the producer's current proposal snapshot (`current_proposal.version`, `artifacts`, `commit_sha`) so the reviewer can re-fetch, re-review the diff, and re-submit without a separate status query. ACK guard already enforced version-match in `check_ack_guard`; the NACK guard now mirrors it via `check_nack_guard(..., nack_version=...)`.

> **Note — `pending_acks` (exit code 2):** After a re-proposal, previously-confirmed reviewers are un-confirmed and must re-ACK. If the producer calls `confirmed` before those re-ACKs arrive, the command returns exit code **2** (`pending_acks`) — this is transient, not an error. The orchestrator re-arms the "ready to confirm" `STATUS` nudge on every producer `pending_acks` rejection ([#2100](https://github.com/jwbron/egg/issues/2100)), and `STATUS` is in the wrapper's pre-confirm wait filter — the nudge fires automatically when the blocking condition (missing proposal, missing ACK, or stale ACK) clears and the wrapper re-invokes the producer. Disambiguate via `metadata.ready_to_confirm == True` (the same `STATUS` type is also used for unrelated notifications such as "Producer X excused from consensus"). Via `mcp__brc__confirm`, the equivalent is `ok=False` with `status="pending_acks"`; the agent exits and the wrapper re-invokes it when the STATUS nudge arrives. The wrapper's pre-confirm filter deliberately **omits** `CONSENSUS_CONFIRMED` — waiting on it before the producer's own confirm has succeeded deadlocks the pipeline (see [Anti-pattern 5](../reference/agent-wait-patterns.md#anti-pattern-5--producer-waits-on-consensus_confirmed-before-its-own-confirm-has-succeeded-2064)).
>
> **Note — Reviewer `pending_acks`:** Reviewers can also receive exit code 2 from `confirmed` when they have stale ACKs (e.g., an ACK recorded before the producer proposed) **or unresolved NACKs** (a NACK issued against a producer that has not yet re-proposed). In the stale-ACK case, the reviewer must re-ACK the listed producers at their current proposal version before confirming. In the unresolved-NACK case, the reviewer must wait for the NACKed producer to re-propose, then re-review and ACK/NACK the new version before confirming.

### Reviewer verdict variants

A reviewer has three outcomes on a proposal:

- **ACK** — proposal is correct as-is; ready to merge.
- **NACK** — proposal is wrong; producer must iterate before merge.
- **Conditional ACK** — proposal is correct but requires a specific human-only action *at merge time* (e.g. a `git mv`, a cross-repo config flip). Pass `--pre-merge-condition "..."` on `egg-orch consensus ack`; the condition is persisted on the approval-matrix edge, scoped to the current proposal version, surfaced in `egg-orch consensus status`, and rendered in a **Pre-merge Obligations** section on the auto-created PR body so the merger cannot skim past it. Not a soft NACK — if the agents can address the issue themselves, NACK instead. See the [Conditional ACK reference](../reference/conditional-ack.md).

### Implement-phase Reviewer Roster

On the implement phase, `reviewer_code` reviews every changed file systematically and emits a single CRITICAL ACK / NACK on the full diff. `reviewer_code_holistic` ([#2126](https://github.com/jwbron/egg/issues/2126)) runs alongside as a distinct CRITICAL reviewer focused on cross-module coherence — it skims the full diff once and runs four holistic passes (end-to-end use case, doc↔code symmetry, synthetic-key/sentinel audit, silent-fallback hunt) rather than verifying every line. Its NACK gates consensus independently of `reviewer_code`'s. Two CRITICAL lens reviewers `reviewer_security` and `reviewer_concurrency` (criteria in [`security-review-criteria.md`](../../shared/prompts/security-review-criteria.md) and [`concurrency-review-criteria.md`](../../shared/prompts/concurrency-review-criteria.md)) also run on the same change set; a NACK from either blocks consensus until the producer re-proposes ([#2139](https://github.com/jwbron/egg/issues/2139) — promoted from ADVISORY, closing [#1997](https://github.com/jwbron/egg/issues/1997)). `reviewer_contract` holds a CRITICAL edge to **every** producer — coder, tester, and documenter ([#3114](https://github.com/jwbron/egg/issues/3114)) — because it is the contract-completeness enforcer (next section); before #3114 its only edge was to the coder, so a documenter's contract rows could go entirely undelivered with no blocking reviewer.

### Contract Completeness Gate (#3114)

The contract reviewer (`reviewer_contract`; capability set `CONTRACT_ENFORCER_ROLES` in `shared/egg_contracts/agent_roles.py`) is the structural enforcer of contract-task delivery in the implement phase. The orchestrator's signal routes (`_contract_completeness_rejection` in `orchestrator/routes/signals.py`) read the **live** contract task records from the shared pipeline worktree and reject:

- **Enforcer ACK** (`409 contract_incomplete`) — while any task row owned by the ACKed producer in the active slice is not `status=complete`. The structured rejection inlines the incomplete rows so the reviewer NACKs the producer citing exact task ids (the producer either delivers the work or records finished work via `mcp__task__complete`).
- **Enforcer ACK without attestation** (`409 attestation_required` / `attestation_mismatch`) — a passing enforcer ACK must carry `attestation.tasks_verified` covering every task id the producer owns in the slice. The attestation rides the `attestation` parameter on `mcp__brc__ack` (threaded end-to-end since #3114; previously the MCP boundary dropped attestations entirely).
- **Enforcer CONFIRM** (`409 contract_incomplete`) — while **any** task row in the active slice is incomplete, regardless of owner. This is the closing backstop: it covers role-less rows and producers whose no-op proposal made `is_fully_acked` vacuous.
- **No-op proposal from a producer with open rows** (`400`) — `--no-changes-needed` asserts "no work for me in this slice"; the orchestrator rejects it when the producer still owns incomplete task rows, so a no-op cannot be an exit from assigned work.

A producer's declared deferral ("remaining tasks will land in later proposals") is therefore an open obligation that holds the slice's consensus open — not a statement reviewers can ACK past. The pending rows block the enforcer's ACK/CONFIRM until the work is delivered and recorded, or a human descopes the rows via the HITL path.

Scope and posture: implement phase only (plan/refine contracts are expected to have pending rows; the apply phase tracks lifecycle in `jira_action_status`). The gate fails **open** on orchestrator-side read failures (state store, worktree, contract load, unknown slice id) so an infrastructure glitch cannot deadlock in-flight consensus — same graceful-degradation posture as the propose-time validators ([#3081](https://github.com/jwbron/egg/issues/3081)). `EGG_CONTRACT_ACK_GATE=off` is the operator kill switch. Complementing the gate, reviewer task-status writes are demote-only (`shared/egg_contracts/validator.py`): a reviewer may flag a row `incomplete`/`blocked`/`pending` but only the implementer may set `complete`, so the enforcer cannot satisfy its own gate.

### Pre-Proposal ACK Protection

When agents work at different speeds, a faster reviewer may ACK a producer before the producer has submitted its proposal. The BRC protocol handles this automatically:

1. **On propose**: When a producer submits `CONSENSUS_PROPOSE`, any pre-existing version-0 ACKs (recorded before the first proposal) are invalidated. Affected reviewers appear in the `stale_reviewers` list in the proposal response and receive a `CONSENSUS_RE_REVIEW` notification to re-review.

2. **On confirm**: A version-match guard prevents reviewers from confirming with stale ACKs. If a reviewer's ACK version does not match the producer's current proposal version, `CONSENSUS_CONFIRMED` returns `pending_acks` (exit code 2) with a message listing which producers need re-ACKing.

These protections prevent a deadlock that previously occurred when a reviewer's stale version-0 ACK could never satisfy `is_fully_acked()`, permanently blocking the producer from confirming.

### Formal BRC State Machine

The BRC protocol is defined by a formal state machine with explicit **action guards** (preconditions) for every protocol action. Guards are implemented in `orchestrator/action_guards.py` as the canonical protocol specification — each `PeerConsensusTracker` handler delegates to the corresponding guard function before mutating state.

#### State Transition Diagram

**Producer states:**

```
                   ┌──────────────────────────────────┐
                   │                                  │
                   ▼                                  │
              ┌─────────┐    propose    ┌──────────┐  │  confirm    ┌───────────┐
              │ WORKING ├──────────────►│ PROPOSED ├──┼────────────►│ CONFIRMED │
              └────┬────┘               └─────┬────┘  │             └───────────┘
                   ▲                          │  │    │
                   │  NACK (auto-transition)  │  │    │
                   ├──────────────────────────┘  │    │
                   │  withdraw (voluntary)       │    │
                   ├─────────────────────────────┘    │
                   │                                  │
                   │    auto re-propose on push       │
                   │    (back to PROPOSED)             │
                   └──────────────────────────────────┘
```

- `WORKING → PROPOSED`: Producer calls `propose` after completing work and pushing commits.
- `PROPOSED → WORKING` (on NACK): Producer receives a NACK; the handler auto-transitions the producer back to WORKING so it can address feedback and re-propose.
- `PROPOSED → WORKING` (on withdraw): Producer voluntarily calls `withdraw` to retract its proposal (e.g., to address feedback proactively). Subject to cooldown and flip-flop limits.
- `PROPOSED → PROPOSED`: Producer pushes new commits, triggering auto re-propose via `handle_producer_push()`, which increments the proposal version and invalidates stale reviews.
- `PROPOSED → CONFIRMED`: All critical reviewers ACK and the producer calls `confirmed`.

**Reviewer states:**

```
              ┌─────────┐   ACK/NACK   ┌───────────┐  confirm   ┌───────────┐
              │ WORKING ├──────────────►│ REVIEWING ├───────────►│ CONFIRMED │
              └─────────┘               └─────┬─────┘            └─────┬─────┘
                                              │    ▲                   │
                                              │    │                   │
                                              └────┘                   │
                                        re-review on                   │
                                        re-proposal                    │
                                              ▲                        │
                                              │  CONSENSUS_RE_REVIEW   │
                                              └────────────────────────┘
```

- `WORKING → REVIEWING`: Reviewer submits first ACK or NACK against any producer.
- `REVIEWING → REVIEWING`: Producer re-proposes; reviewer's prior ACK is invalidated and they must re-review.
- `REVIEWING → CONFIRMED`: All assigned producers are reviewed with current-version ACKs.
- `CONFIRMED → REVIEWING`: Producer re-proposes after reviewer confirmed; `CONSENSUS_RE_REVIEW` un-confirms the reviewer.

**Dual-role agents** (e.g., `tester`): Both the producer and reviewer state machines must independently reach `CONFIRMED` before the agent is considered confirmed.

#### Action Guards

Guards are implemented in `orchestrator/action_guards.py` as standalone functions — one per protocol action. Each guard returns a `GuardResult` (a frozen dataclass with `allowed: bool`, `reason: str`, and `details: dict`). When `allowed` is `False`, the `reason` field contains a human-readable explanation and `details` provides machine-readable context (guard name, blocking agents, version info, etc.).

Each handler in `PeerConsensusTracker` calls its corresponding guard before mutating state:

| Guard function | Called by | Preconditions enforced |
|----------------|-----------|----------------------|
| `check_propose_guard()` | `handle_propose()` | Agent must be a producer. Must not be fully ACKed in PROPOSED state ([#1185](https://github.com/jwbron/egg/issues/1185)). |
| `check_re_propose_guard()` | `handle_re_propose()` | Agent must be a producer. **Multi-reviewer NACK aggregation barrier ([#2142](https://github.com/jwbron/egg/issues/2142)):** when ≥2 distinct reviewers have NACKed the current version and the producer hasn't yet been informed of the full set via a prior rejection, the call returns a structured `open_nacks_blocked` envelope (HTTP 409) inlining every unresolved NACK. The retry advances the version. |
| `check_ack_guard()` | `handle_ack()` | Agent must be a reviewer. Review edge must exist from reviewer to producer. |
| `check_nack_guard()` | `handle_nack()` | Agent must be a reviewer. Review edge must exist from reviewer to producer. |
| `check_confirm_guard()` | `handle_confirmed()` | **Global:** All producers in the review graph must have proposed at least once (global zero-proposal guard, [#1648](https://github.com/jwbron/egg/issues/1648)). **Producer path:** Must be fully ACKed by all critical reviewers at current version. **Reviewer path:** Must have reviewed all assigned producers. ACK versions must match current proposal versions (version-match guard, [#1405](https://github.com/jwbron/egg/issues/1405)). Must not hold unresolved NACKs ([#1576](https://github.com/jwbron/egg/issues/1576)). All assigned producers must have proposed at least once (per-reviewer zero-proposal guard, [#1598](https://github.com/jwbron/egg/issues/1598)). |
| `check_withdraw_guard()` | `handle_withdraw()` | Agent must be a producer. Reason required. Must have waited at least `cooldown_seconds` (default 30s) since proposing. Must not exceed `max_flip_flops` (default 3) propose→withdraw cycles. |

#### Protocol Invariants

The `validate_invariants()` function in `action_guards.py` (also exposed as a method on `PeerConsensusTracker`) checks five invariants against the current protocol state. Violations are returned as a list of `InvariantViolation` objects (a dataclass with `invariant`, `agent`, `description`, and `details` fields) — violations are not raised as exceptions.

| ID | Invariant | Description |
|----|-----------|-------------|
| **INV-1** | No confirmed agent with unresolved NACK | No agent in CONFIRMED state may hold an unresolved NACK against a producer that hasn't re-proposed since the NACK |
| **INV-2** | No confirmed reviewer with stale ACK | No reviewer in CONFIRMED state may have an ACK whose version does not match the producer's current proposal version |
| **INV-3** | No confirmed reviewer with unreviewed changes | No reviewer in CONFIRMED state if any producer has proposed at a version newer than the reviewer's last ACK |
| **INV-4** | No confirmed agent with zero-proposal producer | No agent (producer or reviewer) in CONFIRMED state if any producer in the review graph has proposal version 0 (never proposed). This is a global invariant — it applies regardless of review edge assignments ([#1648](https://github.com/jwbron/egg/issues/1648)). |
| **INV-5** | Consistent `is_fully_acked` | `is_fully_acked()` must be consistent with the actual approval matrix state — all critical reviewers must have ACKED entries at the current proposal version, and version 0 producers must report `is_fully_acked=False` |

### Deadlock Prevention Guards

The `handle_confirmed()` method enforces guards on both the producer and reviewer confirmation paths that prevent different classes of BRC deadlock. Guards return `pending_acks` (exit code 2) when triggered — the agent must resolve the condition before confirming.

| Guard | Trigger | Resolution |
|-------|---------|------------|
| **Stale-ACK version-match** | Reviewer's ACK version does not match the producer's current proposal version (e.g., ACK recorded before the producer proposed) | Re-ACK the listed producers at their current proposal version |
| **Unresolved-NACK (reviewer)** | Reviewer has NACKed a producer that has not yet re-proposed since the NACK | Wait for the NACKed producer to re-propose, then re-review and ACK/NACK the new version |
| **Unresolved-NACK (producer)** | Producer has unresolved NACKs from reviewers | Address the NACK feedback and re-propose |
| **Global zero-proposal** | Any agent (producer or reviewer) attempts to confirm but a producer anywhere in the review graph has never proposed (version 0) | Wait for all producers to propose, or escalate via HITL if a producer is non-delivering. This guard applies regardless of review edge assignments — even if the confirming agent doesn't directly review the zero-proposal producer ([#1648](https://github.com/jwbron/egg/issues/1648)). |
| **Zero-proposal producer (per-reviewer)** | Reviewer attempts to confirm but an assigned producer has never proposed (version 0) | Wait for the producer to propose, or escalate via HITL if the producer is non-delivering ([#1598](https://github.com/jwbron/egg/issues/1598)). This is a defense-in-depth check scoped to the reviewer's assigned producers. |
| **All-changes-reviewed** | Reviewer attempts to confirm but a producer has re-proposed (higher version) since the reviewer's last ACK | Re-review and ACK the producer's latest proposal version before confirming |

**Why the unresolved-NACK guard is needed:** Without this guard, a reviewer can enter terminal CONFIRMED state while still holding an open NACK against a producer. When that producer later re-proposes, the reviewer — already confirmed and only sending heartbeats — never re-reviews the new proposal. The producer is permanently blocked waiting for a re-ACK that never comes. The `_un_confirm_stale_reviewers()` mechanism handles the inverse ordering (reviewer confirms *after* re-proposal with a stale ACK), but cannot catch the case where the reviewer confirms *before* the re-proposal. The unresolved-NACK guard blocks this at the source by preventing the reviewer from confirming in the first place. See [#1576](https://github.com/jwbron/egg/issues/1576) for the original deadlock scenario.

**Why the zero-proposal guard is needed:** Without this guard, a reviewer can NACK a non-delivering producer (who never proposed), then confirm — causing consensus to complete without the primary deliverable. The original per-reviewer guard ([#1598](https://github.com/jwbron/egg/issues/1598)) blocked reviewer confirmation when any *assigned* producer had version 0. However, this was scoped to `graph.producers_for(agent_role)` — only the producers the reviewer is assigned to review. In [#1648](https://github.com/jwbron/egg/issues/1648), `reviewer_contract` (which only reviews `coder`) was able to confirm while `tester` had never proposed, because `tester` was not in `reviewer_contract`'s review edge list. The **global zero-proposal guard** closes this gap by checking *all* producers in the review graph before allowing *any* agent to confirm — regardless of review assignments. The per-reviewer guard is retained as defense-in-depth with a more specific error message.

### Auto Re-Propose on Push/Commit

When a producer pushes new commits after proposing, existing reviews become stale — reviewers may have ACKed code that no longer reflects the current state. The `handle_producer_push()` method on `PeerConsensusTracker` detects this and triggers a re-proposal, invalidating existing reviews and notifying reviewers. **Auto re-propose is always enabled** — to disable it, set `max_auto_repropose: 0` in `PipelineConfig`.

**How it works:**

1. When a producer pushes new commits (detected via commit SHA change in the signal handler), the orchestrator calls `tracker.handle_producer_push(agent_role, commit_sha, changed_files)`.
2. If the producer is still in `WORKING` state (hasn't proposed yet), the call is a no-op — there are no reviews to invalidate.
3. If the producer is in `PROPOSED` state, the method builds a minimal proposal payload using the changed files (or the previous proposal's artifacts if no specific files provided).
4. ACKs overlapping with the changed files are invalidated via `invalidate_overlapping_acks()` (scoped invalidation). If no specific files are provided, all ACKs for the producer are invalidated (conservative fallback).
5. The method calls `_handle_propose_inner()` to increment the proposal version and emit events, which triggers `CONSENSUS_RE_REVIEW` messages to affected reviewers.
6. Reviewers must re-review and ACK the new version before confirming.

**Guard enforcement**: The `check_confirm_guard()` function enforces that no reviewer can confirm with a stale ACK — if a producer has re-proposed at a higher version since the reviewer's last ACK, the version-match guard blocks confirmation. This provides a server-side blocking mechanism even if a reviewer misses the `CONSENSUS_RE_REVIEW` notification.

**Safety mechanisms** (configurable via `PipelineConfig`):

| Field | Default | Description |
|-------|---------|-------------|
| `auto_repropose_debounce_seconds` | `60` | Minimum seconds between consecutive auto re-proposals for the same producer. Prevents proposal storms on rapid-fire pushes. Also used as the explicit-proposal cover window (see below). |
| `max_auto_repropose` | `5` | Maximum automatic re-proposals per producer per review cycle. Set to `0` to disable auto re-propose entirely. Once the limit is reached, the producer must explicitly re-propose via `egg-orch consensus propose`. |

**Explicit proposal cover**: When a producer calls `egg-orch consensus propose --push`, the push and proposal happen atomically. If a push arrives within the `auto_repropose_debounce_seconds` window of an explicit `propose` call, the tracker skips the auto re-propose — the explicit proposal already covers the push, so no redundant re-review is triggered.

### Gateway-Level Push Enforcement (Pipeline Sessions)

While auto re-propose provides a **safety net** for stale reviews, it relies on the orchestrator detecting post-proposal pushes. A stronger guarantee comes from the gateway itself: for all pipeline sessions, **direct `git push` is blocked** — all pushes must go through `mcp__brc__propose` (the fallback CLI is `egg-orch consensus propose --push`).

**How the marker flows:**

1. Agent calls `mcp__brc__propose(...)` (push defaults to true) — or runs `egg-orch consensus propose --push`
2. Both surfaces delegate to `egg_agent_tools.push.consensus_push()`, which calls the gateway push API directly (bypassing the git wrapper) with `"consensus_push": true` in the JSON payload
3. The gateway checks: if the session has a `pipeline_id` AND the push is not infrastructure (pipeline state), then `consensus_push` must be present. The check no longer requires `EGG_CONCURRENT_MODE=true` — all SDLC producer phases are BRC phases, so direct push is blocked for every pipeline session ([#2028](https://github.com/jwbron/egg/issues/2028))
4. Pushes without the marker are rejected with HTTP 403 and the error points at `mcp__brc__propose`
5. Fallback: when `GATEWAY_URL` is not set (e.g., local development), the helper falls back to plain `git push`. No pipeline-push enforcement exists in this path — the gateway is not running to enforce it

**Relationship to auto re-propose:** Gateway enforcement makes auto re-propose less critical — every push IS a proposal, so there are no "orphan pushes" to detect. Auto re-propose remains as defense-in-depth for edge cases (e.g., if an agent manages to push through an alternative path).

**Killswitch:** Set `PIPELINE_PUSH_ENFORCEMENT=false` (or the legacy alias `CONCURRENT_PUSH_ENFORCEMENT=false`) on the gateway to disable. Use only for emergency bypass.

**Error message for agents:**
```
Direct git push is blocked for pipeline sessions. Publish your artifact via
the mcp__brc__propose tool (which pushes to origin and sends CONSENSUS_PROPOSE
in one step). Fallback CLI: `egg-orch consensus propose --push`.
```

See [Gateway README — Pipeline Push Enforcement](../../gateway/README.md#pipeline-push-enforcement-brc-sessions) for implementation details. See [#1669](https://github.com/jwbron/egg/issues/1669) for the motivating incident and design rationale.

### Excusing Non-Delivering Agents

When an agent fails to deliver (crashes, stalls, or exhausts restarts), it can block other agents from confirming. The protocol provides HITL-gated escape hatches to unblock consensus:

**`excuse_reviewer(role)`** (implemented): Removes a reviewer from the review graph entirely. All edges from the reviewer are removed, allowing affected producers to reach `is_fully_acked()` and call `confirmed` without the excused reviewer's ACK. Used when a reviewer crashes and the human selects "Continue without" in the HITL decision.

**`excuse_producer()`** (planned, [#1598](https://github.com/jwbron/egg/issues/1598)): Analogous to `excuse_reviewer()` but for producers. Would remove all review edges targeting the excused producer, unblocking agents who are blocked by the global zero-proposal confirm guard ([#1648](https://github.com/jwbron/egg/issues/1648)). Currently, non-delivering producers are handled via `handle_agent_crash()`, which assesses the impact and creates a HITL decision for the operator.

**When to use**: Agent excusal is intended for agents that cannot recover and would otherwise permanently block consensus. Both mechanisms are gated behind HITL decisions to prevent automated removal of critical roles.

### Recovery Mechanisms

The BRC protocol retains two recovery mechanisms as **defense-in-depth**, even though the formal guard table should prevent the conditions they handle. Both mechanisms log warnings and increment counters when they fire — a non-zero counter indicates a gap in the guard table that should be investigated.

| Mechanism | Method | Purpose | When it fires |
|-----------|--------|---------|---------------|
| **Stale reviewer un-confirmation** | `_un_confirm_stale_reviewers()` | Un-confirms reviewers whose ACKs are stale after a producer re-proposes | Called during `handle_propose()` and `handle_re_propose()`. Fires when a reviewer confirmed on a prior proposal version and the producer has since re-proposed. Transitions the reviewer back to REVIEWING and invalidates stale ACKs. |
| **Pre-proposal ACK invalidation** | `_invalidate_pre_proposal_acks()` | Invalidates version-0 ACKs that can never match a post-proposal version | Called during `handle_propose()`. Fires when a reviewer ACKed before the producer's first proposal (version 0 ACK). Only processes non-confirmed reviewers. |

**Design rationale**: These mechanisms were added before the formal guard table to fix specific deadlock scenarios ([#1405](https://github.com/jwbron/egg/issues/1405), [#1576](https://github.com/jwbron/egg/issues/1576)). With the guard table in place, `guard_version_match_at_ack` and `guard_producer_proposed` should prevent the conditions that trigger these mechanisms. They are retained as a safety net — if they fire in production, it indicates the guards missed an edge case.

### Generic No-Op Propose (#3027)

When a producer has no work in a slice — no assigned tasks, or its domain is not impacted by the diff — it submits a **generic no-op proposal** instead of a normal one:

```bash
egg-orch consensus propose \
  --no-changes-needed \
  --no-changes-reason "No assigned tasks in this slice; coder diff touches no documented surface"
```

Or via the MCP tool, pass `no_changes_needed=True` and `no_changes_reason="..."` in the `mcp__brc__propose` request.

**How the orchestrator handles it:**

- The proposal **counts as proposed** for the global zero-proposal guard (consensus is not blocked waiting for the role to step forward).
- `is_fully_acked()` returns `True` immediately — reviewers neither review nor NACK it, so it creates no blocking edges.
- `--artifacts` and `--commit-sha` are not required; strict per-role attestation pre-flight is skipped.
- A subsequent real proposal from the same producer (one with `no_changes_needed=False`) overwrites the no-op state normally.

**Applies to any producer role** — no per-role flag or orchestrator-side configuration needed.

**Implement phase only.** The orchestrator's no-op propose guard (`orchestrator/routes/signals.py`) rejects `--no-changes-needed` in any phase other than `implement` with HTTP 400 ("the producer's draft is required for this phase"). Refine and plan-phase producers (architect, task_planner, risk_analyst) must author and commit their draft — a no-op propose is not a substitute for the analysis or plan artifact.

**Tester edge case: mutually exclusive with `tests_execution_blocked`.** A tester proposal that combines `no_changes_needed=true` with `attestation.tests_execution_blocked=true` is rejected by `validate_no_changes_blocked_mutual_exclusion` (`orchestrator/attestation_schemas.py`). The two flags mean different things — "no changes needed" means the configured checks ran and there was nothing to author; "tests execution blocked" means the checks could not run at all. Pick one.

**`--no-changes-reason` is required.** The reason must be a non-empty string explaining why the producer has no work (e.g. "no tasks assigned in this slice", "doc-only slice, no code changes"). A silent skip is not accepted; the justification is an audit record.

#### Agent lifecycle for no-op producers

1. **ORIENT** — run `egg-contract show` and confirm the role has no tasks in the current slice.
2. **Propose no-op**: run `egg-orch consensus propose --no-changes-needed --no-changes-reason "..."`.
3. **Confirm**: run `egg-orch consensus confirmed` (or `mcp__brc__confirm`):
   - **Success** → exit; the wrapper re-invokes on `CONSENSUS_CONFIRMED`, `CONSENSUS_RE_REVIEW`, or `OVERSEER_ALERT`.
   - **`pending_acks` / `global_zero_proposal`** (other producers haven't proposed yet) → exit; the wrapper blocks in `egg-orch message wait-loop` on the pre-confirm filter set — `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `STATUS`, `CONSENSUS_RE_REVIEW`, `OVERSEER_ALERT` (the six-event set minus `CONSENSUS_CONFIRMED`; see [Agent Wait Patterns §10.2](../reference/agent-wait-patterns.md#102-wait-filter-construction-is-conditional-on-is_role_confirmed)) — and re-invokes you when any of them arrives. Retry `mcp__brc__confirm` on re-invocation when `ready_to_confirm: true`.
4. On re-invocation: handle the event (`CONSENSUS_RE_REVIEW`, `CONSENSUS_CONFIRMED`, or `OVERSEER_ALERT`) and exit normally.

**Source**: `ApprovalMatrix.record_proposal` (`no_changes` flag), `ApprovalMatrix.is_no_changes_proposal`, `ApprovalMatrix.is_fully_acked`, `ApprovalMatrix.get_blocking_edges` in `orchestrator/approval_matrix.py`; `_has_pending_peer_proposals` skip in `orchestrator/routes/consensus.py`.

### Delphi Redaction

The Delphi filter prevents reviewer anchoring by redacting `CONSENSUS_PROPOSE` messages until the reviewer has submitted their own independent evaluation. When a reviewer polls for messages before ACK/NACK:

- The message `body` is cleared (empty string)
- All `metadata.payload` keys are stripped except `version` and `commit_sha`
- `metadata.delphi_redacted` is set to `True`

The redacted message preserves enough information for the reviewer to know *who* proposed and *which commit* to review, without exposing the producer's summary, attestations, or self-assessment. After the reviewer submits their ACK or NACK, subsequent polls return the full unredacted message.

> **Why redact instead of withhold?** Previously, the filter dropped `CONSENSUS_PROPOSE` messages entirely from reviewers who hadn't evaluated the producer. This created a deadlock: reviewers waiting for a PROPOSE message to discover proposals never received one, because the filter withheld it until they reviewed. Redaction preserves the notification while protecting independent evaluation.

Use `egg-orch consensus` commands to participate in the BRC protocol. **For any flag carrying free-form prose** — `--summary` on `propose`; `--reason` on `ack` / `nack` / `withdraw`; `--files-reviewed` on `ack` / `nack` — prefer the `--*-file PATH` or stdin sentinel (`-`) channel over the argv form. The argv form still works but now emits a deprecation warning; the file/stdin channels are mandatory in any wrapper bash that composes the CLI invocation, because shell metacharacters (`$VAR`, backticks, `;`, `&&`, embedded newlines) in the prose are otherwise reinterpreted by the shell before argparse sees them and silently corrupt the verdict. See [#2741](https://github.com/jwbron/egg/issues/2741) for the original shell-injection finding and [Orchestrator CLI — Prose-bearing args](../reference/orchestrator-cli.md#prose-bearing-args-stdin-and---file-channels-2741) for the full channel table.

```bash
# Producer: commit and push work, then propose for review (--commit-sha defaults to HEAD if omitted)
# --summary must be ≥50 chars describing what was built, tested, and which contract tasks it satisfies.
# Boilerplate like "looks good" or "approved" is rejected with HTTP 400.
git add src/feature.py && git commit -m "Implement feature X"
printf '%s\n' "Implemented feature X with JWT validation and session management. All contract tasks satisfied." \
  > .egg-state/agent-outputs/coder-proposal.md
egg-orch consensus propose --push \
  --summary-file .egg-state/agent-outputs/coder-proposal.md \
  --artifacts src/feature.py --files-changed src/feature.py --tests-run tests/test_feature.py \
  --tasks task-1-1 task-1-2 --risk "No retry on transient failures" --commit-sha $(git rev-parse HEAD)
# --push runs git push before sending the proposal; because the push is bundled with the
# explicit proposal, auto re-propose is suppressed for that push (no redundant re-review).
# --files-changed, --tests-run, --tasks are optional but recommended for traceability.
# Argv `--summary "…"` still works but writes a deprecation warning to stderr — see #2741.

# Reviewer worktree sync is handled automatically by the consensus wrapper's
# sync_to_proposals step (#3076, role-gated in #3216): the working-tree merge
# runs only for execution reviewers (tester); other reviewers read via git-show.
# Manual fetch+merge is only needed in non-event-pump contexts or for debugging.
# git fetch origin && git merge origin/egg/feature-x --no-edit

# Reviewer: ACK after reviewing
# --reason is required and must be ≥50 chars. Your --reason IS your review — include full analysis.
# Boilerplate like "lgtm" or "no issues" is rejected with HTTP 400.
# Prefer --reason-file (or `--reason -` with stdin) for multi-line prose containing
# Markdown, backticks, $VAR, or embedded newlines — see #2741.
cat > /tmp/reviewer-code-ack.md <<'EOF'
Reviewed src/feature.py lines 10-85 and tests/test_feature.py. Verified JWT expiry
and invalid-signature handling. All branches covered by tests.
### Non-blocking
- **src/feature.py:72** — Consider extracting token_from_header() for readability.
EOF
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py \
  --reason-file /tmp/reviewer-code-ack.md --ack-version 1

# Reviewer: conditional ACK — work approved but requires a human action before merge.
# Use --pre-merge-condition when the work is correct but requires a merge-time action
# that agents cannot perform (e.g. git mv, secret rotation, config flip in another repo).
# The obligation is rendered as a "Pre-merge Obligations" section on the PR — do NOT use
# this to smuggle blocking issues past the producer; NACK if the producer can fix it.
# --pre-merge-condition is validated like --reason: boilerplate and short values are rejected with 400.
# Author a distinct review-prose file for the conditional case so the verdict
# narrative actually matches the obligation (don't re-use the unconditional file).
cat > /tmp/reviewer-code-cond-ack.md <<'EOF'
Reviewed src/feature.py lines 10-85 and tests/test_feature.py. JWT validation and
session-management logic are correct end-to-end, and tests cover all branches.
One file rename cannot be automated by the agent and must be performed by a human
before merge — captured in --pre-merge-condition below.
EOF
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py \
  --reason-file /tmp/reviewer-code-cond-ack.md --ack-version 1 \
  --pre-merge-condition "A human must \`git mv legacy/auth.py src/auth.py\` before merging — agents cannot push renames through the gateway"

# Reviewer: NACK with structured blocking/non-blocking sections — stdin sentinel form
egg-orch consensus nack coder --files-reviewed src/feature.py --nack-version 1 \
  --reason - <<'EOF'
### Blocking
1. **src/feature.py:42** — Missing error handling for expired tokens;
   auth bypass possible. Fix: wrap in try/except and return 401.
### Non-blocking
- **src/feature.py:18** — Unused import `datetime`.
EOF

# Producer: withdraw proposal to address NACK feedback
printf '%s\n' "Addressing NACK: adding retry logic for transient HTTP failures in src/feature.py" \
  | egg-orch consensus withdraw --reason -

# Producer: confirm after all reviewers ACK
# Exit 0 = confirmed. Exit 1 = error. Exit 2 = waiting for reviewer re-ACKs (retry after polling).
egg-orch consensus confirmed

# Check consensus status (scopes to $EGG_SLICE_ID automatically in implement phase)
egg-orch consensus status

# Resolve a reviewer's conditional-ACK obligation in-cycle (#2338).
# Typically called by the tester after cherry-picking work the coder is gateway-blocked from.
# --note is prose; pass via --note-file PATH or `--note -` with stdin for any
# multi-line body, per the same prose-arg rule above.
cat > /tmp/obligation-resolved.md <<'EOF'
Cherry-picked the rename commit ($(git rev-parse HEAD)) onto the slice branch on
the tester's behalf; the gateway accepts the legacy/auth.py → src/auth.py move
under the tester's role-restriction set, so the original conditional-ACK
obligation no longer needs to surface on the PR body or the HITL gate.
EOF
egg-orch brc resolve-obligation \
  --reviewer-role reviewer_code \
  --producer-role coder \
  --commit-sha $(git rev-parse HEAD) \
  --note-file /tmp/obligation-resolved.md
```

#### `egg-orch brc` — the verb-level read/derive surface

The `consensus` subcommands above are the **write-side** of BRC (proposes, acks, withdraws, confirms). The companion `egg-orch brc` surface — `next-action`, `get-state`, `list-blocking`, `resolve-obligation`, `read-peer-artifact` — is the **read / derive** side: each subcommand is a thin CLI shim over the corresponding `mcp__brc__*` handler, used by the event-pump consensus wrapper to decide what the agent should do next without paying for an LLM round-trip. See [Orchestrator CLI — BRC verb-level operations](../reference/orchestrator-cli.md#brc-verb-level-operations-egg-orch-brc) for the full subcommand table and shell examples.

### BRC History Persistence

At each phase boundary, the orchestrator writes a **lossless** chronological log of all BRC-related messages to `.egg-state/brc-history/{identifier}-{phase}.md` and a companion `.egg-state/brc-history/{identifier}-{phase}.json` (where `{identifier}` is the issue number or pipeline ID). This deterministically preserves the full agent communication context — including structured metadata — in git history for code review reference and machine consumption.

**How it works:**

1. After a phase completes (before `_commit_statefiles_to_worktree`), the orchestrator retrieves all messages from the message store for the pipeline
2. Messages are filtered using `BRC_HISTORY_TYPES` — the six `CONSENSUS_*` types (`CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_WITHDRAW`, `CONSENSUS_CONFIRMED`, `CONSENSUS_RE_REVIEW`) **plus** orchestrator-adjacent types (`STATUS`, `HANDOFF`, `AGENT_FAILED`, `NUDGE`, `OVERSEER_ALERT`, `HEARTBEAT`) — **and** by phase, so each file contains only that phase's BRC and coordination activity
3. If matching messages exist, they are formatted as chronological markdown entries with full metadata (see file format below) and written to `.egg-state/brc-history/{identifier}-{phase}.md`. A companion `.json` file containing `msg.to_dict()` for every filtered message is also written for machine consumers
4. If no matching messages exist for that phase, no files are created (graceful no-op)

> **Note:** `BRC_HISTORY_TYPES` is a single unified frozenset containing all twelve message types listed above. There is no separate subset — the PR body links to the committed transcripts rather than computing inline tallies (see [#1828](https://github.com/jwbron/egg/issues/1828)). `QUESTION` was dropped from this set in [#1897](https://github.com/jwbron/egg/issues/1897); `HEARTBEAT` replaced it.

**Per-phase write is the only path:** The per-phase write (step 1) is best-effort — if the commit or push fails, BRC history files may not make it to the branch. The legacy "PR-phase safety net" that re-wrote BRC history for all completed phases before opening the terminal PR was removed alongside the PR phase itself in [#2777](https://github.com/jwbron/egg/issues/2777); under the new topology the context PR opens up-front at the plan→implement boundary and there is no separate terminal stage to re-write from. If BRC history files are missing on the branch, diagnose the per-phase push directly via the worktree-state-synchronization push logs.

**Diagnostic logging:** All functions in the BRC history persistence chain (`_write_brc_history`, `_commit_statefiles_to_worktree`, `_rewrite_brc_history_for_pr`) emit INFO-level logs at entry, exit, and each decision point (early returns, glob match counts, staged file counts, commit outcomes). This makes it possible to trace exactly which code path was taken when BRC history files are missing from a PR. See [PR-Phase State File Troubleshooting](sdlc-pipeline.md#pr-phase-state-file-troubleshooting) for a step-by-step diagnostic guide.

**Phase tracking requirement:** The phase filter in step 2 (`m.phase == phase`) means that every BRC message must have its `phase` field set at creation time. The consensus signal handlers in `orchestrator/routes/signals.py` resolve the phase via `_resolve_pipeline_phase()`, which loads the pipeline state and returns the current phase name (falling back to `"implement"` on error). Without a phase value, messages would be invisible to the phase filter and the history file would be empty.

**Markdown file format:**

Each message is rendered with a header showing `from_role`, `to_role` (for directed messages only — omitted for broadcasts), `message_type`, and `subject`, followed by the message body and a fenced YAML metadata block containing the message `id`, `phase`, and the full `metadata` dict (when non-empty). YAML blocks use a 4-backtick fence (` ```` `) so that triple-backtick code fences in message bodies cannot break the metadata block. This preserves structured fields that were previously dropped: `artifact_references`, `ack_version`, `commit_sha`, `revision_count`, and `version`.

`````markdown
# BRC Consensus History — {phase} phase

Generated: {timestamp}
Pipeline: {pipeline_id}

### [2026-04-08T12:00:00Z] coder (CONSENSUS_PROPOSE): Implemented feature X

Summary of proposal and artifacts...

````yaml
id: abc123def456
phase: implement
metadata:
  commit_sha: 9f3a1b2c
  version: 1
  payload:
    artifact_references:
      - orchestrator/routes/pipelines.py
````

### [2026-04-08T12:05:00Z] reviewer_code → coder (CONSENSUS_ACK): Reviewed coder proposal

ACK with file-level feedback...

````yaml
id: def789abc012
phase: implement
metadata:
  version: 1
  payload:
    ack_version: 1
    artifact_references:
      - orchestrator/routes/pipelines.py
      - orchestrator/tests/test_brc_history.py
````

### [2026-04-08T12:10:00Z] coder (CONSENSUS_CONFIRMED): All reviewers ACKed
`````

> In the example above, note `reviewer_code → coder` on the ACK — the `→ {to_role}` component appears only when the message is directed (i.e., `to_role` is not `"all"`). Broadcast messages omit this.

**JSON companion file:** The `.json` file contains an array of `msg.to_dict()` dicts for every filtered message, serialized with `json.dumps(..., indent=2, default=str)` to tolerate non-JSON-serializable metadata values. The JSON write is independent of the markdown write — a failure in one does not block the other (both log at warning level). This file is the authoritative machine-readable record; the markdown file is the human-readable projection.

**Design rationale:** BRC messages live in the Redis-backed message store and are cleared at phase transitions. Without persistence, this valuable communication context is lost. Previously, some agents would commit review files to `.egg-state/agent-outputs/`, but this was non-deterministic — it depended on whether the LLM agent happened to `git add` those files. Writing BRC history from the orchestrator at phase boundaries makes persistence deterministic. The lossless projection (full metadata in YAML blocks, all BRC-adjacent message types, and a JSON companion) ensures that no structured data is dropped during persistence — even if agents send rich metadata (artifact references, commit SHAs, ack versions), it all reaches the PR branch.

### BRC History Link in PR Body

When the orchestrator auto-creates the context PR (up-front at the plan→implement boundary, [#2777](https://github.com/jwbron/egg/issues/2777)), it includes a one-line pointer to the committed BRC history transcripts rather than inlining the full consensus discourse. The link line is built by `_build_brc_history_link_line`, which scans `.egg-state/brc-history/` for `{identifier}-<phase>.md` files and renders a sentence like:

> _Per-phase BRC transcripts: [`refine`](./.egg-state/brc-history/42-refine.md), [`plan`](./.egg-state/brc-history/42-plan.md), [`implement`](./.egg-state/brc-history/42-implement.md)._

In slice-aware mode (issue mode with `contract.slices`), the implement phase is partitioned per slice — the writer produces `{identifier}-implement-slice-<N>.md` (one file per slice) plus `{identifier}-implement-unattributed.md` for cross-cutting messages without canonical slice scope (HEARTBEAT, OVERSEER_ALERT, AGENT_FAILED, …). The aggregate `{identifier}-implement.md` file is **not** produced in slice mode. The link line clusters the per-slice files at the canonical `implement` rank in natural-sort order, with the unattributed sibling rendered last:

> _Per-phase BRC transcripts: [`refine`](./.egg-state/brc-history/42-refine.md), [`plan`](./.egg-state/brc-history/42-plan.md), [`implement-slice-1`](./.egg-state/brc-history/42-implement-slice-1.md), [`implement-slice-2`](./.egg-state/brc-history/42-implement-slice-2.md), [`implement-unattributed`](./.egg-state/brc-history/42-implement-unattributed.md)._

Non-slice implement runs continue to emit the aggregate `{identifier}-implement.md` file. Phases are ordered by canonical execution order (`refine` → `plan` → `implement` → `pr`); any non-canonical names sort alphabetically after. The line is omitted entirely when no transcript files exist on disk or the identifier is `None`. See [#1828](https://github.com/jwbron/egg/issues/1828) for why the old inline BRC Consensus Summary was removed and [#2548](https://github.com/jwbron/egg/issues/2548) for the per-slice partition.

### Consensus Check

```
GET /api/v1/pipelines/{id}/status   // concurrent.consensus in the response
```

The `concurrent.consensus` key is **only present** when a consensus tracker with registered agents is active. It is omitted entirely when no tracker or evaluator is available (e.g., phases that do not yet implement BRC). After an orchestrator restart, the tracker is reconstructed from message store history during startup reconciliation, so the key is typically present for in-flight concurrent phases. Callers should still check for the key's presence before using it, as reconstruction may find no prior messages in edge cases (e.g., a brand-new phase that hasn't exchanged consensus messages yet).

The consensus block returns:

```json
{
  "is_complete": false,
  "blocking_agents": ["tester"],
  "has_unresolved_nacks": true,
  "unresolved_nacks": [
    {"reviewer": "reviewer_code", "producer": "coder", "reason": "Missing error handling", "version": 1}
  ],
  "protocol": "brc",
  "agents": {
    "coder": {"producer_phase": "PROPOSED", "confirmed": false},
    "reviewer_code": {"reviewer_phase": "REVIEWING", "confirmed": false}
  }
}
```

Consensus is reached when `is_complete: true` — all registered agents are confirmed **and there are no unresolved NACKs** in the approval matrix. An agent can be in the `confirmed` set but `is_complete` still remains `false` if a reviewer has issued a NACK that the producer has not yet addressed. The `version` field in each NACK entry tracks which proposal iteration the NACK was issued against, so agents and operators can tell whether the producer has re-proposed since the NACK.

### Objections

If any agent is in the `OBJECTING` readiness state (separate from BRC phase), the orchestrator detects the objection and surfaces it to the human as a HITL decision for resolution before the phase can advance.

### Timeout Handling

If consensus is not reached within the resolved per-phase timeout (per-phase override > legacy global > calibrated default — refine 30, plan 60, implement 90; see issue #2263), the orchestrator first checks the **BRC progress gate** before publishing the `OVERSEER_ALERT`. While any of the following have fired within `brc_consensus_progress_gate_seconds` (default 300 s), the orchestrator continues polling rather than escalating immediately:

- A `CONSENSUS_PROPOSE` or ACK/NACK on the BRC bus
- A container heartbeat from any active role in the current phase

Once the bus and containers have been quiet for the full gate window, the BRC tracker (`PeerConsensusTracker.handle_timeout()`) evaluates blocking agents by role criticality:

- **Critical blockers** (required reviewers still unconfirmed): emits `CONSENSUS_FAILURE` and publishes an `OVERSEER_ALERT` (subject `consensus-timeout: <agent_role> [high]`, where `<agent_role>` is the first critical-blocker role; the phase is preserved in `metadata.phase`) so the SDLC skill surfaces it as a non-blocking notification — see [issue #2264](https://github.com/jwbron/egg/issues/2264). The alert's `metadata.blocking_agents` is narrowed to the critical-blocker roles only (advisory roles, if any, are excluded so the high-priority signal isn't diluted).
- **Advisory-only blockers** (non-critical roles unconfirmed): emits `CONSENSUS_TIMEOUT` and proceeds automatically — no alert created.
- **No blockers**: proceeds immediately with no alert.

After the timeout check, if the approval matrix still has unresolved NACKs (producers that exited without addressing reviewer feedback), the phase returns failure regardless of which agents are confirmed.

If the BRC tracker is unavailable, the orchestrator publishes a fallback `OVERSEER_ALERT` (subject `consensus-timeout: <agent_role> [medium]`, falling back to `<phase>` in the role slot when no blocking agents are reported) so the timeout remains visible to the operator. The alert metadata carries `anomaly_type=consensus-timeout`, `phase`, `blocking_agents`, `latest_proposal_at`, `latest_heartbeat_at`, and `consensus_timeout_minutes`. The polling loop continues with a bounded post-timeout budget (~60 minutes before still-running containers are force-killed); the alert is informational, not a gate, and the operator can intervene with `cancel_task` or `restart_phase` if desired.

Timeout handling is idempotent — if the timeout fires multiple times (e.g., due to a race with the overseer), only the first invocation takes effect.

**Consensus reached during timeout wait**: After the BRC timeout evaluation, the orchestrator enters an event-driven polling loop that rechecks consensus proactively:

- **Polling mechanics**: 30-second intervals. The per-iteration budget (`post_consensus_iteration_budget_seconds`, default 3600s) resets each time a producer issues a new `CONSENSUS_PROPOSE`, so productive multi-iteration BRC cycles are not cut off mid-iteration. An absolute cap (`post_consensus_max_total_seconds`, default 14400s) bounds the total wait regardless of proposal churn.
- **Consensus complete** (all agents confirmed, no unresolved NACKs): the orchestrator immediately stops remaining containers, marks agents complete, restores the pipeline from `FAILED` to `RUNNING` if needed, and returns success — the timeout evaluation is overridden by the consensus outcome.
- **Unresolved NACKs remain**: the orchestrator escalates to HITL with options "Retry phase", "Accept current state", "Abort phase". See [issue #1693](https://github.com/jwbron/egg/issues/1693).

This replaces the former per-container blocking wait, closing the race where a late NACK→re-propose→ACK cycle completing near the end of the budget could previously be missed ([issue #1921](https://github.com/jwbron/egg/issues/1921)).

### Consensus Stall Recovery

A separate scenario from timeout: all agents have confirmed (consensus is complete) but the phase execution has not advanced — for example, because the orchestrator's polling loop missed the completion event. The `ConsensusStallCheck` (Tier 1 health check) detects this on each `RUNTIME_TICK` (and `ON_DEMAND`) after a 60-second grace period.

When a stall is detected, `ContainerMonitor` drives a two-track recovery:

1. **Tracker reconstruction**: Attempts to rebuild the in-memory consensus tracker from message history so the polling loop can pick up completed consensus naturally.
2. **Aggressive recovery**: If reconstruction fails, marks all running agents and the phase as `COMPLETE` directly, using optimistic locking to avoid conflicts with concurrent state writers.

Startup reconciliation also handles this: when tracker reconstruction succeeds on orchestrator restart and `evaluate()` reports `is_complete: true`, agents and the phase are marked `COMPLETE` before normal pipeline polling resumes. In slice-aware mode (issue mode with `contract.slices`), reconciliation iterates `contract.slices` and reconstructs each per-slice tracker by calling `reconstruct_tracker_from_messages(pipeline_id, graph, slice_id=<slice>)` in addition to the pipeline-level call (closes [#2409](https://github.com/jwbron/egg/issues/2409)). Each tracker is registered under the runtime key `{pipeline_id}/{slice_id}` so a recycled orchestrator pod does not lose in-flight slice consensus; cross-slice isolation is enforced by the strict-equality `_message_slice_id(m) == slice_id` filter in `reconstruct_tracker_from_messages` (not by the lenient store-level filter, which lets `metadata.slice_id is None` messages through for OVERSEER_ALERT fan-out).

A complementary check, `IncompleteConsensusStallCheck`, handles the inverse scenario: consensus is **not yet complete** and the same blocking agents are not progressing (e.g., stuck in a heartbeat loop after a re-review cycle). After a 5-minute grace period, if the blocking set is unchanged for 10 consecutive `RUNTIME_TICK` events, the check reports `DEGRADED`. The overseer then sends targeted nudges and escalates to HITL if unresolved. See [Pipeline Health Monitoring](pipeline-health-monitoring.md#incomplete-consensus-stall-detection) for details.

### All-Container-Exit Consensus Recovery

Step 5 of `_run_concurrent_phase` handles the terminal path where all agent containers have exited. It has two branches — one for when failures occurred (non-clean exit codes) and one for clean exits (exit code 0 or 143). **Both branches now perform a final consensus recheck** via `executor.check_consensus()` before deciding the phase outcome.

**Has-failures branch** (at least one non-zero exit code):

A race condition exists between the orchestrator's consensus polling (step 2) and this all-container-exit fallback (step 5). When all containers exit with non-zero codes (e.g., after a withdrawal/re-proposal cascade causes agents to exit code 1), the step-2 consensus check may read stale tracker state — missing that consensus actually completed — and step 5 would immediately return `exit_code=1` without rechecking.

To close this race window, step 5 performs a **final consensus recheck**. If consensus is complete on this second check, the orchestrator recovers: it calls `_update_agents_complete()` and `_stop_running_containers()`, restores the pipeline from `FAILED` to `RUNNING` status if needed (same recovery logic as step 2), and returns `exit_code=0` — the successful outcome.

This scenario was observed in issue #1564, where an overseer restart triggered a coder withdrawal cascade. All 5 agents sent `CONSENSUS_CONFIRMED` and exited within seconds, but the orchestrator returned failure because the step-2 check read stale consensus state and step 5 returned immediately without a recheck.

**No-failures branch** (all containers exit code 0):

Even when all containers exit cleanly, consensus may not have been reached — agents can exit code 0 without completing the full BRC lifecycle (e.g., exiting before reaching `CONFIRMED` state). Previously, this branch returned success unconditionally (after checking for unresolved NACKs), which allowed the pipeline to advance and open a PR despite consensus never being reached.

To close this gap, the no-failures branch now performs the same **final consensus recheck**. If `is_complete` is False, it logs a warning ("All containers exited cleanly but consensus not reached") and returns `exit_code=1` — preventing phase advancement. This ensures BRC consensus is a hard gate for phase completion regardless of exit codes.

This scenario was observed in issue #1581, where a PR was opened with code changes despite consensus never being reached for any phase.

The additional API call in step 5 is negligible — it only runs on the terminal path where all containers have already exited.

### Transient Crash Recovery

Before an agent failure reaches the orchestrator's `handle_agent_crash()` path, the consensus wrapper absorbs non-zero agent exits at the wrapper level rather than propagating them. There is **no per-crash restart and no exponential backoff** — every non-zero exit from a `propose|ack|nack` agent invocation is handled identically: `AGENT_FAIL_STREAK++`, `sleep 1 s`, and the orchestrator spawns the next actionable event normally. Because the agent is one-shot per event, the orchestrator simply derives the next action via `egg-orch brc next-action` on the following iteration. If the next actionable event yields a successful invocation, the failure is fully absorbed at the wrapper level — the orchestrator never sees a failure event. Repeated failures are bounded by the **idle / no-progress safety budget** (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 minutes): the wrapper emits an `OVERSEER_ALERT` at threshold and at `2 ×` threshold but does **not** mark the pipeline FAILED. There is no `MAX_CONSENSUS_RESTARTS` hard cap and no `TRANSIENT_RESTART_BACKOFF_INITIAL` backoff. The `is_transient_crash` / `is_buffer_overflow` / `is_startup_failure` helpers in `consensus_wrapper.py` are defined but not invoked. See [Agent Recovery: Consensus Wrapper](../reference/agent-recovery.md#crash-handling-in-the-event-pump-wrapper) for the full exit-code table.

### Agent Failure During Consensus

When an agent crashes, `PeerConsensusTracker.handle_agent_crash()` assesses impact:
- Escalation occurs when a crashed reviewer was the **sole reviewer** for a producer, **or** when the reviewer had pending (non-ACKed) reviews for a producer that has already proposed. Both cases create a HITL decision. When the reviewer had pending reviews, the question lists the affected producers.
- When the human selects **"Continue without"** for a failed reviewer, `excuse_reviewer()` removes all of that reviewer's edges from the review graph. This allows affected producers to reach `is_fully_acked()` and call `confirmed` without the excused reviewer's ACK.
- For failed producers, `handle_agent_crash()` assesses the impact on reviewers and creates a HITL decision. See [Excusing Non-Delivering Agents](#excusing-non-delivering-agents) for the planned `excuse_producer()` mechanism.
- Otherwise, the agent is removed from consensus tracking and treated as a single-agent failure (see failure recovery below).

**Stall demotion for dual-role agents**: If a dual-role agent (e.g., `tester`) misses heartbeats for 5+ minutes without crashing, the orchestrator automatically demotes its reviewer edges from CRITICAL to ADVISORY via `PeerConsensusTracker.handle_stall_demotion()`. This allows producers that the stalled agent was assigned to review to reach `is_fully_acked()` and call `confirmed` without waiting for that agent's ACK. The demotion is permanent for the current phase and emits a `CONSENSUS_FAILURE` event with type `stall_demotion`. Unlike a crash (which triggers a HITL decision), stall demotion is fully automatic.

### SIGTERM Handling During Phase Transitions

When a phase completes and the orchestrator stops agent containers, agents receive SIGTERM and exit with code 143. The Kubernetes monitor's `_classify_exit` treats exit codes 0 and 143 identically as clean exits — the agent is marked `COMPLETE`, the container `EXITED`, and `pipeline.status` is never mutated by the monitor regardless of phase state. The `routes/pipelines.py` BRC poll loop applies the same classifier, so the two layers cannot race to write contradictory agent status values.

This means clean BRC exits (exit code 0) and orchestrator-initiated teardowns (exit code 143) are both safe to observe without triggering HITL escalation or false `FAILED` pipeline transitions. Only genuinely unexpected exits (any other non-zero code) are classified as failures.

## Failure Recovery

### Single Failure

When a single agent fails, the `ConcurrentPhaseExecutor`:

1. Records the failure timestamp
2. Sends an `AGENT_FAILED` broadcast message to all other agents via the message bus
3. Removes the failed agent from consensus tracking
4. Creates a HITL decision with options: "Retry (respawn agent)", "Abort phase", "Continue without"

### Multiple Failures (2+ within 60 seconds)

If 2 or more agents fail within a 60-second window (`MULTI_FAILURE_WINDOW_SECONDS`), the executor immediately aborts the phase:

1. Emits a `PHASE_FAILED` event
2. Creates a HITL decision with options: "Retry phase", "Cancel pipeline"

The abort path does not create individual HITL decisions per failure — it treats simultaneous failures as a systemic issue requiring human intervention.

The 60-second window is tracked via the `_failure_times` list, filtered to recent entries on each failure.

### Agent and Phase Restart

Beyond the automatic retry-on-failure path above, the orchestrator supports explicit **restart** operations that can be triggered by the overseer, HITL operators, or the CLI/API:

**Agent-level restart** (`POST /api/v1/pipelines/{id}/agents/{role}/restart`):
- Stops the stuck container and removes it
- Resets the agent's BRC consensus state (withdraws proposals, ACKs, NACKs, confirmations)
- Respawns the agent with the same role/phase/env, **reusing the existing worktree** (committed work is preserved)
- Injects recovery context so the new agent can resume from where its predecessor left off
- Tracked per agent per phase; the overseer auto-restarts up to 2 times (configurable) before escalating to HITL

**Phase-level restart** (`POST /api/v1/pipelines/{id}/phases/{phase}/restart`):
- Stops and removes all containers for the phase
- Resets all BRC consensus state (`PeerConsensusTracker.clear()`) and review cycle counters
- Preserves all prior phase artifacts, HITL decisions, and branch commits
- Respawns all agents from scratch with optional additional context
- Requires HITL approval by default when triggered by the overseer

Both are also available as MCP tools (`restart_agent`, `restart_phase`) and CLI commands (`egg-orch agent restart`, `egg-orch phase restart`). See [Agent Recovery Reference](../reference/agent-recovery.md) for detailed mechanics.

### HITL Escalation Paths

| Scenario | HITL Options |
|----------|-------------|
| Single agent failure | Retry (respawn), Abort phase, Continue without |
| Agent stall (overseer-detected) | *(auto-restarted by overseer, up to max restarts)* |
| Agent stall (restarts exhausted) | Restart agent, Abort phase, Continue without |
| Multiple agent stalls (2+ restarts exhausted) | Restart phase, Cancel pipeline |
| Multiple failures (2+ / 60s) | Retry phase, Cancel pipeline |
| Consensus timeout (critical blockers) | *(no HITL — `OVERSEER_ALERT` `priority=high`; orchestrator continues post-timeout polling)* |
| Consensus timeout (advisory only) | *(no HITL — proceeds automatically)* |
| Consensus timeout fires, consensus reached during wait | *(no HITL — recovered automatically via timeout recheck)* |
| Agent objection | Resolve then advance, Override, Abort |
| All agents exited with failures, consensus complete | *(no HITL — recovered automatically via final recheck)* |
| All agents exited cleanly, consensus incomplete | *(phase fails with exit code 1 — no advancement)* |
| All agents exited with unresolved NACKs | Retry phase, Accept current state, Abort phase |

## Slice PR Stack

The implement phase opens a **stacked PR train** rather than a single PR
against the pipeline base branch. Reviewers approaching any PR in the
stack see the strategic context (analysis + plan + refine/plan BRC
consensus) below them, and per-slice diffs that each carry their own
implement-phase BRC history. The model satisfies two structural goals:
(1) every PR that lands on `main` carries the consensus record that
produced it; (2) reviewers don't have to discover `.egg-state/drafts/`
and `.egg-state/brc-history/` on a side branch — every artifact a
reviewer needs is in the stack below them.

**Stack shape (top-down):**

```
{pipeline.base_branch}                   (e.g. main)
 ↑
egg/<id>/work                            (Context PR — egg/<id>/work → main;
                                         carries analysis + plan + BRC + program-level
                                         framing, test plan, manual steps, pre-merge obligations)
 ↑
egg/<id>/slice-1                         (Slice-1 PR — slice-1 code + slice-1 BRC)
 ↑
egg/<id>/slice-2                         (Slice-2 PR — slice-2 code + slice-2 BRC)
 ↑
…                                        (subsequent slices stack onto their parent slice)
```

The base branch is taken from `pipeline.base_branch` (typically `main`,
falling back to the repo default branch) — it is **not** hardcoded.
Slices cascade-merge: once the context PR merges into the base branch,
slice-1 retargets onto the base branch via the
[stacked-PR rebase reconciler](../architecture/slice-dag.md#stacked-pr-rebase-reconciler);
once slice-1 merges, slice-2 retargets, and so on through the chain.

### Context PR is opened up-front

After the plan phase completes and the plan_gate is approved, the
orchestrator (not an agent) opens the **Context PR** —
`egg/<pipeline_id>/work → main` — **up-front, hard-required and
idempotent**, before any slice spawns. The PR's purpose is to
establish the program (analysis + plan + the consensus that produced
them, plus the program-level test plan, manual steps and pre-merge
obligations) so reviewers approaching any slice PR see the strategic
direction below it.

The pipeline work branch is itself the context PR's head — there is no
separate `egg/<id>/context` doc-only branch, so slice-1 (and every
later slice) stacks directly on `egg/<id>/work` rather than on a
distinct context branch ([#2548](https://github.com/jwbron/egg/issues/2548)/[#2777](https://github.com/jwbron/egg/issues/2777)
— the context branch / `context_*` fields introduced in #2548 were
reshaped into this single-work-branch model by #2777). The open path is a single
idempotent gateway call: `GatewayClient.lookup_open_pr("egg/<id>/work", base)`
runs `gh pr list --head <branch> --base <base> --state open --json
number` first; on hit, the existing PR number is reused, on miss
`gh pr create` runs once. There is no separate PR phase as a pipeline
stage; the up-front open is hard-required so there is no silent-failure
path a terminal stage would need to back-stop.

Mechanics:

1. The orchestrator validates that the plan agent has produced the
   artifacts the context PR references (analysis + plan drafts +
   refine/plan BRC history + populated contract). Validation failure
   refuses the plan→implement transition (no orphaned slices).
2. The refine/plan artifacts are already on the work branch via the
   post-phase pushes (see [SDLC Pipeline — Worktree State
   Synchronization](sdlc-pipeline.md#worktree-state-synchronization)):
   - `.egg-state/drafts/<id>-analysis.md` (refine output)
   - `.egg-state/drafts/<id>-plan.md` (plan output, with full
     `yaml-tasks` block)
   - `.egg-state/brc-history/<id>-refine.{md,json}` (refine BRC
     consensus record)
   - `.egg-state/brc-history/<id>-plan.{md,json}` (plan BRC consensus
     record)
   - `.egg-state/agent-outputs/<id>-refine-*.{md,json}` and
     `<id>-plan-*.{md,json}` (per-phase agent transcripts — included
     for maximum transparency)
   - The contract JSON, resolved dynamically via `get_contract_path`
     so integer issue identifiers map to the canonical
     `.egg-state/contracts/issue-<N>.json` shape (with the legacy
     bare `<N>.json` as a fallback).
3. It opens the PR against the pipeline base branch using
   `contract.pr.title` and `contract.pr.description` — the same fields
   that frame the implementation PR. Planners use the standard title
   and description; there are no separate context-PR framing keys. See
   the [schema migration note](../architecture/sdlc-pipeline.md#schema-v11--v12-migration-note-2777)
   for the field names.
4. The PR is **doc-only auto-open**: the orchestrator opens it, humans
   review on the PR, and the pipeline does **not** block on its merge
   before slicing begins. Slice-1 stacks on `egg/<id>/work` (the
   context PR's head) immediately so the slice train can run in
   parallel with reviewers approving the context.

The context PR is recorded on the contract via a single context-PR
field on `PRMetadata`:

| Field | Description |
|-------|-------------|
| `pr.context_pr_number` | The GitHub PR number of the `egg/<id>/work → main` context PR — populated by the orchestrator when the PR is opened. |

There are no separate context-PR framing fields: the head branch is
always derivable as `egg/<pipeline_id>/work`, and the title/description
live on the standard `pr.title` / `pr.description`. See the
[schema migration note](../architecture/sdlc-pipeline.md#schema-v11--v12-migration-note-2777)
for the field history.

### Slice-1 base resolution

Slice-1's `parent_branch` resolves to **`egg/<id>/work`** — the
pipeline work branch is itself the context PR's head. Subsequent
slices (slice-2, slice-3, …) stack onto the preceding slice's
integration branch.

The stacked-PR reconciler's last-resort fallback (when a parent branch
has been merged or deleted out from under an open child PR) prefers
`egg/<id>/work` (the context PR's head) when retargeting orphaned
slices. The work branch therefore acts as the canonical "program
root" reference for the lifetime of the pipeline.

### Per-slice BRC history on each slice PR

When a slice's implement-phase BRC reaches consensus, the orchestrator
commits `.egg-state/brc-history/<id>-implement-slice-<N>.{md,json}`
to the slice integration branch as a final orchestrator-authored
commit before the slice PR is opened. Each slice PR's diff therefore
carries its own slice-scoped consensus history — reviewers approaching
any PR in the stack see the proposals, ACKs, and NACKs that produced
*that slice's* code, with cross-cutting messages (HEARTBEAT,
OVERSEER_ALERT, AGENT_FAILED, …) captured in a sibling
`<id>-implement-unattributed.{md,json}` file.

The orchestrator-authored final commit is necessary because the
`coder` and `tester` role boundaries forbid pushes under
`.egg-state/brc-history/`; mirroring the existing
`_commit_statefiles_to_worktree` pattern keeps history persistence
deterministic regardless of which agent role last touched the slice.

The aggregate `<id>-implement.{md,json}` file is **not** produced in
slice-aware mode — the per-slice and unattributed files are the only
implement-phase transcripts. See
[BRC History Link in PR Body](#brc-history-link-in-pr-body) for the
link-line behaviour and the per-slice/unattributed natural-sort
ordering.

## Per-Agent Worktree Isolation

Each concurrent agent runs in its own isolated git worktree. This prevents agents from overwriting each other's uncommitted work, ensures a clean `git status` per agent, and surfaces merge conflicts explicitly at push time rather than silently in a shared working directory.

**Architecture:**
- Each agent pod receives a unique worktree created by the gateway, keyed by Job name (not pipeline ID)
- For **refine** and **plan**, all agents push to the same shared pipeline branch (e.g., `egg/issue-{N}/work`); for **implement**, all agents within a slice push to that slice's integration branch (`egg/issue-{N}/slice-{M}`) — see [Slice-DAG Implement Phase](../architecture/slice-dag.md)
- Git worktrees share the object store — only working tree files are duplicated, so disk overhead is marginal

**Push coordination (pull-before-push):**
1. Agent finishes work, commits in its own worktree
2. Agent pushes to the team's branch (pipeline branch for refine/plan, slice integration branch for implement) via the gateway
3. If push is rejected (another agent pushed first) → `git pull --rebase` → retry push
4. Rebase rarely conflicts because agents have largely non-overlapping file write permissions (see [Agent Roles Reference](../reference/agent-roles.md))

The documenter's docs/markdown scope is mutually exclusive from the others, so it never conflicts. The **coder and tester share the test scope** (the coder authors its tests; the tester reviews-and-hardens them), but the writes are serialized in time rather than concurrent: the tester does no test work until the coder's `CONSENSUS_PROPOSE`, so by the time it hardens a test the coder's version is already on the branch and `git pull --rebase` brings it in first. A genuine same-line conflict is possible but rare, and — by design (see the section intro) — surfaces explicitly at push time rather than silently.

**What changed:** Previously, all agents in a pipeline shared a single worktree. The orchestrator used the `pipeline_id` as the worktree key, forcing all agents to share one working directory. Now each agent pod gets its own worktree, using the Job name as the key.

### Reviewer Worktree Sync

Per-agent worktrees are created at phase start from the team's branch — the pipeline branch for refine/plan, the slice integration branch for each implement slice. When a producer pushes commits and proposes, the reviewer's worktree does not automatically have those commits.

The consensus wrapper's `sync_to_proposals` step (#3076) handles this automatically: before every `ack` or `nack` invocation the wrapper checks the agent's `EGG_AGENT_ROLE` against the execution-reviewer set (`REVIEWER_CHECKOUT_ROLE_VALUES` in `egg_contracts.agent_roles`, rendered into the wrapper as `checkout_roles`, #3216).

- **Execution reviewers** (currently: `tester`) — the reviewer must run `make test` against the proposed tree, so the wrapper extracts each pending producer's `proposal_commit_sha` from the event payload, hex-validates the SHA, and attempts a `git merge --no-edit` of each SHA into the reviewer's worktree. If the merge fails the wrapper prepends a `> **WARNING:** worktree NOT synced to <sha> (<outcome>)` banner at the very top of the agent's prompt ([#3077](https://github.com/jwbron/egg/issues/3077)) — on `merge-failed` it aborts the merge first, while on `unresolvable` the SHA never resolves so there is no merge to abort. Either way the reviewer should check for that banner and re-run the `git log` command from the event prompt against the producer's branch rather than trusting the local diff.
- **All other reviewers** — the wrapper skips the working-tree merge entirely and returns immediately (no banner, no merge). These roles read peer artifacts via the per-event-prompt served reads — `egg-artifact get` for spec-registered artifacts, the `git log` delta for unregistered paths; merging the peer's whole tree into their worktree buys nothing and risks the dual-role criss-cross propagation that corrupts shared drafts (#3208/#3216).

The BRC preamble (`_build_brc_preamble()`) still emits a SYNC instruction in the spawn prompt for reviewers — but the event-pump discards spawn prompts between events (#3033), so the wrapper bash is the reliable sync layer. For spec-registered coordination artifacts (plan-draft, analysis-draft, architect-output), the first-review event prompt exclusively renders `egg-artifact get <name> --ref <proposal_commit_sha>` read commands (#3216 WS1 of #3209), replacing the former path-bearing `git show <sha>:<path>` commands. The gateway resolves the artifact repo path server-side from the spec-registered name, so no shared object store or local checkout is required. Unregistered paths (e.g. code files) are covered by the full `git log <sha> --not origin/<base> -p` delta.

The producer `propose` arm intentionally skips the sync: a producer's own commits are already on HEAD, and merging peer proposal commits onto a producer turn would cause dual-role bleed-through.

**Reviewer diff command:** Reviewers use `git diff origin/{base_branch}...HEAD` (three-dot merge-base syntax) to see the full changeset against the base branch, rather than an arbitrary truncated window. The `base_branch` is resolved from `pipeline.base_branch` or the repository's default branch. This matches the context available to PR review bots, which see the complete PR diff.

**Delta re-review command (BRC `review_cycle > 1`):** When a reviewer has already reviewed a prior proposal and the producer re-proposes at a new commit, `_build_review_prompt()` emits a *delta* command instead of the full changeset:

```bash
git fetch origin {base_branch}
git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p
```

`git log A..HEAD --not origin/{base}` lists only PR-side commits that are reachable from `HEAD`, reachable since the last review at `A`, and **not** reachable from the base branch — so commits that arrived via a base-branch merge between the last review and `HEAD` are excluded. This prevents the reviewer from attributing merged-in base-branch work to the producer's delta (see [#1758](https://github.com/jwbron/egg/issues/1758)). The naive alternatives both show those merged-in changes: two-dot `git diff A..HEAD` compares the `A` tree to the `HEAD` tree directly and so naturally includes everything that landed in between, merge or not; three-dot `git diff A...HEAD` expands to `git diff $(git merge-base A HEAD)..HEAD`, and because `A` is already an ancestor of `HEAD` the merge-base collapses to `A`, so three-dot reduces to the same tree diff.

In practice, BRC is designed to reach consensus in a single cycle, so the delta re-review path is rare for orchestrator reviewers — but the fix applies identically to all multi-cycle BRC reviews (`reviewer_code`, `reviewer_contract`, etc.) and to the GitHub Action PR/design/contract-verify review bots, which hit the re-review path more often.

### Per-Agent Git Author

Each agent commits with a role-scoped author identity for auditability:

```
Author: egg (coder) <coder@egg.local>
Author: egg (tester) <tester@egg.local>
Author: egg (documenter) <documenter@egg.local>
```

This makes `git log` immediately readable — you can see which agent wrote each commit. The author identity is set via `EGG_AGENT_ROLE` in the container entrypoint.

### Scoped Push File Detection

The gateway's push validation (`get_changed_files_in_push()`) only reports files from the current agent's commits, not the entire branch diff. It does this by using `git rev-list remote/branch..HEAD` to enumerate the new commits being pushed, then running `git diff-tree` on each commit individually to collect the changed files. This per-commit approach avoids a tree-level `git diff remote/branch..HEAD`, which would report all cumulative differences between the two tree states — including files changed by other agents who previously pushed to the same remote branch.

Combined with per-agent worktree isolation, this eliminates false positives where an agent's push was rejected because a *different* agent had committed files outside this agent's role boundaries.

### Contract API in Concurrent Mode

Contract state is owned by the **orchestrator**, not individual agent worktrees. The gateway proxies all `egg-contract` requests to the orchestrator's `/api/v1/contracts/` endpoints, which read and write the **shared pipeline worktree** (`/home/egg/.egg-worktrees/<pipeline_id>/<repo>/`). This ensures every agent — producer and reviewers — sees the same contract regardless of which per-agent worktree it runs in (see [#1781](https://github.com/jwbron/egg/issues/1781)).

Per-agent worktrees are used only for code isolation. Contract files live exclusively in the shared worktree and are serialized to the feature branch at phase checkpoints via `_commit_statefiles_to_worktree`.

## Orchestrator API Reference

For the full message bus and signal API, see [Orchestrator Architecture: API Endpoints](../architecture/orchestrator.md#api-endpoints).

Concurrent-execution-specific endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/pipelines/{id}/messages` | Send a message to the bus |
| `GET` | `/api/v1/pipelines/{id}/messages` | Poll messages (with filters; `?wait=<s>` for long-poll) |
| `GET` | `/api/v1/pipelines/{id}/messages/status` | Message bus statistics |
| `POST` | `/api/v1/pipelines/{id}/signal` | Readiness or BRC consensus signal |

## Structured Progress Reporting

In addition to the message bus, agents emit structured progress events to the orchestrator for health monitoring. These events feed the deterministic tripwire system that detects stalls, loops, and failures.

```bash
# Report progress on current work step
egg-orch progress emit --step "running tests" --state working --detail "pytest suite 3/5"

# Report a blocker
egg-orch progress emit --step "waiting for dependency" --state blocked --blocker "coder not ready"
```

Agents should emit progress at key milestones (starting/completing steps, encountering blockers, during long operations). See [Pipeline Health Monitoring](pipeline-health-monitoring.md) for the full structured progress API and health monitoring architecture.

### BRC-Aware Stall Suppression

The health monitor is aware of BRC protocol state and **suppresses stall alerts for reviewer-only agents correctly idle in BRC protocol**. During concurrent execution, reviewer-only agents legitimately sit idle while waiting for upstream producers to send a `CONSENSUS_PROPOSE` message. The health monitor queries the peer consensus tracker's review graph and skips heartbeat/progress stall alerts for reviewer-only agents whose upstream producers are all still in the `WORKING` phase. Dual-role agents (those that are both producers and reviewers) are not suppressed, since they have their own work to complete.

**Two-phase suppression:** Stall suppression for reviewers now covers two distinct periods:

1. **Pre-proposal idle**: Reviewer-only agents whose upstream producers are all still in `WORKING` phase are fully suppressed. This is the original BRC-idle suppression behavior.
2. **Post-proposal grace**: After an upstream producer sends `CONSENSUS_PROPOSE`, the reviewer has a configurable grace period (`post_proposal_grace_seconds`, default 300s / 5 minutes) before heartbeat/progress stall checks apply. This covers the transition period where the reviewer is actively reading code, verifying claims, and preparing their review — showing tool call activity but no BRC messages yet.

Once both conditions expire (all producers have proposed AND the grace period has elapsed), normal heartbeat/progress monitoring resumes.

**Post-ACK confirmation timeout:** In the opposite direction, the health monitor also detects **producers stuck after being fully ACKed**. If all reviewers have ACKed a producer's proposal but the producer hasn't sent `CONSENSUS_CONFIRMED` within `orchestrator_post_ack_confirmation_timeout_seconds` (default 180s / 3 minutes), the health monitor sends a direct `OVERSEER_ALERT` to the stuck producer instructing it to call `mcp__brc__confirm`, and also escalates to overseer/HITL — regardless of whether the producer is still sending heartbeats. This catches the failure mode where a producer enters a tight heartbeat loop without ever confirming. The timeout is also gated on `check_confirm_guard`: if any peer producer in the review graph has `proposal_version == 0` (never proposed), the global zero-proposal guard ([#1648](https://github.com/jwbron/egg/issues/1648)) would reject the confirm call anyway, so the health monitor suppresses the alert rather than sending a spurious nudge ([#2187](https://github.com/jwbron/egg/issues/2187)). The 180s window starts only once the guard clears.

See [Pipeline Health Monitoring](pipeline-health-monitoring.md#post-propose-grace-period-for-reviewers) for implementation details and configuration.

## Agent Anchors (Post-Compaction Recovery)

In long-running concurrent sessions, agents may exhaust their context window. Rather than relying on lossy compaction, agents fully clear their context and reload from a structured **anchor file** that captures task progress, cross-agent decisions, BRC consensus state, and key context.

Each agent maintains an anchor at `.egg-state/agent-anchors/<agent-id>.json`. The `brc_state` section mirrors `PeerConsensusTracker` state, enabling agents to re-enter the BRC protocol at the correct point after a context clear.

```bash
# Update anchor after a BRC state change
egg-orch anchor update --status in_progress \
  --progress '{"state":"current","description":"Responding to NACK feedback"}'

# After context clear, recover and catch up
egg-orch anchor show
egg-orch message poll --since <last_message_id>
```

See [Anchor Recovery Guide](anchor-recovery.md) for the full recovery protocol.

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard wave-based execution
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and API details
- [Pipeline Health Monitoring](pipeline-health-monitoring.md) — Two-tier health monitoring and structured progress
- [Anchor Recovery Guide](anchor-recovery.md) — Agent post-compaction state recovery
- [BRC Memory Artifact](../architecture/brc-memory.md) — Per-role-per-pipeline distilled memory file written by `brc_ack`/`brc_nack` so a stateless event-pump handler can re-enter a review cycle with continuity ([#2908](https://github.com/jwbron/egg/issues/2908))
