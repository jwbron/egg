# Agent Recovery Reference

This document describes the recovery mechanisms for multi-agent pipeline failures, including retry logic, circuit breaking, conflict detection, and resilience utilities.

Source: `shared/egg_contracts/agent_recovery.py`, `shared/egg_contracts/resilience.py`

## Retry Manager

`AgentRetryManager` tracks retry attempts per agent role and determines whether a failed agent should be retried based on error type and retry count.

### Configuration

```python
AgentRetryConfig(
    max_retries=2,
    initial_delay_seconds=30,
    max_delay_seconds=300,
    backoff_multiplier=2.0,
    retryable_errors=["timeout", "rate_limit", "transient", "network"],
)
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_retries` | 2 | Maximum retry attempts per role |
| `initial_delay_seconds` | 30 | First retry delay |
| `max_delay_seconds` | 300 | Maximum delay cap |
| `backoff_multiplier` | 2.0 | Exponential backoff multiplier |
| `retryable_errors` | `["timeout", "rate_limit", "transient", "network"]` | Substrings matched (case-insensitive) against the error message |

### Retry Decision Logic

When an agent fails, `should_retry(role, error)` returns a `RetryDecision`:

1. If `retry_count >= max_retries`: return `no_retry` (MANUAL policy)
2. If the error message does not contain any `retryable_errors` substring: return `no_retry`
3. Otherwise: return `retry_with_backoff` with delay `min(initial_delay * backoff_multiplier^retry_count, max_delay_seconds)`

### Retry Policies

| Policy | Meaning |
|--------|---------|
| `IMMEDIATE` | Retry without delay |
| `BACKOFF` | Retry after exponential backoff delay |
| `MANUAL` | Requires human intervention (no automatic retry) |
| `SKIP` | Skip this agent and continue pipeline |

### State Tracking

The retry manager tracks per-role:
- Attempt count (reset on success)
- Last failure time
- Error history

`record_success(role)` resets the attempt counter for that role.

## Circuit Breaker

`AgentCircuitBreaker` prevents wasted compute when multiple agents fail repeatedly. It follows the standard three-state circuit breaker pattern.

### States

```
CLOSED → (failures >= threshold) → OPEN → (reset_timeout elapsed) → HALF_OPEN
HALF_OPEN → (success_threshold met) → CLOSED
HALF_OPEN → (any failure) → OPEN
```

| State | Meaning | Operations allowed |
|-------|---------|-------------------|
| `CLOSED` | Normal operation | Yes |
| `OPEN` | Blocking due to failures | No |
| `HALF_OPEN` | Testing recovery | Yes (limited) |

### Configuration

```python
CircuitBreakerConfig(
    failure_threshold=3,        # Failures before opening
    reset_timeout_seconds=300,  # Time before half-open (5 minutes)
    success_threshold=2,        # Successes needed to close from half-open
)
```

### Transition Details

- **CLOSED → OPEN**: When `failure_count >= failure_threshold`
- **OPEN → HALF_OPEN**: After `reset_timeout_seconds` have elapsed (checked lazily on `is_open()` / `can_execute()` calls, not via a timer thread)
- **HALF_OPEN → CLOSED**: When `success_count >= success_threshold`
- **HALF_OPEN → OPEN**: On any failure

The `is_open()` method returns `True` only in the `OPEN` state. `can_execute()` returns `True` when in `CLOSED` or `HALF_OPEN` states.

## Conflict Detection

`ConflictDetector` identifies file-level conflicts between parallel agents.

### Methods

**`check_for_merge_conflicts()`**: Runs `git diff --name-only --diff-filter=U` to detect unmerged paths in the repository. Returns `ConflictInfo` entries with `conflict_type="merge"`.

**`check_file_overlap(agent1_files, agent1_role, agent2_files, agent2_role)`**: Compares two sets of modified files. Returns `ConflictInfo` entries with `conflict_type="edit"` for any overlap.

**`detect_conflicts_from_outputs(agent_outputs)`**: Takes a dict of `{role: output}` where each output has a `changed_files` list. Checks all role pairs for overlapping files.

### ConflictInfo Fields

| Field | Description |
|-------|-------------|
| `conflicting_files` | List of file paths in conflict |
| `agents_involved` | Agent roles involved |
| `conflict_type` | `"merge"`, `"edit"`, or `"delete"` |
| `resolution_hint` | Human-readable guidance |
| `detected_at` | Timestamp |

## Consensus Wrapper: Transient Crash Recovery

Source: `orchestrator/consensus_wrapper.py`

In concurrent (BRC) mode, all agents are wrapped with a shell script that handles exit conditions. The wrapper distinguishes **transient runtime crashes** from application-level failures and restarts agents that crash due to infrastructure issues. The wrapper also detects stale consensus tracker state — where the tracker shows an agent as not confirmed despite a `CONSENSUS_CONFIRMED` message existing in the message bus — and falls back to the message bus to avoid false failures after withdrawal/re-proposal cascades.

### Transient Exit Codes

The `is_transient_crash()` function classifies these exit codes as transient:

