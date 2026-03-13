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

## When Recovery Is Triggered vs. HITL Escalation

| Scenario | Behavior |
|----------|----------|
| Agent fails, error is retryable, retries remain | `AgentRetryManager`: retry with backoff |
| Agent fails, error not retryable | Escalate to HITL (MANUAL policy) |
| Agent fails, max retries exceeded | Escalate to HITL |
| Single agent failure in concurrent mode | HITL decision: retry / abort / continue without |
| Multiple failures (2+ within 60s) in concurrent mode | Immediate phase abort + HITL decision |
| Circuit breaker OPEN | Block new agent spawns; alert operators |
| Agent failure during consensus | Remove from consensus; treat as single failure |

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
