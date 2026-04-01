# Concurrent Execution Mode

Concurrent execution mode runs all agents for the current pipeline phase simultaneously — all sharing the pipeline branch — rather than sequentially in dependency-ordered waves. Agents communicate via the orchestrator message bus and signal readiness for phase completion via a consensus protocol. BRC consensus is active by default for the **refine**, **plan**, and **implement** phases. Additional phases (such as `review`) can be added via the `concurrent_phases` config.

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
| `consensus_timeout_minutes` | `30` | Consensus timeout before escalation or auto-advance |
| `agent_idle_timeout_minutes` | `60` | Idle agent timeout before termination |

## Agent Startup Protocol

When concurrent execution starts, the `ConcurrentPhaseExecutor` (in `orchestrator/concurrent_executor.py`) queries `get_roles_for_phase(phase, include_reviewers=True)` (from `shared/egg_contracts/agent_roles.py`) to determine which roles to spawn simultaneously using a `ThreadPoolExecutor`. Roles are phase-dependent:

| Phase | Spawned roles |
|-------|--------------|
| `refine` | `refiner`, `reviewer_refine`, `reviewer_agent_design` (egg repo only) |
| `plan` | `architect`, `task_planner`, `risk_analyst`, `reviewer_plan` |
| `implement` | `coder`, `tester`, `documenter`, `reviewer_code`, `reviewer_contract` |

**Shared branch**: All agents operate on the pipeline's shared branch (e.g., `egg/issue-123`). Agents coordinate commits via the message bus to sequence their work and avoid conflicts.

**Environment injection**: Each concurrent agent receives:

| Variable | Value | Description |
|----------|-------|-------------|
| `EGG_CONCURRENT_MODE` | `"true"` | Signals to the agent that concurrent mode is active |
| `EGG_MESSAGE_POLL_INTERVAL` | `<seconds>` | Suggested polling interval for the message bus |
| `EGG_BRC_ROLE_TYPE` | `"producer"`, `"reviewer"`, or `"producer,reviewer"` | Agent's role in the BRC review graph |
| `EGG_BRC_REVIEWERS` | Comma-separated roles | Reviewer roles assigned to this producer (producers only) |
| `EGG_BRC_PRODUCERS` | Comma-separated roles | Producer roles this agent must review (reviewers only) |

Each agent is registered in the peer consensus tracker before spawning begins.

## Consensus Wrapper

All concurrent agent containers are wrapped with a shell script defined in `orchestrator/consensus_wrapper.py`. The wrapper detects when Claude exits without the orchestrator confirming consensus and restarts the agent with recovery instructions instead of silently marking it as ready.

**How it works:**

1. Claude runs inside the wrapper script with the original task prompt.
2. If Claude exits non-zero (crashed), the wrapper exits immediately with the same code — no restart.
3. If Claude exits cleanly (code 0), the wrapper checks whether this agent is already confirmed before restarting. It queries the pipeline status endpoint; if the consensus tracker state is empty (e.g., because the orchestrator restarted and the in-memory tracker was not yet reconstructed), the wrapper falls back to checking the message bus directly for a prior `CONSENSUS_CONFIRMED` message from this agent's role. If found, the agent is treated as already confirmed and enters the wait-for-consensus poll loop — no restart needed.
4. If not already confirmed, the wrapper restarts Claude with recovery instructions injected as the **system prompt** (not the user prompt). Using the system prompt prevents the Agent SDK from flagging the recovery context as prompt injection. The recovery system prompt explains that the agent was restarted, includes the current BRC state, and (for producers with unresolved NACKs) includes the NACK feedback so the agent knows exactly what to address before re-proposing. A short user prompt ("Continue the BRC consensus protocol…") accompanies it.
5. Restarts are capped at `MAX_CONSENSUS_RESTARTS` (default: 2). After each restart, the wrapper checks if global consensus was reached (exit cleanly) or if this agent individually reached `CONFIRMED` state (enter the wait-for-consensus poll loop). This prevents a confirmed agent from consuming a restart slot while waiting for peers to finish.
6. After exhausting all restarts, the wrapper exits with code 1, triggering the orchestrator's agent failure path (HITL decision with retry/abort/continue options).

**Key design principle:** Agents must **explicitly** participate in consensus. The wrapper never auto-signals `READY` on behalf of an agent — it restarts the agent so it can assess state and signal for itself.