| Exit Code | Signal | Cause |
|-----------|--------|-------|
| 134 | SIGABRT | Assertion failure, `abort()` |
| 136 | SIGFPE | Floating-point exception |
| 137 | SIGKILL | OOM kill or external `kill -9` |
| 139 | SIGSEGV | Segmentation fault |
| 255 | *(Bun-specific)* | Bun runtime segfault (wraps the crash as exit 255) |

All other non-zero exit codes (e.g., exit 1) are treated as non-transient and cause immediate failure without restart.

### Restart with Backoff

When a transient crash is detected, the wrapper:

1. Logs `"Transient crash (code $AGENT_EXIT). Will restart with backoff."`
2. Sleeps for `CRASH_BACKOFF` seconds (initial: `TRANSIENT_RESTART_BACKOFF_INITIAL`, default 5)
3. Restarts the agent via the same recovery loop used for clean-exit restarts (with BRC state, NACK feedback, and anchor state injected into the recovery system prompt)
4. Doubles the backoff after each crash restart (capped at 30 seconds)
5. Shares the `MAX_CONSENSUS_RESTARTS` (default: 2) cap with clean-exit restarts

Clean-exit restarts (exit code 0) do not incur any backoff delay.

### Before and After

| Behavior | Before | After |
|----------|--------|-------|
| Segfault (exit 139/255) | `NOT restarting` — agent permanently dead | Classified as transient, restarted with backoff |
| OOM kill (exit 137) | `NOT restarting` — agent permanently dead | Classified as transient, restarted with backoff |
| Application error (exit 1) | `NOT restarting` — immediate failure | Unchanged — still exits immediately |
| Pipeline impact | Single crash kills pipeline | Transient crashes recovered; pipeline continues |

