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

## Consensus Wrapper: Exit-Code Classifiers (Preserved Helpers)

Source: `orchestrator/consensus_wrapper.py`

In concurrent (BRC) mode, all agents are wrapped with a shell script. The wrapper bash template defines three exit-code classifier helpers (`is_buffer_overflow`, `is_transient_crash`, `is_startup_failure`) that were the dispatch surface in the pre-#2908 capped-restart era. **Since slice-4 (#2908) these helpers are defined but not invoked by the `propose|ack|nack` arm** — every non-zero agent exit goes through the uniform `AGENT_FAIL_STREAK++` + streak-specific escalation (#3138) + idle-budget path described in [Crash Handling in the Event-Pump Wrapper](#crash-handling-in-the-event-pump-wrapper) below. The subsections that follow describe the original design intent of each helper and the SDK-level mechanics they were built around; treat them as background context for the named helpers rather than as a description of live wrapper behaviour.

The wrapper also detects stale consensus tracker state — where the tracker shows an agent as not confirmed despite a `CONSENSUS_CONFIRMED` message existing in the message bus — and falls back to the message bus to avoid false failures after withdrawal/re-proposal cascades. (That fallback path is independent of the classifier helpers and is still live.)

### Buffer Overflow Detection (helper definition — currently inert)

The `is_buffer_overflow()` function greps the captured agent output log for the Claude Agent SDK's `CLIJSONDecodeError` marker (`"exceeded maximum buffer size"`). In the pre-#2908 capped-restart wrapper the marker triggered an immediate wrapper exit; in the post-slice-4 event-pump wrapper the helper is **defined but not called**, so the SDK 1 MiB JSON overflow exit takes the same `AGENT_FAIL_STREAK++` path as any other non-zero exit. The deterministic-failure framing below still applies — re-running the agent against the same oversized tool result reproduces the overflow each iteration — but the operator-visible escalation today is the streak-specific alert (`agent-invocation-fail-streak` `OVERSEER_ALERT` at streak ≥ 10, priority `high`) and the idle-budget alert (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 min), not an immediate wrapper exit.