**Design intent — safety net, not primary mechanism:** The wrapper exists as a fallback for the edge case where an agent exits prematurely (e.g., context exhaustion). The intended lifecycle is for agents to run with enough turns to finish their work *and* complete the full BRC consensus protocol (including stay-alive polling while peers finish). The orchestrator detects consensus and sends SIGTERM to terminate containers — agents should exit because they are told to, not because they exhaust turns. The restart path is expensive (requires reloading context and re-evaluating BRC state) and should be rare.

**Configuration:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_turns` | `1000` | Maximum tool-call turns per agent run (set high so agents can complete work and stay alive for the full BRC lifecycle) |
| `max_restarts` | `2` | Maximum restart attempts (passed to `build_consensus_wrapped_command()`) |
| `max_ready_polls` | `10` | Maximum poll cycles (each ~30 s) to wait for global consensus when this agent has already reached `CONFIRMED` |
| `EGG_MESSAGE_POLL_INTERVAL` | `30` | Seconds between message polls during restarts |

## Message Bus

Agents communicate with each other during concurrent execution via the orchestrator message bus (`orchestrator/message_store.py`). In production, messages are stored in Redis Streams, surviving orchestrator restarts. Messages are cleared at phase transition. In test environments, an in-memory fallback is used when Redis is not available.

### Sending Messages

```
POST /api/v1/pipelines/{id}/messages
```

Request body:

```json
{
  "from_role": "coder",
  "to_role": "tester",          // or "all" for broadcast
  "message_type": "PROGRESS",   // PROGRESS, QUESTION, STATUS, AGENT_FAILED, HANDOFF
  "subject": "Implemented auth module",
  "body": "auth.py is complete, tests can begin",
  "metadata": {}
}
```

The pipeline's current phase is automatically attached to each message.

### Polling Messages

```
GET /api/v1/pipelines/{id}/messages?role=tester&since_id=<id>&limit=100
```

Query parameters:

| Parameter | Description |
|-----------|-------------|
| `role` | Return messages targeted to this role or broadcast to `"all"` |
| `since_id` | Return only messages after this message ID (for incremental polling) |
| `limit` | Maximum messages to return (default: 100) |

Messages are returned oldest-first. The `since_id` filter excludes the reference message itself — only messages that follow it are returned.

### Message Bus Status

```
GET /api/v1/pipelines/{id}/messages/status
```

Returns total message count and a breakdown by message type.

### Message Types

| Type | Purpose |
|------|---------|
| `PROGRESS` | Agent progress updates for other agents |
| `QUESTION` | Agent asking another agent a question |
| `STATUS` | General status announcements |
| `AGENT_FAILED` | Orchestrator notifying agents of a peer failure |
| `HANDOFF` | Agent signaling completion of a handoff artifact |
| `CONSENSUS_PROPOSE` | Producer broadcasting its proposal for review |
| `CONSENSUS_ACK` | Reviewer approving a producer's proposal |
| `CONSENSUS_NACK` | Reviewer rejecting a producer's proposal (with reason) |
| `CONSENSUS_WITHDRAW` | Producer withdrawing its proposal (e.g., to address NACK) |
| `CONSENSUS_CONFIRMED` | Agent confirmed after all required reviews are ACKed |
| `CONSENSUS_RE_REVIEW` | Orchestrator notifying a reviewer that their prior confirmation is stale and they must re-review the producer's new proposal version |

### Message Store Backend

The message store uses Redis Streams when Redis is available, falling back to an in-memory store for tests or unconfigured environments. The backend is selected via the `EGG_MESSAGE_STORE_BACKEND` environment variable (`"auto"` by default, `"redis"` to require Redis, `"memory"` to force in-memory).

**Note:** Long-poll (`?wait=<s>`) only blocks with the Redis Streams backend. The in-memory store silently falls back to a non-blocking poll, so agents in test environments may see immediate empty responses instead of blocking.

### Per-Phase Cleanup

The message store is cleared when the phase transitions. Each new phase execution starts with an empty message bus for the pipeline. This prevents stale messages from a prior phase from being delivered to agents in the next phase.

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

1. **Propose**: Producer completes work and sends `CONSENSUS_PROPOSE` signal with a summary and artifact list.
2. **Review**: Assigned reviewers evaluate the proposal and send `CONSENSUS_ACK` or `CONSENSUS_NACK`.
3. **Converge**: When all critical reviewers ACK, the producer sends `CONSENSUS_CONFIRMED`. When all agents are confirmed, the phase advances.
4. **Re-propose**: If a NACK is received, the producer addresses the feedback and re-proposes (with `changed_artifacts` to scope re-evaluation). Flip-flop cycles are capped at `max_flip_flops` (default: 3). If any reviewer had already confirmed on a prior proposal version, they automatically receive a `CONSENSUS_RE_REVIEW` message and are un-confirmed so they re-enter the review loop — preventing a deadlock where a stale-confirmed reviewer can never see the new proposal.

   > **Note — `CONSENSUS_RE_REVIEW` handling:** Agents that receive a `CONSENSUS_RE_REVIEW` while staying alive **must** act on it immediately: reviewers of the re-proposing producer must re-review and ACK/NACK; all other agents must re-confirm via `egg-orch consensus confirmed`. Ignoring this message stalls the pipeline.

> **Note — `pending_acks` (exit code 2):** After a re-proposal, previously-confirmed reviewers are un-confirmed and must re-ACK. If the producer calls `confirmed` before those re-ACKs arrive, the command returns exit code **2** (`pending_acks`) — this is transient, not an error. The producer should poll for messages and retry `confirmed` until it exits 0.
>
> **Note — Reviewer `pending_acks`:** Reviewers can also receive exit code 2 from `confirmed` when they have stale ACKs (e.g., an ACK recorded before the producer proposed). The reviewer must re-ACK the listed producers at their current proposal version before confirming.

### Pre-Proposal ACK Protection

When agents work at different speeds, a faster reviewer may ACK a producer before the producer has submitted its proposal. The BRC protocol handles this automatically:

1. **On propose**: When a producer submits `CONSENSUS_PROPOSE`, any pre-existing version-0 ACKs (recorded before the first proposal) are invalidated. Affected reviewers appear in the `stale_reviewers` list in the proposal response and receive a `CONSENSUS_RE_REVIEW` notification to re-review.

2. **On confirm**: A version-match guard prevents reviewers from confirming with stale ACKs. If a reviewer's ACK version does not match the producer's current proposal version, `CONSENSUS_CONFIRMED` returns `pending_acks` (exit code 2) with a message listing which producers need re-ACKing.

These protections prevent a deadlock that previously occurred when a reviewer's stale version-0 ACK could never satisfy `is_fully_acked()`, permanently blocking the producer from confirming.

Use `egg-orch consensus` commands to participate in the BRC protocol:

```bash
# Producer: propose work for review
egg-orch consensus propose --summary "Implemented feature X" --artifacts src/feature.py --risk "No retry on transient failures"

