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

## Consensus Wrapper: Crash & Exit-Code Handling

Source: `orchestrator/consensus_wrapper.py`

In concurrent (BRC) mode, all agents are wrapped with a shell script. In the earlier capped-restart era the wrapper bash template defined three exit-code classifier helpers (`is_buffer_overflow`, `is_transient_crash`, `is_startup_failure`) that were its dispatch surface. **#3164 removed those helpers along with the in-pod loop** — they no longer exist in the wrapper (`grep is_buffer_overflow orchestrator/consensus_wrapper.py` → no match), and every non-zero agent exit is now handled uniformly by the orchestrator's `JobSupervisor` (`event_loop.py`), described in [Crash Handling in the Event-Pump Wrapper](#crash-handling-in-the-event-pump-wrapper) below. The subsections that follow describe the original classifier design intent and the SDK-level mechanics they were built around; treat them as historical / reference background, not as a description of live wrapper behaviour.

Reconciling stale consensus-tracker state — where the tracker shows an agent as not confirmed despite a `CONSENSUS_CONFIRMED` already on the message bus, e.g. after a withdrawal/re-proposal cascade — is likewise an orchestrator concern under the one-shot model: the event loop derives the next BRC action from consensus state each event (`egg-orch brc next-action`, `event_loop.py`), so the wrapper no longer second-guesses the tracker. The earlier wrapper-side message-bus fallback went away with the in-pod loop.

### Buffer Overflow Detection (capped-restart helper, removed by #3164)

In the capped-restart wrapper an `is_buffer_overflow()` helper greped the captured agent output log for the Claude Agent SDK's `CLIJSONDecodeError` marker (`"exceeded maximum buffer size"`) and triggered an immediate wrapper exit. #3164 removed that helper — and the output-log capture it read — along with the in-pod loop, so the SDK JSON overflow exit now takes the same orchestrator-side streak path (`record_abort`) as any other non-zero exit. The deterministic-failure framing below still applies — re-running the agent against the same oversized tool result reproduces the overflow each iteration — but the operator-visible escalation today is the streak-specific alert (`agent-invocation-fail-streak` `OVERSEER_ALERT` at streak ≥ 10, priority `high`) and the idle-budget alert (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 min), not an immediate wrapper exit.

