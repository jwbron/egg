# Concurrent Execution Mode

Concurrent execution mode runs all implement-phase agents simultaneously — all sharing the pipeline branch — rather than sequentially in dependency-ordered waves. Agents communicate via the orchestrator message bus and signal readiness for phase completion via a consensus protocol.

This is distinct from the standard wave-based parallel execution (Tier 2), where agents run in dependency order but multiple independent agents execute in parallel within each wave.

## Enabling Concurrent Execution

Set `concurrent_execution: true` in the pipeline config when creating a pipeline:

```bash
egg-orch pipeline create --repo owner/name --issue 123 \
  --config '{"concurrent_execution": true}'
```

Relevant `PipelineConfig` fields:

| Field | Default | Description |
|-------|---------|-------------|
| `concurrent_execution` | `false` | Enable concurrent agent execution |
| `max_concurrent_agents` | `6` | Maximum agents per phase |
| `message_poll_hint_seconds` | `30` | Suggested polling interval for agents |
| `consensus_timeout_minutes` | `30` | Timeout before HITL escalation |
| `agent_idle_timeout_minutes` | `60` | Idle agent timeout before termination |

Agent containers also respect `EGG_CONSENSUS_WRAPPER_TIMEOUT` (default: `300` seconds) — see [Consensus Wrapper](#consensus-wrapper) below.

## Agent Startup Protocol

When concurrent execution starts for the implement phase, the `ConcurrentPhaseExecutor` (in `orchestrator/concurrent_executor.py`) spawns the following roles simultaneously using a `ThreadPoolExecutor`:

- `coder`
- `tester`
- `documenter`
- `checker`
- `reviewer_code`
- `reviewer_contract`

**Shared branch**: All agents operate on the pipeline's shared branch (e.g., `egg/issue-123`). Agents coordinate commits via the message bus to sequence their work and avoid conflicts.

**Environment injection**: Each concurrent agent receives:

| Variable | Value | Description |
|----------|-------|-------------|
| `EGG_CONCURRENT_MODE` | `"true"` | Signals to the agent that concurrent mode is active |
| `EGG_MESSAGE_POLL_INTERVAL` | `<seconds>` | Suggested polling interval for the message bus |

Each agent is registered in the consensus evaluator before spawning begins.

## Consensus Wrapper

All concurrent agent containers are wrapped with a shell safety net defined in `orchestrator/consensus_wrapper.py`. The wrapper runs after the Claude process exits and enforces lifecycle compliance for agents that exit before the orchestrator stops them.

**How it works:**

1. Claude runs inside the wrapper script.
2. When Claude exits cleanly (code 0), the wrapper auto-signals `READY` via `egg-orch signal readiness` (a no-op if the agent already signaled).
3. The wrapper then polls the consensus endpoint in a loop, sleeping `EGG_MESSAGE_POLL_INTERVAL` seconds between checks, until consensus is reached or `EGG_CONSENSUS_WRAPPER_TIMEOUT` expires.
4. When consensus is reached (`is_complete: true`), the wrapper exits with Claude's original exit code.
5. If Claude exits non-zero (crashed), the wrapper does **not** signal `READY` and exits immediately with the same code.

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `EGG_CONSENSUS_WRAPPER_TIMEOUT` | `300` | Seconds the wrapper polls for consensus before giving up |
| `EGG_MESSAGE_POLL_INTERVAL` | `30` | Seconds between wrapper poll iterations |

**Implicit READY on clean exit (orchestrator-side):**

In addition to the wrapper, the orchestrator's `_run_concurrent_phase()` monitors container exit codes. When a container exits with code 0 and has not yet signaled `READY`, the orchestrator auto-registers it as `READY` with the reason `"Container exited cleanly (implicit READY)"`. This ensures agents that complete their work and exit without explicitly signaling do not block consensus indefinitely.

**Agents should still follow the protocol.** The wrapper and implicit READY are safety nets for cases where Claude's exit is unavoidable (e.g., max turns reached, context exhausted). Well-behaved agents explicitly signal `READY` and enter a polling loop — this allows them to react to late-arriving messages before the orchestrator stops the container.

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

Phase completion requires all registered agents to be in the `READY` state. The `ConsensusEvaluator` (in `orchestrator/consensus.py`) tracks per-agent readiness per pipeline in a thread-safe in-memory store.

### Consensus Check

```
GET /api/v1/pipelines/{id}/consensus   // or via ConcurrentPhaseExecutor.check_consensus()
```

Returns:

```json
{
  "is_complete": false,
  "blocking_agents": ["tester", "documenter"],
  "has_objections": false,
  "agents": {
    "coder": {"role": "coder", "state": "READY", ...},
    "tester": {"role": "tester", "state": "WORKING", ...}
  }
}
```

Consensus is reached when `is_complete: true` — all registered agents are `READY`.

### Objections

If any agent is in the `OBJECTING` state, `has_objections: true` is returned. The orchestrator surfaces objections to the human for resolution before the phase can advance.

### Timeout Handling

If consensus is not reached within `consensus_timeout_minutes`, the orchestrator creates a HITL decision asking the human how to proceed (advance anyway, wait, or abort).

### Agent Failure During Consensus

When an agent fails after signaling `READY`, the `handle_agent_failure` method:
1. Removes the agent from consensus tracking via `evaluator.remove_agent()`
2. Treats the failure as a single-agent failure (see failure recovery below)

This prevents a failed agent's `READY` signal from erroneously completing the phase.

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
| Consensus timeout | Advance anyway, Wait longer, Abort |
| Agent objection | Resolve then advance, Override, Abort |

## Shared Pipeline Branch

All concurrent agents operate on the pipeline's shared branch (e.g., `egg/issue-{N}`). Rather than each agent having an isolated worktree branch, all agents commit directly to a single shared history. Agents coordinate via the message bus to sequence commits and avoid conflicts — for example, the coder signals `HANDOFF` when its changes are committed so downstream agents (tester, documenter) know it is safe to pull and build on top.

## Orchestrator API Reference

For the full message bus and signal API, see [Orchestrator Architecture: API Endpoints](../architecture/orchestrator.md#api-endpoints).

Concurrent-execution-specific endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/pipelines/{id}/messages` | Send a message to the bus |
| `GET` | `/api/v1/pipelines/{id}/messages` | Poll messages (with filters) |
| `GET` | `/api/v1/pipelines/{id}/messages/status` | Message bus statistics |
| `POST` | `/api/v1/pipelines/{id}/signal` | Readiness signal (state update) |

## Related Documentation

- [SDLC Pipeline Guide](sdlc-pipeline.md) — Standard wave-based execution
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and API details
- [Checkpoint Access](checkpoint-access.md) — Cross-agent checkpoint queries