# Reviewer: ACK after reviewing
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py

# Reviewer: NACK with a reason
egg-orch consensus nack coder --reason "Missing error handling in edge case" --files-reviewed src/feature.py

# Producer: withdraw proposal to address NACK feedback
egg-orch consensus withdraw --reason "Addressing NACK: adding error handling"

# Producer: confirm after all reviewers ACK
# Exit 0 = confirmed. Exit 1 = error. Exit 2 = waiting for reviewer re-ACKs (retry after polling).
egg-orch consensus confirmed

# Check overall consensus status
egg-orch consensus status
```

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

If consensus is not reached within `consensus_timeout_minutes`, the BRC tracker (`PeerConsensusTracker.handle_timeout()`) evaluates blocking agents by role criticality:

- **Critical blockers** (required reviewers still unconfirmed): emits `CONSENSUS_FAILURE` and creates a HITL decision asking how to proceed.
- **Advisory-only blockers** (non-critical roles unconfirmed): emits `CONSENSUS_TIMEOUT` and proceeds automatically — no HITL created.
- **No blockers**: proceeds immediately with no HITL.

After the timeout check, if the approval matrix still has unresolved NACKs (producers that exited without addressing reviewer feedback), the phase returns failure regardless of which agents are confirmed.

If the BRC tracker is unavailable, the orchestrator falls back to the old behavior and creates a generic HITL decision for any timeout.

Timeout handling is idempotent — if the timeout fires multiple times (e.g., due to a race with the overseer), only the first invocation takes effect.

### Consensus Stall Recovery

A separate scenario from timeout: all agents have confirmed (consensus is complete) but the phase execution has not advanced — for example, because the orchestrator's polling loop missed the completion event. The `ConsensusStallCheck` (Tier 1 health check) detects this on each `RUNTIME_TICK` (and `ON_DEMAND`) after a 60-second grace period.

When a stall is detected, `ContainerMonitor` drives a two-track recovery:

1. **Tracker reconstruction**: Attempts to rebuild the in-memory consensus tracker from message history so the polling loop can pick up completed consensus naturally.
2. **Aggressive recovery**: If reconstruction fails, marks all running agents and the phase as `COMPLETE` directly, using optimistic locking to avoid conflicts with concurrent state writers.

Startup reconciliation also handles this: when tracker reconstruction succeeds on orchestrator restart and `evaluate()` reports `is_complete: true`, agents and the phase are marked `COMPLETE` before normal pipeline polling resumes.

A complementary check, `IncompleteConsensusStallCheck`, handles the inverse scenario: consensus is **not yet complete** and the same blocking agents are not progressing (e.g., stuck in a heartbeat loop after a re-review cycle). After a 5-minute grace period, if the blocking set is unchanged for 10 consecutive `RUNTIME_TICK` events, the check reports `DEGRADED`. The overseer then sends targeted nudges and escalates to HITL if unresolved. See [Pipeline Health Monitoring](pipeline-health-monitoring.md#incomplete-consensus-stall-detection) for details.

### Agent Failure During Consensus

When an agent crashes, `PeerConsensusTracker.handle_agent_crash()` assesses impact:
- Escalation occurs when a crashed reviewer was the **sole reviewer** for a producer, **or** when the reviewer had pending (non-ACKed) reviews for a producer that has already proposed. Both cases create a HITL decision. When the reviewer had pending reviews, the question lists the affected producers.
- When the human selects **"Continue without"** for a failed reviewer, `excuse_reviewer()` removes all of that reviewer's edges from the review graph. This allows affected producers to reach `is_fully_acked()` and call `confirmed` without the excused reviewer's ACK.
- Otherwise, the agent is removed from consensus tracking and treated as a single-agent failure (see failure recovery below).

**Stall demotion for dual-role agents**: If a dual-role agent (e.g., `tester`) misses heartbeats for 5+ minutes without crashing, the orchestrator automatically demotes its reviewer edges from CRITICAL to ADVISORY via `PeerConsensusTracker.handle_stall_demotion()`. This allows producers that the stalled agent was assigned to review to reach `is_fully_acked()` and call `confirmed` without waiting for that agent's ACK. The demotion is permanent for the current phase and emits a `CONSENSUS_FAILURE` event with type `stall_demotion`. Unlike a crash (which triggers a HITL decision), stall demotion is fully automatic.

### SIGTERM Handling During Phase Transitions

When a phase completes and the orchestrator stops agent containers, agents receive SIGTERM and exit with code 143. The container monitor's reconciliation loop distinguishes these expected exits from genuine failures:

- **Exit code 143 when phase status is no longer `RUNNING`** (i.e., the phase has already transitioned): Skipped during reconciliation — no `FAILED` status is set, no HITL escalation is triggered.
- **Exit code 143 during an actively `RUNNING` phase**: Treated as a genuine failure and reconciled normally, since the phase has not yet completed.

This scoping ensures that the container polling loop (`_check_container`) still emits a `FAILED` event for exit code 143 — preserving the signal for the agent-loop guard that checks exit codes — while the reconciliation layer suppresses the false failure when phase context confirms the exit was expected. This prevents noisy `[ERROR] Agent failed` log entries and false HITL escalations on successful phase transitions.

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

### HITL Escalation Paths

| Scenario | HITL Options |
|----------|-------------|
| Single agent failure | Retry (respawn), Abort phase, Continue without |
| Multiple failures (2+ / 60s) | Retry phase, Cancel pipeline |
| Consensus timeout (critical blockers) | Continue waiting, Accept current state, Abort phase |
| Consensus timeout (advisory only) | *(no HITL — proceeds automatically)* |
| Agent objection | Resolve then advance, Override, Abort |
| All agents exited with unresolved NACKs | Retry phase, Accept current state, Abort phase |

## Shared Pipeline Branch

All concurrent agents operate on the pipeline's shared branch (e.g., `egg/issue-{N}`). Rather than each agent having an isolated worktree branch, all agents commit directly to a single shared history. Agents coordinate via the message bus to sequence commits and avoid conflicts — for example, the coder signals `HANDOFF` when its changes are committed so downstream agents (tester, documenter) know it is safe to pull and build on top.

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
- [Checkpoint Access](checkpoint-access.md) — Cross-agent checkpoint queries
- [Pipeline Health Monitoring](pipeline-health-monitoring.md) — Two-tier health monitoring and structured progress
- [Anchor Recovery Guide](anchor-recovery.md) — Agent post-compaction state recovery