The upstream Claude Agent SDK ships a 1 MiB JSON message-reader buffer; egg raises it to 32 MiB on this path (see the next section, [#2884](https://github.com/jwbron/egg/issues/2884)), so the cap that's actually in effect is much higher than the SDK default. A tool result that exceeds *the configured cap* — whatever it is — kills the agent with exit 255. This failure is **deterministic** — re-running the agent against the same codebase produces the same oversized payload and hits the same crash. Retrying it inside the same wrapper run is therefore wasteful; the event-pump wrapper does not branch on this case, so the overflow recurs on each iteration until escalation fires — the streak-specific `agent-invocation-fail-streak` alert at streak ≥ 10 (#3138), or the idle budget if it trips first.

In the capped-restart wrapper the agent output was captured by piping stdout and stderr through `tee` into a temporary log file (`AGENT_OUTPUT_LOG`, created via `mktemp`) and truncated at the start of each run so old crash signatures didn't bleed across runs; the overflow helper read that log. #3164 removed both the helper and this capture together with the in-pod loop.

> **Note:** The `_BUFFER_OVERFLOW_MARKER` constant (`"exceeded maximum buffer size"`) still lives in `shared/egg_agent/client.py`, but the wrapper no longer greps for it — #3164 removed the classifier (`grep "exceeded maximum buffer" orchestrator/consensus_wrapper.py` → no match). [#2823](https://github.com/jwbron/egg/issues/2823) tracks pinning the marker against the installed SDK should a future classifier-gated fast-fail path reintroduce a grep. With the reader buffer raised (next section, [#2884](https://github.com/jwbron/egg/issues/2884)) the buffer overflow is now a rare backstop — it fires only if a single stream message exceeds the generous raised buffer — not the common path it was when the cap was 1 MiB.

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

In the capped-restart era an `is_transient_crash()` helper (removed by #3164) classified these exit codes as transient. The table is retained as a reference for what each signal means — under the current model the orchestrator routes all of them through the same `record_abort` streak path:

| Exit Code | Signal | Cause |
|-----------|--------|-------|
| 134 | SIGABRT | Assertion failure, `abort()` |
| 136 | SIGFPE | Floating-point exception |
| 137 | SIGKILL | OOM kill or external `kill -9` |
| 139 | SIGSEGV | Segmentation fault |
| 255 | *(Bun-specific)* | Bun runtime segfault (wraps the crash as exit 255) |

### Startup Failure Detection

In the capped-restart era an `is_startup_failure()` helper (removed by #3164) handled a separate class of transient error: exit code 1 within the first `STARTUP_FAILURE_WINDOW_SECONDS` (default: 30 seconds) of agent lifetime. The Agent SDK surfaces API-level errors — network blips, socket closes, 5xx responses during the first few turns — as `success=False` + exit 1, which is indistinguishable from a prompt-level failure by exit code alone. Agents that exited 1 within the startup window had almost certainly not done meaningful work, so the retry cost was negligible; the window was tunable via `startup_failure_window_seconds` (`0` to disable).

Under the current event-pump model there is no startup-window special-casing: the bash wrapper does no exit-code branching at all, so an exit 1 (whether at startup or after real work) takes the same orchestrator-side `record_abort` streak path as every other non-77 exit. The orchestrator cannot distinguish an early crash from a later one — it reads only the terminated pod's exit code via `outcome_for`, not its run duration (the wrapper computes `one_shot_secs` but only writes it to `cw_log`; it is never transmitted), so there is no early-vs-late classification anywhere in the streak path.

### Crash Handling in the Event-Pump Wrapper

In the event-pump model (default since #2908, with the in-pod loop retired
by #3164), the classifiers above no longer exist in the wrapper — the
orchestrator's `JobSupervisor` owns all crash handling. A non-zero exit
from the agent during a `propose|ack|nack` event invocation increments a
per-dedupe-key failure streak (`record_abort`). Two escalation paths apply (#3138):
the streak-specific path fires an `OVERSEER_ALERT` (anomaly
`agent-invocation-fail-streak`, priority `high`) after 10 consecutive
failures; the idle-budget path (`EGG_BRC_IDLE_BUDGET_MIN`, default 30 min)
emits `OVERSEER_ALERT` (anomaly `stuck-phase-transition`) when no actionable
event arrives for the budget duration. The old `MAX_CONSENSUS_RESTARTS`
restart cap and `_RECOVERY_SYSTEM_PROMPT` recovery loop were deleted (#2908).

### Crash exit-code classification (current behaviour)

All non-zero exits from a `propose|ack|nack` agent invocation are handled identically by the orchestrator's `JobSupervisor` (`event_loop.py`), not the bash wrapper: the wrapper is one-shot and merely exits with the agent's rc (#3164 retired the in-pod loop — there is no `AGENT_FAIL_STREAK` and no loop to resume in the wrapper), then the orchestrator reads the terminated pod's outcome and calls `record_abort`, which increments a per-dedupe-key failure streak (`self._streaks`), applies linear backoff (`streak × 2 s`, capped at 30 s) to the next respawn, and re-derives the next event. At streak ≥ 5, a sticky log warning fires classifying the failure as likely permanent (unknown model alias, auth misconfiguration [non-fatal — fatal credential rejections fast-fail at exit 77], prompt-rendering crash). At streak ≥ 10, a sticky `OVERSEER_ALERT` fires (anomaly `agent-invocation-fail-streak`, priority `high`) (`_emit_alert`, `event_loop.py`): it reports the consecutive-failure count, action, role, exhausted dedupe key, and the key's recent termination history (the last 5 exit categories with pod exit codes when readable, #3496) — but no duration branch: the orchestrator never sees the agent's run duration. Both latches are per-dedupe-key sticky (`_alerted_warn` / `_alerted_10` in `JobSupervisor`) — they fire exactly once per dedupe key regardless of how many subsequent consecutive failures occur, and reset when the key changes. The exit-code table below maps each crash class to this single `record_abort` path; the named classifiers it once routed through (`is_transient_crash`, `is_buffer_overflow`, `is_startup_failure`) were removed from the wrapper by #3164, so there is **no per-exit-code branching in the wrapper today** — it passes every rc straight through to the orchestrator.

**Exception — auth-fatal fast-fail (exit 77):** the orchestrator event loop (#3373) intercepts exit code 77 (`EX_AUTH_FATAL`, `egg_agent.auth_errors`) before the streak mechanism applies. A credential or quota failure (subscription weekly/usage limit, expired/invalid OAuth token, 401 Unauthorized, exhausted credit balance) causes the agent CLI to exit 77; the orchestrator classifies the pod outcome as `JOB_OUTCOME_FATAL`, exhausts the dedupe key on the *first* failure, and emits a named `OVERSEER_ALERT` (anomaly `agent-credential-fatal`, priority `high`) with the cause and remediation. No retry is attempted — every respawn would re-use the same rejected credential. The bash wrapper passes exit 77 through unchanged (as it does every rc); the distinction is orchestrator-side, between `record_fatal` (this fast-fail path) and `record_abort` (the streak path every other non-zero exit takes) — both in `JobSupervisor`. **Remediation**: rotate the Claude credential (`CLAUDE_CODE_OAUTH_TOKEN` in `secrets.env` and apply the gateway secret), then restart the phase to mint a fresh dedupe key.

**Exception — transient rate-limit paced retry (exit 69 / `EX_RATE_LIMITED`, [#3364](https://github.com/jwbron/egg/issues/3364)):** a *bare* throttling signal — HTTP `429` / "rate limit" / "overloaded" / "too many requests" — is a **transient** cap wall that self-heals once the rolling window lifts, so `shared/egg_agent/auth_errors.py` deliberately excludes it from the auth-fatal patterns ([#3373](https://github.com/jwbron/egg/issues/3373)). `is_transient_rate_limit_error` matches these signatures and the agent CLI exits `EX_RATE_LIMITED` (69) — checked *after* `is_auth_fatal_error`, so a subscription weekly/usage cap the API delivers *as* a 429 still returns exit 77 (the two classifiers are disjoint). `outcome_for` maps exit 69 to `JOB_OUTCOME_RATE_LIMITED` (a stray code still falls back to `abnormal`), and `_observe_jobs` routes it to `JobSupervisor.record_rate_limited` instead of `record_abort`. That path leaves the abnormal `_streaks` / `_exhausted` state **entirely untouched** (mirroring `record_legitimate_outcome`), so an all-producers cap wall can never trip the `agent-invocation-fail-streak` halt — the 30 s `SUPERVISION_BACKOFF_CAP_SECONDS` that made that halt fire within minutes is irrelevant here. Instead the supervisor **paces** the respawn across the cap window: reset-time-paced when the error carries a hint, else a bounded rate-limit backoff (`SUPERVISION_RATE_LIMIT_BACKOFF_FACTOR`, capped at `SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS` = 15 min; a reset-derived wait is capped at `SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS` = 1 h) — hours-scale-capable and wholly separate from the 30 s abnormal cap. The paced retry runs through the normal respawn gate, so **completed slices are never discarded** (a `restart_phase`-equivalent, not a teardown). Per resolved decision `cq-1` there is **no hard wall-clock ceiling** — the pipeline keeps retrying until the cap lifts (a weekly cap can stay shut for hours-to-days) — but a sticky `OVERSEER_ALERT` (anomaly `agent-rate-limited`, priority `high`) fires once the *cumulative* paced wait crosses `SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS` (30 min), so an attended operator is informed while auto-recovery continues; no action is required. A **deterministic-loop guard** rides alongside but is deliberately narrow: because a genuine account-wide cap wall freezes the BRC progression *exactly* like a stuck failure would (every producer exits `EX_RATE_LIMITED` before doing any work, so the bus never moves), a frozen / identical fingerprint is **not** treated as a loop. The guard escalates only on **positive non-throttle evidence** — an exit signature that is present *and* does not classify as a transient rate-limit *and* carries no parseable reset hint (i.e. the failure changed to a genuinely different, non-throttle error) — reproduced `SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS` (5) times with no state advance. On that evidence it emits a distinct `OVERSEER_ALERT` (anomaly `rate-limit-deterministic-loop`, priority `high`) and **halts** the paced loop (`halt_rate_limited` marks the key exhausted), handing off to the arms-exhausted HITL; an advancing progression resets the repeat counter and keeps pacing. A bare production throttle (the exit-code path carries no classifiable error text) never satisfies the gate, so it paces indefinitely with only the threshold alert. **Remediation**: usually none — the cap self-heals and the pipeline resumes; the threshold alert is informational.

Every non-77, non-69 exit now takes the identical orchestrator-side `record_abort` streak path — the wrapper does not branch on the code — so the rows below differ only in what the exit code *means*, not in how it is handled:

| Exit code | Current event-pump handling |
|-----------|-----------------------------|
| Auth-fatal (exit 77 / `EX_AUTH_FATAL`) | `record_fatal`: orchestrator exhausts dedupe key immediately; emits `agent-credential-fatal` alert; no streak increment; no retry |
| Transient rate-limit (exit 69 / `EX_RATE_LIMITED`, #3364) | `record_rate_limited`: abnormal streak untouched (no fail-streak halt); paced retry across the cap window (reset-time-paced, else bounded backoff — hours-scale, not the 30 s cap); no hard wall-clock ceiling; sticky `agent-rate-limited` `OVERSEER_ALERT` once cumulative wait > 30 min; landed slices preserved; deterministic-loop guard halts + emits `rate-limit-deterministic-loop` only on positive non-throttle evidence |
| Segfault (exit 139/255) | `record_abort`: increments per-dedupe-key streak; linear backoff; streak (≥ 10) + idle-budget escalation emit alerts |
| SDK buffer overflow (exit 255 + overflow marker) | `record_abort`: increments per-dedupe-key streak; linear backoff; streak (≥ 10) + idle-budget escalation emit alerts |
| OOM kill (exit 137) | `record_abort`: increments per-dedupe-key streak; linear backoff; streak (≥ 10) + idle-budget escalation emit alerts |
| API/network error at startup (exit 1, age &lt; 30s) | `record_abort`: increments per-dedupe-key streak; linear backoff; streak (≥ 10) + idle-budget escalation emit alerts |
| Application error (exit 1, age &ge; 30s) | `record_abort`: increments per-dedupe-key streak; linear backoff; streak (≥ 10) + idle-budget escalation emit alerts |

The exit-code classifiers were added in [issue #1512](https://github.com/jwbron/egg/issues/1512). In the capped-restart era they drove a restart-with-backoff path; #3164 removed them from the wrapper along with the in-pod loop, and the event-pump's streak-specific escalation (#3138) + `EGG_BRC_IDLE_BUDGET_MIN` escalation are now the operator-visible signals. The bash wrapper has no per-exit-code branching for any exit code — it passes every rc through to the orchestrator. Two codes diverge, and both divergences are entirely orchestrator-side: exit 77 routes to `record_fatal` (immediate exhaust, named alert, no retry — #3373), and exit 69 / `EX_RATE_LIMITED` routes to `record_rate_limited` (abnormal streak untouched, paced retry across the cap window, no hard ceiling — #3364); every other non-zero exit takes the `record_abort` streak path.

**Buffer-overflow note:** the SDK 1 MiB JSON buffer overflow (exit 255 + overflow marker) is deterministic — re-running the same agent invocation against the same oversized tool result will reproduce the same overflow. Today the wrapper does not catch this case specially; the overflow recurs each iteration until escalation fires — the streak-specific `agent-invocation-fail-streak` `OVERSEER_ALERT` at streak ≥ 10 (#3138), or `EGG_BRC_IDLE_BUDGET_MIN` (default 30 min) if it trips first. There is no classifier helper for it in the wrapper — #3164 removed `is_buffer_overflow` along with the in-pod loop, so the marker is no longer grepped anywhere in `consensus_wrapper.py`; reintroducing a fast-fail path would mean re-adding both the grep and the `_BUFFER_OVERFLOW_MARKER` sync ([#2823](https://github.com/jwbron/egg/issues/2823)).

### Exhausted-key escalation and in-band reset (#3496)

An exhausted dedupe key is terminal by design: only `record_success` for the same key clears it, and an exhausted key can no longer spawn, so that path is unreachable. When *every* arm a slice needs in order to advance is exhausted (the #3496 incident: all six ack arms), the event loop used to sit in a silent livelock — pipeline `running`, `pending_decisions` empty, "spawn blocked due to exhausted key" logged every poll until the consensus timeout hard-failed the slice hours later.

The loop now detects that wedge (`_check_arms_exhausted`, `event_loop.py`): every derived spawn action blocked on an exhausted key, no one-shot Job in flight, no agent-free progress this tick. Once per episode it fires two surfaces via the executor (`_handle_arms_exhausted`, `concurrent_executor.py`):

- an `OVERSEER_ALERT` (anomaly `event-arms-exhausted`, priority `high`), and
- a **persisted HITL decision** (context `event_arms_exhausted`) carrying each exhausted key's role/action, failure streak, and recent termination history, with three options executable on resolve (`routes/decisions/_handlers.py`):
  - **Retry arms (reset spawn budgets)** — clears the exhausted keys on the pipeline's live event loop(s) through the in-process live-loop registry (`event_loop.get_live_event_loops`), giving each arm a fresh budget without tearing anything down. If the underlying failure persists the arms re-exhaust and the decision re-fires.
  - **Restart phase** — the in-process `restart_phase` call (same executor as the consensus-timeout "Retry phase", #3421).
  - **Abort (manual — recorded only)** — recorded with a pointer at `cancel_task` (stopping the pipeline stays an explicit operator action; the option label spells out that resolving it does *not* stop the still-wedged phase on its own).

The escalation report is scoped to the keys currently blocking a derivable arm — stale exhausted keys from superseded BRC rounds are filtered out — and the dedup gate suppresses **both** surfaces when a decision is already pending, so a re-armed latch (after a failed retry) does not re-broadcast the alert. If the wedge later clears by another route (a fresh key is derived, a spawn succeeds, or an unrelated decision re-keys the arms) the loop auto-withdraws the now-stale HITL (`_withdraw_arms_exhausted_hitl`, mirroring the consensus-timeout auto-withdrawal), guarded against the multi-slice case so a still-wedged sibling slice holds the shared decision in place.

Supervision state is per-process: an orchestrator restart already resets all streaks and exhausted keys (`JobSupervisor.reconcile`), so "Retry arms" is meaningful only against a live loop; when none exists the resolution reports that and points at "Restart phase".

### All-arms-parked escalation (#3548)

A no-op-parked dedupe key (#3425) is not terminal like an exhausted one — it self-releases — but only for a single probe spawn per fingerprint change or per `SUPERVISION_NOOP_PARK_RETRY_SECONDS` heartbeat. When *every* arm a slice needs is blocked on a no-op-park (or exhausted) key at once, a round that is one verdict away from converging can sit silently for the whole heartbeat window with `pending_decisions` empty — the same zero-operator-signal shape as the exhausted wedge, but on a live-but-ineffective arm instead of a dead one.

The loop detects this (`_check_arms_parked`, `event_loop/_loop.py`): every derivable spawn action is blocked `parked` or `exhausted` (at least one `parked`), no one-shot Job is in flight, and no agent-free progress ran this tick — a mixed parked+exhausted round is caught here rather than falling between the two detectors, since `_check_arms_exhausted`'s `all(== "exhausted")` predicate can't see it. Once per episode it fires the same two surfaces via the executor (`_handle_arms_parked`, `concurrent_executor.py`):

- an `OVERSEER_ALERT` (anomaly `event-arms-parked`, priority `high`), and
- a **persisted HITL decision** (context `event_arms_parked`) carrying each parked key's role/action and no-op streak (plus any co-blocking exhausted keys), with the same three resolution options as the exhausted decision except the first: **Retry arms (release no-op parks)** clears the parked keys on the pipeline's live event loop(s) (`event_loop.get_live_event_loops` → `reset_parked_arms` → `JobSupervisor.reset_noop_parks`) instead of resetting failure streaks.

The wedge auto-clears the sticky latch and withdraws a pending decision (`_withdraw_arms_parked_hitl` / `_withdraw_arms_parked_decisions`) the same way the exhausted escalation does, with the same multi-slice guard via `arms_parked_escalated`.

## Agent-Level Restart

Source: `orchestrator/container_spawner.py`, `orchestrator/routes/pipelines.py`

When an agent becomes unresponsive (e.g., hung after a tool error, context window exhaustion, dropped API connection), an **agent-level restart** stops the stuck container and respawns a replacement — without affecting other agents in the same phase.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. The restart count is incremented **before** the spawn attempt — a failed spawn still counts toward the per-agent restart limit, preventing infinite retry loops when Docker consistently fails (e.g., out of disk)
2. The orchestrator stops the existing container via `stop_agent_container()` and removes it via `remove_agent_container()`
3. A new container is spawned via `spawn_agent_container()` with the **same role, phase, and environment** — the gateway's idempotent worktree creation rediscovers the existing worktree and mounts it into the new container
4. Only after a successful spawn, the agent's consensus state is reset — `PeerConsensusTracker.remove_agent()` withdraws any proposals, ACKs, or confirmations. (The legacy `ConsensusEvaluator` in `orchestrator/consensus.py` was deleted in [#2777](https://github.com/jwbron/egg/issues/2777); BRC's `PeerConsensusTracker` is the only consensus path in production.) If spawn fails, consensus state is preserved so the pipeline remains in a consistent state. **The Redis message store (`pipeline:{id}:messages`) is NOT cleared** — it is the durable BRC message record and survives the restart boundary so the reseeded session can re-pull it via `/brc-transcript` + `read_peer_artifact` and re-derive deterministic anchors ([#3200](https://github.com/jwbron/egg/issues/3200)). The store is cleared only at phase transitions (`_clear_concurrent_state`) and pipeline create/delete, never on agent restart.
5. The restarted role's event-loop arms are invalidated on every live loop for the slice (`invalidate_role_arms`, #3548) — dropped from `_live_keys`/`_key_meta` and retired in the supervisor. Without this the re-derived event carries the *same* dedupe key as before the restart, so the loop's dedupe branch (the key never left `_live_keys`) or a surviving exhaustion/no-op-park latch silently blocks the respawn — observed twice in the #3548 incident. This is a best-effort reach-in: failures are logged and never fail the restart.
6. If consensus reset fails after a successful spawn, a warning is logged but the restart is still considered successful — the restarted agent will re-enter consensus
7. Recovery context is injected into the respawned agent (e.g., "You are being restarted after a stall. Resume from where your predecessor left off.")
8. The pipeline's `PhaseExecution` state is updated with the new container/agent entries
9. Restart count is tracked per agent per phase — configurable maximum (default: 2) prevents infinite restart loops

The response reports whether step 5 actually found a live loop to delegate the respawn to (`live_event_loop`, `arms_invalidated: <count>`) and adjusts the `respawn` field accordingly instead of unconditionally claiming success (#3548) — see the *Event-loop arm invalidation* deep-dive below for the exact `respawn` values and their conditions. It also reports the teardown itself: `jobs_torn_down: <count>` (`0` means the role had already exited and there was nothing to kill) and `teardown_confirmed` (whether the deletion was observed to complete before the route returned) — see *Asynchronous Job deletion* below. The `restart_agent` MCP tool passes all four fields through, replacing the always-empty `container_id` it used to return (#3597).

**Asynchronous Job deletion ([#3597](https://github.com/jwbron/egg/issues/3597)).** Deleting a Kubernetes Job is asynchronous: the API server accepts the request and the Job then sits in `Terminating` — still reporting `active > 0`, i.e. `RUNNING` — until its dependent pods finish terminating. Step 2's teardown returned immediately into that window, and the event loop polls every ~5s, so a poll landing inside it matched the still-terminating Job on its dedupe-key label, logged `Adopting existing live Job for event (dedupe hit)`, and declined to spawn a replacement. Because adoption also re-arms the key in the loop's live set — where a *missing* Job reads as "still running" — the role then stayed vanished indefinitely: no pod, no Job, `get_status` reporting `status: running` with `container_id: null`. An immediate second `restart_agent` call typically worked (the corpse had been reaped by then), which reads as a fluke rather than a race. Two changes close it:

- **Terminating Jobs are not adoptable.** `ContainerInfo` carries the Job's `deletionTimestamp` (populated by `KubernetesClient.list_jobs`), and `_event_dedupe_key_live` (`orchestrator/kubernetes_spawner/_events.py`) counts a Job as live only when it is in `LIVE_POD_STATUSES` **and** unstamped. Deletion-in-progress is the third state the predicate models, alongside terminal Jobs ([#3181](https://github.com/jwbron/egg/issues/3181)) and the live ones adoption exists for. Since a one-shot Job's name is derived from its dedupe key, the replacement collides with the name of the Job being reaped, so `spawn_event_job` waits the corpse out (bounded by `_EVENT_JOB_TERMINATION_WAIT_S`, 15s) before creating — the event-loop twin of the `restart_agent_job` wait added in [#2655](https://github.com/jwbron/egg/issues/2655). Overrunning that budget is logged and the spawn proceeds: a 409 `AlreadyExists` is isolated per-role by the loop and retried on the next poll, which costs a poll interval rather than the role.
- **The route waits for the teardown it requested.** `_restart_agent_body` waits (bounded by `_JOB_TEARDOWN_WAIT_SECONDS`, 20s shared across every Job it deleted, well under the MCP client's 60s restart timeout) for each deleted Job to be observed gone before returning, so the respawn it delegates starts from a clean slate. A timeout is reported as `teardown_confirmed: false`, never a failed restart.

**Event-loop arm invalidation (#3548).** The consensus reset in step 4 makes the event loop re-derive the role's next event, but with the *same* identity — and therefore the same dedupe key — as before the restart, so loop-local state silently blocked the respawn: the key stayed in the loop's live-key set (the route deletes the Job by label, and Job observation maps a missing Job to "still running"), and any exhaustion / no-op-park latch for the key survived untouched. `_restart_agent_body` (`orchestrator/routes/pipelines/_routes_restart.py`) now reaches into the live event loop for the restarted role's `(pipeline_id, slice_id)` and calls `invalidate_role_arms(agent_role)`, which drops the role's keys from the loop's live-key/metadata tracking and retires their supervisor state (unioning `_key_meta` with the supervisor's own parked/exhausted key reports, since a parked key has already been popped from `_key_meta`) so the next poll re-derives the key as fresh and actually spawns. The route's JSON response now reports `live_event_loop` (bool) and `arms_invalidated` (count), and the `respawn` field is honest about whether a live loop exists to honor the delegation: `"delegated to orchestrator event loop"` when one was found, `"driver thread relaunched; event loop will respawn the role"` when the pipeline was inactive, or `"no live event loop for this slice — no respawn will occur; restart the phase if the agent must re-run"` otherwise.

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
| **API** | `POST /api/v1/pipelines/{id}/agents/{role}/restart` with optional `{"reason": "...", "slice_id": "slice-N", "fresh_session": true}` body (`slice_id` also accepted as a query param) |
| **MCP tool** | `restart_agent(task_id, agent_role, reason?, slice_id?, fresh_session?)` via the orchestrator MCP server |
| **Overseer** | Automatic — after consecutive heartbeat failures or unresponsive nudges (see below) |

For a per-slice agent in a multi-slice implement phase, `slice_id` scopes the restart to the slice's Job, worktree, and BRC tracker. When omitted, the route derives it from the phase's agent records: if exactly one slice has a non-complete record for the role, that slice is used; if the choice is ambiguous the request is rejected with HTTP 400 reason `slice_id_required` and a `details` object listing `known_slices` / `restart_candidates`. The scan is **scoped to `pipeline.current_phase`** — if the pipeline has already advanced past `implement` (e.g. to `pr` or a later iteration), no current-phase records will name the role, derivation falls through, and the operator should supply `slice_id` explicitly. This is **operator guidance, not a code-enforced precondition**: the fall-through branch still proceeds to a pipeline-level spawn, which would re-trigger the wedge mode below if a slice tracker is somehow still live past `implement` (in practice it is not — slice trackers are resolved by the time `current_phase` advances). This guards against a slice-mode restart silently spawning an unscoped agent — `EGG_SLICE_ID` unset — whose BRC signals route to the bare pipeline tracker instead of the slice's, wedging the slice's consensus ([#2759](https://github.com/jwbron/egg/issues/2759)).

By default a restart preserves the role's durable warm-resume session record, so the respawned agent resumes its prior Claude session rather than cold-starting. When the restart's purpose is to change the agent's mind about the world (e.g. it is livelocked on a stale conclusion that plain consensus reset and worktree state can't reach), pass `fresh_session: true` to also evict that record — see [Cross-pod persistence](../architecture/context-discipline.md#cross-pod-persistence-3278) in the context-discipline doc ([#3537](https://github.com/jwbron/egg/issues/3537)).

### Worktree Preservation

Agent restart preserves the agent's git worktree, including any committed work on the branch. The gateway's `create_worktrees` API is **idempotent** — when called with a worktree ID that already exists (keyed by `{pipeline_id}-{role}`), it returns the existing worktree and its host paths rather than creating a new one. This means the respawned agent starts with the full commit history and all prior committed work intact.

**Uncommitted work is also captured before respawn** ([#2807](https://github.com/jwbron/egg/issues/2807)): auto-salvage runs with `salvage_uncommitted=True`, which stages and commits the agent's dirty working tree (using identity `egg-salvage <egg-salvage@localhost>` and commit message `[salvage] pre-crash working-tree state (#2807)`) before pushing to `egg/recovered/…`. By the time the gateway's subsequent `git reset --hard` runs during worktree reuse, that state has already been committed and pushed — so the reset only abandons the synthetic salvage commit locally, and it remains recoverable via the pushed `egg/recovered/…` ref.

The **event-loop respawn** path (worktree re-attach, not operator restart) gets the same protection from [#3639](https://github.com/jwbron/egg/issues/3639): `_clean_reused_worktree` commits a dirty tree as `[salvage] pre-reset working-tree state (#3639)` (same identity, so one `[salvage]` grep finds either snapshot) before its `reset --hard`, which feeds the existing [#3509](https://github.com/jwbron/egg/issues/3509) recovery-ref push and bus record. Until that landed, this path was the one gap in the preservation story: it salvaged commits only, so a session that worked for hours without committing lost the entire working tree on the next respawn, logged at INFO as `cleaned and synced`.

**Implementation detail:** `spawn_agent_container()` always calls the gateway to create (or reuse) the per-agent worktree when `repos` is provided, regardless of whether `repo_volumes` was passed by the caller. This ensures both the initial spawn path and the restart path (which does not pass `repo_volumes`) correctly mount the agent's worktree. See issue [#1597](https://github.com/jwbron/egg/issues/1597) for the fix that resolved a bug where the restart path skipped worktree creation.

## Phase-Level Restart

Source: `orchestrator/routes/pipelines.py`

When agent-level restarts are insufficient (e.g., multiple agents stuck, consensus state corrupted, or the phase needs a fresh start), a **phase-level restart** kills all containers for the phase and respawns all agents from scratch.

Restarts are allowed when the pipeline is in `RUNNING`, `AWAITING_HUMAN`, `FAILED`, or `CANCELLED` state. If the pipeline is in `FAILED` or `CANCELLED` state, the restart automatically resets both the pipeline and phase status to `RUNNING`. The `CANCELLED` case supports resuming a pipeline that was stopped via `cancel_task(cleanup=false)` without a full resubmission — see [#1725](https://github.com/jwbron/egg/issues/1725).

### How It Works

1. Before container teardown, the in-flight phase's BRC message record is persisted to disk via `_persist_phase_brc_history` as a belt-and-suspenders complement to the Redis store ([#3200](https://github.com/jwbron/egg/issues/3200)). For non-slice phases and the unattributed sibling of slice-aware runs this extends the #1827 persist-before-clear invariant to the restart path; per-slice `CONSENSUS_*` buckets are not written to disk here (the primary mechanism for those is the Redis store surviving the restart — see step 3). This step is best-effort: a failure is logged and does not block recovery.
2. All running containers for the specified phase are stopped and removed
3. `PeerConsensusTracker.clear()` resets all consensus state (proposals, ACKs, NACKs, confirmations). In slice-aware mode the clear extends to every per-slice tracker: `restart_phase` loads the contract, iterates `contract.slices`, and calls `clear()` on each `get_peer_consensus_tracker(pipeline_id, slice_id=<slice>)` in addition to the pipeline-level key — without this, stale slice-scoped consensus state survives the restart and deadlocks the new run ([#2777](https://github.com/jwbron/egg/issues/2777), bundles [#2409](https://github.com/jwbron/egg/issues/2409)). Contract-load failures preserve the historical pipeline-level-only behaviour rather than blocking the restart. **The Redis message store (`pipeline:{id}:messages`) is NOT cleared** — it is the durable BRC message record and survives the restart boundary so the reseeded session can re-pull it via `/brc-transcript` + `read_peer_artifact` and re-derive deterministic anchors ([#3200](https://github.com/jwbron/egg/issues/3200)). The store is cleared only at phase transitions (`_clear_concurrent_state`) and pipeline create/delete, never on restart.
4. The phase's review cycle counter in `PhaseExecution` is reset
5. All prior phase artifacts and HITL decisions are preserved (e.g., refine output carries into a restarted plan phase)
6. Per-agent worktrees and their local branches are **deleted** — unpushed commits are salvaged to `egg/recovered/*` refs on a best-effort basis first (see [Salvaging Unpushed Local Commits](#salvaging-unpushed-local-commits); worktrees with a corrupted `.git` marker may be skipped without salvage). Fresh worktrees then re-fork from `origin/<assigned_branch>` tip, so only commits pushed to the shared work branch survive into respawned agents' trees. For per-worktree retention, use `restart_agent` instead (#3080).
7. All agents for the phase are respawned from scratch

**Resume-after-orchestrator-restart vs. operator-driven phase restart.** The clear above runs when an operator (or the overseer/HITL ladder) calls `restart_phase` to start the phase over. The orthogonal case — an orchestrator-pod recycle mid-phase that should **resume** the in-flight slice DAG rather than start it over — is handled by **Layer-C bootstrap reconciliation** on the next orchestrator startup. Layer C iterates non-`COMPLETE` slices, observes integration-branch commit counts and consensus tracker presence, and applies a 5-way classification: re-yield as `READY` (no commits yet), mark already-spawned (commits but no tracker), mark `COMPLETE` (commits + consensus reached but unrecorded), preserve `BLOCKED` (and escalate to HITL if no pending decision), or escalate corrupt state to HITL. Cases 4 and 5 create an unresolved `Decision` on the contract — silent classification error is worse than an operator pause. See [`Slice/phase restart hardening`](../architecture/orchestrator.md#slicephase-restart-hardening-closes-2409).

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
| **Auto-salvage** | Best-effort, automatic — runs from `kubernetes_spawner.cleanup_pipeline` (skipped when `preserve_worktrees=True`, since the worktree survives and there's nothing to mirror), from `restart_phase` (always runs against the worktrees of the roles being restarted), from **agent restart** (`restart_agent_job`, [#2807](https://github.com/jwbron/egg/issues/2807)) with `salvage_uncommitted=True` — which also commits the dirty working tree onto the work branch before the recovery push, so uncommitted edits survive the respawn's `git reset --hard` — and from **worktree re-attach** (`_clean_reused_worktree`'s dirty-discard reset, [#3509](https://github.com/jwbron/egg/issues/3509)): a dirty tree is first committed as a `[salvage] pre-reset working-tree state (#3639)` snapshot so uncommitted work is salvageable at all ([#3639](https://github.com/jwbron/egg/issues/3639)), then `salvage_discarded_tip` pushes the doomed HEAD to a recovery ref *before* the hard-reset runs (the tip is otherwise unreachable to every other salvage path once the reset moves the worktree branch), and a message-bus system message durably records the discarded tip + recovery ref so a resuming agent with no session memory can find its prior work. Requires `pipeline_id` (plus `agent_role`/`slice_id` for ref scoping) and the pipeline's real gateway `mode` — omitting `mode` risks a "public" push being denied on a private-mode pipeline, silently degrading to record-only. Failures are logged and never block cleanup or restart |

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

**Review before replaying: a recovery ref may hold un-reviewed working-tree residue.** Both working-tree snapshot paths ([#2807](https://github.com/jwbron/egg/issues/2807) restart, [#3639](https://github.com/jwbron/egg/issues/3639) re-attach) stage with `git add -A`, so a snapshot commit contains everything the agent left in the worktree that is not `.gitignore`d — scratch dumps, logs, stray state files — with no agent or human intent behind any of it. Recovery refs are a preservation mechanism, not an endorsement: read the diff before cherry-picking, and expect a snapshot-only ref to sometimes hold nothing you want. (A snapshot containing a secret is rejected by GitHub push protection rather than leaked; the push then fails and the discard is recorded with `salvage_error` set instead of a recovery ref.)

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
