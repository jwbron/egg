# Refiner Proposal: Supervision Layer — Second Pass (#3665)

## Summary

This proposal addresses GitHub issue #3665: "Supervision, second pass: the layer was silent on seven livelocks and loud at healthy agents." The issue identifies four problem areas in the supervision layer and asks for two deliverables: (1) the work to build, and (2) a ranked candidate list of further improvements.

## What I Verified in the Tree

### Already Implemented (per the "What has already landed" list)

All nine items in the issue's "already landed" list are present and verified in the tree:

1. **Terminating-Job adoption on the event-loop respawn path (#3613)** — `kubernetes_spawner/_events.py:_await_terminating_event_jobs` (line 110)
2. **Re-attached worktree uncommitted work preserved (#3644, #3647, #3652, #3654, #3656, #3660)** — `kubernetes_spawner/_worktree.py` (multiple functions)
3. **Cancel stops the driver (#3645, #3649, #3655, #3657)** — `event_loop/_loop.py:stop()` (line 1011)
4. **Phase-gate approvals parse on first line (#3648)** — `routes/pipelines/_alerts.py` and `overseer/monitor/_anomaly_checks.py`
5. **Never-heartbeated roles anchor at Job start (#3612)** — `health_monitor.py:set_active_roles()` + `_job_active_since` (line 248-275)
6. **Simplifier's first propose gated on upstream producer (#3607)** — `event_loop/_loop.py:_handle_role()` + `noop_parked()` logic
7. **Green gate defaults to on (#3609)** — `models/_config.py` + `routes/pipelines/_run_concurrent.py`
8. **Every routed call records its decoding config (#3611, #3625)** — `consensus_wrapper.py` + `agent_model_resolution.py`
9. **Re-reviews are blocking-only (#3661)** — `peer_consensus` + `event_loop/_supervisor.py`

### What EXISTS but is NOT CONSULTED (Area 1: Signals that exist and are not consulted)

- **`detect_loop` in `overseer/classifier.py:224`** — An LLM-based loop detector that takes `recent_actions` and returns `{is_loop, loop_pattern, confidence}`. This is a Haiku-tier classifier, NOT a deterministic detector. It is called from `classify_stall` but only when an alert has already been raised — it does not proactively detect loops.
- **`classify_activity_pattern` in `overseer/classifier.py:298`** — Classifies agents as `productive`, `thrashing`, `spinning`, or `improper_tool_use`. Same pattern: only invoked after an alert, not proactively.
- **`detect_heartbeat_stall` in `health_checks/tier1/consensus_stall.py:217`** — A deterministic detector that fires when BOTH `last_tool_call_age_s` AND `last_heartbeat_age_s` are past the stall window. This IS registered in the detection plane (`detection_plane.py:454-458`) but only reads from `snapshot.running_agents` which is populated by `snapshot_from_health_context` — and that function only populates `running_agents` from `context.live_container_ids` (line 534-538), which is a set of container IDs, not role names with tool-call/heartbeat ages. **The detector is unreachable because its input is never populated.**
- **`PhaseStallDetector` in `detection_plane.py:295`** — Correctly handles the #3230 false-stall case (lifecycle owner) and is registered. This is the one detector that works correctly.
- **`DriverLivenessCheck` in `health_checks/tier1/driver_liveness.py`** — Three modes: `driver_dead`, `driver_hung`, `driver_no_progress`. Registered and functional. This catches the "driver thread died" case.
- **`detect_brc_thrash` in `health_checks/tier1/brc_thrashing.py`** — Detects NACK→propose→NACK thrash. Registered and functional.
- **`detect_container_restart_loop` in `health_checks/tier1/container_k8s.py`** — Detects crash-loops. Registered and functional.
- **`detect_duration_drift` in `health_checks/tier1/runtime_liveness.py`** — Fires when a phase runs past 2× its expected budget. Registered but `expected_duration_s` is rarely set in the snapshot.

### What EXISTS but is NOT CONSULTED for Loop Detection (Area 3: Loops that nothing detects)

The issue states the empirical finding: "counting *tool inputs never issued before in the session* over a trailing window separates a loop from work cleanly." Let me verify:

- **`agent_log_store.py`** — Captures pod logs at removal (Redis-backed, 24h TTL, 1 MiB tail). The comment at line 4 says "tool inputs are truncated at about 100 characters" — but this refers to the pod log capture, NOT the agent's own tool-call tracking. The log store captures stdout, not structured tool-call data.
- **`shared/egg_agent/client.py`** — The SDK client logs tool calls via `_truncate` (line 35, max 2000 chars for log events). But this is for logging, not for loop detection. There is NO structured tool-call history tracking that counts unique tool inputs.
- **`shared/egg_agent/tool_interceptor.py`** — Intercepts tool calls for permission checking but does NOT track them for loop detection.
- **`progress_store`** — Tracks progress events emitted by agents, but these are agent-emitted (not tool-call-level).
- **`message_store`** — Tracks BRC messages, not tool calls.

**Conclusion: There is NO existing mechanism to count unique tool inputs over a trailing window. The empirical finding from the issue has NOT been implemented.**

### What EXISTS but is NOT CONSULTED for Session Boundaries (Area 2: Session boundaries read as failures)

- **`shared/egg_agent/__main__.py:47`** — `parser.add_argument("--timeout", type=int, default=7200, help="Timeout in seconds.")` — The 2-hour timeout is hardcoded here. The agent process is killed by `asyncio.timeout(7200)` in `client.py:765`. This is a hard process-level timeout with NO operator visibility — the agent cannot see it coming.
- **`kubernetes_client.py:350`** — `active_deadline = kwargs.get("active_deadline_seconds", 14400)` — K8s Job has a 4-hour active deadline. This is the K8s-level kill, separate from the agent-level timeout.
- **`event_loop/_supervisor.py`** — The `JobSupervisor` tracks `record_abort` (abnormal termination), `record_success` (clean exit), `record_fatal` (credential failure), and `record_rate_limited` (transient throttle). A timeout (exit code -1 from `asyncio.timeout`) maps to `JOB_OUTCOME_ABNORMAL` — it increments the failure streak and counts against the retry budget, exactly as the issue describes ("both kills were counted as crashes against the fail-streak budget").
- **`agent_log_store.py`** — Captures logs at removal, so timeout evidence IS preserved. But the timeout itself is not surfaced as a distinct outcome — it's lumped with crashes.
- **`health_monitor.py`** — The `_on_container_stopped` handler fires on `CONTAINER_STOPPED` events. A timeout-killed pod exits with code -1 (from the `asyncio.timeout` in the agent), which would be treated as an abnormal exit. There is no special handling for "clean exit without verdict" vs "timeout" vs "crash."
- **`event_loop/_loop.py:_observe_jobs`** — Maps Job outcomes: `success` (rc=0), `legitimate` (stale-event/NACK), `abnormal` (non-zero rc), `fatal` (auth), `rate_limited` (throttle). A timeout exit (rc=-1) is `abnormal`. There is no `timeout` outcome category.

**Conclusion: Timeouts and clean-exits-without-verdict are indistinguishable from crashes in the current supervision layer. The 2-hour timeout is invisible to the agent and counted as a crash.**

### What EXISTS but is NOT CONSULTED for Alert Quality (Area 4: Alerts an operator cannot act on)

- **`overseer/monitor/_anomaly_checks.py:_filter_current_phase_agents`** — Filters alerts to current-phase agents. This is a good improvement but doesn't address the core issue of alerts lacking evidence.
- **`overseer/monitor/_poll.py`** — The poll cycle fetches container logs for alerted agents (`_query_container_logs`). But the alert itself doesn't carry the evidence inline — the operator must read the alert, then separately look up logs.
- **`overseer/monitor/_escalation.py`** — The `handle_escalation` method does classify-then-decide, which is good. But the escalation payload from `health_monitor.py` is minimal: `{type, agent_id, reason, timestamp}` — no container logs, no progress events, no consensus state.
- **`routes/pipelines/_alerts.py:_check_brc_progress_gate`** — The progress gate that defers consensus-timeout alerts while BRC bus activity is live. This is good but only applies to consensus timeouts, not heartbeat/progress stall alerts.
- **`health_monitor.py:_has_recent_peer_progress`** — Defers heartbeat/progress alerts when peer activity is recent. This is the mechanism that SHOULD prevent false positives against healthy agents, but it's only consulted in `check_heartbeats` and `check_progress`, NOT in the convergence-stall check in `event_loop/_loop.py:_check_convergence_stall`.

**Conclusion: The convergence-stall alert in the event loop (`_check_convergence_stall`) fires at `high` priority without consulting the health monitor's alive-signal gates. This is the exact false-positive described in the issue — a coder with seconds-old heartbeats gets flagged.**

## Analysis of the Five "Is This Role Stuck?" States

The issue describes five states that a detector must handle correctly. Let me map each to the current code:

1. **"Producers are legitimately podless between events"** — Handled by `health_monitor.py:_orchestrator_skip_tripwire()` (line 507) and `event_loop/_loop.py:_publish_active_roles()` (line 572). The `_active_jobs` set tracks roles with live one-shot Jobs. BUT: the convergence-stall check in `_check_convergence_stall` (line 836) does NOT consult `_active_jobs` — it only checks if a key is in `_live_keys`. A role between events (no live key) with a pending actionable event WILL trigger the stall alert. **This is a bug.**

2. **"Reviewers legitimately wait on upstream producers"** — Handled by `health_monitor.py:_is_brc_idle()` (line 524) and the #3520 `WAITING_ON_ROLE` self-report probe in `_supervisor.py:_emit_noop_alert()`. BUT: the convergence-stall check does NOT consult these. **This is a bug.**

3. **"A declared no-op leaves its review edges pending forever"** — Handled by the #3425 successful-no-op park (`_supervisor.py:record_success` line 37, `noop_parked` line 665). A no-op park releases on fingerprint change or retry heartbeat. **This is handled.**

4. **"A NACK is a verdict and discharges the obligation just as an ACK does"** — Handled by `peer_consensus` tracker and the `record_legitimate_outcome` path. **This is handled.**

5. **"Two of those five states are not visible in the status payload at all"** — The `WAITING_ON_ROLE` self-report and the `awaiting_spawn` flag are not in the BRC status payload. The `snapshot_from_health_context` function does read `awaiting_spawn` from the context (line 531), but the convergence-stall check in `_loop.py` does NOT use the snapshot — it uses its own tracker-based logic. **This is a gap.**

## Proposed Work

### Priority 1: Tool-Input Loop Detection (Area 3)

**Problem:** No mechanism exists to count unique tool inputs over a trailing window. The issue's empirical finding is not implemented.

**Proposal:** Add a deterministic loop detector to the detection plane that:
- Reads tool-call data from the agent's session state or a structured log
- Counts unique tool inputs (tool name + input parameters) over a trailing window (e.g., 30 minutes)
- Fires when zero new unique tool inputs appear in the window
- Registers as a new `detect_tool_input_loop` detector in `health_checks/tier1/`

**Implementation approach:**
- The agent SDK client (`shared/egg_agent/client.py`) already intercepts tool calls via `tool_interceptor.py`. Add a lightweight tool-call log (tool name + truncated input hash) to the session state store.
- The detection plane's `snapshot_from_health_context` reads this from the session state.
- A new `detect_tool_input_loop` detector in `health_checks/tier1/` fires when the unique-input count is zero over the window.

**Files to create/modify:**
- `shared/egg_agent/tool_interceptor.py` — add tool-call logging
- `shared/egg_agent/session.py` — add tool-call history to session state
- `health_checks/tier1/tool_input_loop.py` — new detector
- `health_checks/detection_plane.py` — register the new detector
- `health_checks/context.py` — add tool-call history to `PipelineHealthContext`

### Priority 2: Fix Convergence-Stall False Positives (Area 1 + Area 4)

**Problem:** The convergence-stall check in `event_loop/_loop.py:_check_convergence_stall` fires `high`-priority alerts without consulting the health monitor's alive-signal gates. It does not check:
- Whether the role has an active one-shot Job (podless-between-events)
- Whether the role is a reviewer waiting on a live upstream producer
- Whether the role self-reported `WAITING_ON_ROLE` on a live producer

**Proposal:** Before raising the `stuck-phase-transition` anomaly, consult:
1. The `_active_jobs` set (already published by `_publish_active_roles`) — if the role has an active Job, it's not stalled
2. The `WAITING_ON_ROLE` self-report probe (already wired in `_supervisor.py:_probe_waiting_on`) — if the role is waiting on a live producer, downgrade to `low` priority
3. The BRC bus activity (already checked via `bus_timestamp`) — if the bus moved recently, reset

**Files to modify:**
- `event_loop/_loop.py:_check_convergence_stall` — add alive-signal gates before alerting

### Priority 3: Distinguish Timeouts and Clean Exits from Crashes (Area 2)

**Problem:** A 2-hour timeout (exit code -1) and a clean exit without a verdict are both counted as `abnormal` in the `JobSupervisor`, consuming retry budget. The agent cannot see the timeout coming.

**Proposal:**
1. Add a `timeout` outcome category to the event loop's job-outcome vocabulary, distinct from `abnormal`.
2. Surface the timeout threshold to the agent via env var (`EGG_AGENT_TIMEOUT_SECONDS`) so the agent can self-terminate gracefully before the hard kill.
3. When a timeout occurs, emit a `WORKING` heartbeat with `state=TIMEOUT_IMMINENT` and a countdown, so the operator's monitor sees the agent is aware.
4. A timeout should NOT increment the failure streak — it's a budget exhaustion, not a crash. Route it to a separate `timeout_streak` that triggers a different alert (operator attention, not retry).

**Files to modify:**
- `shared/egg_agent/__main__.py` — add `--timeout` env var injection
- `shared/egg_agent/client.py` — emit pre-timeout heartbeat
- `event_loop/__init__.py` — add `JOB_OUTCOME_TIMEOUT` constant
- `event_loop/_supervisor.py` — add `record_timeout` method
- `event_loop/_loop.py` — map timeout exit codes to `JOB_OUTCOME_TIMEOUT`
- `kubernetes_spawner/_models.py` — detect timeout exit code (-1) in `outcome_for`

### Priority 4: Alert Evidence Bundling (Area 4)

**Problem:** Alerts fire without the evidence that would make them readable. The operator must manually correlate alert → logs → progress → consensus state.

**Proposal:** Enrich the `OVERSEER_ALERT` payload with structured evidence:
- `latest_heartbeat_age_s` — seconds since the agent's last heartbeat
- `latest_tool_call_age_s` — seconds since the agent's last tool call
- `last_progress_event` — the agent's last progress event data
- `blocking_agents` — current BRC blocking set
- `container_logs_tail` — last N lines of container logs (already fetched by `_query_container_logs`)
- `consensus_state` — the BRC consensus status dict

**Files to modify:**
- `health_monitor.py` — enrich escalation payloads
- `overseer/monitor/_poll.py` — include evidence in alert processing
- `routes/pipelines/_alerts.py` — include evidence in OVERSEER_ALERT emission

## Candidate List (Ranked)

This list is a deliverable in its own right. Each entry carries a file-and-symbol citation and a verdict on whether the thing is present or absent in the tree today.

### 1. [ABSENT] Tool-input uniqueness loop detector
**Citation:** `health_checks/detection_plane.py:454` (registers detectors); `health_checks/tier1/consensus_stall.py:217` (detect_heartbeat_stall, which reads `last_tool_call_age_s` but the snapshot never populates it).
**Verdict:** ABSENT. No mechanism counts unique tool inputs over a trailing window. The `detect_heartbeat_stall` detector exists but is unreachable because `snapshot_from_health_context` only populates `running_agents` from container IDs, not with tool-call/heartbeat ages.
**Impact:** This is the core gap — seven livelocks went undetected.

### 2. [PRESENT but UNWIRED] `detect_heartbeat_stall` detector
**Citation:** `health_checks/tier1/consensus_stall.py:217`
**Verdict:** PRESENT but UNWIRED. The detector fires when BOTH `last_tool_call_age_s` AND `last_heartbeat_age_s` are stale. But `snapshot_from_health_context` (line 534-538) only creates `RunningAgent` entries from `context.live_container_ids` (a set of container ID strings), setting only `role` and `state="running"`. The `last_tool_call_age_s` and `last_heartbeat_age_s` fields are never populated. **Fix:** populate these fields from the health monitor's state or the session state store.

### 3. [ABSENT] Timeout outcome category in JobSupervisor
**Citation:** `event_loop/__init__.py:172-177` (JOB_OUTCOME_* constants); `event_loop/_supervisor.py:record_abort` (increments streak)
**Verdict:** ABSENT. A timeout (exit code -1 from `asyncio.timeout` in `client.py:765`) is classified as `JOB_OUTCOME_ABNORMAL` (line 80 in `_models.py`), incrementing the failure streak. There is no `JOB_OUTCOME_TIMEOUT` constant. The 2-hour timeout in `__main__.py:47` is invisible to the agent.

### 4. [ABSENT] Agent-visible timeout warning
**Citation:** `shared/egg_agent/__main__.py:47` (default=7200); `shared/egg_agent/client.py:765` (asyncio.timeout)
**Verdict:** ABSENT. The agent has no way to know it will be killed at 2 hours. The timeout is a hard process-level kill with no pre-warning. The agent cannot self-terminate gracefully or emit a final verdict.

### 5. [PRESENT but INCOMPLETE] Convergence-stall alive-signal gates
**Citation:** `event_loop/_loop.py:836` (`_check_convergence_stall`)
**Verdict:** PRESENT but INCOMPLETE. The check consults `bus_timestamp` (BRC bus activity) but does NOT consult:
- `_active_jobs` / `_live_keys` for whether the role has a live pod (false positive on podless-between-events)
- `WAITING_ON_ROLE` self-report for reviewer waits (false positive on legitimate reviewer waits)
The health monitor's `_orchestrator_skip_tripwire` and `_is_brc_idle` gates exist but are not consulted here.

### 6. [ABSENT] Structured tool-call history for loop detection
**Citation:** `shared/egg_agent/client.py:31` (`_MAX_TOOL_CONTENT_LOG_LEN`); `shared/egg_agent/tool_interceptor.py:29` (intercepts tool calls for permissions only)
**Verdict:** ABSENT. Tool calls are logged (truncated to 2000 chars) but not tracked in a structured history that could be queried for uniqueness. The `tool_interceptor.py` module intercepts calls but only for permission checking, not for loop detection.

### 7. [PRESENT but UNWIRED] `detect_duration_drift` detector
**Citation:** `health_checks/tier1/runtime_liveness.py:138`
**Verdict:** PRESENT but UNWIRED. The detector fires when `started_age_s > expected_duration_s * 2`, but `expected_duration_s` is only set in `phase_state` if the pipeline config provides it. The `snapshot_from_health_context` function (line 525-532) does NOT populate `expected_duration_s` from the pipeline config. **Fix:** populate it from `pipeline.config.consensus_timeout_minutes`.

### 8. [ABSENT] Session-boundary outcome classification
**Citation:** `event_loop/_supervisor.py:record_success` (line 37, counts toward no-op streak); `event_loop/_loop.py:88-91` (JOB_OUTCOME_SUCCESS)
**Verdict:** ABSENT. A clean exit (rc=0) that produced no BRC progress is classified as `JOB_OUTCOME_SUCCESS` and increments the no-op streak (#3425). But a clean exit that DID produce progress is also `JOB_OUTCOME_SUCCESS` — the two are indistinguishable at the outcome level. The no-op streak mechanism (#3425) is the closest thing, but it only catches repeated no-ops of the SAME dedupe key, not a one-shot clean exit that forgot to emit a verdict.

### 9. [PRESENT but NOISY] Overseer poll cycle alert filtering
**Citation:** `overseer/monitor/_poll.py:71` (`_filter_current_phase_agents`)
**Verdict:** PRESENT but NOISY. The overseer filters alerts to current-phase agents, but the convergence-stall alert from the event loop (`_check_convergence_stall`) bypasses the overseer entirely — it goes directly to `OVERSEER_ALERT` via the `convergence_stall_notifier` callback. The overseer's filtering does not apply.

### 10. [ABSENT] Alert evidence bundling
**Citation:** `overseer/monitor/_poll.py:78-85` (fetches container logs per alert); `health_monitor.py:730-736` (escalation dict has only type/agent_id/reason/timestamp)
**Verdict:** ABSENT. The escalation payload from `health_monitor.py` contains only `{type, agent_id, reason, timestamp}` — no structured evidence. The overseer fetches container logs separately, but the alert itself carries no heartbeat age, tool-call age, or consensus state. An operator reading the alert must manually correlate multiple sources.

### 11. [PRESENT] BRC thrash detector
**Citation:** `health_checks/tier1/brc_thrashing.py:57` (`detect_brc_thrash`)
**Verdict:** PRESENT and FUNCTIONAL. Detects NACK→propose→NACK cycles (threshold 3) and late CONFIRMED-then-re-NACK. Registered in the detection plane. This is the one detector that correctly handles a loop pattern.

### 12. [PRESENT] Container restart loop detector
**Citation:** `health_checks/tier1/container_k8s.py:220` (`detect_container_restart_loop`)
**Verdict:** PRESENT and FUNCTIONAL. Detects crash-loops (restart count ≥ 3). Registered in the detection plane.

### 13. [PRESENT] Driver liveness check
**Citation:** `health_checks/tier1/driver_liveness.py:93` (`DriverLivenessCheck`)
**Verdict:** PRESENT and FUNCTIONAL. Three modes: `driver_dead`, `driver_hung`, `driver_no_progress`. Runs on RUNTIME_TICK. This caught the #3540 incident (11+ hours of RUNNING with zero spawns).

### 14. [PRESENT] Phase stall detector (lifecycle-owner-aware)
**Citation:** `health_checks/detection_plane.py:295` (`PhaseStallDetector`)
**Verdict:** PRESENT and FUNCTIONAL. The #3230 fix: fires only when phase is RUNNING, zero running agents, NO lifecycle owner queued, no HITL pending, and past grace window. This is the model the other detectors should follow.

### 15. [ABSENT] Pod-vanishing detection
**Citation:** `kubernetes_monitor.py:458-463` (reconciliation marks agents FAILED when container_id not in live_ids); `health_checks/tier1/container_liveness.py:88-108` (ContainerLivenessCheck)
**Verdict:** PARTIALLY PRESENT. `ContainerLivenessCheck` detects missing containers but marks them as `FAILED` (infrastructure problem), not as a session boundary. A pod that vanishes (node drain, eviction) is indistinguishable from a crash. The `agent_log_store` preserves logs, but the outcome classification doesn't distinguish "pod vanished" from "agent crashed."

### 16. [ABSENT] Fail-streak budget protection for timeouts
**Citation:** `event_loop/_supervisor.py:158-229` (`record_abort` increments streak); `event_loop/_supervisor.py:32` (JOB_OUTCOME_ABNORMAL)
**Verdict:** ABSENT. A timeout exit (rc=-1) maps to `JOB_OUTCOME_ABNORMAL`, which increments the failure streak toward `SUPERVISION_FAILURE_STREAK_ALERT` (10) and engages `AGENT_FAILED`. The issue states "both kills were counted as crashes against the fail-streak budget." This is confirmed in the code — there is no timeout-specific path.

### 17. [PRESENT] No-op park mechanism (#3425)
**Citation:** `event_loop/_supervisor.py:37-98` (`record_success` increments no-op streak); `event_loop/_supervisor.py:665-779` (`noop_parked`)
**Verdict:** PRESENT and FUNCTIONAL. A clean exit with no BRC progress re-derives the identical dedupe key, climbing the no-op streak. After `SUPERVISION_NOOP_STREAK_PARK` (3) clean completions, the arm parks. Self-releases on contract-decision fingerprint change or retry heartbeat. This handles the "declared no-op" case from the issue.

### 18. [PRESENT] WAITING_ON_ROLE self-report probe (#3520)
**Citation:** `event_loop/_supervisor.py:924-988` (`_emit_noop_alert` with `waiting` probe)
**Verdict:** PRESENT but NARROWLY APPLIED. The probe is only consulted in `_emit_noop_alert` for no-op parks. It is NOT consulted in:
- `_check_convergence_stall` (the false-positive convergence-stall alert)
- `health_monitor.py:check_heartbeats` (the heartbeat timeout alert)
This is the exact gap that produced the false positive described in the issue.

### 19. [ABSENT] Tool-input truncation in pod logs
**Citation:** `agent_log_store.py:49-51` (MAX_LOG_BYTES = 1 MiB tail); `shared/egg_agent/client.py:32` (`_MAX_TOOL_CONTENT_LOG_LEN = 2000`)
**Verdict:** PARTIALLY ADDRESSED. The issue states "the pod log cannot support this, because tool inputs are truncated at about 100 characters." The `agent_log_store` captures the tail (1 MiB), and the SDK client truncates individual log entries to 2000 chars. But neither captures the full structured tool input — the truncation is at the log level, not the data level. A structured tool-call history (candidate #6) would bypass this.

### 20. [PRESENT] Post-consensus-timeout absolute cap
**Citation:** `models/_config.py:167-177` (`post_consensus_max_total_seconds` default 14400 = 4 hours); `routes/pipelines/_run_concurrent.py:1227-1234`
**Verdict:** PRESENT. The post-timeout poll loop has an absolute cap of 4 hours (14400s). This prevents unbounded waiting but does not address the 2-hour agent timeout being counted as a crash.

## Recommended Build Order

1. **Priority 1 (Tool-input loop detection)** — This is the core gap. Seven livelocks went undetected. Build the structured tool-call history + deterministic detector.
2. **Priority 2 (Fix convergence-stall false positives)** — This is the false-positive problem. The convergence-stall check must consult alive-signal gates before alerting.
3. **Priority 3 (Timeout vs. crash distinction)** — This addresses the session-boundary problem. Timeouts must not consume retry budget.
4. **Priority 4 (Alert evidence bundling)** — This addresses the "alerts an operator cannot act on" problem.

## What I Left Out

- **The overseer poll cycle (`_poll_cycle`)** — This is the deprecated standing-pod shape. The issue's "health monitor was logging, every 30 seconds, that the same agent was alive" refers to the old in-pod overseer. In the current architecture, the orchestrator-side detection plane (`health_checks/`) and the event loop's convergence-stall check are the active supervision paths. The overseer is now on-demand (adjudication only). I did not propose changes to the overseer poll cycle because it is deprecated (#2270 slice-4).
- **The `detect_loop` / `classify_activity_pattern` LLM classifiers** — These are Haiku-tier classifiers that run only after an alert is raised. They are not the right tool for proactive loop detection (the issue explicitly says the empirical finding is about counting unique tool inputs, which is deterministic). I did not propose enhancing them.
- **The 4-hour K8s `active_deadline_seconds`** — This is a K8s-level safety net, not an application-level timeout. It is correctly set higher than the 2-hour agent timeout. No change needed.
