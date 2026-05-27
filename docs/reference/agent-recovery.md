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

### Buffer Overflow Detection

The `is_buffer_overflow()` function is checked **before** `is_transient_crash()`. It greps the captured agent output log for the Claude Agent SDK's `CLIJSONDecodeError` marker (`"exceeded maximum buffer size"`) and, when found, exits the wrapper immediately without consuming any restart budget.

The SDK has a 1 MB JSON message-reader buffer cap; a tool result that exceeds it kills the agent with exit 255. This failure is **deterministic** — re-running the agent against the same codebase produces the same oversized payload and hits the same crash. Retrying is therefore wasteful. The wrapper logs:

```
Agent crashed on Claude Agent SDK buffer overflow (issue #2804). Deterministic failure; retry budget would be wasted. NOT restarting.
```

The agent output is captured by piping stdout and stderr through `tee` into a temporary log file (`AGENT_OUTPUT_LOG`, created via `mktemp`). This log is truncated at the start of each agent run so old crash signatures don't bleed into subsequent runs.

> **Note:** The buffer-overflow marker string is synchronized between the wrapper's `grep` and the `_BUFFER_OVERFLOW_MARKER` constant in `shared/egg_agent/client.py`. If a future `claude-agent-sdk` release changes the wording, the wrapper silently falls back to burning the transient-crash retry budget. See [#2823](https://github.com/jwbron/egg/issues/2823) for the follow-up to pin this against the installed SDK. The real fix is tool-layer truncation of oversized payloads ([#2805](https://github.com/jwbron/egg/issues/2805)); this is the fail-fast path until that lands.

### Transient Exit Codes

The `is_transient_crash()` function classifies these exit codes as transient:

| Exit Code | Signal | Cause |
|-----------|--------|-------|
| 134 | SIGABRT | Assertion failure, `abort()` |
| 136 | SIGFPE | Floating-point exception |
| 137 | SIGKILL | OOM kill or external `kill -9` |
| 139 | SIGSEGV | Segmentation fault |
| 255 | *(Bun-specific)* | Bun runtime segfault (wraps the crash as exit 255) |

### Startup Failure Detection

The `is_startup_failure()` function handles a separate class of transient error: exit code 1 within the first `STARTUP_FAILURE_WINDOW_SECONDS` (default: 30 seconds) of agent lifetime. The Agent SDK surfaces API-level errors — network blips, socket closes, 5xx responses during the first few turns — as `success=False` + exit 1, which is indistinguishable from a prompt-level failure by exit code alone. Agents that exit 1 within the startup window have almost certainly not done meaningful work, so the retry cost is negligible.

Agents that exit 1 after the startup window (i.e., after doing real work) are still treated as permanent failures. The window is configurable via the `startup_failure_window_seconds` parameter; set to `0` to disable the heuristic.

All other non-zero exit codes (2, 3, 42, etc.) that are neither signal-transient nor exit 1 are treated as permanent failures with no restart.

### Restart with Backoff

When a transient crash or startup failure is detected, the wrapper:

1. Logs `"Transient crash (code $AGENT_EXIT). Will restart with backoff."`
2. Sleeps for `CRASH_BACKOFF` seconds (initial: `TRANSIENT_RESTART_BACKOFF_INITIAL`, default 5)
3. Restarts the agent via the same recovery loop used for clean-exit restarts (with BRC state, NACK feedback, and anchor state injected into the recovery system prompt)
4. Doubles the backoff after each crash restart (capped at 30 seconds)
5. Shares the `MAX_CONSENSUS_RESTARTS` (default: 3) cap with clean-exit restarts

Clean-exit restarts (exit code 0) do not incur any backoff delay.

### Before and After

| Behavior | Before | After |
|----------|--------|-------|
| Segfault (exit 139/255) | `NOT restarting` — agent permanently dead | Classified as transient, restarted with backoff |
| SDK buffer overflow (exit 255 + overflow marker) | Classified as transient, burns restart budget | Detected by `is_buffer_overflow()`, exits immediately without retrying |
| OOM kill (exit 137) | `NOT restarting` — agent permanently dead | Classified as transient, restarted with backoff |
| API/network error at startup (exit 1, age &lt; 30s) | `NOT restarting` — immediate failure | Classified as startup failure, restarted with backoff |
| Application error (exit 1, age &ge; 30s) | `NOT restarting` — immediate failure | Unchanged — still exits immediately |
| Pipeline impact | Single crash kills pipeline | Transient crashes recovered; pipeline continues |

