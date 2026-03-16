# Concurrent Execution Mode

Concurrent execution mode runs all agents for the current pipeline phase simultaneously — all sharing the pipeline branch — rather than sequentially in dependency-ordered waves. Agents communicate via the orchestrator message bus and signal readiness for phase completion via a consensus protocol. BRC consensus is active by default for the **refine**, **plan**, and **implement** phases.

This is distinct from the standard wave-based parallel execution (Tier 2), where agents run in dependency order but multiple independent agents execute in parallel within each wave.

## Configuring Concurrent Execution

BRC concurrent execution is **enabled by default** for the refine, plan, and implement phases via the `concurrent_phases` config field. No additional configuration is required for standard pipelines.

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
| `max_concurrent_agents` | `6` | Maximum agents per phase |
| `message_poll_hint_seconds` | `30` | Suggested polling interval for agents |
| `consensus_timeout_minutes` | `30` | Consensus timeout before escalation or auto-advance |
| `agent_idle_timeout_minutes` | `60` | Idle agent timeout before termination |

## Agent Startup Protocol

When concurrent execution starts, the `ConcurrentPhaseExecutor` (in `orchestrator/concurrent_executor.py`) queries `get_roles_for_phase(phase, include_reviewers=True)` (from `shared/egg_contracts/agent_roles.py`) to determine which roles to spawn simultaneously using a `ThreadPoolExecutor`. Roles are phase-dependent:

| Phase | Spawned roles |
|-------|--------------|
| `refine` | `refiner`, `reviewer_refine`, `reviewer_agent_design` |
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
3. If Claude exits cleanly (code 0), the wrapper restarts Claude with recovery instructions injected as the **system prompt** (not the user prompt). Using the system prompt prevents the Agent SDK from flagging the recovery context as prompt injection. The recovery system prompt explains that the agent was restarted, includes the current BRC state, and (for producers with unresolved NACKs) includes the NACK feedback so the agent knows exactly what to address before re-proposing. A short user prompt ("Continue the BRC consensus protocol…") accompanies it.
4. Restarts are capped at `MAX_CONSENSUS_RESTARTS` (default: 2). After each restart, the wrapper checks if consensus was reached. If so, it exits cleanly.
5. After exhausting all restarts, the wrapper exits with code 1, triggering the orchestrator's agent failure path (HITL decision with retry/abort/continue options).

**Key design principle:** Agents must **explicitly** participate in consensus. The wrapper never auto-signals `READY` on behalf of an agent — it restarts the agent so it can assess state and signal for itself.

**Configuration:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_restarts` | `2` | Maximum restart attempts (passed to `build_consensus_wrapped_command()`) |
| `EGG_MESSAGE_POLL_INTERVAL` | `30` | Seconds between message polls during restarts |

## Message Bus

Agents communicate with each other during concurrent execution via the orchestrator message bus (`orchestrator/message_store.py`). Messages are ephemeral in-memory per phase and are cleared at phase transition.

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

> **Note — `pending_acks` (exit code 2):** After a re-proposal, previously-confirmed reviewers are un-confirmed and must re-ACK. If the producer calls `confirmed` before those re-ACKs arrive, the command returns exit code **2** (`pending_acks`) — this is transient, not an error. The producer should poll for messages and retry `confirmed` until it exits 0.

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

The `concurrent.consensus` key is **only present** when a consensus tracker with registered agents is active. It is omitted entirely when no tracker or evaluator is available (e.g., phases that do not yet implement BRC, or after an orchestrator restart where the in-memory tracker is lost). Callers should check for the key's presence before using it rather than relying on an empty placeholder.

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

### Agent Failure During Consensus

When an agent crashes, `PeerConsensusTracker.handle_agent_crash()` assesses impact:
- Escalation occurs when a crashed reviewer was the **sole reviewer** for a producer, **or** when the reviewer had pending (non-ACKed) reviews for a producer that has already proposed. Both cases create a HITL decision. When the reviewer had pending reviews, the question lists the affected producers.
- When the human selects **"Continue without"** for a failed reviewer, `excuse_reviewer()` removes all of that reviewer's edges from the review graph. This allows affected producers to reach `is_fully_acked()` and call `confirmed` without the excused reviewer's ACK.
- Otherwise, the agent is removed from consensus tracking and treated as a single-agent failure (see failure recovery below).

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

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard wave-based execution
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and API details
- [Checkpoint Access](checkpoint-access.md) — Cross-agent checkpoint queries