The upstream Claude Agent SDK ships a 1 MiB JSON message-reader buffer; egg raises it to 32 MiB on this path (see the next section, [#2884](https://github.com/jwbron/egg/issues/2884)), so the cap that's actually in effect is much higher than the SDK default. A tool result that exceeds *the configured cap* — whatever it is — kills the agent with exit 255. This failure is **deterministic** — re-running the agent against the same codebase produces the same oversized payload and hits the same crash. Retrying it inside the same wrapper run is therefore wasteful; the post-slice-4 wrapper does not yet branch on this case, so the overflow recurs on each iteration until escalation fires — the streak-specific `agent-invocation-fail-streak` alert at streak ≥ 10 (#3138), or the idle budget if it trips first.

The agent output is captured by piping stdout and stderr through `tee` into a temporary log file (`AGENT_OUTPUT_LOG`, created via `mktemp`). This log is truncated at the start of each agent run so old crash signatures don't bleed into subsequent runs.

> **Note:** The buffer-overflow marker string is synchronized between the wrapper's `grep` and the `_BUFFER_OVERFLOW_MARKER` constant in `shared/egg_agent/client.py`. If a future `claude-agent-sdk` release changes the wording, future classifier-gated fast-fail logic that relies on it would silently miss the marker. See [#2823](https://github.com/jwbron/egg/issues/2823) for the follow-up to pin this against the installed SDK. With the reader buffer raised (next section, [#2884](https://github.com/jwbron/egg/issues/2884)) the buffer overflow is now a rare backstop — it fires only if a single stream message exceeds the generous raised buffer — not the common path it was when the cap was 1 MiB.

### SDK Reader Buffer (the crash-prevention layer)

Source: `_DEFAULT_SDK_MAX_BUFFER_BYTES` / `_sdk_max_buffer_bytes()` in `shared/egg_agent/client.py`, wired as `ClaudeAgentOptions.max_buffer_size`.

This is what actually prevents the [#2804](https://github.com/jwbron/egg/issues/2804) crash. egg's Agent SDK reader decodes the CLI's stream-json output into a JSON buffer; a single message larger than `max_buffer_size` raises `CLIJSONDecodeError` and kills the agent (exit 255). The SDK default is 1 MiB.

The crucial point ([#2884](https://github.com/jwbron/egg/issues/2884)): **the messages that overflow are not model-bound.** Claude Code attaches the **entire original file** to every `Edit`/`Write` result as transcript metadata (`toolUseResult.originalFile`) that the model never sees — only egg's reader decodes it. So a routine ~2 KB edit to the 1.1 MB, 25k-line `orchestrator/routes/pipelines.py` emits a >1 MB stream message and crashes the reader, even though the model's `tool_result` is just a bounded `cat -n` snippet. (The original #2884 framing guessed the *edit snippet* scaled with file size; the CLI's `fBB` snippet builder bounds it to `new_string` lines + 8, so the culprit is the result *envelope* metadata, not the snippet.)

egg raises `max_buffer_size` to **32 MiB** (default; override with `EGG_SDK_MAX_BUFFER_BYTES`). This costs **no model context or tokens** — the oversized field never reaches the model, and egg logs at most `_MAX_TOOL_CONTENT_LOG_LEN` of any result — only transient reader memory for one message. It cannot let an oversized payload reach the model either: model-bound result sizes are bounded independently (the predictive caps below, the MCP `@tool` caps [#2805](https://github.com/jwbron/egg/issues/2805), and Claude Code's own Bash truncation). 32 MiB covers source files far larger than anything in this repo while still bounding a runaway/malformed stream; the fail-fast above is the clean backstop for anything beyond it.

### Predictive Output Cap (PreToolUse)

Source: `shared/egg_agent/tool_output_cap.py`, wired in `shared/egg_agent/client.py`.

These caps are **model-context/cost discipline, not crash prevention** (the reader buffer above is the crash fix). What they bound is the volume a tool sends *to the model*: a whole-file `Read` returns the file's content to the model (the 1.1 MB `pipelines.py` ≈ ~275k tokens), and a whole-repo content `Grep` dumps every matching line — both wasteful. **Built-in** Claude Code tools (`Read`, `Grep`, `Bash`) run inside the CLI and can't be wrapped the way egg caps its own MCP `@tool` payloads ([#2805](https://github.com/jwbron/egg/issues/2805)), so [#2876](https://github.com/jwbron/egg/issues/2876) bounds them via a **PreToolUse** hook that fires *before* the tool runs and denies calls whose model-bound result is likely to be excessive, telling the agent how to narrow the call. (`Edit`/`Write` are deliberately *not* capped here: their model-bound result is the small snippet, and their crash vector was the reader-buffer metadata, fixed above — not anything a per-call cap could see.)

Current heuristics — predictive, so expect some false positives/negatives:

| Tool | Denied when | Deny reason points at |
|------|-------------|------------------------|
| `Read` (text) | Target file > `EGG_READ_CAP_BYTES` (default 256 KiB) **and** the read is unbounded — no `limit`, or a `limit` whose estimated payload (`limit` × ~128 B/line) still exceeds the cap | `offset` / `limit` to page through the file; or, if the whole file is genuinely needed, write the required byte size to `/tmp/egg-read-cap-bytes` and retry (the deny message includes the exact `echo` command) |
| `Read` (PDF) | Target PDF > `EGG_READ_CAP_BYTES` **and** no non-empty `pages` range — a `pages`-scoped read is bounded (the Read tool caps it at 20 pages), mirroring `limit` for text | `pages` to read a bounded page range (e.g. `pages='1-5'`) |
| `Read` (image/notebook) | Target image/notebook > `EGG_READ_CAP_BYTES` (returned whole; `offset`/`limit`/`pages` don't bound it) | images: avoid reading whole, use Bash (`file`/`stat`) for metadata; notebooks: inspect cells with `jq` (e.g. `jq '.cells[].source'`) |
| `Grep` | `output_mode=content`, no `head_limit`, **and** no `path`/`glob` scope (whole-repo content dump) | `head_limit`, a `path`/`glob` scope, or `output_mode=files_with_matches` |

The hook is **always-on** (excess model-bound output is wasteful on every route, including first-party Opus). Set `EGG_TOOL_OUTPUT_CAP=false` (or `0`/`no`/`off`) to disable.

The `Read` byte threshold resolves in precedence order: (1) the agent-writable override file `/tmp/egg-read-cap-bytes` — the agent writes the byte size it needs (the deny message supplies the exact `echo` command) and retries; the file is pod-local and never committed; (2) the `EGG_READ_CAP_BYTES` env var — the operator tuning knob; (3) the 256 KiB built-in default. A set-but-invalid value at any source — non-integer or non-positive — is logged once and skipped in favour of the next source in the chain. Operators further narrow the threshold on the LiteLLM path via `EGG_LITELLM_READ_CAP_BYTES` (injected at spawn; see the [Context guardrails section in the per-agent-models guide](../guides/per-agent-models.md#context-guardrails-3175)).

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

### Crash Handling in the Event-Pump Wrapper

In the event-pump model (default since #2908 slice-4), the classifiers above
are retained as named helpers in the wrapper bash template. A non-zero exit
from the agent during a `propose|ack|nack` event invocation increments an
internal consecutive-failure counter. Two escalation paths apply (#3138):
the streak-specific path fires an `OVERSEER_ALERT` (anomaly
`agent-invocation-fail-streak`, priority `high`) after 10 consecutive
failures; the idle-budget path (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 min)
emits `OVERSEER_ALERT` (anomaly `stuck-phase-transition`) when no actionable
event arrives for the budget duration. The old `MAX_CONSENSUS_RESTARTS`
restart cap and `_RECOVERY_SYSTEM_PROMPT` recovery loop were deleted in slice-4.

### Crash exit-code classification (current behaviour)

All non-zero exits from a `propose|ack|nack` agent invocation are handled identically in the current event-pump wrapper: increment `AGENT_FAIL_STREAK`, apply linear backoff (`streak × 2 s`, capped at 30 s), and resume the loop. At streak ≥ 5, a sticky log warning fires classifying the failure as likely permanent (unknown model alias, auth misconfiguration, prompt-rendering crash). At streak ≥ 10, a sticky `OVERSEER_ALERT` fires (anomaly `agent-invocation-fail-streak`, priority `high`) with duration-aware detail: failures completing in ≤ 2 s are classified as configuration-class (pre-SDK-init crash); longer failures as potentially transient (API/quota/transport). Both latches are wrapper-lifetime sticky — they fire exactly once per run regardless of how many subsequent consecutive failures occur. The classifiers listed below remain as named helpers in the wrapper bash template but are **not invoked** by the `propose|ack|nack` arm — they are kept against a future need (e.g. a classifier-gated fast-fail) but produce no per-exit-code branching today.

| Exit code | Named helper (currently inert) | Current event-pump handling |
|-----------|-------------------------------|-----------------------------|
| Segfault (exit 139/255) | `is_transient_crash` | Increments streak counter; linear backoff; streak escalation + idle-budget escalation emit alerts |
| SDK buffer overflow (exit 255 + overflow marker) | `is_buffer_overflow` | Increments streak counter; linear backoff; streak escalation + idle-budget escalation emit alerts |
| OOM kill (exit 137) | `is_transient_crash` | Increments streak counter; linear backoff; streak escalation + idle-budget escalation emit alerts |
| API/network error at startup (exit 1, age &lt; 30s) | `is_startup_failure` | Increments streak counter; linear backoff; streak escalation + idle-budget escalation emit alerts |
| Application error (exit 1, age &ge; 30s) | *(none)* | Increments streak counter; linear backoff; streak escalation + idle-budget escalation emit alerts |

The exit-code classifiers were added in [issue #1512](https://github.com/jwbron/egg/issues/1512). In the capped-restart era they drove a restart-with-backoff path; since slice-4 (#2908) they are retained as named helpers but the event-pump's streak-specific escalation (#3138) + `EGG_BRC_IDLE_BUDGET_MIN` escalation are the operator-visible signals — there is no per-exit-code branch on the `propose|ack|nack` path.

**Buffer-overflow note:** the SDK 1 MiB JSON buffer overflow (exit 255 + overflow marker) is deterministic — re-running the same agent invocation against the same oversized tool result will reproduce the same overflow. Today the wrapper does not catch this case specially; the overflow recurs each iteration until escalation fires — the streak-specific `agent-invocation-fail-streak` `OVERSEER_ALERT` at streak ≥ 10 (#3138), or `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) if it trips first. The classifier helper exists for a future fast-fail path but is not wired up yet — see the wrapper's L145–150 comment.

## Agent-Level Restart

Source: `orchestrator/container_spawner.py`, `orchestrator/routes/pipelines.py`

When an agent becomes unresponsive (e.g., hung after a tool error, context window exhaustion, dropped API connection), an **agent-level restart** stops the stuck container and respawns a replacement — without affecting other agents in the same phase.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. The restart count is incremented **before** the spawn attempt — a failed spawn still counts toward the per-agent restart limit, preventing infinite retry loops when Docker consistently fails (e.g., out of disk)
2. The orchestrator stops the existing container via `stop_agent_container()` and removes it via `remove_agent_container()`
3. A new container is spawned via `spawn_agent_container()` with the **same role, phase, and environment** — the gateway's idempotent worktree creation rediscovers the existing worktree and mounts it into the new container
4. Only after a successful spawn, the agent's consensus state is reset — `PeerConsensusTracker.remove_agent()` withdraws any proposals, ACKs, or confirmations. (The legacy `ConsensusEvaluator` in `orchestrator/consensus.py` was deleted in [#2777](https://github.com/jwbron/egg/issues/2777); BRC's `PeerConsensusTracker` is the only consensus path in production.) If spawn fails, consensus state is preserved so the pipeline remains in a consistent state
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

**Uncommitted work is also captured before respawn** ([#2807](https://github.com/jwbron/egg/issues/2807)): auto-salvage runs with `salvage_uncommitted=True`, which stages and commits the agent's dirty working tree (using identity `egg-salvage <egg-salvage@localhost>` and commit message `[salvage] pre-crash working-tree state (#2807)`) before pushing to `egg/recovered/…`. By the time the gateway's subsequent `git reset --hard` runs during worktree reuse, that state has already been committed and pushed — so the reset only abandons the synthetic salvage commit locally, and it remains recoverable via the pushed `egg/recovered/…` ref.

**Implementation detail:** `spawn_agent_container()` always calls the gateway to create (or reuse) the per-agent worktree when `repos` is provided, regardless of whether `repo_volumes` was passed by the caller. This ensures both the initial spawn path and the restart path (which does not pass `repo_volumes`) correctly mount the agent's worktree. See issue [#1597](https://github.com/jwbron/egg/issues/1597) for the fix that resolved a bug where the restart path skipped worktree creation.

## Phase-Level Restart

Source: `orchestrator/routes/pipelines.py`

When agent-level restarts are insufficient (e.g., multiple agents stuck, consensus state corrupted, or the phase needs a fresh start), a **phase-level restart** kills all containers for the phase and respawns all agents from scratch.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets both the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. All running containers for the specified phase are stopped and removed
2. `PeerConsensusTracker.clear()` resets all consensus state (proposals, ACKs, NACKs, confirmations). In slice-aware mode the clear extends to every per-slice tracker: `restart_phase` loads the contract, iterates `contract.slices`, and calls `clear()` on each `get_peer_consensus_tracker(pipeline_id, slice_id=<slice>)` in addition to the pipeline-level key — without this, stale slice-scoped consensus state survives the restart and deadlocks the new run ([#2777](https://github.com/jwbron/egg/issues/2777) slice-4 TASK-4-1, bundles [#2409](https://github.com/jwbron/egg/issues/2409)). Contract-load failures preserve the historical pipeline-level-only behaviour rather than blocking the restart.
3. The phase's review cycle counter in `PhaseExecution` is reset
4. All prior phase artifacts and HITL decisions are preserved (e.g., refine output carries into a restarted plan phase)
5. Per-agent worktrees and their local branches are **deleted** — unpushed commits are salvaged to `egg/recovered/*` refs on a best-effort basis first (see [Salvaging Unpushed Local Commits](#salvaging-unpushed-local-commits); worktrees with a corrupted `.git` marker may be skipped without salvage). Fresh worktrees then re-fork from `origin/<assigned_branch>` tip, so only commits pushed to the shared work branch survive into respawned agents' trees. For per-worktree retention, use `restart_agent` instead (#3080).
6. All agents for the phase are respawned from scratch

**Resume-after-orchestrator-restart vs. operator-driven phase restart.** The clear above runs when an operator (or the overseer/HITL ladder) calls `restart_phase` to start the phase over. The orthogonal case — an orchestrator-pod recycle mid-phase that should **resume** the in-flight slice DAG rather than start it over — is handled by **Layer-C bootstrap reconciliation** on the next orchestrator startup. Layer C iterates non-`COMPLETE` slices, observes integration-branch commit counts and consensus tracker presence, and applies a 5-way classification: re-yield as `READY` (no commits yet), mark already-spawned (commits but no tracker), mark `COMPLETE` (commits + consensus reached but unrecorded), preserve `BLOCKED` (and escalate to HITL if no pending decision), or escalate corrupt state to HITL. Cases 4 and 5 create an unresolved `Decision` on the contract — silent classification error is worse than an operator pause. See [`Slice/phase restart hardening`](../architecture/orchestrator.md#slicephase-restart-hardening-2777-slice-4-bundles-2409).

Startup reconciliation also rebuilds per-slice consensus trackers ([#2409](https://github.com/jwbron/egg/issues/2409) closure): for each slice in `contract.slices`, `reconstruct_tracker_from_messages(pipeline_id, graph, slice_id=<slice>)` rebuilds the nested `{pipeline_id}/{slice_id}` tracker from the message store, so a recycled orchestrator pod no longer loses in-flight slice consensus.

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
| **Auto-salvage** | Best-effort, automatic — runs from `kubernetes_spawner.cleanup_pipeline` (skipped when `preserve_worktrees=True`, since the worktree survives and there's nothing to mirror), from `restart_phase` (always runs against the worktrees of the roles being restarted), and from **agent restart** (`restart_agent_job`, [#2807](https://github.com/jwbron/egg/issues/2807)) with `salvage_uncommitted=True` — which also commits the dirty working tree onto the work branch before the recovery push, so uncommitted edits survive the respawn's `git reset --hard`. Failures are logged and never block cleanup or restart |

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
| Transient crash in consensus wrapper (segfault, OOM) | Increments consecutive-failure counter; idle-budget escalation emits `OVERSEER_ALERT` — no restart cap |
| Startup failure in consensus wrapper (exit 1 within 30s) | Increments consecutive-failure counter; idle-budget escalation emits `OVERSEER_ALERT` — same handling as all non-zero exits today |
| Non-transient crash in consensus wrapper (exit 1 after 30s) | Increments consecutive-failure counter; idle-budget handles escalation |
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

## Related Documentation

- [Concurrent Execution Guide](../guides/concurrent-execution.md) — Failure handling in concurrent mode
- [Orchestrator Architecture](../architecture/orchestrator.md) — Container monitoring and startup reconciliation
- [Post-Agent Commit Reference](post-agent-commit.md) — Work preservation on container exit