This addresses the scenario described in [issue #1512](https://github.com/jwbron/egg/issues/1512), where a Bun segfault permanently killed an agent and caused the entire pipeline to fail even though 4 of 5 agents were healthy.

## Agent-Level Restart

Source: `orchestrator/container_spawner.py`, `orchestrator/routes/pipelines.py`

When an agent becomes unresponsive (e.g., hung after a tool error, context window exhaustion, dropped API connection), an **agent-level restart** stops the stuck container and respawns a replacement — without affecting other agents in the same phase.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. The restart count is incremented **before** the spawn attempt — a failed spawn still counts toward the per-agent restart limit, preventing infinite retry loops when Docker consistently fails (e.g., out of disk)
2. The orchestrator stops the existing container via `stop_agent_container()` and removes it via `remove_agent_container()`
3. A new container is spawned via `spawn_agent_container()` with the **same role, phase, and environment** — the gateway's idempotent worktree creation rediscovers the existing worktree and mounts it into the new container
4. Only after a successful spawn, the agent's consensus state is reset — `PeerConsensusTracker.remove_agent()` and `ConsensusEvaluator.remove_agent()` withdraw any proposals, ACKs, or confirmations. If spawn fails, consensus state is preserved so the pipeline remains in a consistent state
5. If consensus reset fails after a successful spawn, a warning is logged but the restart is still considered successful — the restarted agent will re-enter consensus
6. Recovery context is injected into the respawned agent (e.g., "You are being restarted after a stall. Resume from where your predecessor left off.")
7. The pipeline's `PhaseExecution` state is updated with the new container/agent entries
8. Restart count is tracked per agent per phase — configurable maximum (default: 2) prevents infinite restart loops

### Concurrency Safety

Restart operations on the same `(pipeline_id, agent_role)` pair are serialized via a per-key `threading.Lock`, following the same pattern used in `state_store.py:get_pipeline_state_lock`. This prevents race conditions when concurrent restart requests (e.g., from both the overseer and a human operator via MCP) target the same agent simultaneously — without the lock, both callers could read `count=0`, both pass the limit check, and one caller's freshly spawned container would be destroyed by the other's cleanup.

The lock guards all access to `_restart_counts` in `restart_agent_container()`, `get_restart_count()`, and `reset_restart_counts()`.

### Unified Restart Counting

Restart counts are tracked exclusively by the `ContainerSpawner` — keyed by `(pipeline_id, agent_role)` and reset per-phase via `reset_restart_counts()`. The overseer reads the authoritative restart count from the spawner's REST API response rather than maintaining an independent counter. This eliminates a previous issue where the overseer's shadow counter allowed up to 4 total restarts (2 via MCP + 2 via overseer) instead of the intended cap of 2.

### Mode Parameter Safety

The `restart_agent_container()` method requires the `mode` parameter (gateway network mode) to be explicitly provided. If `mode` is omitted or `None`, a `ValueError` is raised. This prevents a future caller from silently defaulting to public mode, which could expose a private repo's gateway session.

### Triggering a Restart

| Method | How |
|--------|-----|
| **CLI** | `egg-orch agent restart <role> [--reason "..."]` |
| **API** | `POST /api/v1/pipelines/{id}/agents/{role}/restart` with optional `{"reason": "...", "slice_id": "slice-N"}` body (`slice_id` also accepted as a query param) |
| **MCP tool** | `restart_agent(task_id, agent_role, reason?, slice_id?)` via the orchestrator MCP server |
| **Overseer** | Automatic — after consecutive heartbeat failures or unresponsive nudges (see below) |

For a per-slice agent in a multi-slice implement phase, `slice_id` scopes the restart to the slice's Job, worktree, and BRC tracker. When omitted, the route derives it from the phase's agent records: if exactly one slice has a non-complete record for the role, that slice is used; if the choice is ambiguous the request is rejected with HTTP 400 reason `slice_id_required` and a `details` object listing `known_slices` / `restart_candidates`. The scan is **scoped to `pipeline.current_phase`** — if the pipeline has already advanced past `implement` (e.g. to `pr` or a later iteration), no current-phase records will name the role, derivation falls through, and the operator should supply `slice_id` explicitly. This is **operator guidance, not a code-enforced precondition**: the fall-through branch still proceeds to a pipeline-level spawn, which would re-trigger the wedge mode below if a slice tracker is somehow still live past `implement` (in practice it is not — slice trackers are resolved by the time `current_phase` advances). This guards against a slice-mode restart silently spawning an unscoped agent — `EGG_SLICE_ID` unset — whose BRC signals route to the bare pipeline tracker instead of the slice's, wedging the slice's consensus ([#2759](https://github.com/jwbron/egg/issues/2759)).

### Worktree Preservation

Agent restart preserves the agent's git worktree, including any committed work on the branch. The gateway's `create_worktrees` API is **idempotent** — when called with a worktree ID that already exists (keyed by `{pipeline_id}-{role}`), it returns the existing worktree and its host paths rather than creating a new one. This means the respawned agent starts with the full commit history and all prior committed work intact.

**Implementation detail:** `spawn_agent_container()` always calls the gateway to create (or reuse) the per-agent worktree when `repos` is provided, regardless of whether `repo_volumes` was passed by the caller. This ensures both the initial spawn path and the restart path (which does not pass `repo_volumes`) correctly mount the agent's worktree. See issue [#1597](https://github.com/jwbron/egg/issues/1597) for the fix that resolved a bug where the restart path skipped worktree creation.

## Phase-Level Restart

Source: `orchestrator/routes/pipelines.py`

When agent-level restarts are insufficient (e.g., multiple agents stuck, consensus state corrupted, or the phase needs a fresh start), a **phase-level restart** kills all containers for the phase and respawns all agents from scratch.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets both the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. All running containers for the specified phase are stopped and removed
2. `PeerConsensusTracker.clear()` resets all consensus state (proposals, ACKs, NACKs, confirmations)
3. The phase's review cycle counter in `PhaseExecution` is reset
4. All prior phase artifacts and HITL decisions are preserved (e.g., refine output carries into a restarted plan phase)
5. All commits on the branch are preserved — each respawned agent's worktree is recreated from the pipeline branch HEAD via the gateway's idempotent `create_worktrees` API, so all prior pushed commits are immediately available
6. All agents for the phase are respawned from scratch

### Triggering a Phase Restart

| Method | How |
|--------|-----|
| **CLI** | `egg-orch phase restart <phase> [--reason "..."] [--context "..."]` |
| **API** | `POST /api/v1/pipelines/{id}/phases/{phase}/restart` with optional `{"reason": "...", "context": "..."}` body |
| **MCP tool** | `restart_phase(task_id, phase, reason, context)` via the orchestrator MCP server |
| **Overseer** | Via HITL decision — phase restarts require human approval by default |

The optional `context` parameter injects additional guidance into the respawned agents (e.g., "Previous attempt stalled during BRC convergence — focus on completing reviews first").

## Salvaging Unpushed Local Commits

Source: `orchestrator/agent_salvage.py`

When an agent's pushes to its assigned branch are wedged — gateway branch-allowlist rejection from a wrong-branch spawn-time env var ([#2428](https://github.com/jwbron/egg/issues/2428)), restart-reconciliation marking a still-running pipeline `failed` ([#2411](https://github.com/jwbron/egg/issues/2411)), or any other class of in-flight failure that leaves work committed locally but unreachable from origin — the orchestrator's per-agent worktree at `/home/egg/.egg-worktrees/{worktree_id}/{repo_short}` still holds the work on its local `egg/{worktree_id}/work` branch. Without recovery, `cleanup_pipeline` later deletes that worktree and the work is silently lost. Salvage closes that gap ([#2429](https://github.com/jwbron/egg/issues/2429)).

### How It Works

1. **Enumeration** scans `WORKTREE_BASE_DIR` for the pipeline's worktrees — pipeline-level (`{pipeline_id}`), per-role (`{pipeline_id}-{role}`), and slice-scoped (`{pipeline_id}-slice-{N}-{role}`). Same parsing rules as `cleanup_pipeline`, so salvage covers exactly the worktrees cleanup is about to delete.
2. **Diff** runs `git log {local_branch} ^origin/{assigned_branch}` against the worktree, falling back to `^origin/{base_branch}` when the assigned-branch tracking ref is absent. No fetch — over-including already-pushed commits is harmless because the receiver dedupes when cherry-picking, but trimming a real commit would be silent loss.
3. **Push to recovery ref** sends the worktree's HEAD to `egg/recovered/{pipeline_id}/{scope}/{short_sha}` via `gateway.push_worktree_branch(...)` with launcher auth. Launcher auth bypasses the agent-targeted branch-allowlist check, so this works to recover work even when the agent's own pushes were the thing that wedged.
4. The recovery-ref name embeds the HEAD short SHA, so re-salvages produce immutable refs instead of force-overwriting earlier ones.

### Triggering Salvage

| Method | How |
|--------|-----|
| **API (read)** | `GET /api/v1/pipelines/{id}/local-commits[?agent_role=&slice_id=]` — list unpushed commits per worktree (read-only) |
| **API (write)** | `POST /api/v1/pipelines/{id}/salvage[?agent_role=&slice_id=]` — push HEAD to `egg/recovered/...` |
| **MCP tool** | `list_agent_local_commits(task_id, agent_role?, slice_id?)` and `salvage_agent_commits(task_id, agent_role?, slice_id?)` |
| **Auto-salvage** | Best-effort, automatic — runs from `kubernetes_spawner.cleanup_pipeline` (skipped when `preserve_worktrees=True`, since the worktree survives and there's nothing to mirror) and from `restart_phase` (always runs against the worktrees of the roles being restarted). Failures are logged and never block cleanup or restart |

### Recovery Workflow

After salvage, the unpushed work is reachable from origin under the recovery namespace. To replay it onto an intended branch:

```bash
# Find every recovery ref for a pipeline
git ls-remote origin 'refs/heads/egg/recovered/<pipeline-id>/*'

# Fetch the recovered ref into a local branch
git fetch origin egg/recovered/<pipeline-id>/<scope>/<short-sha>:recovered/<scope>

# Cherry-pick onto the intended branch (de-dupes already-pushed commits)
git switch <target-branch>
git cherry-pick <recovered-base>..recovered/<scope>
```

Operators may delete `egg/recovered/*` refs manually after replay (`git push origin --delete <ref>`). For automatic cleanup of refs left behind by replays that never came, see [Recovery Ref Cleanup](#recovery-ref-cleanup) below.

### Recovery Ref Cleanup

Source: `orchestrator/agent_salvage_cleanup.py`

A periodic background sweep prunes `egg/recovered/*` refs older than a configurable TTL so the salvage namespace stays bounded on a busy cluster ([#2446](https://github.com/jwbron/egg/issues/2446)).

**How it works**

1. **List** every `egg/recovered/*` ref on origin via `git ls-remote --heads`. The same round-trip captures the SHA at each ref tip.
2. **Refresh** local tracking refs (`refs/remotes/origin/egg/recovered/*`) via a scoped `git fetch --prune` so the staleness and reachability checks run against current state.
3. **Classify** each ref by reading the committer date of the tip commit (`git log -1 --format=%cI`):
   - **Recent**: committer date within the TTL window — leave alone.
   - **Reachable**: committer date past the TTL but the SHA is reachable from any non-`egg/recovered/*` remote-tracking branch — leave alone (defensive against a pending or in-flight replay).
   - **Unknown age**: tip commit not present locally and fetch couldn't recover it — leave alone (we never delete a ref whose age we can't determine).
   - **Stale**: committer date past the TTL and not reachable elsewhere — delete via `gateway.delete_remote_branch`.
4. **Delete** is idempotent: `already_deleted` from origin is treated as success, so concurrent operator deletes don't surface as errors.

The sweep is best-effort. Any per-ref failure (gateway error, unparseable git output, etc.) is logged and counted against `refs_skipped_error`; the loop continues to the next ref.

**Configuration**

| Env var | Default | Effect |
|---------|---------|--------|
| `EGG_ORCH_RECOVERY_REF_CLEANUP_ENABLED` | `true` | Master kill switch. Set to `false`/`0`/`no`/`off` to disable the loop entirely. |
| `EGG_ORCH_RECOVERY_REF_TTL_DAYS` | `90` | Committer-date age past which a recovery ref is eligible for deletion. |
| `EGG_ORCH_RECOVERY_REF_CLEANUP_INTERVAL_SECONDS` | `86400` | Period between sweeps (default 24 h). **Also gates the first sweep:** the loop sleeps a full `interval_seconds` *before* its first run so multiple replicas restarted together don't pile onto the same minute. After an orchestrator restart, expect no recovery-ref cleanup for one full interval. There is no operator CLI to force an immediate sweep; the supported escape hatches are (a) restart the orchestrator with a temporarily lowered interval, or (b) call `RecoveryRefCleaner.run_once()` directly on the cleaner stashed in `app.config["RECOVERY_REF_CLEANERS"]`. The sweep is cheap when no refs need deletion, but the gateway round-trip adds up if set very low. |

The cleanup loop runs once per repo path discovered under `EGG_REPO_PATH`. Each loop is a daemon thread; see the `EGG_ORCH_RECOVERY_REF_CLEANUP_INTERVAL_SECONDS` row above for the first-sweep timing.

**Metrics in logs**

Each sweep emits a single structured log line at INFO level:

```
Recovery-ref cleanup sweep complete  repo_path=/repos/foo  ttl_days=90
  refs_inspected=42  refs_deleted=3  refs_skipped_recent=35
  refs_skipped_reachable=2  refs_skipped_unknown_age=1  refs_skipped_error=1
  oldest_remaining_age_days=78.4
```

`oldest_remaining_age_days` is the age of the oldest ref still on origin after the sweep. Drift in this metric (e.g. climbing past `ttl_days`) indicates refs are being skipped — usually by the reachability guard — so look for replay branches that are pinning recovery commits.

**Recovery workflow interaction**

Operators replaying a recovery ref should fetch and cherry-pick promptly: a recovered branch left at HEAD on origin (e.g. `recovered/<scope>` pushed back) keeps the salvage ref alive past the TTL via the reachability guard. After cherry-picking, deleting the replay branch lets the next sweep clean the original `egg/recovered/...` ref on schedule.

### What Salvage Does Not Do

- **No exec into the container.** Agent commits land in the orchestrator-side worktree, not the container's filesystem, so no `kubectl exec` is needed. Containers may already be terminated when salvage runs.
- **No re-targeting of pending pushes.** The original push is still rejected; salvage produces a separate, recoverable record. To stop new rejections, fix the upstream branch-resolution bug.

## When Recovery Is Triggered vs. HITL Escalation

| Scenario | Behavior |
|----------|----------|
| Agent fails, error is retryable, retries remain | `AgentRetryManager`: retry with backoff |
| Agent fails, error not retryable | Escalate to HITL (MANUAL policy) |
| Agent fails, max retries exceeded | Escalate to HITL |
| Transient crash in consensus wrapper (segfault, OOM) | Restart with exponential backoff (shares `MAX_CONSENSUS_RESTARTS` cap) |
| Startup failure in consensus wrapper (exit 1 within 30s) | Restart with exponential backoff (treated as transient API/network error) |
| Non-transient crash in consensus wrapper (exit 1 after 30s) | Immediate failure, no restart |
| Wrapper: tracker stale after withdrawal cascade | Message bus fallback detects `CONSENSUS_CONFIRMED`; agent exits cleanly |
| Overseer detects restartable infra error (unresponsive, crashed, OOM, timeout, hung) | `RESTART_AGENT` action — auto-restart via API (up to max restarts per phase) |
| Overseer detects non-restartable infra error (permission denied, EROFS, filesystem) | Escalate to HITL (bypasses restart) |
| Overseer detects agent stall (N heartbeat failures) | `RESTART_AGENT` action — auto-restart via API (up to max restarts per phase) |
| Overseer detects agent stall (restarts exhausted) | Escalate to HITL |
| Overseer detects multiple stuck agents (2+) | `RESTART_PHASE` action — creates HITL decision for phase restart approval |
| Manual agent restart (CLI/MCP/API) on running pipeline | Stop container, reset consensus (after successful spawn), respawn with same config |
| Manual agent restart (CLI/MCP/API) on failed pipeline | Same as above, plus reset pipeline + phase status to `RUNNING` |
| Manual agent restart (CLI/MCP/API) on cancelled pipeline | Same as above, plus reset pipeline + phase status to `RUNNING`; worktrees preserved by `cancel_task(cleanup=false)` |
| Manual phase restart (CLI/MCP/API) on running pipeline | Stop all containers, reset all consensus + review cycles, respawn all agents |
| Manual phase restart (CLI/MCP/API) on failed pipeline | Same as above, plus reset pipeline + phase status to `RUNNING` |
| Manual phase restart (CLI/MCP/API) on cancelled pipeline | Same as above, plus reset pipeline + phase status to `RUNNING`; worktrees preserved by `cancel_task(cleanup=false)` |
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