This addresses the scenario described in [issue #1512](https://github.com/jwbron/egg/issues/1512), where a Bun segfault permanently killed an agent and caused the entire pipeline to fail even though 4 of 5 agents were healthy.

## Agent-Level Restart

Source: `orchestrator/container_spawner.py`, `orchestrator/routes/pipelines.py`

When an agent becomes unresponsive (e.g., hung after a tool error, context window exhaustion, dropped API connection), an **agent-level restart** stops the stuck container and respawns a replacement — without affecting other agents in the same phase.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, or `FAILED` state. If the pipeline is in `FAILED` state, the restart automatically resets the pipeline and phase status to `RUNNING`.

### How It Works

1. The orchestrator stops the existing container via `stop_agent_container()` and removes it via `remove_agent_container()`
2. The agent's consensus state is reset — `PeerConsensusTracker.remove_agent()` and `ConsensusEvaluator.remove_agent()` withdraw any proposals, ACKs, or confirmations
3. A new container is spawned via `spawn_agent_container()` with the **same role, phase, and environment** — reusing the existing worktree rather than creating a new one
4. Recovery context is injected into the respawned agent (e.g., "You are being restarted after a stall. Resume from where your predecessor left off.")
5. The pipeline's `PhaseExecution` state is updated with the new container/agent entries
6. Restart count is tracked per agent per phase — configurable maximum (default: 2) prevents infinite restart loops

### Triggering a Restart

| Method | How |
|--------|-----|
| **CLI** | `egg-orch agent restart <role> [--reason "..."]` |
| **API** | `POST /api/v1/pipelines/{id}/agents/{role}/restart` with optional `{"reason": "..."}` body |
| **MCP tool** | `restart_agent(task_id, agent_role, reason)` via the orchestrator MCP server |
| **Overseer** | Automatic — after consecutive heartbeat failures or unresponsive nudges (see below) |

### Worktree Preservation

Agent restart preserves the agent's git worktree, including any committed work on the branch. The respawned agent starts with the full commit history intact. This is critical for agents (like coders) that may have pushed partial work before becoming stuck.

## Phase-Level Restart

Source: `orchestrator/routes/pipelines.py`

When agent-level restarts are insufficient (e.g., multiple agents stuck, consensus state corrupted, or the phase needs a fresh start), a **phase-level restart** kills all containers for the phase and respawns all agents from scratch.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, or `FAILED` state. If the pipeline is in `FAILED` state, the restart automatically resets both the pipeline and phase status to `RUNNING`.

### How It Works

1. All running containers for the specified phase are stopped and removed
2. `PeerConsensusTracker.clear()` resets all consensus state (proposals, ACKs, NACKs, confirmations)
3. The phase's review cycle counter in `PhaseExecution` is reset
4. All prior phase artifacts and HITL decisions are preserved (e.g., refine output carries into a restarted plan phase)
5. All commits on the branch are preserved
6. All agents for the phase are respawned from scratch

### Triggering a Phase Restart

| Method | How |
|--------|-----|
| **CLI** | `egg-orch phase restart <phase> [--reason "..."] [--context "..."]` |
| **API** | `POST /api/v1/pipelines/{id}/phases/{phase}/restart` with optional `{"reason": "...", "context": "..."}` body |
| **MCP tool** | `restart_phase(task_id, phase, reason, context)` via the orchestrator MCP server |
| **Overseer** | Via HITL decision — phase restarts require human approval by default |

The optional `context` parameter injects additional guidance into the respawned agents (e.g., "Previous attempt stalled during BRC convergence — focus on completing reviews first").

## When Recovery Is Triggered vs. HITL Escalation

| Scenario | Behavior |
|----------|----------|
| Agent fails, error is retryable, retries remain | `AgentRetryManager`: retry with backoff |
| Agent fails, error not retryable | Escalate to HITL (MANUAL policy) |
| Agent fails, max retries exceeded | Escalate to HITL |
| Transient crash in consensus wrapper (segfault, OOM) | Restart with exponential backoff (shares `MAX_CONSENSUS_RESTARTS` cap) |
| Non-transient crash in consensus wrapper (exit 1) | Immediate failure, no restart |
| Wrapper: tracker stale after withdrawal cascade | Message bus fallback detects `CONSENSUS_CONFIRMED`; agent exits cleanly |
| Overseer detects agent stall (N heartbeat failures) | `RESTART_AGENT` action — auto-restart via API (up to max restarts per phase) |
| Overseer detects agent stall (restarts exhausted) | Escalate to HITL |
| Overseer detects multiple stuck agents (2+) | `RESTART_PHASE` action — creates HITL decision for phase restart approval |
| Manual agent restart (CLI/MCP/API) on running pipeline | Stop container, reset consensus, respawn with same config |
| Manual agent restart (CLI/MCP/API) on failed pipeline | Same as above, plus reset pipeline + phase status to `RUNNING` |
| Manual phase restart (CLI/MPC/API) on running pipeline | Stop all containers, reset all consensus + review cycles, respawn all agents |
| Manual phase restart (CLI/MCP/API) on failed pipeline | Same as above, plus reset pipeline status to `RUNNING` |
| Single agent failure in concurrent mode | HITL decision: retry / abort / continue without |
| Multiple failures (2+ within 60s) in concurrent mode | Immediate phase abort + HITL decision |
| Circuit breaker OPEN | Block new agent spawns; alert operators |
| Agent failure during consensus | Remove from consensus; treat as single failure |
| All containers exit non-zero, consensus actually complete | Final `check_consensus()` recheck recovers pipeline; returns success |
| All agents exit with unresolved NACKs (concurrent mode) | Phase returns failure + HITL decision: retry phase / accept current state / abort phase |

HITL escalation creates a decision in the pipeline's decision queue. Options presented to the human depend on the failure type (see [Concurrent Execution Guide](../guides/concurrent-execution.md) for concurrent-mode options).

## Resilience Utilities

### Rate Limit Handler

`RateLimitHandler` parses standard HTTP rate limit headers and manages wait/retry logic for external API calls.

Supported header formats:
- GitHub: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `X-RateLimit-Used`
- Generic: `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`
- `Retry-After` (takes precedence for reset time on 429 responses)

**`wait_until_reset(max_wait_seconds=300)`**: Sleeps until the rate limit resets, up to the specified maximum. Returns `True` if it waited, `False` if no wait was needed.

### Retry with Backoff

`retry_with_backoff` is a decorator for retrying functions that raise `RetryableError` (or configurable exception types).

```python
@retry_with_backoff(config=RetryConfig(max_retries=3, initial_delay_seconds=1.0))
def call_external_api():
    ...
```

`RetryConfig` fields:

| Field | Default | Description |
|-------|---------|-------------|
| `max_retries` | 3 | Maximum retry attempts |
| `initial_delay_seconds` | 1.0 | Initial delay |
| `max_delay_seconds` | 30.0 | Maximum delay |
| `exponential_base` | 2.0 | Backoff multiplier |
| `jitter` | `True` | Add ±25% randomness to prevent thundering herd |

Delays are calculated as `initial_delay * base^attempt`, capped at `max_delay_seconds`. With jitter enabled, the actual delay is multiplied by a random factor in `[0.75, 1.25]`.

`RetryableError` can carry a `retry_after` value (in seconds) that overrides the calculated backoff delay.

### Timeout Checkpoint

`TimeoutCheckpoint` monitors job execution time and signals when a checkpoint should be created before the job times out. Designed for GitHub Actions (6-hour default timeout).

```python
checkpoint = TimeoutCheckpoint(
    timeout_minutes=360,            # 6 hours
    checkpoint_margin_minutes=10,   # Create checkpoint 10 min before deadline
)

if checkpoint.should_checkpoint:
    state = checkpoint.create_checkpoint(data={"step": "current"})
```

`create_checkpoint(data)` returns a `CheckpointState` with:
- `timestamp`, `job_start_time`
- `elapsed_seconds`, `remaining_seconds`
- `data` — caller-supplied state dict

This is a state-save trigger utility, not the agent checkpoint system (which stores transcripts and tool calls in the `egg/checkpoints/v2` git branch).

## Related Documentation

- [Concurrent Execution Guide](../guides/concurrent-execution.md) — Failure handling in concurrent mode
- [Orchestrator Architecture](../architecture/orchestrator.md) — Container monitoring and startup reconciliation
- [Post-Agent Commit Reference](post-agent-commit.md) — Work preservation on container exit
